"""Layer-1 bwrap simulation: the READ-ONLY leaf profile, under a real bwrap.

Fast (seconds, no LLM, no API) reproduction of what a leaf can and cannot reach under the
rendered profile `record_launch` writes. A synthetic "leaf" script runs inside the sandbox and
reports one line per operation, so each reachability claim is measured rather than argued.

Four larger simulations stood here until issue #171 PR-2: they drove the READ-WRITE profile
(`build_bwrap_profile`), which carried a capability's `write_roots` as `--bind` mounts, and
asserted that the terminal FS-diff attributed exactly the in-scope writes. Both halves are
gone — every CLI leaf is pure, launches read-only, and holds no write authority for a diff to
audit — so what is left is the one profile production builds.
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
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tools import workflow_conductor as wc
from tools.orchestration_runtime import (
    _ensure_orchestration_audit_dirs,
    build_readonly_bwrap_profile,
    codex_isolation_profile_kwargs,
    render_bwrap_command,
)
from tools.tests.llm_samples import sample_config_with


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


@unittest.skipUnless(_bwrap_usable(), "bwrap / user namespaces not available")
class BwrapReadonlyProfileTests(unittest.TestCase):
    """Every leaf runs under the one read-only bwrap profile (`build_readonly_bwrap_profile`)
    — no capability, no write_roots, and since issue #227 NO CHECKOUT: an empty tmpfs sits
    at `repo_root`. Confirm the rendered sandbox hides the repository from the leaf, keeps
    the leaf's own `workspace/tmp/<arid>` writable, keeps the network, and leaves the host's
    copy untouched, so a pure leaf is confined with nothing to read but its launch prompt
    and nothing to attribute (FS-diff trivially empty)."""

    def _leaf_script(self, arid: str, tmp_dir: str) -> str:
        return textwrap.dedent(f"""
            import socket
            from pathlib import Path
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
            import os, re
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

        `CWD_ENTRIES` is the whole listing of the cwd from inside: exactly `workspace`, the
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
            profile = build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id=orch, agent_run_id=arid,
                backend_command="python3", backend_type="claude")
            self.assertTrue(profile.get("readonly"))
            self.assertEqual(profile.get("write_roots"), [])
            cmd = render_bwrap_command(
                profile=profile,
                command_argv=["python3", "-c", self._leaf_script(arid, profile["tmp_dir"])])
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
        """A read-only leaf cannot READ another run's records, the producer's own reasoning,
        or the source trees — the checkout is not in the sandbox (issue #227).

        FOUND BY THE ROUND-2 CODEX REVIEW (P1) and by the round-1 security axis before it. A
        CLAUDE pure leaf is tool-free and could read nothing anyway; a CODEX pure leaf is
        `codex exec --sandbox read-only` — tool-BEARING — and until Z4 (issue #171) its reads
        were refused by the leaf hook layer against the empty `allowed_read_roots` this profile
        still records. Deleting that layer left the read boundary to nothing, and the sharpest
        gain is here: a VERIFY leaf reading the producer's `dialogs/leaf.stdout.jsonl` for the
        run it is reviewing can reuse the producer's conclusions instead of reviewing the
        supplied context, which defeats the persona separation `_run_pure_verify_substep` calls
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
            profile = build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id=orch, agent_run_id=arid,
                backend_command="python3", backend_type="codex")
            script = textwrap.dedent(f"""
                from pathlib import Path
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

    def _assert_backend_cli_starts_inside_its_profile(
        self, backend_type: str, cli: str, *, private_home: bool,
    ) -> None:
        """Exec the operator's REAL backend CLI, by name, under the profile production renders.

        The ro bind of the CLI's install root is what `_backend_runtime_bind_paths` exists
        for, and until issue #226 it was complete for one install shape only: a `claude`
        literal, and nothing for a `codex` behind a volta shim (measured: rc=7, `Volta
        update error`, because the shim needs `~/.volta/tools` and `~/.volta/layout.*`
        beside `~/.volta/bin`). The rule is now shape-delimited, and a unit row over a
        synthetic tree cannot say whether the REAL install is covered — only the real CLI
        under real bwrap can, which is why this row does not fake the shim.

        The rw half follows production's shape per backend, because the rw half is where a
        row can leave something behind in the operator's home. A codex launch passes
        `codex_isolation_profile_kwargs`: a private `CODEX_HOME` bound rw INSTEAD of
        `~/.codex` (the operator's `~/.codex` is never bound rw in production); the row
        passes the same two kwargs with a home under its own tempdir — measured, `codex
        --version` creates `tmp/arg0/codex-arg0*/` under whichever `CODEX_HOME` it runs
        with, which the first version of this row left in the operator's real `~/.codex`.
        (The auth / config ro mappings production adds are not needed by `--version`.) A
        claude launch takes the default rw set, `~/.claude` + `~/.claude.json`, and the row
        does the same; measured over repeated runs with a `find -newer` marker, `claude
        --version` under the profile leaves nothing new there (an operator's own
        interactive session writes `~/.claude.json` concurrently, so one probe is not a
        measurement). The CLI is launched by NAME through the host PATH the profile env carries. `_bwrap_stdout`
        raises with bwrap's stderr and exit code, so a regression reads as the CLI's own
        start-up error. On a host carrying neither CLI both rows skip, and the criterion is
        unmeasured there.
        """
        if shutil.which(cli) is None:
            self.skipTest("backend CLI not installed on this host")
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as h:
            repo = Path(t).resolve()
            orch, arid = f"orch_{cli}", f"arid_{cli}"
            _ensure_orchestration_audit_dirs(repo, orch)
            kwargs: dict = {}
            if private_home:
                home = Path(h).resolve() / "home"
                home.mkdir(mode=0o700)
                kwargs = {"backend_rw_override": [str(home)],
                          "env_overrides": {"CODEX_HOME": str(home)}}
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
        try:
            profile = ort.build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id="o", agent_run_id="A",
                backend_command="cli-sim", backend_type="codex",
                backend_rw_override=[str(d / "ch")], env_overrides={"CODEX_HOME": str(d / "ch")})
        except ValueError as exc:
            print("REFUSED", exc)
        else:
            print("ACCEPTED")
            # An ACCEPTED root is exempt because its spelling contains the checkout and the
            # tmpfs stacks on top: observe that under the rendered profile (a nested bwrap)
            # rather than take the docstring's word for it. Both the run artifact and the
            # gate's implementation are read at the ALIAS the root exposes.
            probe = (
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
        self._assert_backend_cli_starts_inside_its_profile("codex", "codex", private_home=True)

    def test_claude_cli_starts_inside_its_own_profile(self) -> None:
        self._assert_backend_cli_starts_inside_its_profile("claude", "claude",
                                                          private_home=False)

    # ---- issue #227: the pure launch reaches its first API request from the EMPTY cwd ----

    def _pure_launch_under_profile(self, backend: str, *, model: str = "",
                                   private_home: bool) -> tuple[list[str], dict, Path]:
        """The production argv (`Conductor.leaf_command`) and profile for one pure launch.

        Returns `(argv, profile, repo)`; the caller owns the tempdirs through `addCleanup`.
        """
        if shutil.which(backend) is None:
            self.skipTest("backend CLI not installed on this host")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name).resolve() / "repo"
        repo.mkdir()
        # A UUID, as production's child ids are: the claude argv passes it as `--session-id`,
        # which the CLI validates before anything else.
        orch, arid = f"orch_{backend}_lb", str(uuid.uuid4())
        _ensure_orchestration_audit_dirs(repo, orch)
        conductor = wc.Conductor(
            repo_root=repo, orchestration_id=orch, orchestration_agent_run_id="ORCH",
            llm_config=sample_config_with(backend, agent_model=model), env={})
        argv = conductor.leaf_command(session_id=arid)
        kwargs: dict = {}
        if private_home:
            home = Path(tmp.name).resolve() / "home"
            home.mkdir(mode=0o700)
            # What production's private `CODEX_HOME` carries for this checkout: the
            # untrusted marker, keyed on the same `repo_root` spelling the sandbox chdirs to.
            (home / "config.toml").write_text(
                f'[projects."{repo}"]\ntrust_level = "untrusted"\n', encoding="utf-8")
            kwargs = {"backend_rw_override": [str(home)],
                      "env_overrides": {"CODEX_HOME": str(home)}}
        profile = build_readonly_bwrap_profile(
            repo_root=repo, orchestration_id=orch, agent_run_id=arid,
            backend_command=backend, backend_type=backend, **kwargs)
        return argv, profile, repo

    def test_codex_pure_launch_reaches_the_api_from_the_empty_cwd(self) -> None:
        """UNBILLED measurement of what issue #227 costs the codex launch, under real bwrap.

        The sandbox holds no checkout, so `codex exec` starts in an EMPTY, non-git cwd — which
        codex refuses before any API call unless `--skip-git-repo-check` is passed (measured
        on codex-cli 0.154.0: `Not inside a trusted directory and --skip-git-repo-check was
        not specified.`, rc 1). The production argv carries the flag; this row execs the real
        CLI with that argv under the rendered profile and observes the request arriving at a
        loopback stand-in: the CLI got past the git check and reached the network.

        The redirect is a `--config model_provider` override, NOT `OPENAI_BASE_URL`: measured,
        the env variable is not honoured by codex-cli 0.154.0 with a private `CODEX_HOME`
        (the request went to `wss://api.openai.com`), and the profile's env allowlist would
        refuse the name anyway. A custom provider with no `env_key` sends no credential, so
        the row needs no `auth.json`.

        CONTROL: the same launch with the flag removed must NOT reach the stand-in and must
        name the refusal — that is what makes the flag load-bearing rather than decorative,
        and it is the row a revert of `leaf_command`'s hunk fails.

        The request BODY is read too: with `--output-schema` gone (issue #230) it carries no
        `text.format` — the strict `json_schema` format codex-cli built from the host's
        `{"type": "object"}` is what the Responses API refused on every pure codex turn. The
        body is the wire-level observation; the argv assertion in `test_pure_leaf.py` is the
        source-level one, and a revert of `leaf_command`'s schema hunk fails both.
        """
        argv, profile, _repo = self._pure_launch_under_profile(
            "codex", model="gpt-5.6-sol", private_home=True)
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
        self.assertEqual(server.hits, ["POST /v1/responses"], res.stdout + res.stderr)
        self.assertNotIn("skip-git-repo-check", res.stderr)
        request = json.loads(server.bodies[0])
        self.assertNotIn("format", request.get("text") or {}, request.get("text"))
        self.assertNotIn("json_schema", server.bodies[0].decode("utf-8", "replace"))
        self.assertNotIn("--output-schema", with_flag)
        without_flag = [tok for tok in with_flag if tok != "--skip-git-repo-check"]
        res = subprocess.run(render_bwrap_command(profile=profile, command_argv=without_flag),
                             input="Reply with one word.", capture_output=True, text=True,
                             timeout=180, check=False)  # the refusal exits 1 before any request
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--skip-git-repo-check was not specified", res.stderr, res.stdout)
        self.assertNotIn("thread.started", res.stdout)
        self.assertEqual(server.hits, ["POST /v1/responses"], "the control must add no hit")

    # ---- issue #245: one codex lineage's rollout is unreachable from another lineage ----

    _LOOPBACK_OVERRIDES = ("--config", 'model_provider="loopback"',
                           "--config", 'model_providers.loopback.name="loopback"',
                           "--config", 'model_providers.loopback.wire_api="responses"')

    def _codex_lineage_fixture(self) -> tuple[Path, str, wc.Conductor, _Loopback400]:
        """A repo whose codex launches go through the REAL preparer, plus a loopback server.

        The homes root is the suite's redirected `ATMOFAB_WORKFLOW_HOMES_ROOT` and the
        operator credential is this test's own (`seed_codex_auth`), so the container this
        row walks is the one production would build — not a hand-made home, which is what
        `_pure_launch_under_profile` uses and which cannot tell a shared home from a
        per-lineage one.
        """
        if shutil.which("codex") is None:
            self.skipTest("backend CLI not installed on this host")
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
        """Exec the real CLI with the production argv under the profile a prepared home
        implies (`codex_isolation_profile_kwargs`, the same spelling `record_launch` uses)."""
        argv = conductor.leaf_command(session_id=arid, resume_session_id=resume_session_id)
        self.assertEqual(argv[-2:], ["--json", "-"])
        overrides = [*self._LOOPBACK_OVERRIDES,
                     "--config", f'model_providers.loopback.base_url="{server.base_url}/v1"']
        profile = build_readonly_bwrap_profile(
            repo_root=repo, orchestration_id=orch, agent_run_id=arid,
            backend_command="codex", backend_type="codex",
            **codex_isolation_profile_kwargs(isolation))
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
        profile production renders for it: a recursive search of its own `$CODEX_HOME` for
        the marker finds nothing, the container is not listable, and A's rollout file — at
        its exact host path — is ENOENT. The sibling is not hidden; it is not mounted.
        """
        from tools.orchestration_runtime import (
            _prepare_codex_workflow_home,
            codex_isolation_profile_kwargs,
        )
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
            print("HOME_IS_OWN:" + str(home == Path({str(home_b)!r})), flush=True)
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
            **codex_isolation_profile_kwargs(iso_b))
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
            **codex_isolation_profile_kwargs(iso_a))
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
        (`codex_lineage_home_missing`) rather than be created: a created-empty home would
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
        """UNBILLED: the claude pure leaf takes the same profile (one profile for both
        backends — the decision recorded on issue #227), so it too starts from an empty cwd.
        A tool-free `claude -p --safe-mode --tools ""` needs nothing from the checkout; this
        row execs the real CLI with the production argv under the rendered profile and
        observes its first request at a loopback stand-in. `ANTHROPIC_BASE_URL` is set by an
        `env` prefix INSIDE the sandbox because the profile's env allowlist refuses the name
        (it is the redirect the allowlist exists to close); `--safe-mode` reads no settings
        layer, so the env variable is the only redirect available and it is honoured (the
        repository's `measure_claude_tool.py` pattern).
        """
        argv, profile, _repo = self._pure_launch_under_profile("claude", private_home=False)
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


if __name__ == "__main__":
    unittest.main()
