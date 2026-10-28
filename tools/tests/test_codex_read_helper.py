"""The codex reading helper `read.py` (issue #534 PR-J3-a, F-J3-1), executed as the leaf runs
it: a subprocess over fixture files in a directory standing for the work tree."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import leaf_contract

CAP = leaf_contract.CODEX_READ_ADVICE_BYTES
NAME = leaf_contract.CODEX_READ_HELPER_NAME


class CodexReadHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tree = Path(self._tmp.name)
        (self.tree / NAME).write_text(leaf_contract.CODEX_READ_HELPER_TEXT, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_helper(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([sys.executable, NAME, *args], cwd=self.tree,
                              capture_output=True, check=False, timeout=60)

    def read_all(self, name: str) -> tuple[bytes, list[bytes]]:
        """Follow the helper's own `next:` commands to the end; the concatenated pages and
        each page's whole output."""
        pages: list[bytes] = []
        args = [name]
        body = b""
        for _ in range(10_000):
            out = self.run_helper(*args)
            self.assertEqual(out.returncode, 0, out.stdout)
            pages.append(out.stdout)
            _head, _, trailer = out.stdout.rstrip(b"\n").rpartition(b"\n")
            if not trailer.startswith(b"== read.py "):
                trailer = out.stdout.rstrip(b"\n")
            self.assertTrue(trailer.startswith(b"== read.py "), out.stdout[-200:])
            body += out.stdout[:len(out.stdout) - len(trailer) - 1]
            text = trailer.decode()
            if text.endswith("end of file"):
                return body, pages
            match = re.search(r"next: python3 read\.py (\S+) (\d+)$", text)
            self.assertIsNotNone(match, text)
            args = [match.group(1), match.group(2)]
        self.fail("no end of file")

    def test_pages_reassemble_the_file_exactly_and_stay_under_the_cap(self) -> None:
        files = {
            "ascii.txt": "".join(f"line {i} " + "x" * (i % 300) + "\n" for i in range(1000)),
            "two_byte.txt": "".join("é" * (i % 900) + "\n" for i in range(300)),
            "four_byte.txt": "".join("𝄞" * (i % 400) + "\n" for i in range(300)),
            "no_newline.txt": "".join(f"row {i}\n" for i in range(5000)) + "tail",
        }
        for name, text in files.items():
            with self.subTest(name):
                (self.tree / name).write_text(text, encoding="utf-8")
                body, pages = self.read_all(name)
                expected = text.encode() + (b"" if text.endswith("\n") else b"\n")
                self.assertEqual(body, expected)
                self.assertGreater(len(pages), 1)
                for page in pages:
                    self.assertLess(len(page), CAP)

    def test_a_hundred_line_file_and_its_trailers(self) -> None:
        (self.tree / "f.txt").write_text("".join(f"{i:03d} " + "y" * 396 + "\n"
                                                 for i in range(1, 101)))
        out = self.run_helper("f.txt").stdout.decode()
        per_page = out.count("\n") - 1
        self.assertTrue(out.endswith(f"== read.py f.txt: lines 1-{per_page} of 100; "
                                     f"next: python3 read.py f.txt {per_page + 1}\n"), out[-120:])
        last = self.run_helper("f.txt", "91").stdout.decode()
        self.assertTrue(last.endswith("== read.py f.txt: lines 91-100 of 100; end of file\n"))
        self.assertTrue(last.startswith("091 "))

    def test_a_line_over_the_page_is_not_printed(self) -> None:
        (self.tree / "long.txt").write_text("short\n" + "z" * (CAP + 5) + "\nafter\n")
        first = self.run_helper("long.txt").stdout.decode()
        self.assertEqual(first, "short\n== read.py long.txt: lines 1-1 of 3; "
                                "next: python3 read.py long.txt 2\n")
        over = self.run_helper("long.txt", "2")
        self.assertEqual(over.returncode, 0)
        self.assertNotIn(b"zz", over.stdout)
        piece = CAP // 2
        size = CAP + 5
        command = "sed -n '2p' long.txt | cut -b "
        self.assertEqual(over.stdout.decode(), (
            f"== read.py long.txt: line 2 is {size} bytes, too long for one page with this "
            f"line; read it in pieces, one command each: `{command}1-{piece}`, then "
            f"`{command}{piece + 1}-{2 * piece}`, and so on to byte {size}; after it: "
            "python3 read.py long.txt 3\n"))
        # Each printed command, run as printed, returns its piece of the line and nothing else,
        # and the pieces to the stated last byte are the whole line.
        pieces = b""
        for a in range(1, size + 1, piece):
            got = subprocess.run(["/bin/sh", "-c", f"{command}{a}-{a + piece - 1}"],
                                 cwd=self.tree, capture_output=True, check=True).stdout
            self.assertLessEqual(len(got), piece + 1)
            pieces += got.rstrip(b"\n")
        self.assertEqual(pieces, b"z" * size)
        # On the last line the trailer does not say the file is read before its pieces are.
        (self.tree / "last.txt").write_text("a\n" + "q" * (CAP + 1) + "\n")
        self.assertTrue(self.run_helper("last.txt", "2").stdout.decode().endswith(
            "; after its pieces, end of file\n"))
        # A line UNDER the cap that does not fit beside the trailer is named by its own size.
        edge = CAP - 10
        (self.tree / "edge.txt").write_text("y" * edge + "\n")
        self.assertIn(f"line 1 is {edge} bytes", self.run_helper("edge.txt").stdout.decode())

    def test_a_page_never_reaches_the_cap_itself(self) -> None:
        """The cap is exclusive: a line whose page would be EXACTLY the cap, trailer included,
        is not printed (the boundary `>=` decides, which only the digest pinned before)."""
        trailer = "== read.py f.txt: lines 1-1 of 2; next: python3 read.py f.txt 2\n"
        line = "w" * (CAP - len(trailer) - 1) + "\n"
        self.assertEqual(len(line) + len(trailer), CAP)
        (self.tree / "f.txt").write_text(line + "tail\n")
        out = self.run_helper("f.txt").stdout
        self.assertLess(len(out), CAP)
        self.assertNotIn(b"ww", out)
        self.assertIn(b"too long for one page", out)
        shorter = "w" * (CAP - len(trailer) - 2) + "\n"
        (self.tree / "f.txt").write_text(shorter + "tail\n")
        page = self.run_helper("f.txt").stdout.decode()
        self.assertTrue(page.startswith(shorter), page[-120:])
        self.assertLess(len(page.encode()), CAP)

    def test_the_size_check_counts_the_trailer_it_will_print(self) -> None:
        """The trailer is sized with the next page's line number it will carry: where that
        number gains a digit (9 -> 10), a page sized one byte short of the cap with a one-digit
        guess would reach the cap exactly (round 3 of PR-J3-a: a mutant writing the current
        line's number in the sized trailer survived)."""
        trailer = "== read.py f.txt: lines 1-9 of 20; next: python3 read.py f.txt 10\n"
        lines = ["a" * 99 + "\n"] * 8
        lines.append("b" * (CAP - 800 - len(trailer) - 1) + "\n")
        self.assertEqual(sum(map(len, lines)) + len(trailer), CAP)
        lines += ["c\n"] * 11
        (self.tree / "f.txt").write_text("".join(lines))
        body, pages = self.read_all("f.txt")
        self.assertEqual(body, "".join(lines).encode())
        for page in pages:
            self.assertLess(len(page), CAP)

    def test_refusals_are_one_trailer_line_and_exit_two(self) -> None:
        (self.tree / "f.txt").write_text("a\nb\n")
        os.symlink(self.tree / "f.txt", self.tree / "link.txt")
        for args in (("absent.txt",), ("link.txt",), ("f.txt", "x"), ("f.txt", "0"),
                     ("f.txt", "3"), ()):
            with self.subTest(args):
                out = self.run_helper(*args)
                self.assertEqual(out.returncode, 2)
                self.assertEqual(out.stdout.count(b"\n"), 1)
                self.assertTrue(out.stdout.startswith(b"== read.py "), out.stdout)
        self.assertEqual(self.run_helper("link.txt").stdout.count(b"cannot be read"), 1)

    def test_the_codex_reader_row_says_how_to_run_the_helper(self) -> None:
        """Round 4 of PR-J3-a (leaf-read text): the row names the helper as a host seed the
        leaf must not edit, and says that what one `exec` prints is one output, so pages are
        read one `exec` each."""
        row = leaf_contract.WORKSHOP_READER_TOOL_SURFACE_TEXT["codex_cli"]
        for phrase in (f"python3 {NAME} <file>",
                       (f"`{NAME}` is a host seed like `CONTRACT.md` — the host placed it, "
                        "though the list of seeds in your work-tree contract does not name it: "
                        "do not edit it"), "Run each command in an `exec` of its own",
                       "what one `exec` prints, all its `text()` together, is one output too"):
            self.assertEqual(row.count(phrase), 1, phrase)

    def test_the_only_number_in_the_script_is_the_cap(self) -> None:
        numbers = re.findall(r"\d{2,}", leaf_contract.CODEX_READ_HELPER_TEXT)
        self.assertEqual(set(numbers), {str(CAP)})
        self.assertEqual(CAP, leaf_contract.CODEX_EXEC_OUTPUT_BUDGET_BYTES // 2)


if __name__ == "__main__":
    unittest.main()
