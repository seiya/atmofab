"""`.github/workflows/tests.yml`, and the documents that describe what it does.

Nothing read this file before it existed, and nothing reads a CI workflow by habit — which is
exactly the shape this repository has been bitten by twice: `docs/RUNBOOK.md`'s install line drifted
out of a declared version range with no test looking (PR #125), and `docs/DEVELOPMENT.md` §Setup
spelt two ranges nobody compared (issue #161 PR-1). A document that says what CI runs is the same
kind of statement, and this file is the same kind of answer.

What is checked here is the set of DECISIONS the workflow encodes — the ones issue #161's plan
argued for and a later edit could silently reverse — not the file's shape. Pinning the YAML would
refuse every legitimate change to it, which is the error direction this repository records as its
default.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _job() -> dict:
    jobs = _workflow()["jobs"]
    assert len(jobs) == 1, f"the workflow has {len(jobs)} jobs; this file reads the one"
    return next(iter(jobs.values()))


def _run_steps() -> list[str]:
    return [step["run"] for step in _job()["steps"] if "run" in step]


class WorkflowDecisionTests(unittest.TestCase):
    """Each row is one decision, with the reason it was made."""

    def test_the_suite_command_is_the_one_this_repository_measures_with(self) -> None:
        """`tools/tests/`, quiet, and `-rs`.

        `-rs` is not diagnostics here: `tools/tests/test_skip_reasons_are_declared.py` requires
        every skip reason to be declared, and a skip that fires only on a runner is the one an
        operator never sees. It was how the sandbox skips were found on the first run.
        """
        suite = [r for r in _run_steps() if "pytest" in r]
        self.assertEqual(len(suite), 1, f"expected exactly one pytest step, got {suite}")
        self.assertIn("tools/tests/", suite[0])
        self.assertIn("-rs", suite[0])

    def test_the_slow_marker_is_not_deselected(self) -> None:
        """`pytest.ini` says CI runs the full set including `slow`, and says why: those tests are
        the only witnesses for the deadline, abandon and teardown properties. A `-m "not slow"`
        here would make that sentence false and remove the only check of those properties."""
        for command in _run_steps():
            self.assertNotIn("not slow", command)

    def test_no_step_retries_the_suite(self) -> None:
        """`TODO.md` records an intermittent failure in this suite, and CI has now seen two. A
        retry would hide on a second machine exactly what a second machine is here to expose — so
        this refuses the two shapes a retry takes: an action whose name says retry, and a shell
        loop around the suite."""
        for step in _job()["steps"]:
            self.assertNotIn("retry", str(step.get("uses", "")).lower())
        for command in _run_steps():
            if "pytest" not in command:
                continue
            for spelling in ("for i in", "while ", "until ", "||"):
                self.assertNotIn(
                    spelling, command,
                    f"the suite step contains {spelling!r}, which is how a retry is spelled")

    def test_the_job_has_a_timeout_well_under_the_platform_default(self) -> None:
        """The suite takes about 200s on the runner. The default ceiling is six hours, which is
        long enough that a hung job reads as an outage rather than a failure."""
        minutes = _job()["timeout-minutes"]
        self.assertLessEqual(minutes, 60)
        self.assertGreaterEqual(minutes, 10, "below this a slow runner fails for its speed")

    def test_it_runs_on_a_pull_request_and_on_a_push_to_main_only(self) -> None:
        """`pull_request` builds the merge with `main`, which is what catches a branch that
        silently drops a check `main` makes. `push` on `main` catches a direct push, reachable
        because `main` carries no protection rule. NOT `push` on every branch: the same commit
        would run twice per PR and the two runs differ in `github.ref`, so no concurrency group
        folds them."""
        # PyYAML parses the bare key `on` as the boolean True.
        triggers = _workflow().get("on", _workflow().get(True))
        self.assertIn("pull_request", triggers)
        self.assertEqual(triggers["push"]["branches"], ["main"])

    def test_a_run_on_main_is_not_cancelled_by_a_later_one(self) -> None:
        """A `main` run is the record for that commit and nothing later reads it from anywhere
        else; a superseded branch push has a successor that will."""
        concurrency = _workflow()["concurrency"]
        self.assertIn("github.ref", concurrency["group"])
        self.assertIn("refs/heads/main", str(concurrency["cancel-in-progress"]))


class RunnerMatchesTheHostTests(unittest.TestCase):
    """The runner is chosen to BE the development host's kind of machine, not a second opinion.

    Two of these are the fixes CI itself forced, and both are one edit away from being undone by
    someone tidying the file.
    """

    def test_the_runner_image_is_pinned_rather_than_latest(self) -> None:
        """`ubuntu-latest` moved this job to 24.04, whose system Python is not the version every
        measurement here was taken on — and `actions/setup-python` then put the interpreter under
        `/opt/hostedtoolcache`, which the `bwrap` profile does not bind, so every sandbox test
        died with `libpython3.10.so.1.0: cannot open shared object file`. The image is pinned so
        that cannot come back silently."""
        self.assertNotIn("latest", _job()["runs-on"])

    def test_no_step_installs_a_second_interpreter(self) -> None:
        """The suite must run on the runner's SYSTEM python, because that is the one under `/usr`
        that the sandbox can reach. Re-adding `actions/setup-python` would reintroduce the failure
        above, and it would look like a tidy-up."""
        for step in _job()["steps"]:
            self.assertNotIn("setup-python", str(step.get("uses", "")))

    def test_the_sandbox_restriction_is_lifted(self) -> None:
        """`docs/DEVELOPMENT.md` §Repository environment states that the sandbox IS covered by CI.
        That is true only because this step runs: without it the runner reported ten declared
        `bwrap / user namespaces not available` skips, and a skipped sandbox test is the one an
        operator never sees."""
        self.assertTrue(
            any("apparmor_restrict_unprivileged_userns" in r for r in _run_steps()),
            "no step lifts the user-namespace restriction, so the sandbox tests will skip while "
            "docs/DEVELOPMENT.md says CI covers them")

    def test_the_dependencies_come_from_the_declaration(self) -> None:
        """Not a hand-written pip line: `requirements.txt` is where the measured versions live,
        and PR-1 exists so that no second copy of them can drift."""
        installs = [r for r in _run_steps() if "pip install" in r]
        self.assertTrue(installs, "no step installs the Python dependencies")
        for command in installs:
            self.assertIn("-r requirements.txt", command)
            self.assertIn("-r requirements-dev.txt", command)


class DocumentsDescribeThisWorkflowTests(unittest.TestCase):
    """The prose that tells a maintainer what CI does, checked against what it does.

    This is the class the round-0 mutation sweep asked for: the branch's documentation hunks all
    survived, which is the measurement, and these are the claims inside them that a machine can
    check. What stays prose — why each decision was made — is declared as such in the commit.
    """

    def _development(self) -> str:
        return (REPO_ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")

    def test_the_document_names_the_workflow_file(self) -> None:
        self.assertIn(".github/workflows/tests.yml", self._development())

    def test_the_document_states_the_interpreter_the_runner_uses(self) -> None:
        """It says Python 3.10, and the runner image has to be one whose system interpreter is
        that. The image name is the only machine-readable part of that claim, so what is checked
        is that the two are stated consistently — the runner's actual version is printed by the
        job itself, which is where a measurement belongs."""
        document = self._development()
        self.assertIn("Python 3.10", document)
        self.assertIn(_job()["runs-on"], document,
                      "docs/DEVELOPMENT.md does not name the runner image the workflow uses, so a "
                      "change to one cannot be checked against the other")

    def test_the_document_states_the_command_the_workflow_runs(self) -> None:
        """A document that describes a command CI does not run is the drift PR #125 measured on an
        install line, in a different file."""
        suite = next(r for r in _run_steps() if "pytest" in r).strip()
        self.assertIn(suite, self._development())

    def test_the_uncovered_list_is_present_and_names_the_sandbox_as_covered(self) -> None:
        """The list is what stops a green check being read as more than it is. Its MEMBERS are
        prose — no test can decide what a workflow does not do — but its presence, and the one
        claim in it that this file can check, are not."""
        document = self._development()
        self.assertIn("does NOT cover", document)
        self.assertIn("The sandbox IS covered", document)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
