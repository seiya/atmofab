#!/usr/bin/env python3
"""Smoke-test an execution site from `./sites.yaml` without running the workflow (issue #293).

Runs one trivial command at the site through the same executor `Validate.execute` uses
(`tools/remote_execution.py`): the launch probe, then one job — ssh, the site's scheduler prefix
(`srun` for `slurm`), the job script, collection by scp, and removal of the remote job directory.
Nothing under `workspace/` is touched; the command log and the collected job directory go to a
temporary directory.

    python3 tools/site_smoke.py <site_id>
    python3 tools/site_smoke.py --target <target_id> --cmd 'echo OK; hostname; nproc'
    python3 tools/site_smoke.py <gpu_site> --gpu --ship ./a.out --cmd '"$JOB"/a.out'

`--ship FILE` copies FILE into the job directory (mode kept); `$JOB` in `--cmd` is that
directory at the site. `--gpu` adds the `gpu` hardware class's device probe, as a `gpu`
target's Validate does.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import remote_execution as rx  # noqa: E402
from tools.execution_sites import SitesConfigError, load_sites  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("site_id", nargs="?", help="a site id in sites.yaml")
    which.add_argument("--target", help="use the site sites.yaml maps this target to")
    ap.add_argument("--sites", help="site configuration (default ./sites.yaml)")
    ap.add_argument("--cmd", default="echo OK; hostname",
                    help="shell command run at the site with sh -c (default: %(default)r)")
    ap.add_argument("--timeout", type=int, default=60, help="command timeout in seconds")
    ap.add_argument("--ship", action="append", default=[], metavar="FILE",
                    help="local file copied into the job directory ($JOB); repeatable")
    ap.add_argument("--gpu", action="store_true", help="record the gpu class's device probe")
    ap.add_argument("--no-probe", action="store_true", help="skip the launch probe")
    ap.add_argument("--keep", action="store_true",
                    help="keep the local temporary directory (collected job dir, command log)")
    args = ap.parse_args(argv)

    try:
        config = load_sites(REPO_ROOT, path=args.sites)
    except SitesConfigError as exc:
        print(f"sites.yaml refused: {exc}", file=sys.stderr)
        return 2
    site = config.site_for(args.target) if args.target else config.sites.get(args.site_id)
    if site is None:
        print(f"no site {args.site_id!r}; known: {sorted(config.sites)}", file=sys.stderr)
        return 2
    if site.is_local:
        print(f"site {site.site_id!r} is local; nothing remote to test", file=sys.stderr)
        return 2
    print(f"site {site.site_id}: host={site.host} workdir={site.workdir} "
          f"scheduler={site.scheduler} directives={list(site.scheduler_directives)} "
          f"queue_timeout_sec={site.queue_timeout_sec}")

    machine = platform.machine()
    if not args.no_probe:
        exes = (*rx.REMOTE_EXECUTABLES, *rx.scheduler_executables(site.scheduler))
        print(f"probe: asking for {list(exes)} ...")
        try:
            probe = rx.probe_site(site, exes)
        except rx.RemoteExecutionError as exc:
            print(f"probe FAILED: {exc}", file=sys.stderr)
            return 1
        print(f"probe: machine={probe.machine} missing={list(probe.missing)} "
              f"problems={list(probe.problems)}")
        if probe.missing or probe.problems:
            return 1
        if probe.machine != machine:
            # A shipped binary would be refused; a shell command is not, so test on.
            print(f"note: site machine {probe.machine} != this host's {machine}")
            machine = probe.machine

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    orch_id, run_id = f"smoke_{stamp}", f"smoke_{uuid.uuid4().hex[:8]}"
    job_dir = rx.job_dir(site, orch_id, run_id)
    local_tmp = Path(tempfile.mkdtemp(prefix="atmofab-site-smoke-"))
    command = rx.CommandSpec(
        tag="smoke", tool_name="run_program", argv=("sh", "-c", args.cmd), cwd=job_dir,
        env={"JOB": job_dir},
        record_cwd=str(local_tmp), timeout_sec=args.timeout,
        command_log_path=local_tmp / "command_log.jsonl", capture_limit=20000)
    platform_probe = None
    if args.gpu:
        from tools.host_execution import _platform_probe
        platform_probe = _platform_probe("gpu")
    ship = {Path(f).name: Path(f).resolve() for f in args.ship}
    request = rx.JobRequest(site=site, job_dir=job_dir, ship=ship, commands=(command,),
                            platform_probe=platform_probe,
                            attribution={"orchestration_id": orch_id, "agent_run_id": run_id},
                            machine=machine)
    print(f"job: {job_dir}")
    try:
        result = rx.execute_job(request, local_tmp=local_tmp / "job")
    except rx.RemoteExecutionError as exc:
        print(f"job FAILED: {exc}", file=sys.stderr)
        print(f"local files: {local_tmp}", file=sys.stderr)
        return 1

    print("site_record:", json.dumps(result.site_record, ensure_ascii=False))
    print("platform:   ", json.dumps(result.platform, ensure_ascii=False))
    ok = True
    for c, r in zip(request.commands, result.results):
        if r is None:
            print(f"[{c.tag}] did not run")
            ok = False
            continue
        ok = ok and r["ok"]
        print(f"[{c.tag}] rc={r['return_code']} ok={r['ok']}{' ' + r['error'] if 'error' in r else ''}")
        print(f"--- stdout ---\n{r['stdout']}", end="" if r["stdout"].endswith("\n") else "\n")
        if r["stderr"]:
            print(f"--- stderr ---\n{r['stderr']}", end="" if r["stderr"].endswith("\n") else "\n")
    if args.keep:
        print(f"local files: {local_tmp}")
    else:
        import shutil
        shutil.rmtree(local_tmp, ignore_errors=True)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
