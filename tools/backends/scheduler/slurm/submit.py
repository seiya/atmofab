#!/usr/bin/env python3
"""Slurm's `job_submit` (issue #293): a job runs as ONE Slurm job in the foreground of the site's
ssh call.

`tools/remote_execution.py` runs a job the same way for every scheduler: one ssh call whose
command runs the job script held in the shell's memory, with each command's status and the
platform facts read from the script's stdout. What a scheduler adds is the argv PREFIX that
command runs under — here `srun`, which waits for an allocation, runs the script as the job's
single task, relays the task's stdout to its own over Slurm's I/O channel (a pipe at the task's
end, never a file under the job directory), and exits with the task's exit status (all four
measured on Slurm 20.02). So the executor's refusals and the status channel are the ones of a
`none` site, unchanged.

Why not `sbatch`: a batch job's stdout is a file the job's own commands can rewrite, and the
statuses travel on it (the executor's module docstring says what the channel must withstand).

What `srun` does NOT do (measured on Slurm 20.02): when the ssh call ends early — the executor's
local bound, a dropped connection — `srun` gets no hangup (the call has no terminal) and the job
runs on until it ends or reaches its `--time`, which is why this prefix always sets one. The
executor has refused that job by then, and a later job runs in a directory of its own.
"""

from __future__ import annotations

from collections.abc import Sequence

#: The programs a site's non-interactive login must resolve for a job of this scheduler
#: (`host_prerequisites.required_site_executables` adds them to the launch probe).
REMOTE_EXECUTABLES: tuple[str, ...] = ("srun",)

#: The environment variable a job's task reads its own job id from.
JOB_ID_VARIABLE = "SLURM_JOB_ID"


def foreground_argv(*, directives: Sequence[str], job_name: str, wall_clock_sec: int,
                    queue_timeout_sec: int) -> tuple[str, ...]:
    """The argv the job script runs under: `srun`, the operator's `scheduler_directives` (each
    split on whitespace into words, as a `#SBATCH` line's are), and then this executor's own
    options, which come last so that a directive cannot change them — `srun` takes the last of a
    repeated option (measured): one task, the job's name, a time limit of `wall_clock_sec`
    rounded up to whole minutes, and `--immediate`, which ends the call with a non-zero exit, and
    the job with it, when no allocation is granted within `queue_timeout_sec`."""
    if wall_clock_sec < 1 or queue_timeout_sec < 1:
        raise ValueError("wall_clock_sec and queue_timeout_sec must be >= 1")
    minutes = -(-wall_clock_sec // 60)
    return ("srun", *(w for d in directives for w in d.split()),
            "--ntasks=1", f"--job-name={job_name}", f"--time={minutes}",
            f"--immediate={queue_timeout_sec}")
