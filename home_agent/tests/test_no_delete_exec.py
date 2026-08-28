from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "bin" / "no-delete-exec.c"


class NoDeleteExecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("C compiler is unavailable")
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / "no-delete-exec"
        result = subprocess.run(
            [
                compiler,
                "-std=c17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-O2",
                "-o",
                str(cls.binary),
                str(SOURCE),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            cls.build.cleanup()
            raise unittest.SkipTest(f"cannot compile Landlock helper: {result.stderr}")
        probe = subprocess.run(
            [str(cls.binary), "--", "/bin/true"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 125:
            cls.build.cleanup()
            raise unittest.SkipTest(probe.stderr.strip())
        if probe.returncode:
            cls.build.cleanup()
            raise RuntimeError(probe.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build.cleanup()

    def run_guard(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.binary), *arguments],
            capture_output=True,
            text=True,
        )

    def test_allows_create_and_overwrite_only_in_write_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            existing = project / "existing.txt"
            existing.write_text("old", encoding="utf-8")

            allowed = self.run_guard(
                "--write-root",
                str(project),
                "--",
                "/bin/sh",
                "-c",
                'printf new > "$1/existing.txt"; printf created > "$1/new.txt"',
                "guard",
                str(project),
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(existing.read_text(encoding="utf-8"), "new")
            self.assertEqual((project / "new.txt").read_text(encoding="utf-8"), "created")

            denied = self.run_guard(
                "--write-root",
                str(project),
                "--",
                "/bin/sh",
                "-c",
                'printf denied > "$1/outside.txt"',
                "guard",
                str(outside),
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertFalse((outside / "outside.txt").exists())

    def test_denies_unlink_rmdir_and_rename_inside_write_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            victim = project / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            directory = project / "directory"
            directory.mkdir()

            for command in (
                'rm "$1/victim.txt"',
                'rmdir "$1/directory"',
                'mv "$1/victim.txt" "$1/moved.txt"',
            ):
                result = self.run_guard(
                    "--write-root",
                    str(project),
                    "--",
                    "/bin/sh",
                    "-c",
                    command,
                    "guard",
                    str(project),
                )
                self.assertNotEqual(result.returncode, 0)

            self.assertTrue(victim.exists())
            self.assertTrue(directory.exists())
            self.assertFalse((project / "moved.txt").exists())

    def test_allows_overwriting_one_explicit_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            note = Path(temporary) / "main.md"
            note.write_text("old", encoding="utf-8")

            result = self.run_guard(
                "--write-file",
                str(note),
                "--",
                "/bin/sh",
                "-c",
                'printf updated > "$1"',
                "guard",
                str(note),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(note.read_text(encoding="utf-8"), "updated")


if __name__ == "__main__":
    unittest.main()
