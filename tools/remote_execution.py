#!/usr/bin/env python3
"""The remote executor: runs `Validate.execute`'s commands at an execution site reached over ssh
(issue #293).

The local path runs each command through the build-runtime server in this process
(`tool_run_program`, `tool_run_quality_checks`), and the server writes one `command_log.jsonl`
entry per command. A remote site has no server: the host stages the files a job needs, renders
ONE POSIX `sh` job script that runs the commands in order, runs it with one ssh call, copies the
job directory back with scp, and then writes the same log entries itself — through the server's
own `_append_command_log`, so the log keeps one writer and one shape (`docs/ORCHESTRATION.md`
§Execution sites). An entry's `command` names each shipped file by its LOCAL source, because the
post-execute gate binds `command[0]` to the node's own build `bin/`; the argv the site executed
is the entry's `site.remote_command`. Its `cwd` is the command's `record_cwd`, the local
directory it stands for (the gate requires a quality check's to be the node's source `src/`),
and the site's is `site.remote_cwd`.

What the executor knows is a SEQUENCE of commands (`CommandSpec`), each an argv, a working
directory, an environment override set and a timeout, and the files to ship. It does not know
what the commands are: the conductor composes them, and a build system's test target, its
variable names and the runner's argv reach this module only as values. The one piece of the
conductor's layout it asks for is the directories to create before the first command runs.

Every way the evidence could be incomplete or not this job's is a refusal
(`RemoteExecutionError`), never a default:

- the job directory is created with `mkdir` and no `-p`, so a directory that already exists —
  a stale job's output — is refused rather than read;
- the job script is the ssh call's command string, held in the shell's memory, never a file: a
  file is one a running command can rewrite, and bash executes a rewritten script file's rest;
- a command's exit status and times travel on the job script's own stdout, one status line per
  command that ran (`STATUS_MARKER`), and never in a file: the command itself — leaf-authored
  code, for the runner — can write anything under the job directory, including a file planted
  before the script writes its own, but its stdout and stderr go to files and its stdin is
  `/dev/null`, so it holds no descriptor of the script's stdout. A command's missing status line
  is a lost status and is refused, never read as 0; a second line for one command, a line for a
  command that should not have run (a command runs only when every earlier one exited 0), and
  a line that does not parse are refused too. A process that reaches the script's stdout
  anyway — through `/proc/<pid>/fd/1`, which the same user can open — can add lines but cannot
  remove the script's own: an added line is a second one, and text written without a newline
  glues onto the script's next line, which then carries a marker somewhere other than at its
  start and is refused. The platform facts travel the same way (`PLATFORM_MARKER`), printed
  once each before the first command starts;
- a transport failure (ssh or scp exits non-zero, or the job outlives its local bound), a job
  script that fails outside its commands (a directory it cannot make, a `timeout` that does not
  take `-k`, a site machine other than the one the shipped files were built on, a program it
  cannot find or that is not an executable file), a command that exits 126 or 127 (`LAUNCH_CODES`),
  and a job directory that cannot be removed after collection are refused, with the stage and
  the remote path in the message. A refused job's directory, when one was made, is left at the
  site.

126 and 127 are refused because at a site they are the codes of a program that did not START:
`timeout` exits them when it cannot execute the program, a shell when the program's interpreter
is missing, the dynamic loader when a shared library the program links is missing at the site
(a non-interactive ssh login loads no module environment). The local server raises for the
first two and never meets the third, because the host that runs it is the host that built the
program. None of them is the kernel's result, and a Generate repair could not fix one. The cost
is a program that exits 126 or 127 on its own, which the local path records as its result; the
runner and the quality-check command the conductor runs exit neither.

A refusal is a host-side failure, not the kernel's: the conductor lets it propagate, and
`_run_deterministic_substep` turns it into `deterministic_validate_error` (transport
fail_closed). A command that RAN and exited non-zero is not a refusal; it is reported in its
result as the local server reports it — with one difference in the value: a command killed by a
signal N is recorded as `128 + N`, the shell's spelling, where the local server records `-N`
(and when the signal dumped core, `timeout` adds a line saying so to the command's stderr). Nothing on the Validate path reads
the number beyond `ok`. No log entry is written until every status has
been read, so a refused job leaves no evidence behind it.

Nothing calls `execute_job` yet: the conductor is wired to it in a later pull request of issue
#293, and until then this module changes no run. Only `scheduler: none` is implemented — the job
script runs in the foreground of the ssh call; a site whose scheduler is anything else is refused
here until a scheduler backend implements `job_submit`.
"""

from __future__ import annotations

import platform
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.execution_sites import DIRECT_SCHEDULER, Site

#: The local programs the transport runs, in the order it runs them.
TRANSPORT_EXECUTABLES: tuple[str, ...] = ("ssh", "scp")
#: The programs the job script needs at the site beyond the POSIX utilities; the script checks
#: for them before it runs anything and fails without writing a status when one is missing.
REMOTE_EXECUTABLES: tuple[str, ...] = ("timeout",)
#: ssh options every transport call carries: never prompt (a prompt would hang a run), and give
#: up on a host that does not answer.
SSH_OPTIONS: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=30")
#: Seconds the device probe may take; one that hangs answers "unknown".
PROBE_TIMEOUT_SEC = 60
#: Seconds a command is given to exit after its timeout sends TERM, before KILL.
KILL_AFTER_SEC = 30
#: Seconds the job's ssh call is allowed beyond the sum of its commands' bounds, and the bound on
#: every other transport call.
TRANSPORT_GRACE_SEC = 300
#: Each command's output lives in this subdirectory of the job directory, which no shipped file may enter.
CONTROL_DIR = "ctl"
#: A path element of a job directory or a shipped file: no separator, no shell-active character,
#: and not led by `.` (no `..`, no hidden name) or `-` (read as an option).
_ELEMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
_TAG = re.compile(r"[a-z][a-z0-9_]*")
#: The first word of a status line the job script prints: `<marker> <tag> <rc> <t0> <t1>`, the
#: exit status and the epoch seconds before and after.
STATUS_MARKER = "atmofab-status"
#: The first word of a platform line: `<marker> <key> <value>`, one for each of
#: `PLATFORM_KEYS`, plus `gpu` when the request carries a probe; an empty value is "unknown".
PLATFORM_MARKER = "atmofab-platform"
PLATFORM_KEYS = ("machine", "node", "cpu")
#: A line of the script's stdout that carries no marker — what a login shell's startup files
#: print — is not read; one that carries a marker must carry exactly one, at its start.
_MARKERS = (STATUS_MARKER, PLATFORM_MARKER)
_STATUS_LINE = re.compile(rf"{STATUS_MARKER} (\S*) (\S*) (\S*) (\S*)")
_INT = re.compile(r"-?[0-9]+")
#: A `timeout` that fired exits 124, or 137 when the command ignored TERM and was killed.
_TIMEOUT_CODES = frozenset({124, 137})
#: The exit statuses of a program that did not start (see the module docstring).
LAUNCH_CODES = frozenset({126, 127})


class RemoteExecutionError(RuntimeError):
    """The job's evidence could not be produced or collected completely. The message names the
    stage and the remote path; a transport failure is a host-side failure, not the kernel's."""


def _server():
    """The build-runtime server module, reached the way `tools/execution_sites.py` reaches it,
    for the log writer and the validation this executor must share with the local path."""
    mcp_dir = str(Path(__file__).resolve().parents[1] / "mcp_servers")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import build_runtime_server

    return build_runtime_server


@dataclass(frozen=True)
class CommandSpec:
    """One command of a job, as the local path would hand it to the server.

    `tag` names the command's control files; `tool_name` is the tool its log entry is recorded
    under (the validator requires the names the local path records). `argv` and `cwd` are
    REMOTE paths, and `cwd` lies under the job directory. `record_cwd` is the LOCAL directory the
    command stands for — the `project_dir` the local path would have handed the server — and it
    is what the entry records as `cwd`: the post-execute gate requires a quality check's `cwd`
    to be the node's own `source/<source_id>/src`, holding its build control file. The remote
    `cwd` is the entry's `site.remote_cwd`. `env` is an override set, checked with
    the server's own `_validate_env_overrides` before anything is contacted. `timeout_sec` has
    no default: the local server's are 3600 for `run_program` and 1800 for
    `run_quality_checks`, and the same bound is kept only by passing them."""

    tag: str
    tool_name: str
    argv: tuple[str, ...]
    cwd: str
    record_cwd: str
    env: Mapping[str, str]
    timeout_sec: int
    command_log_path: Path
    capture_limit: int


@dataclass(frozen=True)
class JobRequest:
    """Everything one job needs. `ship` maps a path relative to the job directory to the local
    file copied there (the mode is kept, so an executable stays one); `dirs` are the directories,
    relative to the job directory, created before the first command. `platform_probe` is an argv
    whose first output line identifies the site's device, when the hardware class has one;
    `attribution` is recorded in each log entry exactly as the server records it: its
    `orchestration_id` and `agent_run_id`, and nothing else. `machine` is what the
    site's `uname -m` must answer before anything runs — by default this host's own, because the
    shipped binary was built here: a binary the site's loader cannot execute is not a result of
    the kernel, and `timeout`'s `execvp` would otherwise hand it to `sh` and report the shell's
    syntax error as the command's own exit status."""

    site: Site
    job_dir: str
    ship: Mapping[str, Path]
    commands: tuple[CommandSpec, ...]
    dirs: tuple[str, ...] = ()
    platform_probe: tuple[str, ...] | None = None
    attribution: Mapping[str, str] = field(default_factory=dict)
    machine: str = field(default_factory=platform.machine)


@dataclass(frozen=True)
class JobResult:
    """`results[i]` is `commands[i]`'s result in the local server's shape, or None when an
    earlier command failed and it did not run. `platform` is `{machine, node, cpu_model, gpu}`;
    `platform` carries no `site` key — the caller adds the site id where it records one.
    `site_record` is `{site, host, scheduler, job_id, remote_dir, queue_wait_ms}`. `collected` is
    the local copy of the job directory itself, so a command whose cwd was `<job_dir>/run` left
    its output under `collected/run/`."""

    results: tuple[dict[str, Any] | None, ...]
    platform: dict[str, str | None]
    site_record: dict[str, Any]
    collected: Path


def job_dir(site: Site, orchestration_id: str, agent_run_id: str) -> str:
    """The remote directory one job runs in: `<workdir>/<orchestration_id>/<agent_run_id>`."""
    if site.is_local or not site.workdir or not site.host:
        raise ValueError(f"site {site.site_id!r} is not a remote site")
    for name, value in (("orchestration_id", orchestration_id), ("agent_run_id", agent_run_id)):
        if not _ELEMENT.fullmatch(str(value)):
            raise ValueError(f"{name} {value!r} is not a single path element")
    return f"{site.workdir.rstrip('/')}/{orchestration_id}/{agent_run_id}"


def _relative(path: str, what: str) -> str:
    parts = path.split("/")
    if not path or not all(_ELEMENT.fullmatch(p) for p in parts):
        raise ValueError(f"{what} {path!r} is not a relative path of plain elements")
    if parts[0] == CONTROL_DIR:
        raise ValueError(f"{what} {path!r} enters the job's control directory {CONTROL_DIR}/")
    return path


def _under(path: str, root: str, what: str) -> None:
    """`path` is `root` or `root` followed by plain elements (no `..`, no empty element)."""
    rest = path[len(root):] if path.startswith(root) else None
    if rest is None or (rest and not (rest.startswith("/")
                                      and all(_ELEMENT.fullmatch(p) for p in rest[1:].split("/")))):
        raise ValueError(f"{what} {path!r} is not under {root} by plain path elements")


def _validate(request: JobRequest) -> None:
    """Refuse a malformed request before any transport call: a host defect, not the site's."""
    site = request.site
    if site.is_local or not site.host or not site.workdir:
        raise ValueError(f"site {site.site_id!r} is not a remote site")
    if site.scheduler != DIRECT_SCHEDULER:
        raise ValueError(
            f"site {site.site_id!r} submits through scheduler {site.scheduler!r}, which this "
            f"executor does not implement yet; only {DIRECT_SCHEDULER!r} runs")
    _under(request.job_dir, site.workdir.rstrip("/"), "job_dir")
    if request.job_dir == site.workdir.rstrip("/"):
        raise ValueError("job_dir is the site's workdir itself")
    if not request.commands:
        raise ValueError("a job runs at least one command")
    for rel in request.ship:
        _relative(rel, "shipped path")
    for rel in request.dirs:
        _relative(rel, "directory")
    tags = [c.tag for c in request.commands]
    if len(set(tags)) != len(tags):
        raise ValueError(f"command tags repeat: {tags}")
    server = _server()
    for c in request.commands:
        if not _TAG.fullmatch(c.tag):
            raise ValueError(f"command tag {c.tag!r} is not a lowercase token")
        if not c.argv:
            raise ValueError(f"command {c.tag!r} has an empty argv")
        if "/" in c.argv[0] and not c.argv[0].startswith("/"):
            # The script checks the program from the login directory, not from `cwd`.
            raise ValueError(f"command {c.tag!r} program {c.argv[0]!r} is a relative path")
        if isinstance(c.timeout_sec, bool) or not isinstance(c.timeout_sec, int) \
                or c.timeout_sec < 1:
            raise ValueError(f"command {c.tag!r} timeout_sec must be an integer >= 1")
        _under(c.cwd, request.job_dir, f"command {c.tag!r} cwd")
        if not (isinstance(c.record_cwd, str) and c.record_cwd.startswith("/")):
            raise ValueError(f"command {c.tag!r} record_cwd {c.record_cwd!r} is not an absolute "
                             f"local path")
        if c.cwd != request.job_dir:
            _relative(c.cwd[len(request.job_dir) + 1:], f"command {c.tag!r} cwd")
        server._validate_env_overrides(dict(c.env), c.tool_name)
        for key in c.env:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
                raise ValueError(f"command {c.tag!r} env name {key!r} is not a variable name")


def render_job_script(request: JobRequest) -> str:
    """The POSIX `sh` script that runs `request.commands` at the site.

    Each command writes `<tag>.stdout` / `<tag>.stderr` under the control directory, runs only
    when every earlier command exited 0, and is followed by its status line on the script's
    stdout (`STATUS_MARKER`). The script exits non-zero, before any command, when a directory
    cannot be made, a program in `REMOTE_EXECUTABLES` is missing or the machine is not `request.machine`, and before
    a command whose program cannot be found or, named by a path, is not executable: the local
    server raises for a program it cannot start rather than reporting an exit status, so these
    are the host's failures here too. Each such exit names what failed on stderr. Every value is
    quoted with `shlex.quote`."""
    q = shlex.quote
    j = request.job_dir
    ctl = f"{j}/{CONTROL_DIR}"
    # The first line printed is empty, so that a login banner printed without a newline ends
    # there rather than gluing onto the first platform line.
    lines = ["#!/bin/sh", "set -u", "echo",
             "fail() { echo \"job script: $2\" >&2; exit \"$1\"; }",
             f'[ "$(uname -m)" = {q(request.machine)} ] || fail 5 '
             + q(f"the site machine is not {request.machine}, which the shipped files were "
                 f"built on")]
    for prog in REMOTE_EXECUTABLES:
        lines.append(f"command -v {q(prog)} >/dev/null 2>&1 || fail 3 {q(f'{prog} is missing')}")
    # Not every `timeout` takes `-k` (older busybox builds refuse it, exit 1), and a refusal
    # there would be recorded as the command's own failure.
    lines.append("timeout -k 1 5 sh -c : >/dev/null 2>&1 || fail 3 'timeout does not take -k'")
    for rel in request.dirs:
        lines.append(f"mkdir -p {q(f'{j}/{rel}')} || fail 3 {q(f'cannot make {rel}')}")
    for c in request.commands:
        lines.append(f"[ -d {q(c.cwd)} ] || mkdir -p {q(c.cwd)} || fail 3 {q(f'cannot make {c.cwd}')}")
    # The platform facts, before any command runs, each on a line of its own and always printed:
    # a site that lacks one tool prints that fact empty.
    facts = [("machine", "uname -m"), ("node", "hostname"),
             ("cpu", "grep -m1 'model name' /proc/cpuinfo")]
    if request.platform_probe:
        # The probe's first line when it exits 0, empty otherwise.
        lines.append(f"g=$(timeout -k 5 {PROBE_TIMEOUT_SEC} {shlex.join(request.platform_probe)}"
                     f" < /dev/null 2>/dev/null) || g=")
        facts.append(("gpu", "printf '%s\\n' \"$g\" | sed -n 1p"))
    for key, fact in facts:
        lines.append(f"printf '%s %s %s\\n' {PLATFORM_MARKER} {key} \"$({fact} 2>/dev/null)\"")
    # A command's stdin is `/dev/null`, as the ssh call's own is; not pinned, because the second
    # makes the first unobservable under test.
    lines.append("rc=0")
    for c in request.commands:
        env_words = " ".join(q(f"{k}={v}") for k, v in sorted(c.env.items()))
        run = (f"cd {q(c.cwd)} && exec env {env_words + ' ' if env_words else ''}"
               f"timeout -k {KILL_AFTER_SEC} {c.timeout_sec} {shlex.join(c.argv)}")
        base = f"{ctl}/{c.tag}"
        lines += [
            'if [ "$rc" = 0 ]; then',
            f"  p=$(command -v {q(c.argv[0])}) || fail 4 {q(f'{c.argv[0]} is missing')}",
            (f'  case "$p" in /*) [ -f "$p" ] && [ -x "$p" ] || fail 4 '
             f"{q(f'{c.argv[0]} is not an executable file')};; esac"),
            "  t0=$(date +%s) || fail 6 'date failed'",
            f"  ( {run} ) > {q(base + '.stdout')} 2> {q(base + '.stderr')} < /dev/null",
            "  rc=$?",
            "  t1=$(date +%s) || fail 6 'date failed'",
            f'  echo "{STATUS_MARKER} {c.tag} $rc $t0 $t1"',
            "fi",
        ]
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def _transport(argv: list[str], *, stage: str, timeout: int, remote: str) -> str:
    # stdin is /dev/null so that ssh never reads the operator's terminal; not pinned, because
    # the test process's own stdin is not a terminal either.
    try:
        proc = subprocess.run(argv, text=True, capture_output=True, timeout=timeout,
                              check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RemoteExecutionError(
            f"{stage}: {argv[0]} did not finish within {timeout} sec; the job may still be "
            f"running ({remote})") from None
    except OSError as exc:
        raise RemoteExecutionError(f"{stage}: {argv[0]} could not be started: {exc}") from exc
    if proc.returncode != 0:
        raise RemoteExecutionError(
            f"{stage}: {argv[0]} exited {proc.returncode} ({remote}): "
            f"{(proc.stderr or proc.stdout).strip()[-2000:]}")
    return proc.stdout


def _ssh(host: str, command: str, *, stage: str, timeout: int, remote: str) -> str:
    return _transport(["ssh", *SSH_OPTIONS, "--", host, command],
                      stage=stage, timeout=timeout, remote=remote)


def _scp(sources: list[str], dest: str, *, stage: str, remote: str) -> None:
    # scp keeps a file's execute bit without `-p` (measured on OpenSSH 8.9, in both its legacy
    # and its SFTP protocol); `-p` would add only the times, which nothing reads.
    _transport(["scp", "-q", "-r", *SSH_OPTIONS, "--", *sources, dest],
               stage=stage, timeout=TRANSPORT_GRACE_SEC, remote=remote)


def _job_lines(stdout: str, remote: str) -> tuple[dict[str, list[tuple[str, str, str]]],
                                                   dict[str, list[str]]]:
    """The status lines by tag and the platform values by key, from the job script's stdout.
    A line with no marker is skipped; a line with a marker anywhere but once at its start, or
    that does not parse, is refused."""
    statuses: dict[str, list[tuple[str, str, str]]] = {}
    facts: dict[str, list[str]] = {}
    for line in stdout.splitlines():
        hits = sum(line.count(m) for m in _MARKERS)
        if not hits:
            continue
        if hits != 1 or not line.startswith(_MARKERS):
            raise RemoteExecutionError(
                f"a line carries a marker other than once at its start: {line[:200]!r} ({remote})")
        if line.startswith(PLATFORM_MARKER + " "):
            key, _, value = line[len(PLATFORM_MARKER) + 1:].partition(" ")
            facts.setdefault(key, []).append(value)
            continue
        m = _STATUS_LINE.fullmatch(line)
        if not m or not all(_INT.fullmatch(v) for v in m.group(2, 3, 4)):
            raise RemoteExecutionError(f"a status line does not parse: {line[:200]!r} ({remote})")
        statuses.setdefault(m.group(1), []).append(m.group(2, 3, 4))
    return statuses, facts


def _statuses(lines: dict[str, list[tuple[str, str, str]]], commands: tuple[CommandSpec, ...],
              remote: str) -> list[tuple[int, int, int] | None]:
    """Each command's `(rc, t0, t1)` from its status line, or None for a command that did not
    run; see the module docstring for what is refused."""
    unknown = sorted(set(lines) - {c.tag for c in commands})
    if unknown:
        raise RemoteExecutionError(f"status lines for no command of this job: {unknown} ({remote})")
    out: list[tuple[int, int, int] | None] = []
    upstream_ok = True
    for c in commands:
        what = f"command {c.tag!r}"
        got = lines.get(c.tag, [])
        if len(got) > 1:
            raise RemoteExecutionError(f"{what}: {len(got)} status lines, not one ({remote})")
        if not upstream_ok:
            if got:
                raise RemoteExecutionError(
                    f"{what} reports a status although an earlier command failed ({remote})")
            out.append(None)
            continue
        if not got:
            raise RemoteExecutionError(f"{what}: no status line, so the status is lost ({remote})")
        rc, t0, t1 = (int(v) for v in got[0])
        if t1 < t0:
            raise RemoteExecutionError(f"{what}: ended before it started ({remote})")
        out.append((rc, t0, t1))
        upstream_ok = rc == 0
    return out


def _platform(facts: dict[str, list[str]], probed: bool, remote: str) -> dict[str, str | None]:
    """The same record the local path builds, from the site's own answers: machine, node, the
    CPU model name, and the probe's first line (None for an empty answer). Each key the script
    prints must arrive exactly once, and no other."""
    expected = (*PLATFORM_KEYS, "gpu") if probed else PLATFORM_KEYS
    if set(facts) != set(expected) or any(len(v) != 1 for v in facts.values()):
        raise RemoteExecutionError(
            f"the platform lines are not one each of {', '.join(expected)}: "
            f"{ {k: len(v) for k, v in facts.items()} } ({remote})")
    value = {k: (v[0].strip() or None) for k, v in facts.items()}
    cpu = value["cpu"]
    cpu_model = (cpu.split(":", 1)[1].strip() or None) if cpu and ":" in cpu else None
    return {"machine": value["machine"], "node": value["node"], "cpu_model": cpu_model,
            "gpu": value.get("gpu")}


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _read_output(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return _server()._trim(text, limit)


def execute_job(request: JobRequest, *, local_tmp: Path) -> JobResult:
    """Run `request` at its site and return its results; see the module docstring for the
    refusals. `local_tmp` is a local directory this job owns: the files are staged in
    `<local_tmp>/stage` and the job directory is collected to `<local_tmp>/collected`, and
    neither may exist beforehand."""
    _validate(request)
    site = request.site
    host = str(site.host)
    remote = request.job_dir
    q = shlex.quote
    stage_dir = local_tmp / "stage"
    collected = local_tmp / "collected"
    for p in (stage_dir, collected):
        if p.exists():
            raise ValueError(f"{p} already exists; a job owns a fresh local_tmp")

    # 1. The job directory, fresh: `mkdir` without `-p` refuses a directory that is already there.
    parent = remote.rsplit("/", 1)[0]
    _ssh(host, f"if [ -e {q(remote)} ]; then echo {q(f'a stale job directory exists: {remote}')}"
               f" >&2; exit 1; fi; mkdir -p {q(parent)} && mkdir {q(remote)}",
         stage="create the job directory", timeout=TRANSPORT_GRACE_SEC, remote=remote)
    try:
        return _run_job(request, remote=remote, stage_dir=stage_dir, collected=collected)
    except RemoteExecutionError as exc:
        raise RemoteExecutionError(
            f"{exc}; the job directory is left at the site for inspection — remove {remote} "
            f"when done") from exc


def _run_job(request: JobRequest, *, remote: str, stage_dir: Path,
             collected: Path) -> JobResult:
    """`execute_job` once the job directory exists."""
    site = request.site
    host = str(site.host)
    q = shlex.quote

    # 2. Stage and ship the files, and an empty control directory for the commands' output.
    for rel, src in request.ship.items():
        dst = stage_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    (stage_dir / CONTROL_DIR).mkdir(parents=True, exist_ok=True)
    tops = sorted({p.name for p in stage_dir.iterdir()})
    _scp([str(stage_dir / t) for t in tops], f"{host}:{remote}/",
         stage="ship the job's files", remote=remote)

    # 3. Run the script in the foreground; its commands' statuses are on its stdout, and its own
    #    exit status is non-zero only when it failed outside them. The script is the ssh call's
    #    command string, held in the shell's memory — never a file: a file under the job
    #    directory (or anywhere else this user owns) is one the command can rewrite while the
    #    script runs, and bash reads a script file a command at a time, so the rewritten rest is
    #    what it executes (reproduced in round 4: the second command skipped, a clean status
    #    printed in its place).
    bound = sum(c.timeout_sec + KILL_AFTER_SEC for c in request.commands) + TRANSPORT_GRACE_SEC
    job_stdout = _ssh(host, f"sh -c {q(render_job_script(request))}",
                      stage="run the job script", timeout=bound, remote=remote)

    # 4. Collect. A failure here leaves the remote directory for the operator.
    _scp([f"{host}:{remote}"], str(collected), stage="collect the job directory", remote=remote)
    ctl = collected / CONTROL_DIR

    # 5. Read every status, and the platform, before anything is recorded.
    status_lines, facts = _job_lines(job_stdout, remote)
    statuses = _statuses(status_lines, request.commands, remote)
    platform_record = _platform(facts, bool(request.platform_probe), remote)
    for c, status in zip(request.commands, statuses):
        if status is not None and status[0] in LAUNCH_CODES:
            tail = _read_output(ctl / f"{c.tag}.stderr", 2000).strip()[-2000:]
            raise RemoteExecutionError(
                f"command {c.tag!r} exited {status[0]}: at a site that is the code of a program "
                f"that did not start (a missing interpreter or shared library, a program that "
                f"cannot be executed) — or of a program that exited {status[0]} itself; its "
                f"stderr ends: {tail or '(empty)'} ({remote})")

    # 6. Remove the remote directory; the evidence is local now.
    _ssh(host, f"rm -rf {q(remote)}", stage="remove the collected job directory",
         timeout=TRANSPORT_GRACE_SEC, remote=remote)

    # 7. The log entries, one per command that ran, in the local server's shape plus `site`.
    #    `command` names each shipped file by its LOCAL source, so the entry says which of this
    #    host's artifacts ran — `_validate_run_program_inputs` binds `command[0]` to the node's
    #    own build `bin/` — and `site.remote_command` is the argv as the site executed it; `cwd`
    #    is the command's `record_cwd` for the same reason, and `site.remote_cwd` the site's.
    server = _server()
    local_of = {f"{remote}/{rel}": str(src) for rel, src in request.ship.items()}
    results: list[dict[str, Any] | None] = []
    for c, status in zip(request.commands, statuses):
        if status is None:
            results.append(None)
            continue
        rc, t0, t1 = status
        timed_out = rc in _TIMEOUT_CODES and t1 - t0 >= c.timeout_sec
        argv = [local_of.get(a, a) for a in c.argv]
        command_id = uuid.uuid4().hex
        result: dict[str, Any] = {
            "ok": rc == 0,
            "return_code": None if timed_out else rc,
            "command": argv,
            "executed_command": shlex.join(argv),
            "cwd": c.record_cwd,
            "stdout": _read_output(ctl / f"{c.tag}.stdout", c.capture_limit),
            "stderr": _read_output(ctl / f"{c.tag}.stderr", c.capture_limit),
        }
        if timed_out:
            result["error"] = f"timeout: exceeded {c.timeout_sec} sec"
        entry = {
            "version": 1,
            "command_id": command_id,
            "tool_name": c.tool_name,
            "started_at_utc": _iso(t0),
            "ended_at_utc": _iso(t1),
            "elapsed_ms": (t1 - t0) * 1000,
            "cwd": c.record_cwd,
            "command": argv,
            "executed_command": shlex.join(argv),
            "timeout_sec": c.timeout_sec,
            "capture_limit": c.capture_limit,
            "env_override_keys": sorted(c.env),
            "ok": result["ok"],
            "return_code": result["return_code"],
            **({"error": result["error"]} if timed_out else {}),
            "site": {"site": site.site_id, "host": host, "scheduler": site.scheduler,
                     "job_id": None, "remote_cwd": c.cwd, "remote_command": list(c.argv)},
            **server._attribution(dict(request.attribution)),
        }
        server._append_command_log(c.command_log_path, entry)
        result["command_id"] = command_id
        result["command_log_path"] = str(c.command_log_path)
        log_ref = server._path_to_ref(c.command_log_path)
        if log_ref is not None:
            result["command_log_ref"] = log_ref
        results.append(result)

    return JobResult(
        results=tuple(results),
        platform=platform_record,
        site_record={"site": site.site_id, "host": host, "scheduler": site.scheduler,
                     "job_id": None, "remote_dir": remote, "queue_wait_ms": 0},
        collected=collected)
