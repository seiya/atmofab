"""`requirements.txt` / `requirements-dev.txt`, and that they cannot drift from what runs.

This repository had no dependency declaration until this file's branch, and the reason it is safe
to add one is the same reason `test_host_prerequisites.py` gives for its own probe: the
declaration asserts nothing on its own authority. Every name and every version range below is
compared against a source that already decides the question —

  * `tools/run_workflow.py:REQUIRED_PYTHON_MODULES` — the launch probe that refuses a host
    missing a mid-run import, by distribution name;
  * the package table in `docs/RUNBOOK.md` §0-1 — the operator-facing list, which is WIDER than
    the tuple (`PyYAML` is deliberately absent from the tuple: it is imported at module top and
    already fails legibly at launch, so it carries no reason code);
  * each `linter` backend's `SUPPORTED_VERSION_SPEC` — the range the launch probe accepts, and
    the range `docs/RUNBOOK.md` §0-1's table is checked against by
    `test_host_prerequisites.LinterVersionRangeTests`.

The knot the third bullet ties is new: before this file, `docs/DEVELOPMENT.md` §Setup step 6 spelt
those two linter ranges in an install line that NO test read, so it could tell a developer to
install a build the launch probe refuses. That line now points at `requirements-dev.txt`, and this
file is what holds it.

What is deliberately NOT checked here: the CI workflow's `apt-get` line. Comparing it to a
document needs a hand-written executable-name -> package-name map (`bwrap` -> `bubblewrap`) and a
pip-or-apt column (`fortitude` -> `fortitude-lint`), which is a third copy of a fact two documents
already carry — the thing `docs/DEVELOPMENT.md` §Design Policy forbids. What witnesses the apt
line instead is the suite CI runs: `test_host_prerequisites` asserts the derived executables are
on PATH at a supported version, and each linter backend's tests FAIL rather than skip when their
tool is absent.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import run_workflow  # noqa: E402
from tools.backends import registry as backend_registry  # noqa: E402

#: A requirement line, split into the distribution name and everything after it. Deliberately not
#: a PEP 508 parser: these two files are hand-written and this repository installs them with pip,
#: so the shapes that exist are `name`, `name>=x`, `name==x`, and `name>=x,<y`.
_REQUIREMENT_RE = re.compile(r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?P<spec>.*)$")


def _effective_lines(path: Path) -> list[str]:
    """Every line of a requirements file that pip ACTS on, comments and blanks removed.

    One reader for both of the questions below, because the alternative is what the branch's own
    handwritten sweep killed: an `assertIn("-r requirements.txt", path.read_text())` is satisfied
    by a line reading `# -r requirements.txt`, so commenting the include out left the check green.
    A raw-text search cannot tell an instruction from a mention of one.
    """
    lines = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _include_targets(path: Path) -> list[str]:
    """The files a requirements file pip-includes with `-r` / `--requirement`."""
    targets = []
    for line in _effective_lines(path):
        for flag in ("-r", "--requirement"):
            if line == flag or line.startswith(flag + " "):
                targets.append(line[len(flag):].strip())
            elif flag == "--requirement" and line.startswith("--requirement="):
                targets.append(line.split("=", 1)[1].strip())
    return targets


def _requirement_lines(path: Path) -> list[str]:
    """The lines of a requirements file that state a requirement.

    An option line — pip's `-r` include and anything else beginning with `-` — is not one. That
    case is called out because dropping it silently would make `requirements-dev.txt` look as
    though it declared the runtime set too.
    """
    return [line for line in _effective_lines(path) if not line.startswith("-")]


def _parsed(path: Path) -> dict[str, str]:
    """distribution name -> version specifier (possibly empty), for one requirements file."""
    found: dict[str, str] = {}
    for line in _requirement_lines(path):
        match = _REQUIREMENT_RE.match(line)
        if match is None:  # pragma: no cover - a malformed line is a defect, not a state
            raise AssertionError(f"{path.name} carries a line this reader cannot parse: {line!r}")
        found[match.group("name")] = match.group("spec").strip()
    return found


class RuntimeRequirementsTests(unittest.TestCase):
    """`requirements.txt` against the two authorities that already decide what a host needs."""

    #: The §0-1 table this check owns, found by its own header rather than by position — the form
    #: `test_host_prerequisites.LinterVersionRangeTests` uses for the linter table in the same
    #: document, so this file does not invent a second table reader.
    _PACKAGE_TABLE_HEADER = "| package | purpose |"

    def _runbook(self) -> str:
        return (REPO_ROOT / "docs" / "RUNBOOK.md").read_text()

    def _packages_in_table(self, document: str) -> set[str]:
        """The distribution names the §0-1 package table declares, out of `document`.

        Takes the document as an argument so the over-refusal probe below drives THIS function
        rather than re-implementing it with different termination semantics.
        """
        self.assertIn(
            self._PACKAGE_TABLE_HEADER, document,
            "docs/RUNBOOK.md §0-1 no longer carries the Python package table; this check cannot "
            "find what it is supposed to compare")
        found: set[str] = set()
        for line in document.split(self._PACKAGE_TABLE_HEADER, 1)[1].splitlines()[1:]:
            if not line.startswith("|"):
                break
            cell = line.strip().strip("|").split("|")[0].strip()
            if set(cell) <= set("-:") and cell:
                continue  # the header separator row, not a package
            found.add(cell.strip("`"))
        return found

    def test_the_runtime_declaration_is_exactly_the_runbook_package_table(self) -> None:
        """Both directions at once, and the authority is the RUNBOOK table rather than
        `REQUIRED_PYTHON_MODULES`: the tuple deliberately omits `PyYAML` (`tools/run_workflow.py`
        says why), so making the tuple the authority would report the correct file as wrong."""
        self.assertEqual(
            set(_parsed(REPO_ROOT / "requirements.txt")),
            self._packages_in_table(self._runbook()),
            "requirements.txt and the package table in docs/RUNBOOK.md §0-1 disagree about which "
            "distributions the host needs")

    def test_every_module_the_launch_probe_refuses_a_host_for_is_declared(self) -> None:
        """The narrower authority, checked separately because it NAMES the missing member.

        `REQUIRED_PYTHON_MODULES` is a subset of the table; a distribution the probe refuses a
        host for and this file does not install is a machine that passes `pip install -r` and then
        fails at launch.
        """
        declared = set(_parsed(REPO_ROOT / "requirements.txt"))
        for import_name, distribution in run_workflow.REQUIRED_PYTHON_MODULES:
            self.assertIn(
                distribution, declared,
                f"tools/run_workflow.py refuses a host missing {import_name!r} (distribution "
                f"{distribution!r}), but requirements.txt does not install it")

    def test_the_runbook_table_covers_every_module_the_launch_probe_names(self) -> None:
        """The knot between the two authorities themselves, which no test tied before this file.

        Without it the pair can agree with `requirements.txt` and with each other's absence: a new
        entry added to `REQUIRED_PYTHON_MODULES` alone leaves the operator-facing install line in
        `docs/RUNBOOK.md` §0-1 short by one, and an operator who follows it gets refused at launch.
        """
        table = self._packages_in_table(self._runbook())
        for import_name, distribution in run_workflow.REQUIRED_PYTHON_MODULES:
            self.assertIn(
                distribution, table,
                f"tools/run_workflow.py refuses a host missing {import_name!r} (distribution "
                f"{distribution!r}), but the package table in docs/RUNBOOK.md §0-1 omits it")

    def test_the_runbook_install_line_installs_what_the_table_declares(self) -> None:
        """The `pip install ...` line above the table is what an operator actually types; the
        table beside it is what this file is checked against. They are two spellings of one fact
        and nothing compared them."""
        runbook = self._runbook()
        install_lines = [
            line for line in runbook.splitlines()
            if line.startswith("pip install ") and "-r " not in line]
        self.assertEqual(
            len(install_lines), 1,
            "docs/RUNBOOK.md no longer carries exactly one bare `pip install <packages>` line; "
            f"this check has lost the line it observes (found: {install_lines})")
        named = set(install_lines[0].split()[2:])
        self.assertEqual(
            named, self._packages_in_table(runbook),
            "the `pip install` line in docs/RUNBOOK.md §0-1 and the package table under it name "
            "different distributions")

    def test_the_runbook_points_the_operator_at_the_pinned_versions(self) -> None:
        """The bare install line is not sufficient on its own, and this is what says so.

        `pip install PyYAML tree-sitter tree-sitter-fortran` installs whatever is current, and two
        of those three are PINNED in `requirements.txt` because the version is a measured property
        of this repository — `tools/backends/language/fortran/structure.py` drives a py-tree-sitter
        API that has changed across releases. An operator who follows the bare line alone gets a
        host the `Generate.gate` structure read may not work on, which is a wrong verdict reached
        part-way into a billed run. So the document has to carry the `-r` form as well, and
        deleting it has to be something a test notices: without this row it was not (measured —
        the hunk adding that block survived the branch's round-0 mutation sweep).
        """
        runbook = self._runbook()
        self.assertIn(
            "pip install -r requirements.txt", runbook,
            "docs/RUNBOOK.md §0-1 no longer tells the operator to install from requirements.txt; "
            "the bare `pip install <packages>` line beside it installs unpinned versions")

    def test_a_table_elsewhere_in_the_document_is_not_this_check_s_business(self) -> None:
        """The over-refusal probe, driving the REAL extractor over a synthetic document.

        `docs/RUNBOOK.md` carries several tables whose first column is a name in backticks. Only
        the one under this header is a package declaration; a `| tool | purpose |` table of CLI
        tools, or a prose paragraph naming a package, may not be read as one.
        """
        document = (
            "# Runbook\n\n| tool | purpose |\n|---|---|\n| `jq` | extracting shell variables |\n"
            f"\n{self._PACKAGE_TABLE_HEADER}\n|---|---|\n"
            "| `zzpkg` | the thing it is for |\n"
            "\nAfterwards install `cmake`, which is not a Python package.\n")
        self.assertEqual(self._packages_in_table(document), {"zzpkg"})

    def test_the_extractor_refuses_a_document_whose_table_is_gone(self) -> None:
        """A renamed or deleted table must say so, not silently compare an empty set."""
        with self.assertRaises(AssertionError) as caught:
            self._packages_in_table("# Runbook\n\nno table here\n")
        self.assertIn("no longer carries the Python package table", str(caught.exception))


class DevRequirementsTests(unittest.TestCase):
    """`requirements-dev.txt` against the `linter` backends' own declarations."""

    def _declared_ranges(self) -> dict[str, str]:
        """backend id -> `SUPPORTED_VERSION_SPEC`, for every implemented linter that lints.

        The same walk `test_host_prerequisites.LinterVersionRangeTests._declared_ranges` does. It
        is repeated rather than imported because importing a sibling test's helper couples the two
        files' fixtures; what must not be duplicated is the CONSTANT, and it is not — both ask the
        registry.
        """
        found: dict[str, str] = {}
        for backend_id in backend_registry.implemented_backend_ids("linter"):
            if "lint" not in backend_registry.get("linter", backend_id).backend_provides:
                continue
            module = backend_registry.capability_module("linter", backend_id, "lint")
            found[backend_id] = module.SUPPORTED_VERSION_SPEC
        return found

    def _pip_installable_ranges(self) -> dict[str, str]:
        """The subset of the above whose tool pip can install, keyed by DISTRIBUTION name.

        `cppcheck` is a system package, so its range is documented and not declared here; the map
        below is the one place this file states a distribution name that a module does not, and it
        is asserted against the registry's own backend ids so a new linter cannot land unnoticed.
        """
        distributions = {"fortitude": "fortitude-lint", "ruff": "ruff"}
        apt_only = {"cppcheck"}
        declared = self._declared_ranges()
        self.assertEqual(
            set(declared), set(distributions) | apt_only,
            "a linter backend was added or removed; requirements-dev.txt and this map have to say "
            "whether pip installs it")
        return {distributions[b]: spec for b, spec in declared.items() if b in distributions}

    def test_every_pip_installable_linter_range_is_the_range_its_backend_declares(self) -> None:
        """Character for character, in both directions.

        This is the coverage `docs/DEVELOPMENT.md` §Setup step 6 did not have: its two `pipx
        install '<name><range>'` lines spelt these ranges and no test read them, so they could
        drift out of the declared range exactly the way `docs/RUNBOOK.md`'s install line did
        (measured on PR #125). Step 6 now points at this file instead.
        """
        expected = self._pip_installable_ranges()
        parsed = _parsed(REPO_ROOT / "requirements-dev.txt")
        self.assertEqual(
            {name: parsed.get(name) for name in expected}, expected,
            "requirements-dev.txt states a linter version range that is not the one its backend "
            "declares; a developer following this file installs a build the launch probe refuses")

    def test_no_line_claims_to_install_a_linter_that_is_not_a_registered_backend(self) -> None:
        """The other direction over the FILE rather than over the registry: a line naming a tool
        no `linter` backend implements would be an install instruction for nothing."""
        installable = set(self._pip_installable_ranges())
        unknown_linters = {
            name for name in _parsed(REPO_ROOT / "requirements-dev.txt")
            if ("lint" in name or "ruff" in name) and name not in installable}
        self.assertEqual(
            set(), unknown_linters,
            "requirements-dev.txt installs a lint tool that no registered linter backend provides")

    def test_the_dev_file_includes_the_runtime_file(self) -> None:
        """`pip install -r requirements-dev.txt` has to be sufficient to run the suite, which
        imports the runtime dependencies. Without the include it installs a set that cannot.

        Asked of the lines pip ACTS on, not of the file's text: the first version of this row
        searched the raw text, and the branch's handwritten sweep killed it by commenting the
        include out — `# -r requirements.txt` contains the string the assertion looked for.
        """
        self.assertIn(
            "requirements.txt", _include_targets(REPO_ROOT / "requirements-dev.txt"),
            "requirements-dev.txt no longer includes requirements.txt, so installing it alone "
            "leaves the suite without the runtime dependencies it imports")

    def test_the_include_reader_does_not_read_a_commented_out_include(self) -> None:
        """The self-test for `_include_targets`, which is the only thing standing between the row
        above and the false green it used to give. Both directions: a live include is found, and
        the same line commented out is not — so a later change that stops stripping comments turns
        this red rather than turning the row above vacuous."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        scratch = Path(holder.name)
        live = scratch / "live.txt"
        live.write_text("-r requirements.txt\npytest\n")
        self.assertEqual(_include_targets(live), ["requirements.txt"])
        dead = scratch / "dead.txt"
        dead.write_text("# -r requirements.txt\npytest\n")
        self.assertEqual(_include_targets(dead), [])
        long_form = scratch / "long.txt"
        long_form.write_text("--requirement requirements.txt\n")
        self.assertEqual(_include_targets(long_form), ["requirements.txt"])

    def test_the_reader_does_not_mistake_the_include_for_a_requirement(self) -> None:
        """The self-test for `_requirement_lines`: `-r requirements.txt` must not come back as a
        distribution named `-r`, and a comment-only line must not come back at all. Both are what
        every count in this file rests on."""
        self.assertNotIn("-r", _parsed(REPO_ROOT / "requirements-dev.txt"))
        self.assertNotIn("requirements.txt", _parsed(REPO_ROOT / "requirements-dev.txt"))
        self.assertTrue(_parsed(REPO_ROOT / "requirements-dev.txt"),
                        "the reader returned nothing at all, so every comparison above is vacuous")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
