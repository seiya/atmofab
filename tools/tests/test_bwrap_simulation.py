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
import unittest
from pathlib import Path

from tools.orchestration_runtime import (
    _ensure_orchestration_audit_dirs,
    build_readonly_bwrap_profile,
    render_bwrap_command,
)


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
    """P2-4b: the failure diagnostician runs under a read-only bwrap profile
    (`build_readonly_bwrap_profile`) — no capability, no write_roots. Confirm the
    rendered sandbox lets the leaf READ the repo and write tmp scratch, but BLOCKS any
    repo write (no write_roots → repo stays ro), so a read-only reasoning leaf is
    confined with nothing to attribute (FS-diff trivially empty)."""

    def _leaf_script(self, arid: str) -> str:
        return textwrap.dedent(f"""
            import socket
            from pathlib import Path
            def report(tag, ok, e=""):
                print(f"{{tag}}:{{'OK' if ok else 'FAIL'}}", e, flush=True)
            # repo is ro-bound -> reading a repo file works
            try:
                Path("AGENTS_SIM.md").read_text(); report("READ_REPO", True)
            except Exception as e:
                report("READ_REPO", False, repr(e))
            # tmp scratch (workspace/tmp/<arid>) is bound rw
            try:
                Path("workspace/tmp/{arid}/scratch.txt").write_text("x"); report("WRITE_TMP", True)
            except Exception as e:
                report("WRITE_TMP", False, repr(e))
            # a repo write must be blocked: no write_roots, repo stays read-only
            try:
                Path("AGENTS_SIM.md").write_text("mutated")
                print("WRITE_REPO:ALLOWED", flush=True)   # bad: confinement failed
            except Exception:
                print("WRITE_REPO:BLOCKED", flush=True)   # good
            try:
                socket.gethostbyname("api.anthropic.com"); report("DNS", True)
            except Exception as e:
                report("DNS", False, repr(e))
        """)

    def test_readonly_profile_reads_repo_blocks_repo_write(self) -> None:
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
                profile=profile, command_argv=["python3", "-c", self._leaf_script(arid)])
            out = _bwrap_stdout(cmd)
            self.assertIn("READ_REPO:OK", out, out)
            self.assertIn("WRITE_TMP:OK", out, out)
            self.assertIn("WRITE_REPO:BLOCKED", out, out)
            self.assertIn("DNS:OK", out, out)
            # the repo file is unchanged on the host
            self.assertEqual((repo / "AGENTS_SIM.md").read_text(), "orig\n")

    def test_readonly_profile_hides_workspace_from_the_leaf(self) -> None:
        """A read-only leaf cannot READ another run's records, or the producer's own reasoning.

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
            profile = build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id=orch, agent_run_id=arid,
                backend_command="python3", backend_type="codex")
            script = textwrap.dedent(f"""
                from pathlib import Path
                for tag, rel in (
                        ("DIALOG", "workspace/orchestrations/{orch}/agents/producer/dialogs/leaf.stdout.jsonl"),
                        ("SIBLING", "workspace/pipelines/sib/source/s1/src/sib_model.f90"),
                        ("ARCHIVE", "workspace_20260723/orchestrations/old/agents/p/dialogs/leaf.stdout.jsonl"),
                        ("RELEASES", "releases/component/rel_1/model.f90")):
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
            self.assertIn("OWN_TMP:WRITABLE", out, out)
            # The host's copies are untouched — this hides, it does not delete.
            self.assertEqual((dialogs / "leaf.stdout.jsonl").read_text(), "PRODUCER REASONING\n")

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
        repo = Path(sys.argv[3])
        os.environ["HOME"] = str(home)
        os.environ["PATH"] = f"{home}/work/npm/bin:" + os.environ["PATH"]
        ort._ensure_orchestration_audit_dirs(repo, "o")
        try:
            ort.build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id="o", agent_run_id="A",
                backend_command="cli-sim", backend_type="codex",
                backend_rw_override=[str(d / "ch")], env_overrides={"CODEX_HOME": str(d / "ch")})
        except ValueError as exc:
            print("REFUSED", exc)
        else:
            print("ACCEPTED")
    """)

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
            bindir = data_work / "npm" / "bin"
            bindir.mkdir(parents=True)
            (bindir / "cli-sim").write_text("#!/bin/sh\n", encoding="utf-8")
            (bindir / "cli-sim").chmod(0o755)
            (d / "home" / "user" / "work").mkdir(parents=True)
            (d / "ch").mkdir(mode=0o700)
            repo_root = Path(__file__).resolve().parents[2]
            outer = ["bwrap", "--bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                     "--bind", str(data_work), str(d / "home" / "user" / "work"), "--",
                     sys.executable, "-c", self._BIND_ALIAS_CHILD, str(repo_root), str(d)]
            refused = subprocess.run([*outer, str(data_work / "atmofab")],
                                     capture_output=True, text=True, timeout=120,
                                     check=False)  # the exit code is asserted below
            self.assertEqual(refused.returncode, 0, refused.stderr)
            self.assertIn("REFUSED", refused.stdout, refused.stdout)
            self.assertIn("is the same directory as", refused.stdout)
            accepted = subprocess.run([*outer, str(d / "home" / "user" / "work" / "atmofab")],
                                      capture_output=True, text=True, timeout=120,
                                      check=False)  # the exit code is asserted below
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(accepted.stdout.strip(), "ACCEPTED", accepted.stdout)

    def test_codex_cli_starts_inside_its_own_profile(self) -> None:
        self._assert_backend_cli_starts_inside_its_profile("codex", "codex", private_home=True)

    def test_claude_cli_starts_inside_its_own_profile(self) -> None:
        self._assert_backend_cli_starts_inside_its_profile("claude", "claude",
                                                          private_home=False)


if __name__ == "__main__":
    unittest.main()
