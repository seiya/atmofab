"""The Fortran `prompt_fragments` capability: the fragment files' grammar (issue #289, R4-b PR-2)."""
from __future__ import annotations

import unittest

from tools.backends import registry
from tools.backends.language.fortran import prompts


class FragmentGrammarTests(unittest.TestCase):
    def test_bodies_are_verbatim_and_multi_line(self) -> None:
        text = "# comment\n\n@@ one\nline a\nline b\n@@ two\nx\n"
        self.assertEqual({"one": "line a\nline b", "two": "x"}, prompts.parse_fragments(text))

    def test_a_repeated_or_empty_name_and_stray_text_are_refused(self) -> None:
        for text in ("@@ a\nx\n@@ a\ny\n", "@@ \nx\n", "stray\n@@ a\nx\n"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    prompts.parse_fragments(text)

    def test_the_capability_reaches_this_module_and_every_file_parses(self) -> None:
        module = registry.capability_module("language", "fortran", "prompt_fragments")
        self.assertIs(module, prompts)
        files = sorted(prompts.FRAGMENT_DIR.glob("*.txt"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=path.name):
                self.assertTrue(prompts.fragments(path.stem))

    def test_a_template_with_no_fragment_file_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            prompts.fragments("no_such_template")


if __name__ == "__main__":
    unittest.main()
