# Job submission — `slurm`

> **Audience: the operator who maps a target to a Slurm site, and the maintainer of the remote
> executor.** No leaf reads this document. It is the `slurm` binding of the `job_submit`
> capability; the executor, the site configuration and every refusal are
> `docs/ORCHESTRATION.md` §Execution sites. The code is
> `tools/backends/scheduler/slurm/submit.py` (issue #293 PR-4).

## 1. How a job runs

A job runs as ONE Slurm job, in the foreground of the ssh call that runs the job script:

```
ssh <options> -- <host> 'echo; printf ... atmofab-submitted <epoch> && exec srun --time=<minutes> <directives> --ntasks=1 --job-name=atmofab-<agent_run_id> --immediate=<queue_timeout_sec> sh -c <job script>'
```

- `srun` waits for an allocation, runs the job script as the job's single task, relays the
  task's stdout to its own over Slurm's I/O channel, and exits with the task's exit status. The
  task's stdout is a pipe (`/proc/self/fd/1` is `pipe:[…]` inside the task), so the statuses the
  script prints never pass through a file the job's own commands could rewrite. `srun`'s own
  messages go to its stderr only.
- `srun` takes the LAST of a repeated option, so the order is the policy. `--time` comes first:
  it is the job's commands' bounds plus the executor's grace (and the device probe's bound when
  the class names one), rounded up to whole minutes — 96 or 97 minutes with the server's default
  command timeouts — and a directive may replace it. A site whose partition, QOS or association
  allows less states its maximum there (`"--time=30"`); a job that reaches the shorter limit is
  killed and refused, never recorded. Without it such a site's job pends until `--immediate`
  gives up, since the launch probe does not ask the partition's limits.
- `scheduler_directives` come next, each split on whitespace into words (`"-p gpu"` is two
  words), and the executor's remaining options come last: `--ntasks=1` (the statuses are one
  task's stdout), the job's name and the queue bound. A directive that repeats one of them loses
  to it. A directive whose LAST word is an option that takes a separate value (`"--account"`
  with the value missing, or `"-J"`) swallows the next word — `--ntasks=1` — as that value
  (measured: the job ran two tasks, and was refused only after the runner had run twice); write
  a value-taking option as `--name=value` or with its value in the same directive.
- `--immediate=<queue_timeout_sec>` ends the call with exit status 1, and cancels the job, when no
  allocation is granted in that many seconds. The site's `queue_timeout_sec`, or
  `remote_execution.QUEUE_TIMEOUT_DEFAULT_SEC` when it states none.
- The job's id is `SLURM_JOB_ID`, which the script prints before its first command.

Measured on Slurm 20.02.5 (a one-node cluster): the pipe, the argv passed through unchanged, the
exit status passed through (3 → 3), the last repeated option winning (`--job-name`, `--ntasks`,
`--time`), and `--immediate` ending a pending job (exit 1, `Unable to allocate resources`, the job
gone from the queue), and a job cut at its time limit ending `srun` with 143. The whole executor path ran against that cluster with `srun` real and the
ssh transport shimmed: a job recorded its id, and a job held pending by a `--begin` directive was
refused with no log entry.

## 2. What the site must provide

A site entry in `./sites.yaml` (the shape is `docs/ORCHESTRATION.md` §Execution sites):

```yaml
  cluster_cpu:
    host: login-node
    workdir: /work/atmofab-jobs
    executes: [cpu]
    scheduler: slurm
    scheduler_directives: ["--partition=compute", "--time=120"]
    queue_timeout_sec: 7200
  cluster_gpu:
    host: login-node
    workdir: /work/atmofab-jobs
    executes: [gpu]
    scheduler: slurm
    scheduler_directives: ["--partition=gpu", "--gres=gpu:1", "--time=120"]
    queue_timeout_sec: 7200
```

A site's `executes` is a claim about the machines its directives select. Nothing checks it: a
`gpu` job allocated no device runs on whatever node the directives give, its device probe
records `null`, and the runner's failure is recorded as the kernel's — a correct kernel fails
Validate. So a class whose device must be requested gets a site of its own whose directives
request it (`--gres`), and `targets:` maps each target to the site for its class. A `--time`
directive shorter than the job's bound (96-97 minutes with the server's defaults) makes a command
that runs long a refusal rather than the command's own timeout.

- `srun` on the PATH of a NON-INTERACTIVE login. Where it is added by an interactive login's
  startup files only, the launch probe refuses `missing_required_site_tools` naming it.
- Permission to run `srun` from the login the site's `host` reaches (interactive jobs). A site
  that allows batch submission only cannot run this backend; the job is then refused when it
  runs, with `srun`'s own message.
- The site's `workdir` at the same path on the machines Slurm runs the job on (a shared
  filesystem). The launch probe asks the login only; the job script checks its programs and its
  machine again, on the machine it runs on, before its first command.

## 3. What it does not do

- **A job is not cancelled when the ssh call ends early.** The call has no terminal, so `srun` is
  sent no hangup when the connection drops or the executor's local bound kills it (measured: the
  `srun` process was re-parented and the job kept running). The executor has refused that job,
  and a later job runs in a directory of its own; the job runs on until the `--time` in force —
  the prefix's, or a directive's, which may be longer — which is why the prefix always sets one.
  `scancel` it by hand (its name is `atmofab-<agent_run_id>`) to free the allocation sooner.
- **No `sbatch`.** A batch job's stdout is a file under the operator's account, which the job's
  own commands can truncate and rewrite; the status of a command that runs after the runner would
  then be forgeable by the runner.
- **No accounting.** Nothing reads `sacct`: the job's exit is `srun`'s, and a job killed at its
  time limit exits non-zero, which the executor refuses whatever status lines had arrived.
- **Not measured: a limit Slurm enforces on the job beyond time.** Under a task plugin that
  enforces memory, a runner killed for exceeding it may also make `srun` exit non-zero although
  the script finished; the job is then refused as a host-side failure rather than recorded as the
  runner's runtime error. The cluster measured runs no such plugin.
- **One task.** `--ntasks=1`; a launcher that runs more ranks is the `parallel` axis' `launcher`
  capability, not implemented (issue #293 §Out of scope).
