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

from tools import workflow_conductor as wc
from tools.orchestration_runtime import (
    _ensure_orchestration_audit_dirs,
    build_readonly_bwrap_profile,
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

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
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
            import os
            fstype = "none"
            for line in open("/proc/self/mountinfo"):
                fields = line.split()
                if fields[4] == os.getcwd():
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
                # ... while the leaf's OWN tmp root is still there: a codex pure launch needs it
                # for its `--output-schema` file and its TMPDIR.
                p = Path("workspace/tmp/{arid}/schema.json")
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
        d = Path(sys.argv[2]); home = d / "home" / "user"
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
                " ('TOOLS', 'tools/validate_pipeline_semantics.py')):\\n"
                "    print(tag + ':' + ('READABLE' if Path(rel).exists() else 'HIDDEN'), flush=True)\\n")
            import subprocess
            res = subprocess.run(
                ort.render_bwrap_command(profile=profile, command_argv=["python3", "-c", probe]),
                capture_output=True, text=True, timeout=60, check=False)
            print(res.stdout.strip() or f"PROBE FAILED rc={res.returncode} {res.stderr.strip()}")
    """)

    @staticmethod
    def _outer_bwrap(binds: list[tuple[Path, Path]]) -> list[str]:
        argv = ["bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        for src, dst in binds:
            argv += ["--bind", str(src), str(dst)]
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
        mount is accepted. The three EXEMPT-root rows are issue #227's round 1: the same
        bind below a root the spelling exemption used to skip entirely."""
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
            (exempt_repo / "workspace" / "orchestrations").mkdir(parents=True)
            (work / "alias").mkdir()
            (exempt_repo / "alias2").mkdir()
            (work / "npm" / "bin").mkdir(parents=True)
            (work / "npm" / "bin" / "cli-sim").write_text("#!/bin/sh\n", encoding="utf-8")
            (work / "npm" / "bin" / "cli-sim").chmod(0o755)
            exempt_child = [sys.executable, "-c", self._BIND_ALIAS_CHILD, str(repo_root), str(d),
                            str(exempt_repo)]
            cases = [
                ("root is the bind", child, [(repo / "workspace", tools)], tools / "bin", "REFUSED"),
                ("bind below the root", child, [(repo / "workspace", tools / "alias")],
                 tools / "bin", "REFUSED"),
                ("no mount (control)", child, [], tools / "bin", "ACCEPTED"),
                ("exempt root, bind below it outside the checkout", exempt_child,
                 [(exempt_repo / "workspace", work / "alias")], work / "npm" / "bin", "REFUSED"),
                ("exempt root, bind under the checkout's own path (control)", exempt_child,
                 [(exempt_repo / "workspace", exempt_repo / "alias2")], work / "npm" / "bin",
                 "ACCEPTED"),
                ("exempt root, no mount (control)", exempt_child, [], work / "npm" / "bin",
                 "ACCEPTED"),
            ]
            for label, argv, binds, path_dir, expected in cases:
                with self.subTest(case=label):
                    res = subprocess.run([*self._outer_bwrap(binds), "--", *argv, str(path_dir)],
                                         capture_output=True, text=True, timeout=120,
                                         check=False)  # the exit code is asserted below
                    self.assertEqual(res.returncode, 0, res.stderr)
                    self.assertTrue(res.stdout.startswith(expected), res.stdout)
                    if expected == "REFUSED":
                        self.assertIn("carries a mount", res.stdout)

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
        loopback stand-in: the CLI got past the git check, read its `--output-schema` file
        from the tmpfs-mounted `workspace/tmp/<arid>`, and reached the network.

        The redirect is a `--config model_provider` override, NOT `OPENAI_BASE_URL`: measured,
        the env variable is not honoured by codex-cli 0.154.0 with a private `CODEX_HOME`
        (the request went to `wss://api.openai.com`), and the profile's env allowlist would
        refuse the name anyway. A custom provider with no `env_key` sends no credential, so
        the row needs no `auth.json`.

        CONTROL: the same launch with the flag removed must NOT reach the stand-in and must
        name the refusal — that is what makes the flag load-bearing rather than decorative,
        and it is the row a revert of `leaf_command`'s hunk fails.
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
        without_flag = [tok for tok in with_flag if tok != "--skip-git-repo-check"]
        res = subprocess.run(render_bwrap_command(profile=profile, command_argv=without_flag),
                             input="Reply with one word.", capture_output=True, text=True,
                             timeout=180, check=False)  # the refusal exits 1 before any request
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--skip-git-repo-check was not specified", res.stderr, res.stdout)
        self.assertNotIn("thread.started", res.stdout)
        self.assertEqual(server.hits, ["POST /v1/responses"], "the control must add no hit")

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
