#!/usr/bin/env python3
"""Проверить разделение установок и подготовку публичного пакета без сети."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load("setup_apm", "setup-apm.py")
release = load("release_package", "prepare-release-package.py")


class DevelopmentTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".inspect").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".inspect", suffix=".local")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for client in (".agents", ".claude"):
            for base, names in ((self.root, setup.LANGUAGE_SKILLS),
                                (self.root / ".dev", (*setup.LANGUAGE_SKILLS, "ait-routing", "ait-writing"))):
                for name in names:
                    path = base / client / "skills" / name
                    path.mkdir(parents=True)
                    (path / "SKILL.md").write_text(f"{base.name}/{name}")

    def test_skills_available_and_language_not_replaced(self):
        setup.link_skills(self.root)
        setup.link_skills(self.root)
        setup.link_skills(self.root, check=True)
        for client in (".agents", ".claude"):
            for name in setup.LANGUAGE_SKILLS:
                path = self.root / client / "skills" / name
                self.assertFalse(path.is_symlink())
                self.assertEqual((path / "SKILL.md").read_text(), f"{self.root.name}/{name}")
            path = self.root / client / "skills" / "ait-routing"
            self.assertTrue(path.is_symlink())
            self.assertFalse(path.readlink().is_absolute())
            self.assertEqual((path / "SKILL.md").read_text(), ".dev/ait-routing")

    def test_conflict_preserves_user_file_and_adds_no_links(self):
        path = self.root / ".claude/skills/ait-writing"
        path.mkdir()
        (path / "SKILL.md").write_text("user")
        with self.assertRaisesRegex(ValueError, "существующий навык"):
            setup.link_skills(self.root)
        self.assertEqual((path / "SKILL.md").read_text(), "user")
        self.assertFalse((self.root / ".agents/skills/ait-routing").exists())

    def test_missing_development_and_missing_links_fail(self):
        with self.assertRaisesRegex(ValueError, "Не подключён"):
            setup.link_skills(self.root, check=True)
        (self.root / ".dev/.claude/skills/ait-routing/SKILL.md").unlink()
        with self.assertRaisesRegex(ValueError, "Не установлены"):
            setup.link_skills(self.root)
        self.assertFalse((self.root / ".agents/skills/ait-routing").exists())

    def test_setup_stops_after_failed_installation(self):
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "validate-python-artifacts.py").write_text("pass\n")
        with patch.object(setup, "ROOT", self.root), \
             patch("sys.argv", ["setup-apm.py"]), \
             patch.object(setup.subprocess, "run", side_effect=subprocess.CalledProcessError(7, "apm")) as run:
            self.assertEqual(setup.main(), 1)
        self.assertEqual(run.call_count, 1)
        self.assertFalse((self.root / ".agents/skills/ait-routing").exists())

    def test_command_help_and_invalid_arguments(self):
        for filename in ("setup-apm.py", "prepare-release-package.py"):
            command = [setup.sys.executable, str(ROOT / "tools" / filename)]
            result = subprocess.run([*command, "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([*command, "--unknown"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_check_mode_never_installs(self):
        setup.link_skills(self.root)
        with patch.object(setup, "ROOT", self.root), \
             patch("sys.argv", ["setup-apm.py", "--check"]), \
             patch.object(setup.subprocess, "run") as run:
            self.assertEqual(setup.main(), 0)
            run.assert_not_called()

    def test_language_cannot_come_from_development(self):
        path = self.root / ".agents/skills/ru-text"
        (path / "SKILL.md").unlink()
        path.rmdir()
        path.symlink_to(self.root / ".dev/.agents/skills/ru-text", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "языковой навык продукта"):
            setup.link_skills(self.root)

    def test_removed_development_skill_link_is_cleaned(self):
        setup.link_skills(self.root)
        path = self.root / ".dev/.agents/skills/ait-writing/SKILL.md"
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Устаревшая ссылка"):
            setup.link_skills(self.root, check=True)
        setup.link_skills(self.root)
        self.assertFalse((self.root / ".agents/skills/ait-writing").is_symlink())
        setup.link_skills(self.root, check=True)

    def test_public_package_keeps_current_dependencies_only(self):
        source = self.root / "source"
        skill = source / ".apm/skills/example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("public skill")
        for name in ("README.md", "CHANGELOG.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
            (source / name).write_text(name)
        manifest = ("name: example\nversion: 2.0.0\nscripts:\n  tests: secret\n"
                    "dependencies:\n  apm:\n    - talkstream/ru-text#v2.10.10\n"
                    "devDependencies:\n  apm:\n    - development/tools\nlicense: MIT\n")
        (source / "apm.yml").write_text(manifest)
        (source / "AGENTS.md").write_text("private rules")
        (source / ".dev").mkdir()
        target = self.root / "public"
        release.prepare(source, target)
        text = (target / "apm.yml").read_text()
        self.assertIn("talkstream/ru-text#v2.10.10", text)
        self.assertIn("license: MIT", text)
        self.assertNotIn("scripts:", text)
        self.assertNotIn("devDependencies:", text)
        self.assertFalse((target / "AGENTS.md").exists())
        self.assertFalse((target / ".dev").exists())
        self.assertEqual((target / ".apm/skills/example/SKILL.md").read_text(), "public skill")
        with self.assertRaises(FileExistsError):
            release.prepare(source, target)


if __name__ == "__main__":
    unittest.main()
