"""`tools/tests/skip_probes.py`: the probes, the reason table, and the reported-skip check (#498)."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools.backends import registry
from tools.tests import skip_probes

_CUDA_REASON = skip_probes.DEPENDENCIES["cuda_compiler_driver"][1]


def _report(*cases: str) -> str:
    body = "".join(cases)
    return f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">{body}</testsuite></testsuites>'


def _skipped(name: str, *, kind: str = "pytest.skip", message: str = "") -> str:
    return (f'<testcase classname="tools.tests.test_x.T" name="{name}">'
            f'<skipped type="{kind}" message="{message}">detail</skipped></testcase>')


def _passed(name: str) -> str:
    return f'<testcase classname="tools.tests.test_x.T" name="{name}" />'


class CheckReportTests(unittest.TestCase):
    def _check(self, xml: str, *, present=lambda dep: False):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "r.xml")
            path.write_text(xml, encoding="utf-8")
            return skip_probes.check_report(path, present=present)

    def test_a_skip_with_a_foreign_reason_is_refused(self) -> None:
        refusals, accepted = self._check(_report(_skipped("t1", message="gfortran not available")))
        self.assertEqual(len(refusals), 1)
        self.assertIn("tools.tests.test_x.T::t1", refusals[0])
        self.assertIn("'gfortran not available'", refusals[0])
        self.assertIn("not a probe reason", refusals[0])
        self.assertEqual(accepted, {})

    def test_an_xfail_is_refused_whatever_its_message(self) -> None:
        refusals, accepted = self._check(_report(
            _skipped("t_marked", kind="pytest.xfail", message=_CUDA_REASON),
            # `unittest.expectedFailure`: the writer gives it an empty message.
            _skipped("t_expected", kind="pytest.xfail", message="")))
        self.assertEqual(len(refusals), 2)
        for refusal in refusals:
            self.assertIn("type='pytest.xfail'", refusal)
        self.assertEqual(accepted, {})

    def test_a_probe_skip_whose_dependency_is_present_is_refused(self) -> None:
        asked: list[str] = []

        def present(dep: str) -> bool:
            asked.append(dep)
            return True

        refusals, accepted = self._check(_report(_skipped("t1", message=_CUDA_REASON)),
                                         present=present)
        self.assertEqual(asked, ["cuda_compiler_driver"])
        self.assertEqual(len(refusals), 1)
        self.assertIn("cuda_compiler_driver is installed on this host", refusals[0])
        self.assertEqual(accepted, {})

    def test_a_probe_skip_whose_dependency_is_absent_is_accepted(self) -> None:
        claude_reason = skip_probes.DEPENDENCIES["claude"][1]
        refusals, accepted = self._check(_report(
            _skipped("t1", message=_CUDA_REASON), _skipped("t2", message=claude_reason),
            _passed("t3")))
        self.assertEqual(refusals, [])
        self.assertEqual(accepted, {_CUDA_REASON: ["tools.tests.test_x.T::t1"],
                                    claude_reason: ["tools.tests.test_x.T::t2"]})

    def test_every_skipped_child_of_one_case_is_read(self) -> None:
        """pytest writes one `<skipped>` per skipped `subTest` into the same `<testcase>`."""
        xml = _report('<testcase classname="tools.tests.test_x.T" name="t1">'
                      f'<skipped type="pytest.skip" message="{_CUDA_REASON}">a</skipped>'
                      '<skipped type="pytest.skip" message="arbitrary second">b</skipped>'
                      '</testcase>')
        refusals, accepted = self._check(xml)
        self.assertEqual(len(refusals), 1, refusals)
        self.assertIn("'arbitrary second'", refusals[0])
        self.assertEqual(accepted, {_CUDA_REASON: ["tools.tests.test_x.T::t1"]})

    def test_a_collection_skip_is_refused_as_such(self) -> None:
        refusals, _ = self._check(_report(
            '<testcase classname="" name="tools.tests.test_x">'
            '<skipped message="collection skipped">x</skipped></testcase>'))
        self.assertEqual(len(refusals), 1)
        self.assertIn("skipped at collection", refusals[0])
        self.assertNotIn("xfail", refusals[0])

    def test_every_suite_in_the_report_is_read(self) -> None:
        xml = ('<testsuites><testsuite name="a">' + _passed("t0") + '</testsuite>'
               '<testsuite name="b">' + _skipped("t1", message="other") + '</testsuite></testsuites>')
        refusals, _ = self._check(xml)
        self.assertEqual(len(refusals), 1)

    def test_a_missing_or_empty_report_is_an_error_not_an_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                skip_probes.check_report(Path(tmp, "absent.xml"))
            empty = Path(tmp, "empty.xml")
            empty.write_text("", encoding="utf-8")
            with self.assertRaises(skip_probes.ET.ParseError):
                skip_probes.check_report(empty)
            other = Path(tmp, "other.xml")
            other.write_text("<html><testcase/></html>", encoding="utf-8")
            with self.assertRaises(ValueError):
                skip_probes.check_report(other)


class ProbeTableTests(unittest.TestCase):
    def test_the_three_probe_reasons_each_map_to_one_dependency(self) -> None:
        self.assertEqual(set(skip_probes.DEPENDENCIES), {"cuda_compiler_driver", "claude", "codex"})
        self.assertEqual(len(skip_probes.REASON_TO_DEPENDENCY), len(skip_probes.DEPENDENCIES))
        for dep, (_, reason) in skip_probes.DEPENDENCIES.items():
            self.assertEqual(skip_probes.REASON_TO_DEPENDENCY[reason], dep)
        adapter = registry.capability_module("compiler", "nvcc", "syntax_check")
        self.assertEqual(skip_probes.DEPENDENCIES["cuda_compiler_driver"][0](), str(adapter.EXECUTABLE))
        self.assertEqual(skip_probes.DEPENDENCIES["claude"][0](), "claude")
        self.assertEqual(skip_probes.DEPENDENCIES["codex"][0](), "codex")

    def test_present_asks_which_for_the_dependencys_executable(self) -> None:
        with mock.patch.object(skip_probes.shutil, "which", return_value=None) as which:
            self.assertFalse(skip_probes.present("codex"))
        which.assert_called_once_with("codex")
        with mock.patch.object(skip_probes.shutil, "which", return_value="/x/codex"):
            self.assertTrue(skip_probes.present("codex"))


class _Probe(unittest.TestCase):
    ran = False

    def test_it(self) -> None:
        type(self).ran = True


def _run(case_cls: type, name: str) -> unittest.TestResult:
    result = unittest.TestResult()
    case_cls(name).run(result)
    return result


class DecoratorAndHelperTests(unittest.TestCase):
    def test_the_decorator_skips_with_the_fixed_reason_only_when_absent(self) -> None:
        for installed in (False, True):
            with self.subTest(installed=installed):
                with mock.patch.object(skip_probes, "present", return_value=installed) as present:
                    probe = type("P", (_Probe,), {"ran": False})
                    decorated = skip_probes.skip_unless_installed("codex")(probe)
                present.assert_called_once_with("codex")
                result = _run(decorated, "test_it")
                if installed:
                    self.assertEqual(result.skipped, [])
                    self.assertTrue(decorated.ran)
                else:
                    self.assertEqual([r for _, r in result.skipped],
                                     [skip_probes.DEPENDENCIES["codex"][1]])
                    self.assertFalse(decorated.ran)

    def test_the_in_method_helper_does_the_same(self) -> None:
        class Helper(unittest.TestCase):
            ran = False

            def test_it(self) -> None:
                skip_probes.require_installed(self, "claude")
                type(self).ran = True

        for installed in (False, True):
            with self.subTest(installed=installed):
                Helper.ran = False
                with mock.patch.object(skip_probes, "present", return_value=installed) as present:
                    result = _run(Helper, "test_it")
                present.assert_called_once_with("claude")
                if installed:
                    self.assertEqual(result.skipped, [])
                    self.assertTrue(Helper.ran)
                else:
                    self.assertEqual([r for _, r in result.skipped],
                                     [skip_probes.DEPENDENCIES["claude"][1]])
                    self.assertFalse(Helper.ran)


class RealReportTests(unittest.TestCase):
    def test_a_real_report_from_this_pytest_has_the_shape_the_check_reads(self) -> None:
        """Pins the JUnit writer's attribute names against the installed pytest: a pytest that
        changes them turns this red rather than turning the check blind."""
        source = textwrap.dedent(f"""\
            import unittest, pytest

            class T(unittest.TestCase):
                def test_probe_skip(self):
                    self.skipTest({_CUDA_REASON!r})

                @pytest.mark.xfail(reason="marked")
                def test_xfail(self):
                    assert False

                @unittest.expectedFailure
                def test_expected(self):
                    self.assertTrue(False)

                def test_pass(self):
                    pass
            """)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "test_shape.py").write_text(source, encoding="utf-8")
            report = Path(tmp, "r.xml")
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "test_shape.py", "-q", "-o", "addopts=",
                 "-p", "no:cacheprovider", "-p", "no:randomly", f"--junitxml={report}"],
                cwd=tmp, capture_output=True, text=True, timeout=120, check=False)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            refusals, accepted = skip_probes.check_report(report, present=lambda dep: False)
        self.assertEqual(accepted, {_CUDA_REASON: ["test_shape.T::test_probe_skip"]})
        self.assertEqual(len(refusals), 2, refusals)
        self.assertTrue(any("T::test_xfail" in r for r in refusals), refusals)
        self.assertTrue(any("T::test_expected" in r for r in refusals), refusals)


class MainTests(unittest.TestCase):
    def _main(self, xml: str, present=lambda dep: False) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "r.xml")
            path.write_text(xml, encoding="utf-8")
            with mock.patch.object(skip_probes, "present", side_effect=present), \
                    mock.patch("sys.stdout") as out:
                code = skip_probes.main([str(path)])
            printed = "".join(call.args[0] for call in out.write.call_args_list)
        return code, printed

    def test_main_exits_nonzero_on_a_refusal_and_zero_otherwise(self) -> None:
        code, printed = self._main(_report(_passed("t0")))
        self.assertEqual(code, 0)
        self.assertIn("skipped=0", printed)

        code, printed = self._main(_report(_skipped("t1", message=_CUDA_REASON)))
        self.assertEqual(code, 0)
        self.assertIn(f"accepted 1 skipped: {_CUDA_REASON}", printed)
        self.assertIn("tools.tests.test_x.T::t1", printed)

        code, printed = self._main(_report(_skipped("t1", message=_CUDA_REASON)),
                                   present=lambda dep: True)
        self.assertEqual(code, 1)
        self.assertIn("REFUSED", printed)

        code, printed = self._main(_report(_skipped("t1", message="nope")))
        self.assertEqual(code, 1)

    def test_main_refuses_a_wrong_argument_count(self) -> None:
        with mock.patch("sys.stderr"):
            self.assertEqual(skip_probes.main([]), 2)
            self.assertEqual(skip_probes.main(["a.xml", "b.xml"]), 2)


if __name__ == "__main__":
    unittest.main()
