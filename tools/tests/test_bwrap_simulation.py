"""Layer-1 bwrap simulation: the leaf profile, under a real bwrap.

Fast (seconds, no LLM, no billed API) reproduction of what a leaf can and cannot reach under
the rendered profile `record_launch` writes — since issue #534 PR-J4-b the one profile is a
`workshop leaf` lineage's (`build_readonly_bwrap_profile` with `leaf_mode: workshop`, its
cwd the lineage's tree, its two rw binds the lineage's `config/` and `tree/`; the read-only
profile the pure leaf ran under is refused, and the builder keeps its old name until J4-d).
A synthetic "leaf" script runs inside the sandbox and reports one line per operation, so each
reachability claim is measured rather than argued; the classes further down exec the real
claude / codex CLI under the same profile against loopback API stand-ins, and the workshop
wrapper's operations under its own sandbox.

Four larger simulations stood here until issue #171 PR-2: they drove the READ-WRITE profile
(`build_bwrap_profile`), which carried a capability's `write_roots` as `--bind` mounts into the
checkout, and asserted that the terminal FS-diff attributed exactly the in-scope writes. Both
halves are gone — no leaf holds write authority under the checkout (a workshop leaf writes its
own tree, outside it, and the host writes every artifact) — so there is no diff to audit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import yaml

from tools import orchestration_runtime as ort
from tools import workflow_conductor as wc
from tools.orchestration_runtime import (
    _ensure_orchestration_audit_dirs,
    build_readonly_bwrap_profile,
    render_bwrap_command,
)
from tools.tests import skip_probes
from tools.tests.http_stand_ins import ScriptedMessagesApi
from tools.tests.llm_samples import sample_config_with, workshop_yaml


class _Loopback400:
    """An HTTP stand-in for the vendor API that answers every request 400 and counts them.

    The unbilled way to observe that a CLI got as far as its first API request: the
    request never leaves the machine and the CLI stops on the error. The sandbox shares
    the host's network namespace (no `--unshare-net`), so `127.0.0.1` reaches it.
    """

    def __init__(self) -> None:
        hits: list[str] = []
        bodies: list[bytes] = []

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                hits.append(f"{self.command} {self.path}")
                body = (b'{"type":"error","error":{"type":"invalid_request_error",'
                        b'"message":"loopback stand-in"}}')
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = _answer
            do_GET = _answer

            def log_message(self, *args) -> None:  # BaseHTTPRequestHandler's spelling
                pass

        self.hits = hits
        self.bodies = bodies  # one raw request body per hit, same order
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _bwrap_stdout(cmd: list[str], *, timeout: int = 90) -> str:
    """Run a rendered bwrap command and return its stdout — with the FAILURE readable.

    Every call site used to take `.stdout` and throw the rest away, so a sandbox that refused to
    start produced `AssertionError: 'WRITE_NEW:OK' not found in ''` and nothing else. Measured on
    this repository's first CI runs: eight of these tests failed that way on a GitHub runner and
    the log could not say why, which is the same defect shape as a client reporting a framing
    error for a server that never started.

    stdout is still what the caller asserts on — the assertions are about what the confined
    process printed. What changes is that a non-zero exit or a silent run carries bwrap's own
    stderr and the exit code into the exception, so the reason travels with the failure.
    """
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                         check=False)  # the exit code is inspected below
    if res.returncode != 0 or not res.stdout.strip():
        raise AssertionError(
            f"the sandboxed command produced no usable stdout (exit {res.returncode}).\n"
            f"stderr: {res.stderr.strip() or '(empty)'}\n"
            f"stdout: {res.stdout.strip() or '(empty)'}\n"
            f"argv[0:6]: {cmd[:6]}")
    return res.stdout


def _bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    binds: list[str] = []
    for p in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(p).exists():
            binds += ["--ro-bind", p, p]
    try:
        r = subprocess.run(
            ["bwrap", *binds, "--dev", "/dev", "--", "/bin/true"],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _prepared_lineage(test: unittest.TestCase, repo: Path, orch: str, backend: str,
                      lineage: str | None = None) -> tuple[dict, dict]:
    """A `workshop leaf` lineage of `orch`, prepared by the REAL preparer for `backend`
    (`_prepare_claude_workshop_home` / `_prepare_codex_workflow_home`, cold), and the profile
    kwargs production derives from it (`claude_workshop_profile_kwargs` /
    `codex_workshop_profile_kwargs`, the spellings `record_launch` uses). Returns
    `(isolation, profile_kwargs)`.

    Hermetic: the homes root is a tempdir of the row's own, OUTSIDE the checkout (a backend rw
    bind inside it is refused), and so is the operator credential the lineage binds — a
    placeholder claude credentials file, or a seeded codex `auth.json` under `CODEX_HOME`. A
    repository that has only its audit dirs gets an empty `orchestration_meta.json`, which is
    all the preparers read and write."""
    from tools import operator_private_root
    from tools.tests.private_root_fixture import seed_codex_auth
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    base = Path(tmp.name).resolve()
    meta = repo / "workspace" / "orchestrations" / orch / "orchestration_meta.json"
    if not meta.exists():
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text("{}", encoding="utf-8")
    env = {"ATMOFAB_WORKFLOW_HOMES_ROOT": str(base / "homes")}
    patches = []
    if backend == "codex":
        env["CODEX_HOME"] = str(seed_codex_auth(base / "operator-codex"))
    else:
        credentials = base / "operator-claude" / ".credentials.json"
        credentials.parent.mkdir(mode=0o700)
        credentials.write_text('{"claudeAiOauth": {"accessToken": "placeholder"}}',
                               encoding="utf-8")
        os.chmod(credentials, 0o600)
        patches.append(mock.patch.object(operator_private_root, "claude_credentials_file",
                                         lambda: credentials))
    patches.append(mock.patch.dict(os.environ, env))
    for patcher in patches:
        patcher.start()
        test.addCleanup(patcher.stop)
    lineage = lineage or str(uuid.uuid4())
    if backend == "codex":
        iso = ort._prepare_codex_workflow_home(repo, orch, lineage, resume=False)
        return iso, ort.codex_workshop_profile_kwargs(iso)
    iso = ort._prepare_claude_workshop_home(repo, orch, lineage, resume=False)
    return iso, ort.claude_workshop_profile_kwargs(iso)


class BwrapReadonlyProfileTests(unittest.TestCase):
    """Every leaf runs under the one bwrap profile `build_readonly_bwrap_profile` builds —
    since issue #534 PR-J4-b the `workshop leaf` lineage's (`leaf_mode: workshop`, writable,
    its cwd the lineage's tree; the read-only profile the pure leaf ran under is refused, and
    the function and this class keep their names until J4-d because `docs/BWRAP_ENABLEMENT.md`
    and the runtime cite them). No capability, no write_roots, and since issue #227 NO
    CHECKOUT: an empty tmpfs sits at `repo_root`. Confirm the rendered sandbox hides the
    repository from the leaf, keeps the leaf's own `workspace/tmp/<arid>` writable, keeps the
    network, leaves the host's copy untouched, does not mount a sibling lineage, and refuses an
    install root that aliases the checkout. Every profile here is built over a lineage home
    the REAL preparer made (`_prepared_lineage`, or the codex fixture below)."""

    @classmethod
    def setUpClass(cls) -> None:
        if not _bwrap_usable():
            raise AssertionError(
                "bwrap is absent or unprivileged user namespaces are disabled; see "
                "docs/BWRAP_ENABLEMENT.md (CI lifts kernel.apparmor_restrict_unprivileged_userns)")

    def _leaf_script(self, arid: str, tmp_dir: str, repo: Path) -> str:
        # The leaf starts in its lineage's tree; the probe moves to the CHECKOUT'S path, which
        # is what it reports on, so its relative paths and `os.getcwd()` name the checkout.
        return textwrap.dedent(f"""
            import os, socket
            from pathlib import Path
            os.chdir({str(repo)!r})
            def report(tag, ok, e=""):
                print(f"{{tag}}:{{'OK' if ok else 'FAIL'}}", e, flush=True)
            # the checkout is NOT bound -> a repo file is absent, and so is the tree
            print("READ_REPO:" + ("READABLE" if Path("AGENTS_SIM.md").exists() else "HIDDEN"),
                  flush=True)
            print("CWD_ENTRIES:" + ",".join(sorted(p.name for p in Path(".").iterdir())),
                  flush=True)
            # What is MOUNTED at the cwd, from the kernel's own table: the tmpfs emitted at
            # repo_root, not bwrap's auto-created mountpoint parents (a deleted `--tmpfs`
            # still gives a cwd whose only entry is `workspace`, and a write there still
            # never reaches the host — this line is what tells the two apart).
            # The last matching entry is the visible one for a stacked point; mountinfo
            # octal-escapes a space / tab / newline / backslash in the path fields, so
            # decode before comparing (the checkout under `tools/` is hidden here, so this
            # cannot reuse `_mount_table`'s decoder; it is the same one).
            import re
            fstype = "none"
            for line in open("/proc/self/mountinfo"):
                fields = line.split()
                point = re.sub(r"\\\\([0-7]{{3}})", lambda m: chr(int(m.group(1), 8)), fields[4])
                if point == os.getcwd():
                    fstype = fields[fields.index("-") + 1]
            print("CWD_MOUNT:" + fstype, flush=True)
            # tmp scratch (workspace/tmp/<arid>) is bound rw, and so is the profile's tmp_dir
            try:
                Path("workspace/tmp/{arid}/scratch.txt").write_text("x"); report("WRITE_TMP", True)
            except Exception as e:
                report("WRITE_TMP", False, repr(e))
            try:
                Path("{tmp_dir}/scratch.txt").write_text("x"); report("WRITE_SANDBOX_TMP", True)
            except Exception as e:
                report("WRITE_SANDBOX_TMP", False, repr(e))
            # a write at the checkout's path lands on the tmpfs, not on the host
            try:
                Path("AGENTS_SIM.md").write_text("mutated"); report("WRITE_CWD", True)
            except Exception as e:
                report("WRITE_CWD", False, repr(e))
            try:
                socket.gethostbyname("api.anthropic.com"); report("DNS", True)
            except Exception as e:
                report("DNS", False, repr(e))
        """)

    def test_readonly_profile_hides_the_checkout_and_keeps_own_tmp_writable(self) -> None:
        """The repository is absent from the sandbox; only the leaf's own tmp root remains.

        Under a claude lineage's workshop profile (the leaf's cwd is its tree; the probe
        moves to the checkout's path, which is what it reports on). `CWD_ENTRIES` is the
        whole listing of the checkout's path from inside: exactly `workspace`, the
        mountpoint bwrap creates for the rw binds of the leaf's two scratch roots
        (`workspace/tmp/<arid>` and the profile's `tmp_dir` under `sandboxes/`). `CWD_MOUNT`
        is the filesystem type mounted at the cwd per `/proc/self/mountinfo` — `tmpfs` — and
        it is the assertion that pins the `--tmpfs` EMISSION: measured (round 1), with the
        emission deleted outright the listing is still `workspace` (bwrap creates the
        mountpoint's parents) and the host is still unchanged, so the other tags stay green.
        A write at the checkout's path succeeds INTO THE TMPFS and the host's file is
        unchanged — the checkout is not read-only, it is not there.
        """
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t).resolve()
            orch, arid = "orch_ro", "arid_ro"
            _ensure_orchestration_audit_dirs(repo, orch)
            (repo / "AGENTS_SIM.md").write_text("orig\n", encoding="utf-8")
            _iso, kwargs = _prepared_lineage(self, repo, orch, "claude")
            profile = build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id=orch, agent_run_id=arid,
                backend_command="python3", backend_type="claude", **kwargs)
            self.assertEqual(profile.get("write_roots"), [])
            cmd = render_bwrap_command(
                profile=profile,
                command_argv=["python3", "-c",
                              self._leaf_script(arid, profile["tmp_dir"], repo)])
            out = _bwrap_stdout(cmd)
            self.assertIn("READ_REPO:HIDDEN", out, out)
            self.assertIn("CWD_ENTRIES:workspace\n", out, out)
            self.assertIn("CWD_MOUNT:tmpfs\n", out, out)
            self.assertIn("WRITE_TMP:OK", out, out)
            self.assertIn("WRITE_SANDBOX_TMP:OK", out, out)
            self.assertIn("WRITE_CWD:OK", out, out)
            self.assertIn("DNS:OK", out, out)
            # the repo file is unchanged on the host: the write landed on the tmpfs
            self.assertEqual((repo / "AGENTS_SIM.md").read_text(), "orig\n")
            # ... while the own-scratch writes reached the host through the rw binds.
            self.assertTrue((repo / "workspace" / "tmp" / arid / "scratch.txt").is_file())
            self.assertTrue((Path(profile["tmp_dir"]) / "scratch.txt").is_file())

    def test_readonly_profile_hides_workspace_from_the_leaf(self) -> None:
        """A leaf cannot READ another run's records, the producer's own reasoning, or the
        source trees — the checkout is not in the sandbox (issue #227). Driven under a codex
        lineage's workshop profile since issue #534 PR-J4-b; the history below is the read-only
        profile's, under which the same mount set was first measured.

        FOUND BY THE ROUND-2 CODEX REVIEW (P1) and by the round-1 security axis before it. A
        CLAUDE pure leaf is tool-free and could read nothing anyway; a CODEX pure leaf is
        `codex exec --sandbox read-only` — tool-BEARING — and until Z4 (issue #171) its reads
        were refused by the leaf hook layer against the empty `allowed_read_roots` this profile
        still records. Deleting that layer left the read boundary to nothing, and the sharpest
        gain is here: a VERIFY leaf reading the producer's `dialogs/leaf.stdout.jsonl` for the
        run it is reviewing can reuse the producer's conclusions instead of reviewing the
        supplied context, which defeats the persona separation the verify substep calls
        structural. Past artifacts and sibling certified sources are under here too, and
        `docs/workflow/WORKFLOW_CORE.md` invariants 6-8 forbid referencing them.

        Issue #171 PR-1 hid those trees with per-tree tmpfs overlays and left `tools/`,
        `docs/` and `spec/` readable — the prompt gives the leaf the gate's RULES, the checkout
        gave it the CHECKER (`tools/validate_pipeline_semantics.py`), and a leaf that reads
        the checker can satisfy a presence floor's exact signal without doing the work the
        floor stands for. Issue #227 replaced the overlays with one tmpfs over the whole
        checkout, so this row pins the three source trees and `.git/` as HIDDEN beside the
        artifact trees and keeps each formerly overlaid tree as its own tag (the deletion of
        `_leaf_hidden_artifact_trees` is covered by these mounts, not by the function). The
        control against a sandbox in which everything is missing is `OWN_TMP:WRITABLE` — a
        RELATIVE path, so a wrong `--chdir` or a dead rw bind fails it — plus `_bwrap_stdout`'s
        own exit-code check; an absolute read of the interpreter's directory was tried as a
        control and dropped (round 1): it cannot be false while the probe runs.

        Driven under REAL bwrap rather than asserted on the rendered argv: what is under test is
        whether the mount actually hides the tree, which an argv comparison cannot answer.
        """
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t).resolve()
            orch, arid = "orch_ro2", "arid_ro2"
            _ensure_orchestration_audit_dirs(repo, orch)
            # The producer's persisted reasoning, in the run this leaf would be reviewing.
            dialogs = repo / "workspace" / "orchestrations" / orch / "agents" / "producer" / "dialogs"
            dialogs.mkdir(parents=True, exist_ok=True)
            (dialogs / "leaf.stdout.jsonl").write_text("PRODUCER REASONING\n", encoding="utf-8")
            # A sibling node's certified source, and an IR from an earlier run.
            sibling = repo / "workspace" / "pipelines" / "sib" / "source" / "s1" / "src"
            sibling.mkdir(parents=True, exist_ok=True)
            (sibling / "sib_model.f90").write_text("module sib\nend module\n", encoding="utf-8")
            # An operator's ARCHIVE of an earlier run, and the promoted-artifact tree. Both hold
            # the same content under a different name; the round-3 review measured the first
            # version of this profile leaving 51 such archives readable, and gitignored, which is
            # why a `.gitignore`-respecting grep had not surfaced them.
            archive = repo / "workspace_20260723" / "orchestrations" / "old" / "agents" / "p" / "dialogs"
            archive.mkdir(parents=True, exist_ok=True)
            (archive / "leaf.stdout.jsonl").write_text("ARCHIVED REASONING\n", encoding="utf-8")
            released = repo / "releases" / "component" / "rel_1"
            released.mkdir(parents=True, exist_ok=True)
            (released / "model.f90").write_text("module rel\nend module\n", encoding="utf-8")
            # The three source trees and the git store: the gate's implementation, the
            # documents the rules were derived from, the specs of every other node.
            for rel, body in (("tools/validate_pipeline_semantics.py", "def gate(): ...\n"),
                              ("docs/x.md", "# rule\n"),
                              ("spec/x.yaml", "kind: x\n"),
                              (".git/HEAD", "ref: refs/heads/main\n")):
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(body, encoding="utf-8")
            _iso, kwargs = _prepared_lineage(self, repo, orch, "codex")
            profile = build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id=orch, agent_run_id=arid,
                backend_command="python3", backend_type="codex", **kwargs)
            # The leaf starts in its tree; the probe reads at the checkout's path.
            script = textwrap.dedent(f"""
                import os
                from pathlib import Path
                os.chdir({str(repo)!r})
                for tag, rel in (
                        ("DIALOG", "workspace/orchestrations/{orch}/agents/producer/dialogs/leaf.stdout.jsonl"),
                        ("SIBLING", "workspace/pipelines/sib/source/s1/src/sib_model.f90"),
                        ("ARCHIVE", "workspace_20260723/orchestrations/old/agents/p/dialogs/leaf.stdout.jsonl"),
                        ("RELEASES", "releases/component/rel_1/model.f90"),
                        ("TOOLS", "tools/validate_pipeline_semantics.py"),
                        ("DOCS", "docs/x.md"),
                        ("SPEC", "spec/x.yaml"),
                        ("GIT", ".git/HEAD")):
                    print(f"{{tag}}:" + ("READABLE" if Path(rel).exists() else "HIDDEN"), flush=True)
                # ... while the leaf's OWN tmp root is still there: it is the leaf's TMPDIR.
                p = Path("workspace/tmp/{arid}/probe.txt")
                try:
                    p.write_text("{{}}"); print("OWN_TMP:WRITABLE", flush=True)
                except Exception as e:
                    print("OWN_TMP:FAIL " + repr(e), flush=True)
            """)
            out = _bwrap_stdout(render_bwrap_command(
                profile=profile, command_argv=["python3", "-c", script]))
            self.assertIn("DIALOG:HIDDEN", out, out)
            self.assertIn("SIBLING:HIDDEN", out, out)
            self.assertIn("ARCHIVE:HIDDEN", out, out)
            self.assertIn("RELEASES:HIDDEN", out, out)
            self.assertIn("TOOLS:HIDDEN", out, out)
            self.assertIn("DOCS:HIDDEN", out, out)
            self.assertIn("SPEC:HIDDEN", out, out)
            self.assertIn("GIT:HIDDEN", out, out)
            self.assertIn("OWN_TMP:WRITABLE", out, out)
            # The host's copies are untouched — this hides, it does not delete.
            self.assertEqual((dialogs / "leaf.stdout.jsonl").read_text(), "PRODUCER REASONING\n")
            self.assertEqual((repo / "tools" / "validate_pipeline_semantics.py").read_text(),
                             "def gate(): ...\n")

    # ---- issue #226: the backend CLI itself starts inside the profile rendered for it ----

    def _assert_backend_cli_starts_inside_its_profile(self, backend_type: str, cli: str) -> None:
        """Exec the operator's REAL backend CLI, by name, under the profile production renders.

        The ro bind of the CLI's install root is what `_backend_runtime_bind_paths` exists
        for, and until issue #226 it was complete for one install shape only: a `claude`
        literal, and nothing for a `codex` behind a volta shim (measured: rc=7, `Volta
        update error`, because the shim needs `~/.volta/tools` and `~/.volta/layout.*`
        beside `~/.volta/bin`). The rule is now shape-delimited, and a unit row over a
        synthetic tree cannot say whether the REAL install is covered — only the real CLI
        under real bwrap can, which is why this row does not fake the shim.

        The rw half is production's: the profile is a workshop lineage's (`_prepared_lineage`
        — the real preparer and `claude_workshop_profile_kwargs` /
        `codex_workshop_profile_kwargs`), so the CLI home is the lineage's private `config/`
        under the row's own homes root and the operator's `~/.claude` / `~/.codex` is never
        bound — measured, `codex --version` creates `tmp/arg0/codex-arg0*/` under whichever
        `CODEX_HOME` it runs with, which the first version of this row left in the operator's
        real `~/.codex`. The CLI is launched by NAME through the host PATH the profile env
        carries. `_bwrap_stdout` raises with bwrap's stderr and exit code, so a regression
        reads as the CLI's own start-up error. On a host carrying neither CLI both rows skip,
        and the criterion is unmeasured there.
        """
        skip_probes.require_installed(self, cli)
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t).resolve()
            orch, arid = f"orch_{cli}", f"arid_{cli}"
            _ensure_orchestration_audit_dirs(repo, orch)
            _iso, kwargs = _prepared_lineage(self, repo, orch, backend_type)
            profile = build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id=orch, agent_run_id=arid,
                backend_command=cli, backend_type=backend_type, **kwargs)
            out = _bwrap_stdout(render_bwrap_command(profile=profile,
                                                     command_argv=[cli, "--version"]),
                                timeout=120)
        self.assertTrue(out.strip(), out)

    _BIND_ALIAS_CHILD = textwrap.dedent("""
        import os, sys
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        import tools.orchestration_runtime as ort
        d = Path(sys.argv[2]); home = Path(sys.argv[5]) if len(sys.argv) > 5 else d / "home" / "user"
        repo = Path(sys.argv[3]); path_dir = sys.argv[4]
        os.environ["HOME"] = str(home)
        os.environ["PATH"] = f"{path_dir}:" + os.environ["PATH"]
        ort._ensure_orchestration_audit_dirs(repo, "o")
        # A codex workshop lineage, prepared by the real preparer under a homes root of the
        # row's own (outside every layout's checkout), with a seeded operator auth.json.
        meta = repo / "workspace" / "orchestrations" / "o" / "orchestration_meta.json"
        if not meta.exists():
            meta.write_text("{}", encoding="utf-8")
        origin = d / "codex-origin"
        origin.mkdir(mode=0o700, exist_ok=True)
        (origin / "auth.json").write_text("{}", encoding="utf-8")
        os.environ["CODEX_HOME"] = str(origin)
        import uuid
        # One homes root per invocation: the cases of one row share `d`, and a container's
        # owner marker names the checkout that claimed it.
        os.environ["ATMOFAB_WORKFLOW_HOMES_ROOT"] = str(d / f"homes_{uuid.uuid4().hex}")
        iso = ort._prepare_codex_workflow_home(repo, "o", uuid.uuid4().hex, resume=False)
        try:
            profile = ort.build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id="o", agent_run_id="A",
                backend_command="cli-sim", backend_type="codex",
                **ort.codex_workshop_profile_kwargs(iso))
        except ValueError as exc:
            print("REFUSED", exc)
        else:
            print("ACCEPTED")
            # An ACCEPTED root is exempt because its spelling contains the checkout and the
            # tmpfs stacks on top: observe that under the rendered profile (a nested bwrap)
            # rather than take the docstring's word for it. Both the run artifact and the
            # gate's implementation are read at the ALIAS the root exposes. The leaf starts in
            # its tree; the probe reads relative to the checkout's path.
            probe = (
                "import os\\n"
                f"os.chdir({str(repo)!r})\\n"
                "from pathlib import Path\\n"
                "for tag, rel in (('DIALOG', 'workspace/orchestrations/o/agents/p/dialogs/leaf.stdout.jsonl'),"
                " ('TOOLS', 'tools/validate_pipeline_semantics.py'),"
                " ('UNDER_REPO', 'alias2/orchestrations/o/agents/p/dialogs/leaf.stdout.jsonl')):\\n"
                "    print(tag + ':' + ('READABLE' if Path(rel).exists() else 'HIDDEN'), flush=True)\\n")
            import subprocess
            res = subprocess.run(
                ort.render_bwrap_command(profile=profile, command_argv=["python3", "-c", probe]),
                capture_output=True, text=True, timeout=60, check=False)
            print(res.stdout.strip() or f"PROBE FAILED rc={res.returncode} {res.stderr.strip()}")
    """)

    @staticmethod
    def _outer_bwrap(binds: list[tuple[Path, Path]], tmpfs: list[Path] = ()) -> list[str]:
        argv = ["bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        for src, dst in binds:
            argv += ["--bind", str(src), str(dst)]
        for point in tmpfs:
            argv += ["--tmpfs", str(point)]
        return argv

    def test_a_bind_mounted_install_root_that_aliases_the_checkout_is_refused(self) -> None:
        """Round-3 security finding (issue #226): `realpath` is blind to a bind mount, so a
        data disk bound into `~/work` that holds both the checkout and a CLI wrapper, with the
        workflow started from the disk's own spelling, bound `~/work` read-only and exposed
        the hidden `workspace/` at `~/work/atmofab/...` (measured: `PRODUCER REASONING`).
        `_refuse_backend_ro_alias_of_repo` compares inodes now. The bind mount needs a mount
        namespace, so the layout is built under an OUTER bwrap and the builder runs inside
        it; the control row is the same layout with `repo_root` spelled through the bind,
        which the overlays cover and the builder accepts."""
        with tempfile.TemporaryDirectory() as t:
            d = Path(t).resolve()
            data_work = d / "data" / "work"
            (data_work / "atmofab" / "workspace").mkdir(parents=True)
            # Planted for the accepted row's probe: what the alias would expose.
            for rel, body in (("workspace/orchestrations/o/agents/p/dialogs/leaf.stdout.jsonl",
                               "PRODUCER REASONING\n"),
                              ("tools/validate_pipeline_semantics.py", "def gate(): ...\n")):
                (data_work / "atmofab" / rel).parent.mkdir(parents=True, exist_ok=True)
                (data_work / "atmofab" / rel).write_text(body, encoding="utf-8")
            bindir = data_work / "npm" / "bin"
            bindir.mkdir(parents=True)
            (bindir / "cli-sim").write_text("#!/bin/sh\n", encoding="utf-8")
            (bindir / "cli-sim").chmod(0o755)
            (d / "home" / "user" / "work").mkdir(parents=True)
            (d / "ch").mkdir(mode=0o700)
            repo_root = Path(__file__).resolve().parents[2]
            home_work = d / "home" / "user" / "work"
            outer = [*self._outer_bwrap([(data_work, home_work)]), "--",
                     sys.executable, "-c", self._BIND_ALIAS_CHILD, str(repo_root), str(d)]
            refused = subprocess.run([*outer, str(data_work / "atmofab"), str(home_work / "npm" / "bin")],
                                     capture_output=True, text=True, timeout=120,
                                     check=False)  # the exit code is asserted below
            self.assertEqual(refused.returncode, 0, refused.stderr)
            self.assertIn("REFUSED", refused.stdout, refused.stdout)
            self.assertIn("is the same directory as", refused.stdout)
            accepted = subprocess.run([*outer, str(home_work / "atmofab"), str(home_work / "npm" / "bin")],
                                      capture_output=True, text=True, timeout=120,
                                      check=False)  # the exit code is asserted below
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(accepted.stdout.split("\n")[0], "ACCEPTED", accepted.stdout)
            # The exemption holds under the mount: the root `~/work` is ro-bound and the
            # tmpfs at `~/work/atmofab` stacks on top, so neither the dialogs nor the gate's
            # implementation is readable at the alias (issue #227 widened the second name's
            # gain from the artifact trees to `tools/`).
            self.assertIn("DIALOG:HIDDEN", accepted.stdout, accepted.stdout)
            self.assertIn("TOOLS:HIDDEN", accepted.stdout, accepted.stdout)

    def test_a_bind_mount_of_a_hidden_tree_at_or_below_the_install_root_is_refused(self) -> None:
        """Round-5 security finding and the round-3 residual, closed together by the mount
        table (`_mount_table` / `_fs_identity`): a `$HOME` child that IS a bind mount of
        `<checkout>/workspace` holding the wrapper (`realpath` is blind to it, and the inode
        walk only sees ancestors), and a bind mount of that tree BELOW a real root
        (`~/tools/alias`). Both printed the planted dialogs before the mount-table
        comparison landed (measured under a nested bwrap). Control: the same root with no
        mount is accepted. The EXEMPT-root rows are issue #227's rounds 1 and 2: the same
        bind below a root the spelling exemption used to skip entirely; then the two
        layouts that told a rule keyed on the mount point's PATH apart from one keyed on
        where the checkout APPEARS (a checkout that is a bind of a tree under the root, a
        foreign mount between the root and the checkout), the bind-of-the-root shape the
        second rule must still catch, and a stacked mount (`_fs_identity` read the hidden
        bottom entry). The accepted exempt rows also observe the probe under the rendered
        profile: the dialogs stay hidden at the checkout's path and under it."""
        with tempfile.TemporaryDirectory() as t:
            d = Path(t).resolve()
            home = d / "home" / "user"
            repo = home / "atmofab"
            ws_bin = repo / "workspace" / "bin"
            ws_bin.mkdir(parents=True)
            (ws_bin / "cli-sim").write_text("#!/bin/sh\n", encoding="utf-8")
            (ws_bin / "cli-sim").chmod(0o755)
            tools = home / "tools"
            (tools / "bin").mkdir(parents=True)
            (tools / "alias").mkdir()
            (tools / "bin" / "cli-sim").write_text("#!/bin/sh\n", encoding="utf-8")
            (tools / "bin" / "cli-sim").chmod(0o755)
            (d / "ch").mkdir(mode=0o700)
            repo_root = Path(__file__).resolve().parents[2]
            child = [sys.executable, "-c", self._BIND_ALIAS_CHILD, str(repo_root), str(d), str(repo)]
            # The EXEMPT layout (issue #227 round 1): the CLI under `~/work/npm/bin` and the
            # checkout at `~/work/atmofab`, so the root `~/work` contains the checkout by its
            # own spelling and is exempt from the inode walks. A bind mount of the hidden
            # tree BELOW that root and OUTSIDE the checkout's path (`~/work/alias`) was
            # carried in by the recursive ro-bind with nothing on top — measured readable
            # before the mount-table check ran for exempt roots. A mount AT OR UNDER the
            # checkout's path is covered by the tmpfs and stays accepted.
            work = home / "work"
            exempt_repo = work / "atmofab"
            exempt_dialogs = exempt_repo / "workspace" / "orchestrations" / "o" / "agents" / "p" / "dialogs"
            exempt_dialogs.mkdir(parents=True)
            (exempt_dialogs / "leaf.stdout.jsonl").write_text("PRODUCER REASONING\n", encoding="utf-8")
            (work / "alias").mkdir()
            (work / "innocuous").mkdir()
            (exempt_repo / "alias2").mkdir()
            (work / "npm" / "bin").mkdir(parents=True)
            (work / "npm" / "bin" / "cli-sim").write_text("#!/bin/sh\n", encoding="utf-8")
            (work / "npm" / "bin" / "cli-sim").chmod(0o755)
            # Round 2's two layouts. A checkout that is ITSELF a bind of a tree under the
            # exempt root (`~/work/src/atmofab` bound at `~/work/atmofab`, the workflow
            # started from the bind): its physical source sits beside it under the root, and a
            # rule keyed on the mount point's path accepted it while the rendered profile
            # exposed the source. And a foreign mount BETWEEN the root and the checkout (a
            # data disk at `~/work/proj` holding `~/work/proj/atmofab`): the same rule refused
            # it, although the checkout appears only at its own path there.
            src_repo = work / "src" / "atmofab"
            (src_repo / "workspace").mkdir(parents=True)
            (src_repo / "tools").mkdir()
            disk = d / "disk"
            disk_dialogs = disk / "atmofab" / "workspace" / "orchestrations" / "o" / "agents" / "p" / "dialogs"
            disk_dialogs.mkdir(parents=True)
            (disk_dialogs / "leaf.stdout.jsonl").write_text("PRODUCER REASONING\n", encoding="utf-8")
            # Round 4: the legitimate half of the system-directory rule — a checkout that
            # physically lives under `/usr/local/src` (with `$HOME` there too) and is started
            # from that path is exempt by spelling for `/usr`, like any install root, and the
            # tmpfs covers it. Without this row a mutant that never exempts a system directory
            # stayed green while refusing that launch with a false remedy.
            sys_layout = d / "sys_layout"
            sys_home = sys_layout / "home" / "user"
            sys_dialogs = sys_home / "work" / "atmofab" / "workspace" / "orchestrations" / "o" / "agents" / "p" / "dialogs"
            sys_dialogs.mkdir(parents=True)
            (sys_dialogs / "leaf.stdout.jsonl").write_text("PRODUCER REASONING\n", encoding="utf-8")
            (sys_home / "work" / "npm" / "bin").mkdir(parents=True)
            (sys_home / "work" / "npm" / "bin" / "cli-sim").write_text("#!/bin/sh\n", encoding="utf-8")
            (sys_home / "work" / "npm" / "bin" / "cli-sim").chmod(0o755)
            sys_prefix = Path("/usr/local/src") / "home" / "user"
            sys_child = [sys.executable, "-c", self._BIND_ALIAS_CHILD, str(repo_root), str(d),
                         str(sys_prefix / "work" / "atmofab"), str(sys_prefix / "work" / "npm" / "bin"),
                         str(sys_prefix)]
            (work / "proj").mkdir()
            proj_child = [sys.executable, "-c", self._BIND_ALIAS_CHILD, str(repo_root), str(d),
                          str(work / "proj" / "atmofab")]
            # Round 3: the SYSTEM directories are recursive ro-binds too, and the refusal ran
            # over the install roots alone — a checkout kept under `/usr/local/src` and
            # bind-mounted into the working tree rode in through `/usr` with nothing on top
            # (the review measured the dialogs readable there). The outer bwrap puts a
            # scratch tree at `/usr/local/src` so the row needs no privilege on the host.
            usrsrc = d / "usrsrc"
            usrsrc_dialogs = usrsrc / "atmofab" / "workspace" / "orchestrations" / "o" / "agents" / "p" / "dialogs"
            usrsrc_dialogs.mkdir(parents=True)
            (usrsrc_dialogs / "leaf.stdout.jsonl").write_text("PRODUCER REASONING\n", encoding="utf-8")
            (work / "data").mkdir()
            usrsrc_empty = d / "usrsrc_empty"
            (usrsrc_empty / "atmofab").mkdir(parents=True)  # a mountpoint the outer bwrap can use
            exempt_child = [sys.executable, "-c", self._BIND_ALIAS_CHILD, str(repo_root), str(d),
                            str(exempt_repo)]
            cases = [
                ("root is the bind", child, [(repo / "workspace", tools)], tools / "bin", "REFUSED"),
                ("bind below the root", child, [(repo / "workspace", tools / "alias")],
                 tools / "bin", "REFUSED"),
                ("no mount (control)", child, [], tools / "bin", "ACCEPTED"),
                ("exempt root, bind below it outside the checkout", exempt_child,
                 [(exempt_repo / "workspace", work / "alias")], work / "npm" / "bin", "REFUSED"),
                # Stacked: an innocuous tree first, the hidden tree ON TOP at the same point.
                # `_fs_identity` used to read the bottom entry (round 2).
                ("exempt root, hidden tree stacked over an innocuous mount", exempt_child,
                 [(work / "innocuous", work / "alias"), (exempt_repo / "workspace", work / "alias")],
                 work / "npm" / "bin", "REFUSED"),
                ("exempt root, checkout is a bind of a tree under the root", exempt_child,
                 [(src_repo, exempt_repo)], work / "npm" / "bin", "REFUSED"),
                ("exempt root, bind of the root itself with the checkout spelled through it",
                 proj_child, [(work, work / "proj")], work / "npm" / "bin", "REFUSED"),
                ("exempt root, foreign mount between root and checkout (control)", proj_child,
                 [(disk, work / "proj")], work / "npm" / "bin", "ACCEPTED"),
                ("exempt root, checkout bound onto itself (control)", exempt_child,
                 [(exempt_repo, exempt_repo)], work / "npm" / "bin", "ACCEPTED"),
                ("exempt root, bind under the checkout's own path (control)", exempt_child,
                 [(exempt_repo / "workspace", exempt_repo / "alias2")], work / "npm" / "bin",
                 "ACCEPTED"),
                ("exempt root, no mount (control)", exempt_child, [], work / "npm" / "bin",
                 "ACCEPTED"),
                ("checkout physically under /usr/local/src, bound into the working tree",
                 exempt_child, [(usrsrc, Path("/usr/local/src")), (usrsrc / "atmofab", exempt_repo)],
                 work / "npm" / "bin", "REFUSED"),
                ("checkout mirrored under /usr/local/src", exempt_child,
                 [(usrsrc_empty, Path("/usr/local/src")), (exempt_repo, Path("/usr/local/src/atmofab"))],
                 work / "npm" / "bin", "REFUSED"),
                # A whole other filesystem under the exempt root (a scratch tmpfs, a data
                # disk): a different device, `root_within=/`, and the round-3 sweep found
                # that dropping the DEVICE comparison in `_overlaps` survived every row —
                # this is the row that sees it.
                ("exempt root, a separate filesystem mounted under it (control)", exempt_child,
                 [], work / "npm" / "bin", "ACCEPTED", [work / "data"]),
                ("checkout and HOME physically under /usr/local/src, started there (control)",
                 sys_child, [(sys_layout, Path("/usr/local/src"))], None, "ACCEPTED"),
            ]
            for label, argv, binds, path_dir, expected, *tmpfs in cases:
                with self.subTest(case=label):
                    outer = self._outer_bwrap(binds, tmpfs[0] if tmpfs else [])
                    # `sys_child` carries its own path_dir and HOME (both under the bind).
                    tail = [] if path_dir is None else [str(path_dir)]
                    res = subprocess.run([*outer, "--", *argv, *tail],
                                         capture_output=True, text=True, timeout=120,
                                         check=False)  # the exit code is asserted below
                    self.assertEqual(res.returncode, 0, res.stderr)
                    self.assertTrue(res.stdout.startswith(expected), res.stdout)
                    if expected == "REFUSED":
                        self.assertIn("carries a mount", res.stdout)
                        # The refusal names WHICH bind set carried the second name.
                        self.assertIn("system directory bound read-only" if "/usr/local/src" in label
                                      else "backend install root", res.stdout, res.stdout)
                    else:
                        # An accepted layout is only right if the rendered profile then
                        # hides the planted dialogs at every name the probe can reach: the
                        # checkout's own path and the mount under it. (`child`'s own layout
                        # plants nothing, so its control prints HIDDEN vacuously; every other
                        # accepted row has `PRODUCER REASONING` planted at the checkout.)
                        self.assertIn("DIALOG:HIDDEN", res.stdout, res.stdout)
                        self.assertIn("UNDER_REPO:HIDDEN", res.stdout, res.stdout)

    def test_codex_cli_starts_inside_its_own_profile(self) -> None:
        self._assert_backend_cli_starts_inside_its_profile("codex", "codex")

    def test_claude_cli_starts_inside_its_own_profile(self) -> None:
        self._assert_backend_cli_starts_inside_its_profile("claude", "claude")

    # ---- issue #227: the leaf reaches its first API request with no checkout in reach ----

    def _pure_launch_under_profile(self, backend: str, *,
                                   model: str = "") -> tuple[list[str], dict, Path]:
        """The production argv (`Conductor.leaf_command`) and profile of one leaf launch.

        Since issue #534 PR-J4-b the launch is a `workshop leaf`'s — the READING argv of
        `validate.judge` (served no endpoint, so the row needs no wrapper) under the profile a
        lineage the real preparer made implies (`_prepared_lineage`); the leaf's cwd is that
        lineage's tree, a non-git directory like the empty checkout path the pure leaf started
        in. The name is kept because `docs/BWRAP_ENABLEMENT.md` cites the codex row.

        Returns `(argv, profile, repo)`; the caller owns the tempdirs through `addCleanup`.
        """
        skip_probes.require_installed(self, backend)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve() / "repo"
        repo.mkdir()
        # A UUID, as production's child ids are: the claude argv passes it as `--session-id`,
        # which the CLI validates before anything else.
        orch, arid = f"orch_{backend}_lb", str(uuid.uuid4())
        _ensure_orchestration_audit_dirs(repo, orch)
        _iso, kwargs = _prepared_lineage(self, repo, orch, backend, lineage=arid)
        conductor = wc.Conductor(
            repo_root=repo, orchestration_id=orch, orchestration_agent_run_id="ORCH",
            llm_config=sample_config_with(backend, agent_model=model), env={})
        argv = conductor.leaf_command(conductor.entry_for("validate", "judge"),
                                      session_id=arid, reading=True)
        profile = build_readonly_bwrap_profile(
            repo_root=repo, orchestration_id=orch, agent_run_id=arid,
            backend_command=backend, backend_type=backend, **kwargs)
        return argv, profile, repo

    def test_codex_pure_launch_reaches_the_api_from_the_empty_cwd(self) -> None:
        """UNBILLED measurement of what issue #227 costs the codex launch, under real bwrap.

        The sandbox holds no checkout, and the leaf's cwd is its lineage's tree (a workshop
        lineage since issue #534 PR-J4-b; the pure leaf started at the empty checkout path):
        `codex exec` starts in a non-git cwd — which codex refuses before any API call unless
        `--skip-git-repo-check` is passed (measured on codex-cli 0.154.0: `Not inside a trusted
        directory and --skip-git-repo-check was not specified.`, rc 1). The production argv
        carries the flag; this row execs the real CLI with that argv under the rendered
        profile and observes the request arriving at a loopback stand-in: the CLI got past the
        git check and reached the network.

        The redirect is a `--config model_provider` override, NOT `OPENAI_BASE_URL`: measured,
        the env variable is not honoured by codex-cli 0.154.0 with a private `CODEX_HOME`
        (the request went to `wss://api.openai.com`), and the profile's env allowlist would
        refuse the name anyway. A custom provider with no `env_key` sends no credential, so
        the row needs no real `auth.json` (the lineage's is a seeded placeholder).

        CONTROL: the same launch with the flag removed must NOT reach the stand-in and must
        name the refusal — that is what makes the flag load-bearing rather than decorative,
        and it is the row a revert of `leaf_command`'s hunk fails.

        The request BODY is read too: with `--output-schema` gone (issue #230) it carries no
        `text.format` — the strict `json_schema` format codex-cli built from the host's
        `{"type": "object"}` is what the Responses API refused on every pure codex turn; the
        workshop argv carries none either. The body is the wire-level observation.
        """
        argv, profile, _repo = self._pure_launch_under_profile("codex", model="gpt-5.6-sol")
        server = _Loopback400()
        self.addCleanup(server.close)
        overrides = ["--config", 'model_provider="loopback"',
                     "--config", 'model_providers.loopback.name="loopback"',
                     "--config", f'model_providers.loopback.base_url="{server.base_url}/v1"',
                     "--config", 'model_providers.loopback.wire_api="responses"']
        self.assertEqual(argv[-2:], ["--json", "-"])
        with_flag = [*argv[:-2], *overrides, *argv[-2:]]
        self.assertIn("--skip-git-repo-check", with_flag)
        res = subprocess.run(render_bwrap_command(profile=profile, command_argv=with_flag),
                             input="Reply with one word.", capture_output=True, text=True,
                             timeout=180, check=False)  # rc 1 is the 400 turning into turn.failed
        self.assertIn('"type":"thread.started"', res.stdout, res.stdout + res.stderr)
        # One turn request. Two `GET /v1/models` precede it on codex-cli 0.164.0 under the
        # lineage home (observed; not isolated between the CLI version and the lineage's
        # placeholder `auth.json`). A model listing is not the turn, so only the POSTs are
        # compared here, and the control below compares every hit.
        self.assertEqual([hit for hit in server.hits if hit.startswith("POST ")],
                         ["POST /v1/responses"], res.stdout + res.stderr)
        hits_before_control = list(server.hits)
        self.assertNotIn("skip-git-repo-check", res.stderr)
        body = server.bodies[server.hits.index("POST /v1/responses")]
        request = json.loads(body)
        self.assertNotIn("format", request.get("text") or {}, request.get("text"))
        self.assertNotIn("json_schema", body.decode("utf-8", "replace"))
        self.assertNotIn("--output-schema", with_flag)
        without_flag = [tok for tok in with_flag if tok != "--skip-git-repo-check"]
        res = subprocess.run(render_bwrap_command(profile=profile, command_argv=without_flag),
                             input="Reply with one word.", capture_output=True, text=True,
                             timeout=180, check=False)  # the refusal exits 1 before any request
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--skip-git-repo-check was not specified", res.stderr, res.stdout)
        self.assertNotIn("thread.started", res.stdout)
        self.assertEqual(server.hits, hits_before_control, "the control must add no hit")

    # ---- issue #245: one codex lineage's rollout is unreachable from another lineage ----

    _LOOPBACK_OVERRIDES = ("--config", 'model_provider="loopback"',
                           "--config", 'model_providers.loopback.name="loopback"',
                           "--config", 'model_providers.loopback.wire_api="responses"')

    def _codex_lineage_fixture(self) -> tuple[Path, str, wc.Conductor, _Loopback400]:
        """A repo whose codex launches go through the REAL preparer, plus a loopback server.

        Each lineage is a `workshop leaf` lineage (issue #534 PR-J4-b retired the pure codex
        thread lineage this fixture first served): its `CODEX_HOME` is the lineage home's
        `config/`, beside its `tree/`. The homes root is the suite's redirected
        `ATMOFAB_WORKFLOW_HOMES_ROOT` and the
        operator credential is this test's own (`seed_codex_auth`), so the container these
        rows walk — holding SEVERAL lineages of one orchestration — is the one production
        would build; a row that prepares one lineage (`_prepared_lineage`) cannot tell a
        shared home from a per-lineage one.
        """
        skip_probes.require_installed(self, "codex")
        from tools.tests.private_root_fixture import seed_codex_auth
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve() / "repo"
        repo.mkdir()
        orch = "orch_codex_lineage"
        _ensure_orchestration_audit_dirs(repo, orch)
        (repo / "workspace" / "orchestrations" / orch / "orchestration_meta.json").write_text(
            "{}", encoding="utf-8")
        credential = seed_codex_auth(Path(tmp.name).resolve() / "operator-codex")
        env_patch = mock.patch.dict(os.environ, {"CODEX_HOME": str(credential)}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        conductor = wc.Conductor(
            repo_root=repo, orchestration_id=orch, orchestration_agent_run_id="ORCH",
            llm_config=sample_config_with("codex", agent_model="gpt-5.6-sol"), env={})
        server = _Loopback400()
        self.addCleanup(server.close)
        return repo, orch, conductor, server

    def _run_codex_in_lineage(self, repo: Path, orch: str, conductor: wc.Conductor,
                              server: _Loopback400, *, arid: str, isolation: dict,
                              prompt: str, resume_session_id: str | None = None,
                              ) -> subprocess.CompletedProcess:
        """Exec the real CLI with the production argv — the READING argv of `validate.judge`,
        served no endpoint — under the profile a prepared lineage home implies
        (`codex_workshop_profile_kwargs`, the same spelling `record_launch` uses)."""
        argv = conductor.leaf_command(conductor.entry_for("validate", "judge"), session_id=arid,
                                      resume_session_id=resume_session_id, reading=True)
        self.assertEqual(argv[-2:], ["--json", "-"])
        overrides = [*self._LOOPBACK_OVERRIDES,
                     "--config", f'model_providers.loopback.base_url="{server.base_url}/v1"']
        profile = build_readonly_bwrap_profile(
            repo_root=repo, orchestration_id=orch, agent_run_id=arid,
            backend_command="codex", backend_type="codex",
            **ort.codex_workshop_profile_kwargs(isolation))
        command = render_bwrap_command(
            profile=profile, command_argv=[*argv[:-2], *overrides, *argv[-2:]])
        # The API answers 400, so the turn fails and the CLI exits 1 — the rollout is
        # written at thread start, before the request (measured on codex-cli 0.154.0).
        return subprocess.run(command, input=prompt, capture_output=True, text=True,
                              timeout=180, check=False)

    def test_a_second_codex_leaf_cannot_read_the_first_leafs_rollout_anywhere_under_its_home(
            self) -> None:
        """UNBILLED, under real bwrap with the real CLI: the issue #227 finding, closed.

        Leaf A launches through the REAL preparer with a marker in its prompt. Then, on the
        HOST, the whole container `<homes-root>/<oid>/codex/` is walked: every file the CLI
        wrote, and every file holding the marker, lies under A's own lineage home. Enumerated
        by walking, not by name, so a CLI version that adds a fourth rollout location fails
        this row instead of slipping past it — the marker was measured in THREE files
        (`sessions/…/rollout-*.jsonl`, `state_*.sqlite-wal`, `thread_history_*.sqlite-wal`),
        which is why a fix that moved `sessions/` alone would stay red here.

        Then leaf B, a different lineage of the SAME orchestration, runs a probe under the
        profile production renders for it: a recursive search of its own `$CODEX_HOME` (its
        lineage home's `config/`) for
        the marker finds nothing, the container is not listable, and A's rollout file — at
        its exact host path — is ENOENT. The sibling is not hidden; it is not mounted.
        """
        from tools.orchestration_runtime import _prepare_codex_workflow_home
        repo, orch, conductor, server = self._codex_lineage_fixture()
        arid_a, arid_b = str(uuid.uuid4()), str(uuid.uuid4())
        marker = "ATMOFAB_LINEAGE_MARKER_" + uuid.uuid4().hex
        iso_a = _prepare_codex_workflow_home(repo, orch, arid_a, resume=False)
        home_a = Path(iso_a["home"])
        container = home_a.parent
        self.assertEqual(home_a, container / arid_a)
        before = {p for p in container.rglob("*")}
        res = self._run_codex_in_lineage(repo, orch, conductor, server, arid=arid_a,
                                         isolation=iso_a, prompt=f"Reply with {marker}.")
        self.assertIn('"type":"thread.started"', res.stdout, res.stdout + res.stderr)
        self.assertIn("POST /v1/responses", server.hits, res.stdout + res.stderr)
        written = sorted(p for p in container.rglob("*") if p not in before)
        holders = sorted(p for p in written
                         if p.is_file() and marker.encode() in p.read_bytes())
        self.assertTrue(written, "the CLI wrote nothing under the container")
        self.assertGreaterEqual(len(holders), 3, holders)
        for path in written:
            self.assertTrue(path.is_relative_to(home_a), f"written outside A's lineage: {path}")
        # The container holds lineage directories and nothing else.
        self.assertEqual(sorted(p.name for p in container.iterdir()), [arid_a])

        iso_b = _prepare_codex_workflow_home(repo, orch, arid_b, resume=False)
        home_b = Path(iso_b["home"])
        self.assertEqual(home_b, container / arid_b)
        rollout_a = next(p for p in holders if p.suffix == ".jsonl")
        probe = textwrap.dedent(f"""
            import os
            from pathlib import Path
            home = Path(os.environ["CODEX_HOME"])
            print("HOME_IS_OWN:" + str(home == Path({iso_b["config"]!r})), flush=True)
            hits = [str(p) for p in home.rglob("*")
                    if p.is_file() and {marker!r}.encode() in p.read_bytes()]
            print("MARKER_HITS:" + str(len(hits)), flush=True)
            try:
                names = sorted(p.name for p in Path({str(container)!r}).iterdir())
                print("CONTAINER:LISTED:" + ",".join(names), flush=True)
            except OSError as e:
                print("CONTAINER:" + type(e).__name__, flush=True)
            try:
                Path({str(rollout_a)!r}).read_bytes()
                print("ROLLOUT_A:READABLE", flush=True)
            except OSError as e:
                print("ROLLOUT_A:" + type(e).__name__, flush=True)
            try:
                Path({str(home_a)!r}).iterdir().__next__()
                print("HOME_A:LISTED", flush=True)
            except StopIteration:
                print("HOME_A:EMPTY", flush=True)
            except OSError as e:
                print("HOME_A:" + type(e).__name__, flush=True)
        """)
        profile_b = build_readonly_bwrap_profile(
            repo_root=repo, orchestration_id=orch, agent_run_id=arid_b,
            backend_command="codex", backend_type="codex",
            **ort.codex_workshop_profile_kwargs(iso_b))
        out = _bwrap_stdout(render_bwrap_command(profile=profile_b,
                                                 command_argv=["python3", "-c", probe]))
        self.assertIn("HOME_IS_OWN:True", out, out)
        self.assertIn("MARKER_HITS:0", out, out)
        # bwrap creates the mountpoint's parents on its tmpfs, so the container path
        # exists inside the sandbox as scaffolding: what it must NOT do is list A.
        self.assertNotIn(arid_a, out.split("CONTAINER:")[1].splitlines()[0], out)
        self.assertIn("ROLLOUT_A:FileNotFoundError", out, out)
        self.assertNotIn("HOME_A:LISTED", out, out)
        # CONTROL for the probe itself: the same probe under A's OWN profile sees the marker,
        # so a probe that could not read anything would not pass the row above.
        profile_a = build_readonly_bwrap_profile(
            repo_root=repo, orchestration_id=orch, agent_run_id=arid_a,
            backend_command="codex", backend_type="codex",
            **ort.codex_workshop_profile_kwargs(iso_a))
        out = _bwrap_stdout(render_bwrap_command(
            profile=profile_a, command_argv=["python3", "-c", probe.replace(
                "MARKER_HITS:", "OWN_MARKER_HITS:")]))
        self.assertIn(f"OWN_MARKER_HITS:{len(holders)}", out, out)

    def test_a_warm_exec_resume_reaches_the_api_from_its_own_lineage_home(self) -> None:
        """UNBILLED: `codex exec resume <thread>` works from the lineage home and only there.

        Measured before this row was written (codex-cli 0.154.0): a resume from the home
        holding the thread reaches `POST /v1/responses`; from a home that does not, the CLI
        fails BEFORE any request with `no rollout found for thread id` (rc 1). That second
        half is why a missing lineage home must turn the turn cold at `record_launch`
        (`workshop_lineage_home_missing`) rather than be created: a created-empty home would
        move the same failure past the point where the conductor can still rebuild the
        turn — and it is why the second attempt of one thread is prepared `resume=True`
        against the FIRST attempt's lineage, which is what this row drives.
        """
        from tools.orchestration_runtime import _prepare_codex_workflow_home
        repo, orch, conductor, server = self._codex_lineage_fixture()
        arid_a, arid_a2, arid_b = (str(uuid.uuid4()) for _ in range(3))
        iso_a = _prepare_codex_workflow_home(repo, orch, arid_a, resume=False)
        res = self._run_codex_in_lineage(repo, orch, conductor, server, arid=arid_a,
                                         isolation=iso_a, prompt="Reply with one word.")
        self.assertIn('"type":"thread.started"', res.stdout, res.stdout + res.stderr)
        thread = json.loads(res.stdout.splitlines()[0])["thread_id"]
        posts_before = server.hits.count("POST /v1/responses")
        # Attempt 2 of the same thread: a NEW arid, prepared against A's lineage.
        iso_a2 = _prepare_codex_workflow_home(repo, orch, arid_a, resume=True)
        self.assertEqual(iso_a2["home"], iso_a["home"])
        res = self._run_codex_in_lineage(repo, orch, conductor, server, arid=arid_a2,
                                         isolation=iso_a2, prompt="Continue.",
                                         resume_session_id=thread)
        self.assertIn(f'"thread_id":"{thread}"', res.stdout, res.stdout + res.stderr)
        self.assertEqual(server.hits.count("POST /v1/responses"), posts_before + 1,
                         res.stdout + res.stderr)
        # CONTROL: the same resume from a DIFFERENT lineage's home never reaches the API.
        iso_b = _prepare_codex_workflow_home(repo, orch, arid_b, resume=False)
        res = self._run_codex_in_lineage(repo, orch, conductor, server, arid=arid_b,
                                         isolation=iso_b, prompt="Continue.",
                                         resume_session_id=thread)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no rollout found for thread id", res.stderr, res.stdout + res.stderr)
        self.assertNotIn("thread.started", res.stdout)
        self.assertEqual(server.hits.count("POST /v1/responses"), posts_before + 1,
                         "the control must add no request")
        # And a warm preparation for a lineage nobody started answers the sentinel.
        self.assertIsNone(_prepare_codex_workflow_home(repo, orch, str(uuid.uuid4()),
                                                       resume=True))

    def test_claude_pure_launch_reaches_the_api_from_the_empty_cwd(self) -> None:
        """UNBILLED: the claude leaf takes the same profile shape as codex (one profile for
        both backends — the decision recorded on issue #227; a workshop lineage's since issue
        #534 PR-J4-b), so it too starts with no checkout in reach. This row execs the real CLI
        with the production reading argv under the rendered profile and observes its first
        request at a loopback stand-in. `ANTHROPIC_BASE_URL` is set by an `env` prefix INSIDE
        the sandbox because the profile's env allowlist refuses the name (it is the redirect
        the allowlist exists to close); set there, it is honoured.
        """
        argv, profile, _repo = self._pure_launch_under_profile("claude")
        server = _Loopback400()
        self.addCleanup(server.close)
        command = ["env", f"ANTHROPIC_BASE_URL={server.base_url}",
                   "ANTHROPIC_API_KEY=atmofab-loopback", *argv]
        res = subprocess.run(render_bwrap_command(profile=profile, command_argv=command),
                             input="Reply with one word.", capture_output=True, text=True,
                             timeout=240, check=False)  # rc 1 is the 400 surfacing as api_error
        self.assertTrue(server.hits, res.stdout + res.stderr)
        self.assertTrue(all(hit.startswith("POST /v1/messages") for hit in server.hits),
                        server.hits)
        self.assertIn('"terminal_reason":"api_error"', res.stdout, res.stdout + res.stderr)



@skip_probes.skip_unless_installed("claude")
class ClaudeSettingsIsolationTests(unittest.TestCase):
    """UNBILLED (issue #453): the operator's settings FILE does not reach a claude leaf.

    The leaf's `HOME` is the operator's, so `$HOME/.claude/settings.json` is in reach of the
    CLI; `--restricted` (first in `workshop_leaf_flags()`, the flag set of every claude leaf
    since issue #534 PR-J4-b — the pure leaf's `pure_leaf_flags()` carried it first) is what
    makes the CLI ignore it. Each row writes one settings channel into a fresh `HOME`, execs
    the real CLI with the production flag set — the READING kind's, which needs no wrapper
    endpoint; the producing kind's differs only by the endpoint and its grant — against a
    loopback stand-in, and reads the model (or the endpoint) off the request it stored. No
    bwrap: the question is about a settings file, not a mount.

    The CONTROL row removes `--restricted` and requires the marker to be PRESENT, so a dead
    capture — or a CLI that stopped reading settings for some other reason — cannot pass the
    marker-absent rows for the wrong reason.
    """

    def _request(self, settings: dict, extra: list[str], *, restricted: bool = True) -> list:
        from tools.pure_leaf import workshop_leaf_flags
        flags = workshop_leaf_flags(None, reading=True)
        self.assertEqual(flags[0], "--restricted")
        if not restricted:
            flags.remove("--restricted")
        server = _Loopback400()
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            (Path(home) / ".claude").mkdir()
            (Path(home) / ".claude" / "settings.json").write_text(json.dumps(settings))
            env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "ANTHROPIC_BASE_URL": server.base_url,
                   "ANTHROPIC_API_KEY": "atmofab-loopback"}
            res = subprocess.run(["claude", *flags, *extra, "-p"], input="Say hi.",
                                 capture_output=True, text=True, env=env, cwd=cwd,
                                 timeout=240, check=False)
        self.assertTrue(server.bodies, res.stdout + res.stderr)
        return [json.loads(body).get("model") for body in server.bodies]

    _MODEL_CHANNELS = (
        ("model key", {"model": "claude-marker-key"}, [], "claude-marker-key"),
        ("env model", {"env": {"ANTHROPIC_MODEL": "claude-marker-env"}}, [],
         "claude-marker-env"),
        ("env alias remap",
         {"env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-marker-remap"}},
         ["--model", "opus"], "claude-marker-remap"),
    )

    def test_no_settings_channel_reaches_a_restricted_leaf(self) -> None:
        for label, settings, extra, marker in self._MODEL_CHANNELS:
            with self.subTest(channel=label):
                self.assertNotIn(marker, self._request(settings, extra))

    def test_a_settings_endpoint_does_not_redirect_a_restricted_leaf(self) -> None:
        """The `env` block's `ANTHROPIC_BASE_URL` names a closed port; the request still
        arrives at the process-environment loopback (`_request` asserts it arrived). Without
        the flag the CLI goes to the settings URL and retries until its own timeout
        (measured, issue #453), which is why that half is not a row here."""
        self._request({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1/"}}, [])

    def test_control_without_restricted_the_marker_reaches_the_request(self) -> None:
        """One control PER CHANNEL, so a CLI that stops honouring one of them (and would
        leave its marker-absent row green for the wrong reason) turns this row red."""
        for label, settings, extra, marker in self._MODEL_CHANNELS:
            with self.subTest(channel=label):
                self.assertIn(marker, self._request(settings, extra, restricted=False))


class WorkshopWrapperSandboxTests(unittest.TestCase):
    """The wrapper's runs of a `workshop leaf` under a REAL bwrap (issue #534 PR-1;
    `docs/design/producer_workshop_leaf.md` §Sandbox, row 2).

    A two-file bundle that `use`s a staged dependency module is derived, compiled, linted,
    syntax-checked and run through `workshop_wrapper.run_wrapper_op`, each step under
    `render_wrapper_sandbox_prefix`. The built program — leaf-authored code — then probes what
    it can reach: the staged dependency SOURCE, the checkout's `tools/`, the lineage's config
    home, and a TCP listener on the host's loopback. Each must be out of reach."""

    _DEP = textwrap.dedent("""\
        module dep_model
          implicit none
        contains
          pure integer function dep_twice(n)
            integer, intent(in) :: n
            dep_twice = 2 * n
          end function dep_twice
        end module dep_model
        """)
    _MODEL = textwrap.dedent("""\
        module x_model
          use dep_model, only: dep_twice
          implicit none
        contains
          integer function x_value()
            x_value = dep_twice(21)
          end function x_value
        end module x_model
        """)
    _RUNNER = textwrap.dedent("""\
        program x_runner
          use x_model, only: x_value
          implicit none
          print '(a,i0)', 'VALUE:', x_value()
          call execute_command_line('python3 probe.py')
        end program x_runner
        """)
    _CONTROL = textwrap.dedent("""\
        OBJDIR ?= obj
        BINDIR ?= bin
        BIN ?= x_runner
        FC = gfortran
        all: $(BINDIR)/$(BIN)
        $(OBJDIR)/dep_model.o: $(OBJDIR)/dep_model.f90
        \t$(FC) -J$(OBJDIR) -c $(OBJDIR)/dep_model.f90 -o $@
        $(OBJDIR)/x_model.o: x_model.f90 $(OBJDIR)/dep_model.o
        \t$(FC) -J$(OBJDIR) -I$(OBJDIR) -c x_model.f90 -o $@
        $(OBJDIR)/x_runner.o: x_runner.f90 $(OBJDIR)/x_model.o
        \t$(FC) -J$(OBJDIR) -I$(OBJDIR) -c x_runner.f90 -o $@
        $(BINDIR)/$(BIN): $(OBJDIR)/dep_model.o $(OBJDIR)/x_model.o $(OBJDIR)/x_runner.o
        \tmkdir -p $(BINDIR)
        \t$(FC) $^ -o $@
        """)

    @classmethod
    def setUpClass(cls) -> None:
        if not _bwrap_usable():
            raise AssertionError(
                "bwrap is absent or unprivileged user namespaces are disabled; see "
                "docs/BWRAP_ENABLEMENT.md (CI lifts kernel.apparmor_restrict_unprivileged_userns)")
        for tool in ("gfortran", "make", "fortitude"):
            if shutil.which(tool) is None:
                raise AssertionError(
                    f"{tool} is not installed (docs/DEVELOPMENT.md §Fresh-machine setup)")

    def setUp(self) -> None:
        import time

        from tools import workshop_tree as wt
        from tools import workshop_wrapper as ww
        from tools.build_runtime import lint_preset_executables
        from tools.orchestration_runtime import wrapper_toolchain_ro_roots

        self.wt, self.ww = wt, ww
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo_root = Path(wc.__file__).resolve().parents[1]
        self.paths = wt.LineagePaths(base / "homes", "orch_sim", "arid-sim", base / "record")
        self.paths.home.mkdir(parents=True, mode=0o700)
        self.paths.config.mkdir()
        (self.paths.config / ".credentials.json").write_text("{}")
        self.paths.make_work_dirs()
        seeds = wt.seed_tree(
            self.paths, {"spec.ir.yaml": "case:\n  test_case_set:\n  - case_id: c1\n"})
        reserved = wt.reserved_tree_names(seeds, "Makefile")
        wt.write_lineage_record(self.paths, node_key="component/x@0.1.0",
                              started_at_utc="2026-10-23T00:00:00Z")
        (self.paths.staged_closure / "dep_model.f90").write_text(self._DEP)
        for name, text in (("x_model.f90", self._MODEL), ("x_runner.f90", self._RUNNER)):
            (self.paths.tree / name).write_text(text)
        (self.paths.tree / "bundle.yaml").write_text(
            "files:\n  - logical_path: x_model.f90\n    role: model\n"
            "  - logical_path: x_runner.f90\n    role: runner\n")
        self.ctx = ww.WrapperContext(
            paths=self.paths, repo_root=self.repo_root, orchestration_id="orch_sim",
            agent_run_id="arid-sim", reserved_names=reserved, control_file_basename="Makefile",
            violations=lambda doc: None, build_graph=lambda doc: {},
            render_control_file=lambda *, graph: self._CONTROL,
            closure_file_names=("dep_model.f90",), seed_names=seeds, run_raw_dirs=("raw",),
            language="fortran", build_system="make",
            build_overrides=(f"OBJDIR={self.paths.staged_obj}", f"BINDIR={self.paths.staged_bin}",
                             "BIN=x_runner"),
            lint_preset="fortitude", syntax_compiler="gfortran", syntax_std="f2008",
            syntax_architecture=None, syntax_parallel_backend=None,
            syntax_staged_suffixes=(".f90",), model_basename="x_model.f90",
            interface_header_basename=None, launch_argv_prefix=(), launch_env=(),
            run_unavailable=None, base_env=tuple(sorted(ww.wrapper_base_env().items())),
            toolchain_ro_roots=tuple(wrapper_toolchain_ro_roots(
                ["make", "gfortran", "python3", *lint_preset_executables("fortitude")])),
            max_wrapper_calls=20, lineage_deadline=time.time() + 900)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def manifest(self, *files: str) -> None:
        roles = {"x_model.f90": "model", "x_runner.f90": "runner"}
        (self.paths.tree / "bundle.yaml").write_text("files:\n" + "".join(
            f"  - logical_path: {f}\n    role: {roles.get(f, 'helper')}\n" for f in files))

    def test_a_compile_never_answers_from_an_earlier_compiles_objects(self) -> None:
        """A module the bundle no longer has must not compile from the module file an earlier
        compile left: the host's Build starts from an empty object directory, and so must the
        wrapper's (issue #534 round 2, measured on a real node by the blank-slate reviewer)."""
        consts = textwrap.dedent("""\
            module x_consts
              implicit none
              integer, parameter :: x_two = 2
            end module x_consts
            """)
        model = self._MODEL.replace("  use dep_model, only: dep_twice\n",
                                    "  use dep_model, only: dep_twice\n  use x_consts, only: x_two\n")
        self.assertNotEqual(model, self._MODEL)
        (self.paths.tree / "x_consts.f90").write_text(consts)
        (self.paths.tree / "x_model.f90").write_text(model)
        control = self._CONTROL.replace(
            "$(OBJDIR)/x_model.o: x_model.f90 $(OBJDIR)/dep_model.o\n",
            "$(OBJDIR)/x_consts.o: x_consts.f90\n\t$(FC) -J$(OBJDIR) -c x_consts.f90 -o $@\n"
            "$(OBJDIR)/x_model.o: x_model.f90 $(OBJDIR)/dep_model.o $(OBJDIR)/x_consts.o\n"
        ).replace("$(OBJDIR)/x_model.o $(OBJDIR)/x_runner.o\n",
                  "$(OBJDIR)/x_consts.o $(OBJDIR)/x_model.o $(OBJDIR)/x_runner.o\n")
        self.assertNotEqual(control, self._CONTROL)
        import dataclasses
        with_consts = dataclasses.replace(self.ctx, render_control_file=lambda *, graph: control)
        self.manifest("x_model.f90", "x_runner.f90", "x_consts.f90")
        self.assertTrue(self.ww.run_wrapper_op(with_consts, "derive")["ok"])
        built = self.ww.run_wrapper_op(with_consts, "compile")
        self.assertTrue(built["ok"], built)
        # The leaf drops the module from the tree and the manifest and keeps the `use`.
        (self.paths.tree / "x_consts.f90").unlink()
        self.manifest("x_model.f90", "x_runner.f90")
        self.assertTrue(self.op("derive")["ok"])
        rebuilt = self.op("compile")
        self.assertFalse(rebuilt["ok"], rebuilt)
        self.assertEqual(rebuilt["binaries"], [])

    def op(self, name: str, args: dict | None = None) -> dict:
        return self.ww.run_wrapper_op(self.ctx, name, args)

    def test_the_http_host_loop_runs_the_real_operations(self) -> None:
        """Issue #534 PR-J3-b, MH2's bwrap row: an HTTP workshop leaf's host loop
        (`llm_workshop_loop.run_workshop_loop`) answers a scripted Messages-API reply's
        `derive` and `compile` calls through `run_wrapper_op` on this REAL context — so the
        compile is the row-2 sandboxed one — and its `read_file` on the tree; every result
        goes back in one message."""
        import time as _time
        from types import SimpleNamespace

        from tools.llm_workshop_loop import LoopHistory, file_tool, run_workshop_loop
        from tools.pure_leaf import HTTP_FILE_TOOL_DECLARATIONS, HTTP_FILE_TOOLS
        from tools.tests.http_stand_ins import tool_use
        from tools.workshop_mcp import TOOL_DEFINITIONS

        api = ScriptedMessagesApi([
            ([tool_use("t1", "derive", {}), tool_use("t2", "compile", {}),
              tool_use("t3", "read_file", {"path": "x_model.f90", "max_lines": 2})],
             "tool_use"),
            ([{"type": "text", "text": "done"}], "end_turn")])
        self.addCleanup(api.close)
        tools = [*HTTP_FILE_TOOL_DECLARATIONS,
                 *({"name": op, "description": TOOL_DEFINITIONS[op]["description"],
                    "input_schema": TOOL_DEFINITIONS[op]["inputSchema"]}
                   for op in self.ww.WRAPPER_OPS)]

        def call_tool(name: str, args: dict) -> dict:
            if name in HTTP_FILE_TOOLS:
                return file_tool(self.paths.tree, self.ctx.reserved_names, name, args)
            return self.ww.run_wrapper_op(self.ctx, name, args)

        entry = SimpleNamespace(provider="anthropic_api", model="m", base_url=api.base_url,
                                api_key_env="K", timeout_s=None, max_output_tokens=None,
                                stream=True, effort="")
        result = run_workshop_loop(
            entry, system="S", history=LoopHistory(), prompt="P", tools=tools,
            call_tool=call_tool, deadline=_time.monotonic() + 600, deadline_name="d",
            env={"K": "placeholder"})
        self.assertEqual(result.ended, "end_turn", result)
        derive, compiled, read = [json.loads(r["content"]) for r in api.tool_results()]
        self.assertTrue(derive["ok"], derive)
        self.assertTrue(compiled["ok"], compiled)
        self.assertEqual(compiled["binaries"], ["x_runner"])
        self.assertEqual(read["text"], "module x_model\n  use dep_model, only: dep_twice")
        self.assertEqual(self.wt.read_lineage_record(self.paths)["wrapper_calls_used"], 2)

    def test_the_operations_build_and_run_a_bundle_that_reaches_nothing_else(self) -> None:
        import socket

        derived = self.op("derive")
        self.assertTrue(derived["ok"], derived)
        built = self.op("compile")
        self.assertTrue(built["ok"], built)
        self.assertEqual(built["binaries"], ["x_runner"])
        # The objects and module files land beside the staged source, outside the tree.
        self.assertTrue((self.paths.staged_obj / "dep_model.mod").is_file())
        self.assertFalse(any(p.suffix == ".o" for p in self.paths.tree.iterdir()))

        syntax = self.op("syntax_check")
        self.assertTrue(syntax["ok"], syntax)
        lint = self.op("lint")
        # A verdict, not a refusal to start: the linter scanned the tree's sources.
        self.assertIsNotNone(lint.get("rc"), lint)
        self.assertIn("files scanned", lint["stdout"], lint)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.addCleanup(listener.close)
        # Control: the listener answers on the host.
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
        listener.accept()[0].close()
        probe = textwrap.dedent(f"""\
            import os, socket
            def reach(tag, path):
                print(tag + (":REACHABLE" if os.path.exists(path) else ":ABSENT"), flush=True)
            reach("DEP_SOURCE", {str(self.paths.staged_obj / "dep_model.f90")!r})
            reach("STAGED_OBJ", {str(self.paths.staged_obj)!r})
            reach("REPO_TOOLS", {str(self.repo_root / "tools" / "workflow_conductor.py")!r})
            reach("CONFIG_HOME", {str(self.paths.config / ".credentials.json")!r})
            reach("RECORD", {str(self.paths.record_json)!r})
            try:
                socket.create_connection(("127.0.0.1", {port}), timeout=3).close()
                print("HOST_TCP:CONNECTED", flush=True)
            except OSError as e:
                print("HOST_TCP:REFUSED", type(e).__name__, flush=True)
            open("captured.txt", "w").write("written by the run")
            """)
        (self.paths.tree / "probe.py").write_text(probe)
        ran = self.op("run", {"binary": "x_runner"})
        self.assertTrue(ran["ok"], ran)
        out = ran["stdout"]
        self.assertIn("VALUE:42", out)
        for tag in ("DEP_SOURCE", "STAGED_OBJ", "REPO_TOOLS", "CONFIG_HOME", "RECORD"):
            self.assertIn(f"{tag}:ABSENT", out, out)
        self.assertIn("HOST_TCP:REFUSED", out, out)
        # The run's cwd is the tree, read-write.
        self.assertEqual((self.paths.tree / "captured.txt").read_text(), "written by the run")
        # Every build-runtime call is in the lineage's command log, under its sandbox.
        entries = [json.loads(line)
                   for line in self.paths.command_log.read_text().splitlines()]
        sandboxed = [e for e in entries if e["tool_name"] != "derive"]
        self.assertTrue(sandboxed)
        for entry in sandboxed:
            self.assertEqual(entry["sandbox_argv_prefix"][:4],
                             ["bwrap", "--die-with-parent", "--new-session", "--unshare-net"])
            self.assertEqual(entry["agent_run_id"], "arid-sim")
        self.assertEqual(self.wt.read_lineage_record(self.paths)["wrapper_calls_used"], 5)

    def test_syntax_check_runs_the_host_gates_model_alone_probe(self) -> None:
        """A model that `use`s another file of its own bundle passes over the whole tree and
        breaks every consumer, which is staged the model alone (issue #389): the host's
        Generate.gate refuses it, so the wrapper's feedback must too (issue #534 round 1)."""
        (self.paths.tree / "x_helper.f90").write_text(textwrap.dedent("""\
            module x_helper
              implicit none
            contains
              integer function x_one()
                x_one = 1
              end function x_one
            end module x_helper
            """))
        (self.paths.tree / "x_model.f90").write_text(textwrap.dedent("""\
            module x_model
              use dep_model, only: dep_twice
              use x_helper, only: x_one
              implicit none
            contains
              integer function x_value()
                x_value = dep_twice(21) * x_one()
              end function x_value
            end module x_model
            """))
        self.manifest("x_model.f90", "x_runner.f90", "x_helper.f90")
        out = self.op("syntax_check")
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["rc"], 0, "the whole tree is expected to pass")
        self.assertFalse(out["model_alone"]["ok"])
        self.assertIn("x_model.f90 alone did not", out["model_alone"]["findings"])
        # The helper moved into the model file: both pass.
        (self.paths.tree / "x_helper.f90").unlink()
        (self.paths.tree / "x_model.f90").write_text(self._MODEL)
        self.manifest("x_model.f90", "x_runner.f90")
        out = self.op("syntax_check")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["model_alone"]["ok"], out)

    def test_a_tool_installed_in_a_checkout_virtualenv_starts_inside_the_run(self) -> None:
        """`docs/DEVELOPMENT.md` suggests a virtualenv, and one kept in the checkout sat under
        the run's checkout tmpfs: the linter was `No such file` inside the run and found on
        the host (issue #534 round 1)."""
        from tools.build_runtime import tool_run_program
        from tools.orchestration_runtime import (
            render_wrapper_sandbox_prefix,
            wrapper_toolchain_ro_roots,
        )

        repo = Path(self._tmp.name) / "checkout"
        exe = repo / ".venv" / "bin" / "linttool"
        exe.parent.mkdir(parents=True)
        exe.write_text("#!/bin/sh\necho LINTTOOL_RAN\n")
        exe.chmod(0o755)
        (repo / "tools").mkdir()
        (repo / "tools" / "gate.py").write_text("x")
        env = dict(self.ctx.base_env) | {"PATH": f"{exe.parent}:/usr/bin:/bin"}
        with mock.patch.dict(os.environ, {"PATH": env["PATH"]}):
            roots = wrapper_toolchain_ro_roots(["linttool"], repo)
        prefix = render_wrapper_sandbox_prefix(
            repo_root=repo, project_dir=self.paths.tree, rw_binds=[self.paths.tree],
            toolchain_ro_roots=roots, env=env)
        result = tool_run_program({
            "project_dir": str(self.paths.tree),
            "command": ["sh", "-c", f"linttool; ls {repo / 'tools'} || echo TOOLS_HIDDEN"],
            "env": env, "timeout_sec": 30, "sandbox_argv_prefix": prefix,
        })
        self.assertIn("LINTTOOL_RAN", result["stdout"], result)
        self.assertIn("TOOLS_HIDDEN", result["stdout"], result)

    def test_a_timeout_stops_every_process_of_the_run(self) -> None:
        """A background descendant of a wrapper run dies with it. Under `--new-session` alone a
        kill of the launcher's process group reached the launcher only: the descendant kept the
        output pipe open (the call returned when it exited, not at the timeout) and went on
        writing into the tree after the run was over (issue #534 round 1)."""
        import time

        from tools.build_runtime import tool_run_program
        from tools.orchestration_runtime import render_wrapper_sandbox_prefix

        marker = self.paths.tree / "survivor.txt"
        late = (f"import time; time.sleep(4); open({str(marker)!r}, 'w').write('x')")
        hang = "import time; time.sleep(60)"
        env = dict(self.ctx.base_env)
        prefix = render_wrapper_sandbox_prefix(
            repo_root=self.repo_root, project_dir=self.paths.tree, rw_binds=[self.paths.tree],
            toolchain_ro_roots=self.ctx.toolchain_ro_roots, env=env)
        started = time.monotonic()
        result = tool_run_program({
            "project_dir": str(self.paths.tree),
            "command": ["sh", "-c", f'python3 -c "{late}" & python3 -c "{hang}"'],
            "env": env, "timeout_sec": 2, "sandbox_argv_prefix": prefix,
        })
        elapsed = time.monotonic() - started
        self.assertIsNone(result["return_code"], result)
        self.assertLess(elapsed, 10, f"the call outlived its timeout by {elapsed - 2:.1f} s")
        time.sleep(5)
        self.assertFalse(marker.exists(), "a descendant of the timed-out run kept running")



# The Messages-API stand-in the real claude CLI talks to here moved to `http_stand_ins`
# (issue #534 PR-J3-b), where the HTTP workshop leaf's host loop tests use it too.
_ScriptedApi = ScriptedMessagesApi


@skip_probes.skip_unless_installed("claude")
class WorkshopLeafCliTests(unittest.TestCase):
    """UNBILLED (issue #534 PR-2): the real claude CLI under a workshop leaf's launch shape —
    the production argv (`Conductor.leaf_command` with the wrapper's `--mcp-config`) under the
    rendered workshop profile (`_prepare_claude_workshop_home` + `claude_workshop_profile_
    kwargs`, a real bwrap), against a scripted API stand-in and the real wrapper endpoint
    (`workshop_mcp.WrapperMcpServer`, its operation stubbed). The measurements M1-M5 of the
    issue's plan, kept as rows so a CLI version that moves one turns a row red.

    `ANTHROPIC_BASE_URL` and a placeholder `ANTHROPIC_API_KEY` are set by an `env` prefix
    INSIDE the sandbox, as `test_claude_pure_launch_reaches_the_api_from_the_empty_cwd` does:
    the profile's allowlist refuses both names (they are what it closes)."""

    @classmethod
    def setUpClass(cls) -> None:
        if not _bwrap_usable():
            raise AssertionError("bwrap is absent or unprivileged user namespaces are disabled")

    def setUp(self) -> None:
        from tools import operator_private_root
        from tools.orchestration_runtime import init_orchestration
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.repo = base / "repo"
        self.repo.mkdir()
        self.credentials = base / "operator" / ".credentials.json"
        self.credentials.parent.mkdir()
        self.credentials.write_text('{"claudeAiOauth": {"accessToken": "placeholder"}}',
                                    encoding="utf-8")
        os.chmod(self.credentials, 0o600)
        patches = [mock.patch.dict(os.environ, {"ATMOFAB_WORKFLOW_HOMES_ROOT":
                                                str(base / "homes")}),
                   mock.patch.object(operator_private_root, "claude_credentials_file",
                                     lambda: self.credentials)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.orch = "orch_ws_lb"
        init_orchestration(repo_root=self.repo, orchestration_id=self.orch)
        _ensure_orchestration_audit_dirs(self.repo, self.orch)
        cfg = base / "llm.yaml"
        cfg.write_text(workshop_yaml(
            "defaults: {lineage_timeout_s: 2700, provider: claude_cli}\nphases: {generate: "
            "{substeps: {generate: {max_wrapper_calls: 5, lineage_timeout_s: 600}}}}\n"),
            encoding="utf-8")
        from tools import llm_config
        self.conductor = wc.Conductor(
            repo_root=self.repo, orchestration_id=self.orch, orchestration_agent_run_id="ORCH",
            llm_config=llm_config.load_llm_config(cfg), env={})
        self.entry = self.conductor.entry_for("generate", "generate")
        self.lineage = str(uuid.uuid4())
        from tools.orchestration_runtime import _prepare_claude_workshop_home
        self.iso = _prepare_claude_workshop_home(self.repo, self.orch, self.lineage,
                                                 resume=False)
        self.config, self.tree = Path(self.iso["config"]), Path(self.iso["tree"])

    def _endpoint(self, calls: list[str]):
        from tools import workshop_mcp
        ctx = mock.Mock()
        ctx.agent_run_id = self.lineage
        ctx.ops = __import__("tools.workshop_wrapper", fromlist=["x"]).WRAPPER_OPS
        from tools.workshop_tree import LineagePaths
        ctx.paths = LineagePaths(homes_root=Path(self.iso["homes_root"]),
                                 orchestration_id=self.orch, lineage_id=self.lineage,
                                 orchestration_dir=self.repo / "workspace" / "orchestrations"
                                 / self.orch)

        def op(ctx, name, args, *, on_spawn=None):
            calls.append(name)
            return {"ok": True, "marker": "WRAPPER-DERIVED-42"}

        server = workshop_mcp.WrapperMcpServer(ctx, run_op=op).start()
        self.addCleanup(server.stop)
        return server

    def _launch(self, api: _ScriptedApi, server, *, session: str,
                resume: str | None = None, restricted: bool = True,
                arid: str | None = None) -> subprocess.CompletedProcess:
        from tools.pure_leaf import workshop_mcp_config_json
        argv = self.conductor.leaf_command(
            self.entry, session_id=session, resume_session_id=resume,
            mcp_config_json=workshop_mcp_config_json(server.url))
        if not restricted:
            argv.remove("--restricted")
        profile = build_readonly_bwrap_profile(
            repo_root=self.repo, orchestration_id=self.orch, agent_run_id=arid or session,
            backend_command="claude", backend_type="claude",
            **ort.claude_workshop_profile_kwargs(self.iso))
        command = ["env", f"ANTHROPIC_BASE_URL={api.base_url}",
                   "ANTHROPIC_API_KEY=atmofab-loopback", *argv]
        return subprocess.run(render_bwrap_command(profile=profile, command_argv=command),
                              input="Do the task.", capture_output=True, text=True,
                              timeout=240, check=False)

    def _api(self, script) -> _ScriptedApi:
        api = _ScriptedApi(script)
        self.addCleanup(api.close)
        return api

    def test_m1_an_empty_config_home_runs_and_the_cli_populates_it(self) -> None:
        """M1: `config/` holds only the credential placeholder (bound over by the operator's
        file), and `-p` runs from it with no onboarding state; the CLI writes its global config
        and the session transcript THERE, and the operator's credentials file is untouched."""
        before = self.credentials.read_bytes()
        api = self._api([([{"type": "text", "text": "done"}], "end_turn")])
        session = str(uuid.uuid4())
        res = self._launch(api, self._endpoint([]), session=session)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        envelope = json.loads(res.stdout)
        self.assertEqual((envelope["subtype"], envelope["result"]), ("success", "done"))
        created = sorted(str(p.relative_to(self.config)) for p in self.config.rglob("*")
                         if p.is_file())
        self.assertIn(".claude.json", created)
        self.assertTrue(any(c.startswith("projects/") and c.endswith(f"{session}.jsonl")
                            for c in created), created)
        self.assertEqual(self.credentials.read_bytes(), before)

    def test_m3_m4_m5_the_cli_calls_the_wrapper_and_gets_its_result(self) -> None:
        """M3: `initialize`, `tools/list` and `tools/call` reach the endpoint and the CLI accepts
        its JSON answers. M4: the call is EXECUTED under the workshop permission flags — no
        `permission_denials`. M5: the request lists the five file tools and the wrapper's
        tools. And the wrapper's answer is what the model is handed back."""
        calls: list[str] = []
        api = self._api([
            ([{"type": "tool_use", "id": "toolu_1", "name": "mcp__wrapper__derive",
               "input": {}}], "tool_use"),
            ([{"type": "text", "text": "done"}], "end_turn")])
        server = self._endpoint(calls)
        res = self._launch(api, server, session=str(uuid.uuid4()))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        envelope = json.loads(res.stdout)
        self.assertEqual(envelope["permission_denials"], [])
        self.assertEqual(calls, ["derive"])
        methods = [json.loads(line)["request"].get("method")
                   for line in server.ctx.paths.wrapper_calls.read_text().splitlines()
                   if json.loads(line)["request"]]
        for method in ("initialize", "tools/list", "tools/call"):
            self.assertIn(method, methods)
        tools = {t["name"] for t in api.main_requests()[0]["tools"]}
        self.assertTrue({"Read", "Write", "Edit", "Glob", "Grep"} <= tools, tools)
        self.assertTrue({f"mcp__wrapper__{op}" for op in
                         ("derive", "compile", "lint", "syntax_check", "run")} <= tools, tools)
        self.assertFalse(tools & {"Bash", "WebFetch"}, tools)
        result = api.tool_results()[0]
        self.assertIn("WRAPPER-DERIVED-42", json.dumps(result))
        self.assertNotIn("is_error", {k for k, v in result.items() if v})

    def test_mc2_a_compile_tree_is_written_checked_and_assembled(self) -> None:
        """MC2 (issue #534 step 2): the real CLI writes `spec.ir.yaml` with its `Write` tool and
        calls `mcp__wrapper__check_ir`, which the REAL wrapper serves over an IR lineage's
        context (`run_wrapper_op`; the validators stubbed to one marker finding, the document
        contract to clean). The operation ran on the leaf's file, its finding reached the model,
        only `check_ir` was offered among the wrapper's tools, and the tree assembles into the
        document the host hands to `Compile.static`."""
        from tools import workshop_mcp, workshop_tree, workshop_wrapper
        paths = workshop_tree.LineagePaths(
            homes_root=Path(self.iso["homes_root"]), orchestration_id=self.orch,
            lineage_id=self.lineage,
            orchestration_dir=self.repo / "workspace" / "orchestrations" / self.orch)
        paths.make_work_dirs()
        workshop_tree.write_lineage_record(paths, node_key="component/x@0.1.0",
                                           started_at_utc="2026-10-24T00:00:00Z")
        seen: list = []

        def validate(repo_root, ir_dir):
            seen.append((Path(ir_dir) / "spec.ir.yaml").read_text())
            return ["IR-CHECK-FINDING-7: per_case_status is not stated by tests.md"]

        ctx = workshop_wrapper.WrapperContext(
            paths=paths, repo_root=self.repo, orchestration_id=self.orch,
            agent_run_id=self.lineage, max_wrapper_calls=5,
            reserved_names=workshop_tree.reserved_tree_names(
                (), None, (workshop_tree.IR_DOCUMENT_NAME, workshop_tree.COMPILE_FAIL_NAME)),
            seed_names=(), lineage_deadline=time.time() + 600,
            ops=workshop_wrapper.IR_CHECK_OPS, violations=lambda doc: None,
            assemble=workshop_tree.assemble_ir_document, declared_fail=lambda doc: None,
            ir_yaml=lambda ir: yaml.safe_dump(ir), ir_meta_text="{}\n",
            validate_ir_dir=validate)
        server = workshop_mcp.WrapperMcpServer(ctx).start()
        self.addCleanup(server.stop)
        ir_text = "schema_version: '1.0'\nmeta: {spec_kind: component}\n"
        api = self._api([
            ([{"type": "tool_use", "id": "toolu_1", "name": "Write",
               "input": {"file_path": str(self.tree / "spec.ir.yaml"), "content": ir_text}}],
             "tool_use"),
            ([{"type": "tool_use", "id": "toolu_2", "name": "mcp__wrapper__check_ir",
               "input": {}}], "tool_use"),
            ([{"type": "text", "text": "done"}], "end_turn")])
        res = self._launch(api, server, session=str(uuid.uuid4()))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads(res.stdout)["permission_denials"], [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(yaml.safe_load(seen[0]),
                         {"schema_version": "1.0", "meta": {"spec_kind": "component"}})
        self.assertIn("IR-CHECK-FINDING-7", json.dumps(api.tool_results()[-1]))
        tools = {t["name"] for t in api.main_requests()[0]["tools"]}
        self.assertEqual({t for t in tools if t.startswith("mcp__wrapper__")},
                         {"mcp__wrapper__check_ir"})
        self.assertEqual(workshop_tree.assemble_ir_document(self.tree),
                         {"ir": {"schema_version": "1.0", "meta": {"spec_kind": "component"}},
                          "compile_fail": None})
        self.assertEqual(workshop_tree.read_lineage_record(paths)["wrapper_calls_used"], 1)

    def test_m4_control_without_the_grant_the_call_is_denied(self) -> None:
        """The control for M4: the same launch with `--allowedTools mcp__wrapper` removed is
        DENIED (measured: `acceptEdits` + `--permission-prompts none` alone deny an MCP call),
        so the row above passes because of the grant and not because nothing is checked."""
        calls: list[str] = []
        api = self._api([
            ([{"type": "tool_use", "id": "toolu_1", "name": "mcp__wrapper__derive",
               "input": {}}], "tool_use"),
            ([{"type": "text", "text": "done"}], "end_turn")])
        server = self._endpoint(calls)
        original = self.conductor.leaf_command

        def without_grant(*a, **k):
            argv = original(*a, **k)
            i = argv.index("--allowedTools")
            return argv[:i] + argv[i + 2:]

        self.conductor.leaf_command = without_grant  # type: ignore[assignment]
        res = self._launch(api, server, session=str(uuid.uuid4()))
        self.assertEqual(calls, [])
        self.assertEqual([d["tool_name"] for d in json.loads(res.stdout)["permission_denials"]],
                         ["mcp__wrapper__derive"])

    def test_m2a_the_file_tools_reach_the_tree_and_nothing_else(self) -> None:
        """M2 (a): a `Write` inside the tree lands on the host's tree; a `Write` to the CLI's own
        `config/` or beside the tree is refused by `--restricted` and lands nowhere."""
        targets = {"in tree": self.tree / "a.txt",
                   "config home": self.config / "CLAUDE.md",
                   "beside the tree": self.tree.parent / "x.txt"}
        outcomes = {}
        for label, target in targets.items():
            api = self._api([
                ([{"type": "tool_use", "id": "toolu_1", "name": "Write",
                   "input": {"file_path": str(target), "content": "written"}}], "tool_use"),
                ([{"type": "text", "text": "done"}], "end_turn")])
            self._launch(api, self._endpoint([]), session=str(uuid.uuid4()))
            outcomes[label] = (target.exists(), json.dumps(api.tool_results()))
        self.assertTrue(outcomes["in tree"][0], outcomes)
        for label in ("config home", "beside the tree"):
            self.assertFalse(outcomes[label][0], outcomes)
            self.assertIn("--restricted confines the file tools", outcomes[label][1])

    def test_m2b_no_instruction_file_reaches_a_cold_or_a_warm_turn(self) -> None:
        """M2 (b): instruction files planted where the CLI discovers them — the tree's
        `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`, `.claude/settings.json`, and the
        config home's `CLAUDE.md` — reach neither the cold launch nor a warm `--resume
        --fork-session` of it. CONTROL: the same two launches without `--restricted` carry
        every marker, so a dead capture cannot pass the absent half."""
        markers = {"tree CLAUDE.md": (self.tree / "CLAUDE.md", "MARKER-TREE-MD"),
                   "tree .claude/CLAUDE.md": (self.tree / ".claude" / "CLAUDE.md",
                                              "MARKER-DOTCLAUDE-MD"),
                   "tree CLAUDE.local.md": (self.tree / "CLAUDE.local.md", "MARKER-LOCAL-MD"),
                   "config CLAUDE.md": (self.config / "CLAUDE.md", "MARKER-CONFIG-MD")}
        for path, text in markers.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        (self.tree / ".claude" / "settings.json").write_text(
            json.dumps({"model": "claude-marker-settings"}), encoding="utf-8")
        texts = [t for _, t in markers.values()] + ["claude-marker-settings"]
        for restricted in (True, False):
            with self.subTest(restricted=restricted):
                cold, warm = str(uuid.uuid4()), str(uuid.uuid4())
                api = self._api([([{"type": "text", "text": "done"}], "end_turn")])
                res = self._launch(api, self._endpoint([]), session=cold,
                                   restricted=restricted)
                self.assertTrue(api.bodies, res.stdout + res.stderr)
                api2 = self._api([([{"type": "text", "text": "done"}], "end_turn")])
                res2 = self._launch(api2, self._endpoint([]), session=warm, resume=cold,
                                    restricted=restricted)
                self.assertTrue(api2.bodies, res2.stdout + res2.stderr)
                for label, bodies in (("cold", api.bodies), ("warm", api2.bodies)):
                    seen = b"".join(bodies).decode(errors="replace")
                    for text in texts:
                        if restricted:
                            self.assertNotIn(text, seen, f"{label}: {text}")
                        else:
                            self.assertIn(text, seen, f"control {label}: {text}")




class ReadingWorkshopLeafCliTests(WorkshopLeafCliTests):
    """UNBILLED (issue #534 step 3, MR1): the real claude CLI under a READING workshop leaf's
    launch shape — the production argv (`Conductor.leaf_command(reading=True)`, no endpoint)
    under the same writable lineage profile — against the scripted API stand-in. A judging
    lineage is served no operation: the request lists the five file tools and no
    `mcp__wrapper__*` tool, the reviewer's `Write` of its document lands in the tree, and the
    snapshot carries that document and no seed. The inherited M rows run again under the
    producing launch, unchanged."""

    def setUp(self) -> None:
        super().setUp()
        from tools import llm_config
        cfg = self.repo.parent / "llm_reading.yaml"
        cfg.write_text(workshop_yaml(
            "defaults: {lineage_timeout_s: 2700, provider: claude_cli}\nphases: {validate: "
            "{substeps: {judge: {lineage_timeout_s: 600}}}}\n"), encoding="utf-8")
        self.reader = wc.Conductor(
            repo_root=self.repo, orchestration_id=self.orch, orchestration_agent_run_id="ORCH",
            llm_config=llm_config.load_llm_config(cfg), env={})
        self.reader_entry = self.reader.entry_for("validate", "judge")

    def _read_launch(self, api: _ScriptedApi, *, session: str) -> subprocess.CompletedProcess:
        argv = self.reader.leaf_command(self.reader_entry, session_id=session, reading=True)
        profile = build_readonly_bwrap_profile(
            repo_root=self.repo, orchestration_id=self.orch, agent_run_id=session,
            backend_command="claude", backend_type="claude",
            **ort.claude_workshop_profile_kwargs(self.iso))
        command = ["env", f"ANTHROPIC_BASE_URL={api.base_url}",
                   "ANTHROPIC_API_KEY=atmofab-loopback", *argv]
        return subprocess.run(render_bwrap_command(profile=profile, command_argv=command),
                              input="Review the run.", capture_output=True, text=True,
                              timeout=240, check=False)

    def test_mr1_a_reading_lineage_writes_its_document_and_holds_no_wrapper(self) -> None:
        from tools import workshop_tree
        paths = workshop_tree.LineagePaths(
            homes_root=Path(self.iso["homes_root"]), orchestration_id=self.orch,
            lineage_id=self.lineage,
            orchestration_dir=self.repo / "workspace" / "orchestrations" / self.orch)
        paths.make_work_dirs()
        workshop_tree.seed_tree(paths, {"tests.md": "# tests\n", "host_verdict.json": "{}\n"})
        review = {"decision": "pass", "findings": []}
        api = self._api([
            ([{"type": "tool_use", "id": "toolu_1", "name": "Read",
               "input": {"file_path": str(self.tree / "tests.md")}}], "tool_use"),
            ([{"type": "tool_use", "id": "toolu_2", "name": "Write",
               "input": {"file_path": str(self.tree / "review.json"),
                         "content": json.dumps(review)}}], "tool_use"),
            ([{"type": "tool_use", "id": "toolu_3", "name": "Write",
               "input": {"file_path": str(self.tree / "tests.md"),
                         "content": "# edited by the leaf\n"}}], "tool_use"),
            ([{"type": "text", "text": "done"}], "end_turn")])
        argv = self.reader.leaf_command(self.reader_entry, session_id="s", reading=True)
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--allowedTools", argv)
        res = self._read_launch(api, session=str(uuid.uuid4()))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads(res.stdout)["permission_denials"], [])
        tools = {t["name"] for t in api.main_requests()[0]["tools"]}
        self.assertTrue({"Read", "Write", "Edit", "Glob", "Grep"} <= tools, tools)
        self.assertFalse({t for t in tools if t.startswith("mcp__")}, tools)
        self.assertFalse(tools & {"Bash", "WebFetch"}, tools)
        self.assertIn("# tests", json.dumps(api.tool_results()[0]))
        # The document landed; the snapshot carries it and not the seed the leaf edited.
        host_placed = workshop_tree.reserved_tree_names(
            ("tests.md", "host_verdict.json"), None, ("review.json",)) - {"review.json"}
        workshop_tree.snapshot_tree(paths, host_placed)
        self.assertEqual(sorted(p.name for p in paths.snapshot.iterdir()), ["review.json"])
        self.assertEqual(
            workshop_tree.assemble_document_file(paths.snapshot, "review.json", "x"), review)


    def test_mj1_the_diagnosticians_lineage_writes_its_directive_and_holds_no_wrapper(
            self) -> None:
        """Issue #534 PR-J4-a: the `defaults` entry is the diagnostician's reading workshop
        entry, so its argv is the reading one (no `--mcp-config`); its tree holds
        `diagnosis.json`, its `Write` of `directive.json` lands, and the snapshot carries that
        file alone, which `_directive_from_object` reads (parsed whole) as the directive it
        wrote."""
        from tools import workshop_tree
        entry = self.reader.entry_for(None, None)
        paths = workshop_tree.LineagePaths(
            homes_root=Path(self.iso["homes_root"]), orchestration_id=self.orch,
            lineage_id=self.lineage,
            orchestration_dir=self.repo / "workspace" / "orchestrations" / self.orch)
        paths.make_work_dirs()
        workshop_tree.seed_tree(paths, {"diagnosis.json": '{"failed_phase": "validate"}\n'})
        directive = {"action": "fail_closed", "target_phase": None, "severity": "major",
                     "reason": "spec defect"}
        api = self._api([
            ([{"type": "tool_use", "id": "toolu_1", "name": "Read",
               "input": {"file_path": str(self.tree / "diagnosis.json")}}], "tool_use"),
            ([{"type": "tool_use", "id": "toolu_2", "name": "Write",
               "input": {"file_path": str(self.tree / "directive.json"),
                         "content": json.dumps(directive)}}], "tool_use"),
            ([{"type": "text", "text": "done"}], "end_turn")])
        argv = self.reader.leaf_command(entry, session_id="s", reading=True)
        self.assertNotIn("--mcp-config", argv)
        session = str(uuid.uuid4())
        profile = build_readonly_bwrap_profile(
            repo_root=self.repo, orchestration_id=self.orch, agent_run_id=session,
            backend_command="claude", backend_type="claude",
            **ort.claude_workshop_profile_kwargs(self.iso))
        command = ["env", f"ANTHROPIC_BASE_URL={api.base_url}",
                   "ANTHROPIC_API_KEY=atmofab-loopback",
                   *self.reader.leaf_command(entry, session_id=session, reading=True)]
        res = subprocess.run(render_bwrap_command(profile=profile, command_argv=command),
                             input="Diagnose the failure.", capture_output=True, text=True,
                             timeout=240, check=False)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        tools = {t["name"] for t in api.main_requests()[0]["tools"]}
        self.assertFalse({t for t in tools if t.startswith("mcp__")}, tools)
        self.assertIn("failed_phase", json.dumps(api.tool_results()[0]))
        host_placed = workshop_tree.reserved_tree_names(
            ("diagnosis.json",), None, ("directive.json",)) - {"directive.json"}
        workshop_tree.snapshot_tree(paths, host_placed)
        self.assertEqual(sorted(p.name for p in paths.snapshot.iterdir()), ["directive.json"])
        decision = wc._directive_from_object(json.loads(
            (paths.snapshot / "directive.json").read_text(encoding="utf-8")))
        self.assertEqual((decision.action, decision.reason), ("fail_closed", "spec defect"))

class _ScriptedResponsesApi:
    """An OpenAI Responses API stand-in that answers with a SCRIPT, streamed as codex reads it.

    The i-th `POST …/responses` is answered `script[i]` — a list of output items (a
    `custom_tool_call` of codex's `exec` tool carries JavaScript; a `message` ends the turn) —
    and every later one with a one-word message. Request bodies are kept: the next request
    carries the previous call's `custom_tool_call_output`, which is how a row reads what a tool
    returned. Any other path is a 404 (the CLI's model-list refresh). Unbilled: the CLI talks to
    loopback with a placeholder `auth.json`.

    `child_script`, when given, answers every request that does not carry the parent's task
    ("Do the task.") — a sub-agent's thread — from its own script and counter."""

    def __init__(self, script: list[list[dict]],
                 child_script: list[list[dict]] | None = None) -> None:
        bodies: list[bytes] = []
        child_bodies: list[bytes] = []

        def sse(items: list[dict], n: int) -> bytes:
            events = [{"type": "response.created", "response": {"id": f"resp_{n}"}}]
            events += [{"type": "response.output_item.done", "output_index": i, "item": item}
                       for i, item in enumerate(items)]
            events.append({"type": "response.completed", "response": {
                "id": f"resp_{n}", "usage": {
                    "input_tokens": 10, "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 3, "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 13}}})
            return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
                           for e in events).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not self.path.endswith("/responses"):
                    self.send_response(404)
                    self.end_headers()
                    return
                if child_script is not None and b"Do the task." not in body:
                    child_bodies.append(body)
                    n = len(child_bodies)
                    items = child_script[n - 1] if n <= len(child_script) else [
                        {"type": "message", "role": "assistant", "id": f"cmsg_{n}",
                         "content": [{"type": "output_text", "text": "done"}]}]
                    data = sse(items, 1000 + n)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                bodies.append(body)
                n = len(bodies)
                items = script[n - 1] if n <= len(script) else [
                    {"type": "message", "role": "assistant", "id": f"msg_{n}",
                     "content": [{"type": "output_text", "text": "done"}]}]
                data = sse(items, n)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args) -> None:  # BaseHTTPRequestHandler's spelling
                pass

        self.bodies = bodies
        self.child_bodies = child_bodies
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.port = self._server.server_address[1]

    def requests(self) -> list[dict]:
        return [json.loads(b) for b in self.bodies]

    def tool_output(self, index: int) -> str:
        """The text of the `custom_tool_call_output` request `index` carries."""
        for item in reversed(self.requests()[index]["input"]):
            if item.get("type") == "custom_tool_call_output":
                return "".join(part.get("text", "") for part in item.get("output", []))
        raise AssertionError(f"request {index} carries no tool output")

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _exec_call(js: str, n: int = 1) -> dict:
    """A codex `exec` tool call (codex-cli 0.164.0's code-mode surface, measured: the
    `functions` namespace's JavaScript tool, whose nested tools — `exec_command`, `apply_patch`,
    the MCP servers' `mcp__<server>__<tool>` — are reached on its `tools` object)."""
    return {"type": "custom_tool_call", "id": f"ctc_{n}", "call_id": f"call_{n}",
            "name": "exec", "input": js}


class _CodexLeafCliCase(unittest.TestCase):
    """The fixture of the two codex workshop CLI classes below: a lineage home under the homes
    root, the operator's codex auth, the scripted Responses-API stand-in, and the production
    argv of the entry `CONFIG` names for `PAIR` under the rendered workshop profile. `READING`
    launches the judging (reading) argv, served no endpoint."""

    CONFIG = ("defaults: {lineage_timeout_s: 2700, provider: codex_cli, model: gpt-5.6-sol}\nphases: {generate: "
              "{substeps: {generate: {max_wrapper_calls: 5, "
              "lineage_timeout_s: 600}}}}\n")
    PAIR = ("generate", "generate")
    READING = False

    @classmethod
    def setUpClass(cls) -> None:
        if not _bwrap_usable():
            raise AssertionError("bwrap is absent or unprivileged user namespaces are disabled")

    def setUp(self) -> None:
        from tools.orchestration_runtime import init_orchestration
        from tools.tests.private_root_fixture import seed_codex_auth
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.repo = base / "repo"
        self.repo.mkdir()
        self.origin = seed_codex_auth(base / "operator-codex")
        # The homes root OUTSIDE /tmp, as production's `~/.atmofab/homes` is: codex's own
        # `workspace-write` sandbox keeps /tmp writable, so a lineage home under it would make
        # `config/` and the home's other paths writable to the shell and the MX3 row would be
        # measuring bwrap alone (round 1 of step 2 PR-2 measured exactly that).
        homes = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.addCleanup(homes.cleanup)
        homes_root = Path(homes.name).resolve() / "homes"
        self.assertFalse(homes_root.is_relative_to("/tmp"), homes_root)
        patcher = mock.patch.dict(os.environ, {
            "ATMOFAB_WORKFLOW_HOMES_ROOT": str(homes_root),
            "CODEX_HOME": str(self.origin)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.orch = "orch_cx_ws"
        init_orchestration(repo_root=self.repo, orchestration_id=self.orch)
        _ensure_orchestration_audit_dirs(self.repo, self.orch)
        cfg = base / "llm.yaml"
        cfg.write_text(workshop_yaml(self.CONFIG), encoding="utf-8")
        from tools import llm_config
        self.conductor = wc.Conductor(
            repo_root=self.repo, orchestration_id=self.orch, orchestration_agent_run_id="ORCH",
            llm_config=llm_config.load_llm_config(cfg), env={})
        self.entry = self.conductor.entry_for(*self.PAIR)
        self.lineage = str(uuid.uuid4())
        self.iso = ort._prepare_codex_workflow_home(self.repo, self.orch, self.lineage,
                                                    resume=False, workshop=True)
        self.config, self.tree = Path(self.iso["config"]), Path(self.iso["tree"])
        self.paths = self.conductor._workshop_paths(self.entry, self.iso, self.lineage)

    def _endpoint(self, calls: list[str]):
        from tools import workshop_mcp, workshop_wrapper
        ctx = mock.Mock()
        ctx.agent_run_id = self.lineage
        ctx.ops = workshop_wrapper.WRAPPER_OPS
        ctx.paths = self.paths

        def op(ctx, name, args, *, on_spawn=None):
            calls.append(name)
            return {"ok": True, "marker": "WRAPPER-DERIVED-42"}

        server = workshop_mcp.WrapperMcpServer(ctx, run_op=op).start()
        self.addCleanup(server.stop)
        return server

    def _api(self, script) -> _ScriptedResponsesApi:
        api = _ScriptedResponsesApi(script)
        self.addCleanup(api.close)
        return api

    def _argv(self, server, resume: str | None) -> list[str]:
        if self.READING:
            assert server is None, "a reading lineage is served no endpoint"
            return self.conductor.leaf_command(
                self.entry, session_id=self.lineage, resume_session_id=resume, reading=True)
        from tools.pure_leaf import workshop_mcp_config_json
        return self.conductor.leaf_command(
            self.entry, session_id=self.lineage, resume_session_id=resume,
            mcp_config_json=workshop_mcp_config_json(server.url))

    def _launch(self, api, server, *, resume: str | None = None,
                drop: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        argv = self._argv(server, resume)
        for token in drop:
            i = argv.index(token)
            del argv[i - 1:i + 1]
        self.assertEqual(argv[-2:], ["--json", "-"])
        overrides = ["--config", 'model_provider="loopback"',
                     "--config", 'model_providers.loopback.name="loopback"',
                     "--config", f'model_providers.loopback.base_url="{api.base_url}/v1"',
                     "--config", 'model_providers.loopback.wire_api="responses"']
        profile = build_readonly_bwrap_profile(
            repo_root=self.repo, orchestration_id=self.orch, agent_run_id=str(uuid.uuid4()),
            backend_command="codex", backend_type="codex",
            **ort.codex_workshop_profile_kwargs(self.iso))
        command = render_bwrap_command(
            profile=profile, command_argv=[*argv[:-2], *overrides, *argv[-2:]])
        return subprocess.run(command, input="Do the task.", capture_output=True, text=True,
                              timeout=240, check=False)  # the rc is asserted by each row

    @staticmethod
    def _thread_id(stdout: str) -> str:
        for line in stdout.splitlines():
            event = wc._parse_codex_line(line)
            if event is not None and event.get("type") == "thread.started":
                return str(event["thread_id"])
        raise AssertionError(f"no thread.started in {stdout[-2000:]}")


@skip_probes.skip_unless_installed("codex")
class CodexWorkshopLeafCliTests(_CodexLeafCliCase):
    """UNBILLED (issue #534 step 2 PR-2): the real codex CLI under a codex workshop leaf's
    launch shape — the production argv (`Conductor.leaf_command` with the wrapper's endpoint)
    under the rendered profile (`_prepare_codex_workflow_home(workshop=True)` +
    `codex_workshop_profile_kwargs`, a real bwrap) against a scripted Responses-API stand-in and
    the real wrapper endpoint (its operation stubbed). The measurements MX1-MX5 of the step-2
    plan, kept as rows so a CLI version that moves one turns a row red. The stand-in's provider
    is named by `--config model_provider…` overrides spliced before `--json -`."""

    def test_mx1_mx4_an_empty_codex_home_runs_and_holds_the_resumable_rollout(self) -> None:
        """MX1: `config/` holds only `config.toml` and the `auth.json` placeholder (bound over
        by the operator's file), and `exec` runs from it. MX4: the thread's rollout is written
        under `config/sessions/` with the thread id in its name, which is what
        `_workshop_session_resumable` globs; the operator's own codex home is untouched."""
        self.assertEqual(sorted(p.name for p in self.config.iterdir()),
                         ["auth.json", "config.toml"])
        before = sorted(p.name for p in self.origin.iterdir())
        res = self._launch(self._api([]), self._endpoint([]))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        thread = self._thread_id(res.stdout)
        self.assertIn('"type":"turn.completed"', res.stdout)
        rollouts = sorted(self.config.glob("sessions/*/*/*/rollout-*.jsonl"))
        self.assertEqual(len(rollouts), 1, rollouts)
        self.assertTrue(rollouts[0].name.endswith(f"-{thread}.jsonl"), rollouts)
        self.assertTrue(self.conductor._workshop_session_resumable(self.paths, thread))
        self.assertEqual(sorted(p.name for p in self.origin.iterdir()), before)
        self.assertEqual((self.origin / "auth.json").read_text(), "{}\n")

    def test_mx2_the_wrapper_is_reached_and_its_answer_returned(self) -> None:
        """MX2: `mcp_servers.wrapper.url` reaches the endpoint (`initialize`, `tools/list`,
        `tools/call`), the wrapper's tools are offered under `mcp__wrapper__<op>`, and the
        answer is handed back to the model. CONTROL: without
        `default_tools_approval_mode="approve"` the call is refused before it reaches the
        endpoint — the override is load-bearing."""
        js = ("text(JSON.stringify(ALL_TOOLS.map(t => t.name)));"
              "text(JSON.stringify(await tools.mcp__wrapper__derive({})));")
        calls: list[str] = []
        server = self._endpoint(calls)
        api = self._api([[_exec_call(js)]])
        res = self._launch(api, server)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(calls, ["derive"])
        methods = [json.loads(line)["request"].get("method")
                   for line in self.paths.wrapper_calls.read_text().splitlines()
                   if json.loads(line)["request"]]
        for method in ("initialize", "tools/list", "tools/call"):
            self.assertIn(method, methods)
        output = api.tool_output(1)
        for op in ("derive", "compile", "lint", "syntax_check", "run"):
            self.assertIn(f"mcp__wrapper__{op}", output)
        self.assertIn("WRAPPER-DERIVED-42", output)
        calls.clear()
        api = self._api([[_exec_call(js)]])
        res = self._launch(api, server, drop=(
            'mcp_servers.wrapper.default_tools_approval_mode="approve"',))
        self.assertEqual(calls, [])
        self.assertIn("requires approval", api.tool_output(1))

    def test_mx3_the_shell_writes_the_tree_and_reaches_nothing_else(self) -> None:
        """MX3: codex's `workspace-write` sandbox initializes inside bwrap: a command writes
        the tree, and a write beside it or into its own `config/` is refused read-only (with the
        homes root outside /tmp, as production's is); the checkout is the empty tmpfs; a
        command's network is refused (the stand-in itself is unreachable from the shell); an
        escalated command is refused under `approval_policy="never"`. And M2's twin: an
        `AGENTS.md` and a `.codex/AGENTS.md` planted in the tree reach no request."""
        (self.tree / "AGENTS.md").write_text("PLANTED-AGENTS-MARKER-1\n", encoding="utf-8")
        (self.tree / ".codex").mkdir()
        (self.tree / ".codex" / "AGENTS.md").write_text("PLANTED-AGENTS-MARKER-2\n",
                                                        encoding="utf-8")
        api = self._api([])
        probes = {
            "tree": "echo hi > in_tree.txt && cat in_tree.txt",
            "beside": "echo x > ../beside.txt; echo rc=$?",
            "config": "echo x > ../config/AGENTS.md; echo rc=$?",
            "checkout": f"ls {self.repo}/tools 2>&1; echo rc=$?",
            "net": ("python3 -c 'import socket; socket.create_connection((\"127.0.0.1\", "
                    f"{api.port}), 3)' 2>&1 | tail -1; echo rc=$?"),
        }
        js = ["const out = {};"]
        for key, cmd in probes.items():
            js.append(f"out[{json.dumps(key)}] = (await tools.exec_command("
                      f"{{cmd: {json.dumps(cmd)}, login: false}})).output;")
        js.append("try { await tools.exec_command({cmd: 'echo esc > ../esc.txt', "
                  "sandbox_permissions: 'require_escalated', justification: 'x', "
                  "login: false}); out.escalated = 'ran'; } "
                  "catch (e) { out.escalated = 'refused: ' + e; }")
        js.append("text(JSON.stringify(out));")
        api2 = self._api([[_exec_call("\n".join(js))]])
        res = self._launch(api2, self._endpoint([]))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        text = api2.tool_output(1)
        out = json.loads(text[text.index("{"):])
        self.assertEqual(out["tree"].strip(), "hi")
        self.assertEqual((self.tree / "in_tree.txt").read_text(), "hi\n")
        self.assertFalse((Path(self.iso["home"]) / "beside.txt").exists())
        # Refused by codex's own sandbox (bwrap alone would let the beside write land on its
        # tmpfs), and its own CLI home is read-only to a command.
        for key in ("beside", "config"):
            self.assertIn("read-only file system", out[key].lower(), key)
        self.assertFalse((self.config / "AGENTS.md").exists())
        self.assertIn("No such file or directory", out["checkout"])
        # Measured: `PermissionError: [Errno 1] Operation not permitted` (codex's seccomp
        # filter refuses the socket), while the same host reaches the stand-in.
        self.assertIn("Operation not permitted", out["net"])
        self.assertTrue(out["escalated"].startswith("refused"), out["escalated"])
        self.assertFalse((Path(self.iso["home"]) / "esc.txt").exists())
        blob = b"".join(api2.bodies)
        self.assertNotIn(b"PLANTED-AGENTS-MARKER", blob)

    @staticmethod
    def _turn_usage(stdout: str) -> dict:
        for line in stdout.splitlines():
            event = wc._parse_codex_line(line)
            if event is not None and event.get("type") == "turn.completed":
                return dict(event["usage"])
        raise AssertionError(f"no turn.completed in {stdout[-2000:]}")

    def test_mx6_a_warm_turn_reports_the_thread_total_and_the_row_records_its_own(
            self) -> None:
        """MX6: the stand-in answers every request with the same usage, and a resumed turn of
        one request reports twice the cold turn's counts — the thread's running total. The
        usage row `_leaf_usage_row` records for it, decumulated against the cold launch's
        total, is the one request's counts again."""
        server = self._endpoint([])
        cold = self._launch(self._api([]), server)
        self.assertEqual(cold.returncode, 0, cold.stdout + cold.stderr)
        warm = self._launch(self._api([]), server, resume=self._thread_id(cold.stdout))
        self.assertEqual(warm.returncode, 0, warm.stdout + warm.stderr)
        first, total = self._turn_usage(cold.stdout), self._turn_usage(warm.stdout)
        self.assertEqual(first["input_tokens"], 10)
        self.assertEqual(total, {k: 2 * v for k, v in first.items()})
        row = wc._leaf_usage_row(wc.ProcResult(0, "done", "", usage=total, raw_stdout="x"),
                                 self.entry, resumed=("cold", first))
        self.assertEqual((row["input_tokens"], row["output_tokens"]),
                         (first["input_tokens"], first["output_tokens"]))

    def test_mx5_a_warm_resume_continues_the_thread_and_reaches_the_wrapper(self) -> None:
        """MX5: `exec resume <thread>` with `sandbox_mode="workspace-write"` and the same
        overrides, from the lineage home: the resumed thread's history is in the request, a
        command writes the tree, and the wrapper is called."""
        calls: list[str] = []
        server = self._endpoint(calls)
        res = self._launch(self._api([]), server)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        thread = self._thread_id(res.stdout)
        api = self._api([[_exec_call(
            "await tools.exec_command({cmd: 'echo warm > warm.txt', login: false});"
            "text(JSON.stringify(await tools.mcp__wrapper__compile({})));")]])
        res = self._launch(api, server, resume=thread)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self._thread_id(res.stdout), thread)
        self.assertEqual(calls, ["compile"])
        # The resumed thread runs under `sandbox_mode="workspace-write"`, not read-only.
        self.assertEqual((self.tree / "warm.txt").read_text(), "warm\n")
        self.assertIn("Do the task.", json.dumps(api.requests()[0]["input"]))
        self.assertGreater(len(api.requests()[0]["input"]), 2)


@skip_probes.skip_unless_installed("codex")
class CodexReadingLeafCliTests(_CodexLeafCliCase):
    """UNBILLED (issue #534 step 3 PR-J2, MR5): the real codex CLI under a READING workshop
    leaf's launch shape — the production reading argv (`Conductor.leaf_command(reading=True)`,
    no endpoint) of a codex `validate.judge` entry under the same rendered profile — against
    the scripted Responses-API stand-in. MR5-a: no `mcp__` tool; MR5-b: a document a command
    writes lands and assembles; MR5-c: what a large read returns (the ground of the codex
    reader row's paging rule, `pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT`); MR5-d: a warm
    repair resumes the thread."""

    CONFIG = ("defaults: {lineage_timeout_s: 2700, provider: codex_cli, model: gpt-5.6-sol}\nphases: {validate: "
              "{substeps: {judge: {lineage_timeout_s: 600}}}}\n")
    PAIR = ("validate", "judge")
    READING = True

    def _tool_names(self) -> list[str]:
        api = self._api([[_exec_call("text(JSON.stringify(ALL_TOOLS.map(t => t.name)));")]])
        res = self._launch(api, None)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn('"type":"turn.completed"', res.stdout)
        text = api.tool_output(1)
        return json.loads(text[text.index("["):text.rindex("]") + 1])

    def test_mr5a_a_reading_lineage_offers_no_mcp_tool(self) -> None:
        """MR5-a: the reading argv names no `mcp_servers` table, the lineage's `config.toml`
        holds none, and the tool list the model is offered has no `mcp__` name (measured on
        0.164.0: `apply_patch, create_goal, exec_command, get_goal,
        list_mcp_resource_templates, list_mcp_resources, read_mcp_resource, update_goal,
        view_image, write_stdin` — the built-ins whose names contain `mcp` are why the check is
        a prefix). Whether a `.codex/config.toml` planted in the tree is read is observed by no
        row: MX3 plants instruction files only, and `test_workshop_codex.CodexLineageHomeTests`
        checks that the host marks the tree untrusted. A leaf gains nothing toward a verdict by
        planting one (a reading lineage holds no operation to add)."""
        argv = self._argv(None, None)
        self.assertNotIn("mcp_servers", " ".join(argv))
        self.assertNotIn("[mcp_servers", (self.config / "config.toml").read_text())
        names = self._tool_names()
        self.assertIn("exec_command", names)
        self.assertEqual([n for n in names if n.startswith("mcp__")], [], names)

    def test_mr5b_a_command_written_review_lands_and_assembles(self) -> None:
        """MR5-b: a command writes `review.json` (a schema-valid semantic review) at the tree
        root and rewrites the `tests.md` seed; the snapshot holds the review and no seed, and
        the spec's assembly returns the document, which the review validator accepts."""
        from tools import pure_leaf, workshop_tree
        from tools.tests.test_pure_leaf_judge import _review
        self.paths.make_work_dirs()
        workshop_tree.seed_tree(self.paths, {"tests.md": "# tests\n",
                                             "host_verdict.json": "{}\n"})
        review = _review("pass")
        cmd = (f"printf '%s' {json.dumps(json.dumps(review))} > review.json && "
               "echo edited > tests.md")
        api = self._api([[_exec_call(
            f"text((await tools.exec_command({{cmd: {json.dumps(cmd)}, login: false}}))"
            ".output);")]])
        res = self._launch(api, None)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads((self.tree / "review.json").read_text()), review)
        host_placed = workshop_tree.reserved_tree_names(
            ("tests.md", "host_verdict.json"), None, ("review.json",)) - {"review.json"}
        workshop_tree.snapshot_tree(self.paths, host_placed)
        self.assertEqual(sorted(p.name for p in self.paths.snapshot.iterdir()), ["review.json"])
        spec = self.conductor._pure_reviewer_spec("validate", "judge")
        assembled = spec.workshop_assemble(self.paths.snapshot, ())
        self.assertEqual(assembled, review)
        self.assertEqual(pure_leaf.semantic_review_document_violations(assembled), [])

    def test_mr5c_a_large_read_reaches_the_model_with_its_middle_removed(self) -> None:
        """MR5-c: a 600,000-byte, 20,000-line file. Inside the JS `exec` tool a `cat`'s
        `output` is the whole file; what reaches the model through `text()` is cut — a
        `Warning: truncated output` header with the total line count, the first and the LAST
        lines kept, the middle elided at an inline `tokens truncated` marker. A line range read
        with `sed -n` comes back whole. If a CLI version moves this cut, the codex reader row's
        paging sentence (`pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT["codex_cli"]`) must be
        re-measured with it."""
        lines = [f"L{i:05d} " + "x" * 22 for i in range(1, 20001)]
        (self.tree / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual((self.tree / "big.txt").stat().st_size, 600000)
        cat = "const r = await tools.exec_command({cmd: 'cat big.txt', login: false});"
        api = self._api([
            [_exec_call(cat + "text(JSON.stringify({keys: Object.keys(r).sort(), "
                        "len: r.output.length, first: r.output.includes('L00001 '), "
                        "middle: r.output.includes('L10000 '), "
                        "last: r.output.includes('L20000 ')}));", 1)],
            [_exec_call(cat + "text(r.output);", 2)],
            [_exec_call("text((await tools.exec_command({cmd: \"sed -n '9000,9100p' big.txt\", "
                        "login: false})).output);", 3)]])
        res = self._launch(api, None)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        whole_text = api.tool_output(1)
        whole = json.loads(whole_text[whole_text.index("{"):whole_text.rindex("}") + 1])
        self.assertEqual((whole["len"], whole["first"], whole["middle"], whole["last"]),
                         (600000, True, True, True), whole)
        self.assertIn("output", whole["keys"])
        seen = api.tool_output(2)
        msg = ("the codex output cut moved: re-measure the codex reader row's paging sentence "
               "(pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT['codex_cli'])")
        self.assertIn("Warning: truncated output", seen, msg)
        self.assertIn("Total output lines: 20000", seen, msg)
        self.assertIn("tokens truncated", seen, msg)
        self.assertIn("L00001 ", seen, msg)
        self.assertIn("L20000 ", seen, msg)
        self.assertNotIn("L10000 ", seen, msg)
        self.assertLess(len(seen), 100000, msg)
        # The codex reader row states the cut by the markers measured here, and pages through
        # the host's helper (MR6), not a line range (PR-J3-a).
        from tools import pure_leaf
        row = pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT["codex_cli"]
        for marker in ("Warning: truncated output", "tokens truncated",
                       f"python3 {pure_leaf.CODEX_READ_HELPER_NAME} <file>"):
            self.assertIn(marker, row, msg)
        ranged = api.tool_output(3)
        for i in range(9000, 9101):
            self.assertIn(f"L{i:05d} ", ranged)
        self.assertNotIn("L08999 ", ranged)
        self.assertNotIn("L09101 ", ranged)
        self.assertNotIn("tokens truncated", ranged)

    def _rollout_scan(self, thread: str):
        from tools.codex_rollout import scan_launch
        return scan_launch(self.config, thread)

    def _rollout_counts(self, thread: str) -> tuple:
        """`(reason, output_rows, cut_texts)` of the launch's scan."""
        got = self._rollout_scan(thread)
        return got.reason, got.output_rows, got.cut_texts

    def test_mr5e_every_budget_cuts_and_both_host_records_see_it(self) -> None:
        """MR5-e (issue #547), one launch: (i) `exec_command`'s `max_output_tokens` cuts INSIDE
        the script — `r.output` already carries the header, `original_token_count` is
        `ceil(bytes / 4)` — and a stringified result carries it escaped; (ii) a script's own
        output is cut by the `exec` result's budget like a `cat`; (iii) a raised
        `exec_command` budget hands the script the whole output, and a raised `exec` budget
        (the first-line pragma) still cuts what reaches the model — with the marker and NO
        header — while a LOWERED one cuts in the rollout too, header and all; (iv) a script that lowers the budget and strips both marks leaves the rollout
        clean (the residual) while the host's capture holds the whole 600,000 bytes; (vi) that
        capture is uncut for every command; (vii) 40,000 bytes arrive whole and 40,001 cut, so
        the byte rule is `bytes > CODEX_EXEC_OUTPUT_BUDGET_BYTES`; (v) the rollout reader
        counts exactly the cut texts — the raised-budget one by its size, since the rollout
        records it whole (the cut is applied on the way to the model). If a CLI version moves
        any of this, the codex reader row and the byte rule (`pure_leaf.CODEX_*`) move with
        it."""
        from tools import codex_rollout, pure_leaf
        lines = [f"L{i:05d} " + "x" * 22 for i in range(1, 20001)]
        (self.tree / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        rule = pure_leaf.CODEX_EXEC_OUTPUT_BUDGET_BYTES
        (self.tree / "edge.txt").write_text("y" * (rule - 1) + "\n", encoding="utf-8")
        (self.tree / "over.txt").write_text("y" * rule + "\n", encoding="utf-8")
        (self.tree / "mid.txt").write_text("m" * 20999 + "\n", encoding="utf-8")

        def ec(cmd: str, extra: str = "", name: str = "r") -> str:
            return (f"const {name} = await tools.exec_command({{cmd: {json.dumps(cmd)}, "
                    f"login: false{extra}}});")

        low = ", max_output_tokens: 2000"
        high = ", max_output_tokens: 200000"
        script = "python3 -c \"for i in range(1,20001): print(f'P{i:05d} '+'x'*22)\""
        calls = [
            ec("cat big.txt", low) + "text(JSON.stringify({sw: r.output.startsWith("
            "'Warning: truncated output'), otc: r.original_token_count}));",
            ec("cat big.txt", low) + "text(JSON.stringify(r));",
            ec(script) + "text(r.output);",
            ec("cat big.txt", high) + "text(JSON.stringify({len: r.output.length}));",
            '// @exec: {"max_output_tokens": 200000}\n' + ec("cat big.txt", high)
            + "text(r.output);",
            ec("cat big.txt", low) + "text(r.output.replace(/Warning: truncated output[^\\n]*"
            "\\nTotal output lines: \\d+\\n\\n/, '').replace(/\\u2026\\d+ tokens "
            "truncated\\u2026/g, ''));",
            ec("cat edge.txt") + "text(r.output);",
            ec("cat over.txt") + "text(r.output);",
            ec("sed -n '9000,9100p' big.txt") + "text(r.output);",
            '// @exec: {"max_output_tokens": 1000}\n' + ec("cat mid.txt") + "text(r.output);",
        ]
        api = self._api([[_exec_call(js, n)] for n, js in enumerate(calls, 1)])
        res = self._launch(api, None)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        seen = [api.tool_output(n) for n in range(1, len(calls) + 1)]
        msg = ("the codex output cut moved: re-measure the codex reader row and the byte rule "
               "(pure_leaf.WORKSHOP_READER_TOOL_SURFACE_TEXT['codex_cli'], CODEX_*)")
        probe = json.loads(seen[0][seen[0].index("{"):seen[0].rindex("}") + 1])
        self.assertEqual(probe, {"sw": True, "otc": 150000}, msg)
        self.assertEqual(probe["otc"], -(-600000 // pure_leaf.CODEX_BYTES_PER_TOKEN), msg)
        self.assertIn("Warning: truncated output (original token count: 150000)\\n", seen[1])
        self.assertIn("Warning: truncated output", seen[2], msg)
        self.assertIn('"len":600000', seen[3], msg)
        self.assertNotIn("Warning: truncated output", seen[4], msg)
        self.assertRegex(seen[4], codex_rollout.CUT_MARKER_RE, msg)
        self.assertNotIn("L10000 ", seen[4], msg)
        self.assertFalse(codex_rollout.text_is_cut(seen[5]), msg)
        self.assertNotIn("L10000 ", seen[5], msg)
        self.assertNotIn("truncated", seen[6], msg)
        self.assertIn("(original token count: 10001)", seen[7], msg)
        self.assertNotIn("truncated", seen[8], msg)
        # Judged on the output item alone: `tool_output` joins it to the "Script completed"
        # item, which the size rule would count.
        cut = [codex_rollout.text_is_cut(s.split("Output:\n", 1)[1]) for s in seen]
        self.assertEqual(cut, [False, True, True, False, True, False, False, True, False, True],
                         msg)
        # (v): the reader over the real rollout counts the same texts.
        thread = self._thread_id(res.stdout)
        # The rollout keeps a raised budget's text WHOLE (its cut is applied on the way to the
        # model), so the reader counts it by its size; the clean residual stays uncounted.
        (rollout,) = codex_rollout.rollout_paths(self.config, thread)
        texts = [t for ln in rollout.read_text(encoding="utf-8").splitlines()
                 for row in [json.loads(ln)]
                 if str((row.get("payload") or {}).get("type") or "").endswith("_call_output")
                 for t in codex_rollout._texts(row["payload"]["output"])[-1:]]
        self.assertEqual(len(texts[4]), 600000, msg)
        self.assertFalse(codex_rollout.CUT_MARKER_RE.search(texts[4]), msg)
        self.assertEqual(len(texts[6]), rule, msg)
        # A launch that delegates nothing scans no delegation: a CLI that made an ordinary
        # call look like one would refuse every codex judge.
        self.assertEqual(self._rollout_scan(thread).delegations, 0, msg)
        self.assertEqual(self._rollout_counts(thread), (None, len(calls), sum(cut)), msg)
        # (vi): the host's capture, read as the conductor reads it.
        commands: list[tuple[str, int]] = []
        for line in res.stdout.splitlines():
            wc._codex_command_output(line, wc._parse_codex_line(line), commands)
        sizes = [size for _command, size in commands]
        self.assertEqual(sizes, [600000] * 6 + [rule, rule + 1, 101 * 30, 21000], msg)
        self.assertEqual([size > rule for size in sizes],
                         [True] * 6 + [False, True, False, False], msg)

    def test_mr6_a_read_helper_page_reaches_the_model_whole(self) -> None:
        """MR6 (issue #534 PR-J3-a): the host's reading helper, run as the codex reader row
        tells a judge to run it. (a) At the default budget a page of a 600,000-byte seed
        reaches the model whole — no header, no marker, and the rollout scan counts no cut —
        and the `next:` command it prints returns the following page; a `cat` of the same seed
        is still cut (the control that must be refused). (b) At the leaf-chosen
        `max_output_tokens: 7500` of codex closing run (3), a page of a Fortran seed and of a
        one-value-per-line JSON seed arrives whole, and codex reports `original_token_count`
        on the uncut result too: `ceil(bytes / CODEX_BYTES_PER_TOKEN)` for both (measured on
        codex-cli 0.164.0: 4,982 tokens for the 19,926-byte Fortran page), so the 4-byte token
        MR5-e (vii) measured at the default budget holds at the lowered one. If a page is cut
        here the page is too large at that budget (lower `CODEX_READ_ADVICE_BYTES`, not the
        rule)."""
        import re
        import subprocess as sp
        import sys as _sys

        from tools import codex_rollout, pure_leaf
        helper = pure_leaf.CODEX_READ_HELPER_NAME
        (self.tree / helper).write_text(pure_leaf.CODEX_READ_HELPER_TEXT, encoding="utf-8")
        lines = [f"L{i:05d} " + "x" * 22 for i in range(1, 20001)]
        (self.tree / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        model = (Path(__file__).parent / "data" / "channel_aliased_dependency_calls"
                 / "aliased_shallow_water2d_channel_model.f90").read_text(encoding="utf-8")
        (self.tree / "model.f90").write_text(model * 4, encoding="utf-8")
        (self.tree / "seed.json").write_text(wc._indented_json_seed(json.dumps(
            {"cases": [{"case_id": f"c{i:04d}", "residual": i * 1.0e-9, "status": "pass",
                        "metrics": {"l2": i / 7.0, "linf": i / 3.0}}
                       for i in range(2000)]})), encoding="utf-8")

        def page(*args: str) -> str:
            return sp.run([_sys.executable, helper, *args], cwd=self.tree, check=True,
                          capture_output=True, text=True).stdout

        first = page("big.txt")
        nxt = re.search(r"next: (python3 read\.py big\.txt \d+)$", first.rstrip("\n"))
        self.assertIsNotNone(nxt, first[-200:])
        second = page(*nxt.group(1).split()[2:])

        def ec(cmd: str, extra: str = "") -> str:
            return (f"const r = await tools.exec_command({{cmd: {json.dumps(cmd)}, "
                    f"login: false{extra}}});")

        low = ", max_output_tokens: 7500"
        probe = "text(JSON.stringify({otc: r.original_token_count ?? null, len: r.output.length}));"
        calls = [
            ec(f"python3 {helper} big.txt") + "text(r.output);",
            ec(nxt.group(1)) + "text(r.output);",
            ec("cat big.txt") + "text(r.output);",
            ec(f"python3 {helper} model.f90", low) + "text(r.output);",
            ec(f"python3 {helper} model.f90", low) + probe,
            ec(f"python3 {helper} seed.json", low) + "text(r.output);",
            ec(f"python3 {helper} seed.json", low) + probe,
        ]
        api = self._api([[_exec_call(js, n)] for n, js in enumerate(calls, 1)])
        res = self._launch(api, None)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        seen = [api.tool_output(n).split("Output:\n", 1)[1] for n in range(1, len(calls) + 1)]
        msg = ("a read.py page did not reach the model whole: lower "
               "pure_leaf.CODEX_READ_ADVICE_BYTES (the page), not the byte rule")
        self.assertEqual([codex_rollout.text_is_cut(s) for s in seen],
                         [False, False, True, False, False, False, False], msg)
        self.assertEqual(seen[0].rstrip("\n"), first.rstrip("\n"), msg)
        self.assertEqual(seen[1].rstrip("\n"), second.rstrip("\n"), msg)
        self.assertEqual(seen[3].rstrip("\n"), page("model.f90").rstrip("\n"), msg)
        self.assertEqual(seen[5].rstrip("\n"), page("seed.json").rstrip("\n"), msg)
        for n in (4, 6):
            got = json.loads(seen[n][seen[n].index("{"):seen[n].rindex("}") + 1])
            # Both seeds are ASCII, so `len` is the page's bytes; the count is `ceil(bytes /
            # CODEX_BYTES_PER_TOKEN)` at the lowered budget too, and under it.
            self.assertEqual(got["otc"], -(-got["len"] // pure_leaf.CODEX_BYTES_PER_TOKEN),
                             (n, got, msg))
            self.assertLess(got["otc"], 7500, (n, got, msg))
        thread = self._thread_id(res.stdout)
        self.assertEqual(self._rollout_counts(thread), (None, len(calls), 1), msg)
        commands: list[tuple[str, int]] = []
        for line in res.stdout.splitlines():
            wc._codex_command_output(line, wc._parse_codex_line(line), commands)
        rule = pure_leaf.CODEX_EXEC_OUTPUT_BUDGET_BYTES
        self.assertEqual([size > rule for _c, size in commands],
                         [False, False, True, False, False, False, False], msg)

    def test_mr5e_v_a_resumed_turn_is_scanned_on_its_own_rows(self) -> None:
        """MR5-e (v): `exec resume` appends to the thread's one rollout, and the reader scans
        the rows after its LAST `task_started` — a clean warm turn scans zero cut texts while
        the file still holds the cold turn's cut."""
        from tools import codex_rollout
        lines = [f"L{i:05d} " + "x" * 22 for i in range(1, 20001)]
        (self.tree / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        first = self._launch(self._api([[_exec_call(
            "text((await tools.exec_command({cmd: 'cat big.txt', login: false})).output);")]]),
            None)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        thread = self._thread_id(first.stdout)
        self.assertEqual(self._rollout_counts(thread), (None, 1, 1))
        second = self._launch(self._api([[_exec_call(
            "text((await tools.exec_command({cmd: \"sed -n '1,100p' big.txt\", "
            "login: false})).output);")]]), None, resume=thread)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        (rollout,) = codex_rollout.rollout_paths(self.config, thread)
        self.assertIn("tokens truncated", rollout.read_text(encoding="utf-8"))
        self.assertEqual(self._rollout_counts(thread), (None, 1, 0))

    def test_mr5f_a_sub_agent_s_read_is_in_neither_record_but_its_spawn_is_in_the_rollout(
            self) -> None:
        """MR5-f (issue #547, round 1): a reading judge is offered codex's `collaboration`
        tools, and a read it delegates to a sub-agent runs in ANOTHER thread — the parent's
        `--json` capture holds no `command_execution` of it and the parent's rollout no output
        of it, cut or not. What the parent's own rows do hold is the spawn call, which is what
        `codex_rollout.scan_launch` counts as a delegation (a read the judge has not made)."""
        lines = [f"L{i:05d} " + "x" * 22 for i in range(1, 20001)]
        (self.tree / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        spawn = {"type": "function_call", "id": "fc_1", "call_id": "call_s",
                 "name": "spawn_agent", "namespace": "collaboration",
                 "arguments": json.dumps({"task_name": "reader", "fork_turns": "none",
                                          "message": "cat big.txt and report"})}
        wait = {"type": "function_call", "id": "fc_2", "call_id": "call_w",
                "name": "wait_agent", "namespace": "collaboration",
                "arguments": json.dumps({"timeout_ms": 60000})}
        api = self._api_routed(
            [[spawn], [wait], [_exec_call(
                "await tools.exec_command({cmd: 'echo {} > review.json', login: false});"
                "text('ok');", 3)]],
            [[_exec_call("text((await tools.exec_command({cmd: 'cat big.txt', "
                         "login: false})).output);", 1)],
             [{"type": "message", "role": "assistant", "id": "cm",
               "content": [{"type": "output_text", "text": "FINAL: read whole"}]}]])
        res = self._launch(api, None)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        outputs = [item for body in api.requests() for item in body["input"]
                   if item.get("type") == "function_call_output"]
        # The wait must have been served, or the parent can end before the child's request
        # arrives (the row's own flake, measured under load before the arguments were right).
        self.assertNotIn("failed to parse", json.dumps(outputs))
        self.assertTrue(api.child_bodies, "the sub-agent never ran: the spawn shape moved "
                        f"({json.dumps(outputs)[:1500]})")
        commands: list[tuple[str, int]] = []
        for line in res.stdout.splitlines():
            wc._codex_command_output(line, wc._parse_codex_line(line), commands)
        self.assertNotIn(600000, [size for _c, size in commands])
        thread = self._thread_id(res.stdout)
        scan = self._rollout_scan(thread)
        self.assertEqual((scan.reason, scan.cut_texts), (None, 0))
        self.assertGreaterEqual(scan.delegations, 1)
        children = [p for p in sorted(self.config.glob("sessions/*/*/*/rollout-*.jsonl"))
                    if not p.name.endswith(f"-{thread}.jsonl")]
        self.assertTrue(children, "the sub-agent wrote no rollout of its own")

    def _api_routed(self, script, child_script) -> _ScriptedResponsesApi:
        api = _ScriptedResponsesApi(script, child_script)
        self.addCleanup(api.close)
        return api

    def test_mr5d_a_warm_repair_resumes_the_thread_and_rewrites_the_document(self) -> None:
        """MR5-d: `exec resume <thread>` with the reading argv continues the thread (the prior
        turn is in the request) under `sandbox_mode="workspace-write"`, and a command rewrites
        `review.json`."""
        first = self._launch(self._api([[_exec_call(
            "await tools.exec_command({cmd: 'echo {} > review.json', login: false});"
            "text('ok');")]]), None)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        thread = self._thread_id(first.stdout)
        self.assertTrue(self.conductor._workshop_session_resumable(self.paths, thread))
        api = self._api([[_exec_call(
            "await tools.exec_command({cmd: 'echo repaired > review.json', login: false});"
            "text('ok');")]])
        res = self._launch(api, None, resume=thread)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self._thread_id(res.stdout), thread)
        self.assertEqual((self.tree / "review.json").read_text(), "repaired\n")
        self.assertIn("Do the task.", json.dumps(api.requests()[0]["input"]))
        self.assertGreater(len(api.requests()[0]["input"]), 2)


if __name__ == "__main__":
    unittest.main()
