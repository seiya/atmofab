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

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _jobs() -> dict[str, dict]:
    """Every job. NOT "the one".

    An earlier version asserted there was exactly one, which made adding a second job — a lint
    job, say — fail twelve of these rows with a message about a helper rather than about the edit.
    Refusing an ordinary CI addition is the error direction this repository records as its default.
    """
    return _workflow()["jobs"]


def _steps_of(job: dict) -> list[dict]:
    """A job's steps, with LOCAL composite actions expanded.

    A `uses: ./.github/actions/<name>` step is an ordinary way to factor a workflow, and reading
    only inline `run:` made six of these rows refuse it — they simply stopped seeing the commands.
    A remote action cannot be read and is left as itself; what that costs is stated in
    `test_a_step_this_file_cannot_read_is_reported_rather_than_ignored`.
    """
    found: list[dict] = []
    for step in job.get("steps", []):
        uses = str(step.get("uses", ""))
        if uses.startswith("./"):
            action = REPO_ROOT / uses[2:] / "action.yml"
            if not action.is_file():
                action = REPO_ROOT / uses[2:] / "action.yaml"
            if action.is_file():
                inner = yaml.safe_load(action.read_text(encoding="utf-8"))
                found.extend(inner.get("runs", {}).get("steps", []))
                continue
        found.append(step)
    return found


def _all_steps() -> list[dict]:
    return [step for job in _jobs().values() for step in _steps_of(job)]


def _run_steps() -> list[str]:
    return [step["run"] for step in _all_steps() if "run" in step]


def _suite_steps() -> list[tuple[dict, dict]]:
    """(job, step) for every step that runs the suite."""
    return [(job, step) for job in _jobs().values() for step in _steps_of(job)
            if "pytest" in str(step.get("run", ""))]


def _pytest_ini_addopts() -> list[str]:
    """`addopts` as pytest will apply them.

    Read because the workflow's argv is only half the command. `-m "not slow"` added HERE
    deselects the slow rows on CI and locally, and an earlier version of this file — whose own
    docstring asserted CI runs the full set — stayed green while it did. Measured: collection
    dropped by 13 and every row here still passed.
    """
    import configparser
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    return parser.get("pytest", "addopts", fallback="").split()


class WorkflowDecisionTests(unittest.TestCase):
    """Each row is one decision, with the reason it was made."""

    def test_the_suite_command_is_the_one_this_repository_measures_with(self) -> None:
        """`tools/tests/`, quiet, and `-rs`.

        `-rs` is not diagnostics here: `tools/tests/test_skip_reasons_are_declared.py` requires
        every skip reason to be declared, and a skip that fires only on a runner is the one an
        operator never sees. It was how the sandbox skips were found on the first run.
        """
        suite = _suite_steps()
        self.assertEqual(len(suite), 1, f"expected exactly one pytest step, got {len(suite)}")
        command = suite[0][1]["run"]
        self.assertIn("tools/tests/", command)
        self.assertIn("-rs", command)

    def test_the_suite_step_can_actually_fail_the_job(self) -> None:
        """The row without which every other row here is decoration.

        Round 1 measured it: `continue-on-error: true` on the suite step, or an `if:` that never
        matches, leaves the job GREEN with the suite failed or never executed — and every other
        check in this file still passed, because the command string is untouched. A green PR check
        that certifies nothing is a check recorded as run that was not, which is the `leaf
        shortcut` shape performed on CI itself.
        """
        for job, step in _suite_steps():
            for owner, name in ((step, "the suite step"), (job, "the job")):
                self.assertNotIn(
                    "continue-on-error", owner,
                    f"{name} carries `continue-on-error`, so the suite cannot fail the run")
                self.assertNotIn(
                    "if", owner,
                    f"{name} is conditional, so the suite can be skipped while the run is green")

    def test_the_slow_marker_is_not_deselected_by_the_workflow_or_by_pytest_ini(self) -> None:
        """`pytest.ini` says CI runs the full set including `slow`, and says why: those tests are
        the only witnesses for the deadline, abandon and teardown properties.

        BOTH halves of the command, because the workflow's argv is only one of them. Round 1
        added `-m "not slow"` to `pytest.ini`'s `addopts` — deselecting thirteen tests on CI and
        locally — and this file, whose own docstring asserts CI runs the full set, stayed green.
        """
        for command in _run_steps():
            self.assertNotIn("not slow", command)
        addopts = _pytest_ini_addopts()
        self.assertNotIn("-m", addopts,
                         f"pytest.ini's addopts select a marker subset ({addopts}), so CI does "
                         "not run the full set its own comment says it does")
        self.assertNotIn("not slow", " ".join(addopts))

    def test_no_step_retries_the_suite(self) -> None:
        """`TODO.md` records an intermittent failure in this suite, and CI has seen one more. A
        retry would hide on a second machine exactly what a second machine is here to expose.

        Three spellings, because round 1 walked past the first version with two of them: an action
        whose name says retry; a plugin flag (`--reruns`, `--flake-finder`); and a second
        invocation in the same block, which needs no `||` — `cmd && exit 0` newline `cmd` is a
        retry.
        """
        for step in _all_steps():
            self.assertNotIn("retry", str(step.get("uses", "")).lower())
        for _job, step in _suite_steps():
            command = step["run"]
            for flag in ("--reruns", "--flake-finder", "--force-flaky", "--only-rerun"):
                self.assertNotIn(flag, command,
                                 f"the suite step passes {flag}, which retries a failure")
            self.assertEqual(
                command.count("pytest"), 1,
                "the suite step invokes pytest more than once, which is a retry however it is "
                f"spelled:\n{command}")
        # And the plugin cannot arrive through the declaration either.
        dev = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        self.assertNotIn("rerunfailures", dev)

    def test_the_job_has_a_timeout_well_under_the_platform_default(self) -> None:
        """The suite takes about 200s on the runner. The default ceiling is six hours, which is
        long enough that a hung job reads as an outage rather than a failure."""
        for name, job in _jobs().items():
            with self.subTest(job=name):
                minutes = job["timeout-minutes"]
                self.assertLessEqual(minutes, 60)
                self.assertGreaterEqual(
                    minutes, 10, "below this a slow runner fails for its speed rather than for a "
                    "defect; the suite alone takes about 200s there")

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
        for name, job in _jobs().items():
            with self.subTest(job=name):
                self.assertNotIn("latest", str(job["runs-on"]))

    def test_no_step_installs_a_second_interpreter(self) -> None:
        """The suite must run on the runner's SYSTEM python, because that is the one under `/usr`
        that the sandbox can reach. Re-adding `actions/setup-python` would reintroduce the failure
        above, and it would look like a tidy-up."""
        for step in _all_steps():
            self.assertNotIn("setup-python", str(step.get("uses", "")))

    def test_the_sandbox_restriction_is_lifted(self) -> None:
        """`docs/DEVELOPMENT.md` §Repository environment states that the sandbox IS covered by CI.
        That is true only because this step runs: without it the runner reported ten declared
        `bwrap / user namespaces not available` skips, and a skipped sandbox test is the one an
        operator never sees.

        The VALUE, and only on a live line. The first version asserted the setting's name appeared
        in some `run:`, which round 1 satisfied three ways — setting it back to `1`, and commenting
        the line out in two spellings — each returning the runner to the ten skips while this row
        and the document's "The sandbox IS covered" both stayed green.
        """
        settings = []
        for command in _run_steps():
            for line in command.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                stripped = re.split(r"(?:^|\s)#", stripped, maxsplit=1)[0]
                for match in re.finditer(
                        r"kernel\.apparmor_restrict_unprivileged_userns\s*=\s*(\d+)", stripped):
                    settings.append(match.group(1))
        self.assertTrue(
            settings,
            "no live command sets kernel.apparmor_restrict_unprivileged_userns, so the sandbox "
            "tests will skip while docs/DEVELOPMENT.md says CI covers them")
        self.assertEqual(
            set(settings), {"0"},
            f"the user-namespace restriction is set to {sorted(set(settings))}, not lifted")

    def test_the_dependencies_come_from_the_declaration(self) -> None:
        """Not a hand-written pip line: `requirements.txt` is where the measured versions live,
        and PR-1 exists so that no second copy of them can drift.

        Asked ACROSS the install commands, not of each one. The first version required both `-r`
        flags in every command containing `pip install`, which refused two ordinary edits round 1
        constructed: splitting the install into two steps, and a separate
        `pip install --upgrade pip` bootstrap. A `./` path prefix is accepted for the same reason.
        """
        installs = [r for r in _run_steps() if "pip install" in r]
        self.assertTrue(installs, "no step installs the Python dependencies")
        joined = " ".join(installs)
        for declaration in ("requirements.txt", "requirements-dev.txt"):
            with self.subTest(declaration=declaration):
                self.assertRegex(
                    joined, rf"(?:-r|--requirement)[= ]\.?/?{re.escape(declaration)}\b",
                    f"no step installs from {declaration}; the measured versions live there and "
                    "a hand-written package list is a second copy that can drift")

    def test_a_step_this_file_cannot_read_is_reported_rather_than_ignored(self) -> None:
        """The bound on every row above, stated because it is invisible otherwise.

        `_steps_of` expands a LOCAL composite action, so factoring the workflow that way keeps
        these checks working. A REMOTE action cannot be read at all — its steps are in another
        repository — so moving the suite, the sysctl or the install into one would leave the rows
        above green over a workflow whose commands this file never sees. This row makes that a
        failure that names the step instead of a silence.
        """
        opaque = [
            str(step["uses"]) for step in _all_steps()
            if "uses" in step and not str(step["uses"]).startswith(("./", "actions/"))]
        self.assertEqual(
            [], opaque,
            "the workflow uses a third-party action whose steps this file cannot read, so the "
            f"decisions checked above may no longer be in it: {opaque}")


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
        """The image and the interpreter version have to be stated TOGETHER.

        The first version checked `"Python 3.10" in document` and `runs-on in document` as two
        independent substring searches, which round 1 defeated by changing the image in both the
        workflow and the document while "Python 3.10" stayed true somewhere else in the file —
        leaving the sentence "on `ubuntu-24.04` whose SYSTEM interpreter is Python 3.10" false,
        and the sandbox tests broken the way commit 74206c8 measured. They must now appear in the
        same sentence, so the claim is checked as the claim rather than as two words.

        WHAT THIS CANNOT DO: decide what a given image's system interpreter actually IS. That is
        a property of GitHub's images, and the runner prints its own version every run, which is
        where a measurement belongs.
        """
        document = self._development()
        images = {str(job["runs-on"]) for job in _jobs().values()}
        for image in sorted(images):
            if "matrix" in image or "${{" in image:
                continue  # a matrix states its images elsewhere; the sentence check below applies
            with self.subTest(image=image):
                sentences = [s for s in re.split(r"(?<=[.!?])\s+", document) if image in s]
                self.assertTrue(
                    sentences,
                    f"docs/DEVELOPMENT.md does not name the runner image {image!r}, so a change "
                    "to one cannot be checked against the other")
                self.assertTrue(
                    any("Python 3.10" in s for s in sentences),
                    f"docs/DEVELOPMENT.md names {image!r} but not in a sentence that also states "
                    "the interpreter version, so the two can drift apart while both are 'present'")

    def test_the_document_states_the_command_the_workflow_runs(self) -> None:
        """A document that describes a command CI does not run is the drift PR #125 measured on an
        install line, in a different file."""
        suite = next(r for r in _run_steps() if "pytest" in r).strip()
        self.assertIn(suite, self._development())

    def test_the_uncovered_list_is_present_and_names_the_sandbox_as_covered(self) -> None:
        """The list is what stops a green check being read as more than it is. Its MEMBERS are
        prose — no test can decide what a workflow does not do — but its presence, and the one
        claim in it that this file can check, are not."""
        document = self._development().lower()  # the casing of prose is not the claim
        self.assertIn("does not cover", document)
        self.assertIn("the sandbox is covered", document)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
