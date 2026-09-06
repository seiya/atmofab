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

SCOPE, stated because two review rounds spent themselves discovering it. This file is REGRESSION
PREVENTION against ordinary spellings, not enforcement against someone routing around it. Round 1
found four ways past its first version and round 2 found six past its second, each one a different
spelling of the same act — and every one of them requires editing `.github/workflows/tests.yml`,
`pytest.ini` or `requirements-dev.txt`, which only the operator can do (`_write_roots_for_launch`
grants a leaf no authority over any of them, and no leaf reads them). So the party this could
defend against is the author, and the honest form of that is a guard against the edit someone makes
without thinking, not a fence around a file they own.

The one place that is NOT a matter of discipline is pytest's effective configuration, because there
the inputs are a CLOSED set the tool documents: the command line, `pytest.ini`'s `addopts`, and the
`PYTEST_ADDOPTS` environment variable. All three are read below. What stays out of reach is a
`conftest.py` edit (`collect_ignore_glob` and friends), which is Python and cannot be bounded by
reading, and a plugin that changes behaviour without a flag.
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
                for nested in inner.get("runs", {}).get("steps", []):
                    # The WRAPPER's gating keys travel with the expansion. GitHub applies a
                    # step-level `continue-on-error` / `if` to the whole composite action, and the
                    # first version of this expansion dropped them — so `continue-on-error: true`
                    # on a `uses: ./…` wrapper made the suite unable to fail the job while every
                    # row here passed. That defeat was introduced by the SAME commit that added the
                    # row it defeated (round 2 measured it).
                    merged = dict(nested)
                    for key in ("continue-on-error", "if"):
                        if key in step:
                            merged[key] = step[key]
                    found.append(merged)
                continue
        found.append(step)
    return found


def _all_steps() -> list[dict]:
    return [step for job in _jobs().values() for step in _steps_of(job)]


def _run_steps() -> list[str]:
    return [step["run"] for step in _all_steps() if "run" in step]


#: An INVOCATION of pytest at the head of a command, not the word appearing anywhere. The guard
#: step names a `pytest-report.xml` and its body contains `for c in cases`; matching the word made
#: the retry row report that step as a retry loop, and the reference finder count the guard's own
#: comment as a second collection. Both were false positives in code added the same hour.
_PYTEST_INVOCATION = re.compile(r"(?:^|[\s;&|(])(?:python[\d.]*\s+-m\s+)?pytest(?=\s|$)")


def _command_lines(run: str) -> str:
    """`run` with comment lines dropped, so prose about a command is not read as one."""
    return "\n".join(line for line in str(run).splitlines()
                      if not line.strip().startswith("#"))


def _invokes_pytest(run: str) -> bool:
    return bool(_PYTEST_INVOCATION.search(_command_lines(run)))


def _pytest_steps() -> list[tuple[dict, dict]]:
    """(job, step) for every step that invokes pytest at all — including a coverage pass."""
    return [(job, step) for job in _jobs().values() for step in _steps_of(job)
            if _invokes_pytest(str(step.get("run", "")))]


def _suite_steps() -> list[tuple[dict, dict]]:
    """(job, step) for the steps that run THE SUITE — pytest over the whole `tools/tests/`.

    ONE definition, because there were two and they disagreed. A `pytest` step running a single
    file (a coverage pass, a smoke subset) is not the suite, and a row that took "the first step
    whose command contains pytest" refused adding one — round 2's over-refusal probe found it in
    the document row after the same distinction had been fixed in another.
    """
    return [(job, step) for job, step in _pytest_steps()
            if _runs_the_suite(str(step["run"]))]


def _runs_the_suite(command: str) -> bool:
    """Does `command` run the whole suite?

    Either it names one of `pytest.ini`'s `testpaths`, or it passes NO path at all — in which case
    pytest uses `testpaths`, which that file documents must behave like `pytest tools/tests`.
    Requiring the literal directory refused that second form, an ordinary edit the configuration
    explicitly supports.
    """
    command = _command_lines(command).strip()
    if not _invokes_pytest(command):
        return False
    # A COLLECTION is not a run. The workflow's guard takes a reference count with
    # `--collect-only`, and reading that as a second suite made four rows fail on the correct
    # workflow. `--co` is the short spelling.
    if re.search(r"(?:^|\s)--(?:collect-only|co)(?:\s|$)", command):
        return False
    tokens = [re.escape(path.rstrip("/")) for path in _pytest_ini_testpaths()]
    if tokens and re.search(rf"(?:^|\s)(?:{'|'.join(tokens)})/?(?:\s|$)", command):
        return True
    return bool(re.search(r"pytest(?:\s+-[^\s]+)*\s*$", command))


def _pytest_ini_testpaths() -> list[str]:
    import configparser
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    return parser.get("pytest", "testpaths", fallback="").split()


def _suite_job_names() -> set[str]:
    """The NAMES of the jobs that run the suite.

    Names rather than object identity, for the reason the timeout row records: `_workflow()`
    re-parses on every call, so two `job` dicts are never the same object.
    """
    # Computed in ONE traversal. Comparing `id(step)` across two `_jobs()` calls returns the
    # empty set — `_workflow()` re-parses every time — which is the same mistake the timeout row
    # above records, made one level down while fixing it. Found by asserting the result was
    # non-empty, which the first version was not.
    names = {name for name, job in _jobs().items()
             for step in _steps_of(job)
             if _runs_the_suite(str(step.get("run", "")))}
    assert names, "no job runs the suite; every row keyed on this would silently check nothing"
    return names


def _exported_environment() -> list[tuple[str, str]]:
    """(where, line) for every `NAME=value >> $GITHUB_ENV` a step writes.

    The workflow can SET the third pytest input as well as declare it, and reading only static
    `env:` blocks missed that — the file's own install step already writes `$GITHUB_PATH` two
    lines away, so this is a spelling a maintainer has in front of them.
    """
    found: list[tuple[str, str]] = []
    for job_name, job in _jobs().items():
        for index, step in enumerate(_steps_of(job)):
            for line in str(step.get("run", "")).splitlines():
                if "GITHUB_ENV" in line:
                    found.append((f"job {job_name} step {index}", line.strip()))
    return found


def _pytest_ini_addopts() -> list[str]:
    """`addopts` as pytest will apply them.

    Read because the workflow's argv is only one of three inputs. `-m "not slow"` added HERE
    deselects the slow rows on CI and locally, and an earlier version of this file — whose own
    docstring asserted CI runs the full set — stayed green while it did. Measured: collection
    dropped by 13 and every row here still passed.
    """
    import configparser
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    return parser.get("pytest", "addopts", fallback="").split()


def _environment_blocks() -> list[tuple[str, dict]]:
    """Every `env:` mapping in the workflow, with where it was found.

    The THIRD input, and the one two versions of this file did not read. `PYTEST_ADDOPTS` set at
    workflow, job or step level reverses the two decisions this file exists to hold — round 2
    measured `-m 'not slow'`, `--ignore=` and `-k` arriving that way, all green.
    """
    found: list[tuple[str, dict]] = [("workflow", _workflow().get("env", {}) or {})]
    for name, job in _jobs().items():
        found.append((f"job {name}", job.get("env", {}) or {}))
        for index, step in enumerate(_steps_of(job)):
            found.append((f"job {name} step {index}", step.get("env", {}) or {}))
    return found


#: Anything that makes pytest run a SUBSET. Not a list of flags someone might use — the set of
#: selection mechanisms pytest documents, which is what makes this closed rather than a guess.
_SELECTION_FLAGS = ("-m", "-k", "--ignore", "--ignore-glob", "--deselect", "--last-failed",
                    "--failed-first", "--stepwise", "--collect-only", "--co")

#: Anything that re-runs a failure. Also closed: these are the plugins that exist.
_RETRY_FLAGS = ("--reruns", "--retries", "--flake-finder", "--force-flaky", "--only-rerun")
_RETRY_DISTRIBUTIONS = ("rerunfailures", "pytest-retry", "flaky", "flakefinder")


def _selection_or_retry_in(text: str) -> list[str]:
    """The selection/retry flags in `text`, which must be PYTEST's arguments and not a shell's.

    Everything after the `pytest` token, because `python3 -m pytest` carries the interpreter's own
    `-m` and a naive scan reports it as a marker filter. Measured while writing this: it did, and
    the row failed on the correct command.
    """
    words = text.split()
    if "pytest" in words:
        words = words[words.index("pytest") + 1:]
    arguments = " ".join(words)
    return [flag for flag in (*_SELECTION_FLAGS, *_RETRY_FLAGS)
            if re.search(rf"(?:^|\s){re.escape(flag)}(?:[=\s]|$)", arguments)]


class WorkflowDecisionTests(unittest.TestCase):
    """Each row is one decision, with the reason it was made."""

    def test_the_suite_command_is_the_one_this_repository_measures_with(self) -> None:
        """`tools/tests/`, quiet, and `-rs`.

        `-rs` is not diagnostics here: `tools/tests/test_skip_reasons_are_declared.py` requires
        every skip reason to be declared, and a skip that fires only on a runner is the one an
        operator never sees. It was how the sandbox skips were found on the first run.
        """
        suite = _suite_steps()
        self.assertEqual(
            len(suite), 1,
            f"expected exactly one step running the SUITE, got {len(suite)}. A second pytest step "
            "that runs something else — a coverage pass, a smoke subset over one file — is not "
            "this row's business, because it does not take the DIRECTORY as its argument.")
        command = suite[0][1]["run"]
        self.assertIn("-rs", command)

    def test_the_run_time_guard_exists_and_always_runs(self) -> None:
        """The guard is what answers the question this file kept failing to answer by reading.

        Three review rounds each found the next spelling a YAML reader did not model — a wrapper's
        `continue-on-error`, `PYTEST_ADDOPTS` through `$GITHUB_ENV`, `-m 'not slow'` in `addopts`,
        `|| true` copied from two lines above. So the workflow now asks the RUN: a step compares
        what the suite executed against what a pristine configuration collects, and fails if the
        report is missing, short, or carries failures. Every one of those bypasses changes one of
        those three facts.

        `if: always()` is the load-bearing part: the failures it answers are exactly the ones where
        the suite step reports success, so a guard that inherits the default `if` would be skipped
        alongside it.
        """
        steps = _all_steps()
        guards = [s for s in steps if "pytest-report.xml" in _command_lines(str(s.get("run", "")))
                  and "junitxml" not in _command_lines(str(s.get("run", "")))]
        self.assertEqual(
            len(guards), 1,
            "the workflow has no step reading the suite's report; nothing then notices a suite "
            "that was skipped, deselected or had its failure swallowed")
        guard = guards[0]
        self.assertEqual(
            str(guard.get("if", "")).strip(), "always()",
            "the guard is not `if: always()`, so it is skipped by exactly the conditions it "
            f"exists to catch (found {guard.get('if')!r})")
        body = str(guard["run"])
        for property_checked in ("!= expected", "if failures", "did not run"):
            self.assertIn(
                property_checked, body,
                f"the guard no longer checks {property_checked!r}; it answers three questions — "
                "did the suite run, did it run everything, did it pass — and each is load-bearing")

    def test_the_guard_s_reference_is_taken_under_a_pristine_configuration(self) -> None:
        """The reference must not go through the configuration it is checking.

        If it inherited `PYTEST_ADDOPTS` or `pytest.ini`'s `addopts`, a subset selected there would
        shrink BOTH numbers and the comparison would hold — the guard would agree with the thing it
        exists to detect.
        """
        references = [_command_lines(s["run"]) for s in _all_steps()
                      if _invokes_pytest(str(s.get("run", "")))
                      and "--collect-only" in _command_lines(str(s.get("run", "")))]
        self.assertEqual(len(references), 1, "expected exactly one reference collection step")
        reference = references[0]
        self.assertIn("-o addopts=", reference,
                      "the reference inherits pytest.ini's addopts, so a subset selected there "
                      "shrinks both sides of the comparison")
        self.assertIn("-u PYTEST_ADDOPTS", reference,
                      "the reference inherits PYTEST_ADDOPTS, so a subset selected there shrinks "
                      "both sides of the comparison")

    def test_the_suite_step_can_actually_fail_the_job(self) -> None:
        """The row without which every other row here is decoration.

        Round 1 measured it: `continue-on-error: true` on the suite step, or an `if:` that never
        matches, leaves the job GREEN with the suite failed or never executed — and every other
        check in this file still passed, because the command string is untouched. A green PR check
        that certifies nothing is a check recorded as run that was not, which is the `leaf
        shortcut` shape performed on CI itself.
        """
        suite = _suite_steps()
        self.assertTrue(
            suite,
            "no step runs the suite, so this row has nothing to check — its non-vacuity used to "
            "rest entirely on an assertion in a DIFFERENT test")
        for job, step in suite:
            for owner, name in ((step, "the suite step"), (job, "the job")):
                if owner.get("continue-on-error") not in (None, False):
                    self.fail(f"{name} carries `continue-on-error: "
                              f"{owner['continue-on-error']}`, so the suite cannot fail the run")
                self.assertNotIn(
                    "if", owner,
                    f"{name} is conditional, so the suite can be skipped while the run is green")
            # And it must not swallow its own failure IN THE SHELL. This is not an adversarial
            # spelling here: the workflow uses `|| true` twice as a deliberate idiom, each with a
            # comment recommending it, so a maintainer copying that onto the suite step is one
            # line away. Round 3 measured `|| true`, `; true`, `|| echo failed` and
            # `set +e … exit 0` all leaving the job green with the suite failed.
            command = str(step["run"])
            for swallow in ("||", ";", "set +e", "set +o errexit", "exit 0"):
                self.assertNotIn(
                    swallow, command,
                    f"the suite command contains {swallow!r}, which can swallow the failure the "
                    f"whole job exists to report:\n{command}")

    def test_the_slow_marker_is_not_deselected_by_the_workflow_or_by_pytest_ini(self) -> None:
        """`pytest.ini` says CI runs the full set including `slow`, and says why: those tests are
        the only witnesses for the deadline, abandon and teardown properties.

        BOTH halves of the command, because the workflow's argv is only one of them. Round 1
        added `-m "not slow"` to `pytest.ini`'s `addopts` — deselecting thirteen tests on CI and
        locally — and this file, whose own docstring asserts CI runs the full set, stayed green.
        """
        for _job, step in _suite_steps():
            found = _selection_or_retry_in(step["run"])
            self.assertEqual([], found, f"the suite command selects a subset: {found}")
        found = _selection_or_retry_in(" ".join(_pytest_ini_addopts()))
        self.assertEqual(
            [], found,
            f"pytest.ini's addopts select a subset ({found}), so CI does not run the full set its "
            "own comment says it does — and neither does a local run")
        for where, block in _environment_blocks():
            for name, value in block.items():
                if name != "PYTEST_ADDOPTS":
                    continue
                found = _selection_or_retry_in(str(value))
                self.assertEqual(
                    [], found,
                    f"PYTEST_ADDOPTS at {where} selects a subset ({found}); it is the third input "
                    "to pytest's effective command and reverses this decision invisibly")
        # A step can SET that input as well as declare it, and reading only static `env:` missed
        # it — the install step writes `$GITHUB_PATH` two lines away, so the spelling is already
        # in front of whoever edits this file.
        for where, line in _exported_environment():
            self.assertNotIn(
                "PYTEST_ADDOPTS", line,
                f"{where} exports PYTEST_ADDOPTS through $GITHUB_ENV ({line!r}); that is the same "
                "input as an `env:` block and reverses this decision the same way")

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
        for _job, step in _pytest_steps():
            command = _command_lines(step["run"])
            # The LOOP spelling, restored. `2b33145` refused `for i in`, `while `, `until ` and
            # `||` in the suite command; `e725500` replaced that with the count below, which a
            # loop containing ONE invocation walks past — measured red-then-GREEN by round 3's
            # disclosure axis, and the retry ban is the branch's stated reason for existing.
            for spelling in ("for ", "while ", "until ", "&& break", "|| continue"):
                self.assertNotIn(
                    spelling, command,
                    f"a pytest step contains {spelling!r}, which is how a retry loop is spelled")
            # INVOCATIONS, not occurrences of the word: the guard step names a
            # `pytest-report.xml`, and counting the substring read that as a second run.
            invocations = len(re.findall(r"(?:^|[\s;&|])(?:python[\d.]*\s+-m\s+)?pytest(?=\s|$)",
                                         command))
            self.assertLessEqual(
                invocations, 1,
                "a step invokes pytest more than once, which is a retry however it is "
                f"spelled:\n{command}")
        # The flags are covered by the selection row above, which reads all three inputs. What is
        # left is the DECLARATION: a retry plugin installed there needs no flag on some versions,
        # and `rerunfailures` alone was the first version's whole check — `pytest-retry` walked
        # past it.
        dev = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
        for distribution in _RETRY_DISTRIBUTIONS:
            self.assertNotIn(
                distribution, dev,
                f"requirements-dev.txt installs {distribution}, which retries a failed test; "
                "TODO.md records an intermittent this suite exists to expose")

    def test_the_job_has_a_timeout_well_under_the_platform_default(self) -> None:
        """The suite takes about 200s on the runner. The default ceiling is six hours, which is
        long enough that a hung job reads as an outage rather than a failure."""
        # By NAME, not by object identity. `_workflow()` re-parses the YAML on every call, so
        # `id(job)` from one call never matches a job dict from another — the first version
        # compared exactly that, and the floor below NEVER RAN: `timeout-minutes: 1` was green.
        # Found by a line-coverage census, not by reading.
        suite_job_names = _suite_job_names()
        for name, job in _jobs().items():
            with self.subTest(job=name):
                self.assertIn(
                    "timeout-minutes", job,
                    "a job has no timeout, so a hung run reads as an outage rather than a failure")
                minutes = job["timeout-minutes"]
                self.assertLessEqual(minutes, 60)
                if name in suite_job_names:
                    # The floor is about THE SUITE, which takes about 200s on the runner. A lint
                    # job with `timeout-minutes: 5` is ordinary work, and the first version refused
                    # it with a message about the suite's runtime — round 2's over-refusal probe.
                    self.assertGreaterEqual(
                        minutes, 10,
                        "below this the suite fails for the runner's speed rather than a defect")

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
        # And `pull_request` must be UNFILTERED. Round 2 measured both filters: a `branches:` list
        # naming a branch that does not exist, and a `paths:` glob matching nothing, each leaving
        # the job never running on a PR while `docs/DEVELOPMENT.md` says it runs on every one — a
        # PR then shows no check at all and the document says one ran.
        filters = set(triggers["pull_request"] or {}) - {"types"}
        # `types:` is not a filter in the sense that matters — restating the defaults, or adding
        # `ready_for_review`, still runs on every pull request. `branches:` and `paths:` are what
        # make the job never run, and those are what round 2 measured.
        self.assertEqual(
            set(), filters,
            f"`pull_request` carries {sorted(filters)}, which can stop the job running on a pull "
            "request while the document says it runs on every one")

    def test_a_run_on_main_is_not_cancelled_by_a_later_one(self) -> None:
        """A `main` run is the record for that commit and nothing later reads it from anywhere
        else; a superseded branch push has a successor that will."""
        concurrency = _workflow().get("concurrency") or next(
            (job["concurrency"] for job in _jobs().values() if "concurrency" in job), None)
        self.assertIsNotNone(
            concurrency, "no concurrency group is declared at workflow or job level")
        self.assertIn("github.ref", concurrency["group"])
        expression = str(concurrency["cancel-in-progress"])
        # The DIRECTION, not the presence of the string. Round 3 measured the inverse — cancelling
        # `main` and sparing branches, the exact reverse of the decision — as green at every
        # revision, because the row only asked whether `refs/heads/main` appeared.
        self.assertIn("!=", expression,
                      f"cancel-in-progress is {expression!r}; as written it cancels `main` runs, "
                      "which are the record for their commit and have no successor to read them")
        self.assertIn("refs/heads/main", expression)


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
        for job, _step in _suite_steps():
            # The SUITE's job. A lint job on `ubuntu-latest` is ordinary work and the reason given
            # here — the sandbox needs the system interpreter — does not apply to it.
            self.assertNotIn("latest", str(job.get("runs-on", "")))

    def test_no_step_installs_a_second_interpreter(self) -> None:
        """The suite must run on the runner's SYSTEM python, because that is the one under `/usr`
        that the sandbox can reach. Re-adding `actions/setup-python` would reintroduce the failure
        above, and it would look like a tidy-up."""
        for job, _step in _suite_steps():
            for step in _steps_of(job):
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
        images: set[str] = set()
        # The SUITE's job only. The claim in the document is about the interpreter the suite runs
        # on; a lint job's image is not its subject, and requiring the document to name every
        # image refused an ordinary second job (round 2's over-refusal probe).
        for job in {id(j): j for j, _s in _suite_steps()}.values():
            runs_on = str(job.get("runs-on", ""))
            if "${{" not in runs_on:
                images.add(runs_on)
                continue
            # A MATRIX. The first version wrote `continue` here with a comment saying "the
            # sentence check below applies" — there is no check below, so the loop body never ran
            # and the row passed having asserted NOTHING. Measured in round 2: a single-image
            # matrix on `ubuntu-24.04` with the document untouched was green, which is exactly the
            # configuration commit 74206c8 measured as killing every sandbox test. The images come
            # out of the matrix instead, and a `runs-on` this reader cannot resolve is a failure
            # rather than a skip.
            matrix = job.get("strategy", {}).get("matrix", {})
            resolved: set[str] = set()
            for values in matrix.values():
                if not isinstance(values, list):
                    continue
                for value in values:
                    # `matrix: os: [ubuntu-22.04]` and the `include:` list-of-dicts idiom, which
                    # is the standard one and which the first version resolved to nothing.
                    candidates = value.values() if isinstance(value, dict) else [value]
                    resolved |= {str(c) for c in candidates
                                 if isinstance(c, str) and c.startswith("ubuntu")}
            self.assertTrue(
                resolved,
                f"a job's `runs-on` is the expression {runs_on!r} and no matrix entry resolves it; "
                "this row cannot check the document against an image it cannot name")
            images |= resolved
        self.assertTrue(images, "no job names a runner image")
        for image in sorted(images):
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
        suite = _suite_steps()
        self.assertEqual(len(suite), 1, "this row reads THE suite step; there is not exactly one")
        command = suite[0][1]["run"].strip()
        # `--junitxml` is stripped: it is plumbing for the run-time guard, carries a runner
        # expression a document cannot sensibly quote, and selects nothing. Everything that
        # decides WHAT runs stays in the comparison.
        # The value is a QUOTED runner expression containing spaces, so `\S+` stopped inside it
        # and left half the path in the command — measured while writing this row.
        command = re.sub(r'\s*--junitxml=(?:"[^"]*"|\S+)', "", command).strip()
        command = " ".join(command.split())
        document = self._development()
        # EQUALITY against what the document quotes, not containment. The first version asserted
        # the workflow's command was a SUBSTRING of the document, so the document could describe a
        # longer command — `… -q -rs -m 'not slow'` was green. Drift in that direction is the one
        # that matters, because the document is the only statement of what a green check means.
        quoted = re.findall(r"`([^`\n]*pytest[^`\n]*)`", document)
        self.assertIn(
            command, quoted,
            f"docs/DEVELOPMENT.md does not quote the suite command as the workflow runs it. It "
            f"runs {command!r}; the document quotes {quoted!r}.")

    def test_the_document_s_sandbox_claim_matches_the_workflow(self) -> None:
        """The one claim in the "does not cover" list that a machine can decide.

        The list exists so a green check is not read as more than it is, and its MEMBERS are prose
        — no test can decide what a workflow does NOT do. An earlier version also asserted the
        list's heading, which made the CASING and the WORDING of a paragraph load-bearing:
        rewriting "What CI does NOT cover" as "Outside CI's coverage" turned this red, and round 2
        reported it as an over-refusal. The heading is gone from the check; what stays is the
        sandbox, which is checkable because the sysctl step either exists or does not — and the
        row above (`test_the_sandbox_restriction_is_lifted`) is the other half of the same claim.
        """
        document = self._development().lower()  # the casing of prose is not the claim
        # PRESENCE first. `6f22b38` removed the heading assertion to stop refusing a rewording and
        # removed the list's existence check with it — measured red-then-GREEN by round 3: deleting
        # both bullets outright was green, while this row's docstring still said the list exists so
        # a green check is not read as more than it is. Restored WORDING-TOLERANTLY: what has to be
        # there is a statement that CI does not cover everything, in whatever words.
        # Presence, checked by the LIST'S MEMBERS rather than by a phrase. The first version
        # accepted any of four phrasings, and one of them — "ci does not" — also matches an
        # unrelated sentence in the same section, so deleting the whole list was green. What has
        # to be there is the substance: several named things CI does not do.
        members = ("mutation check", "differential harness", "no open pull request",
                   "billed", "outside its", "leaf-`llm` cli")
        present = [m for m in members if m in document]
        self.assertGreaterEqual(
            len(present), 3,
            "docs/DEVELOPMENT.md no longer names what CI does NOT cover (found "
            f"{present}); a green check is then read as more than it is, and nothing else says "
            "otherwise")
        self.assertTrue(
            any(phrase in document for phrase in
                ("the sandbox is covered", "sandbox coverage is included",
                 "the sandbox is exercised")),
            "docs/DEVELOPMENT.md no longer states that the sandbox is covered; that claim is true "
            "only while the sysctl step runs, and the row above is its other half")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
