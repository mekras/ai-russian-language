#!/usr/bin/env python3
"""Установить продукт и средства разработки по независимым файлам блокировки."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
LANGUAGE_SKILLS = frozenset({"ru-lang", "ru-text", "ru-check", "ru-score"})


def link_skills(root: Path, *, check: bool = False) -> None:
    """Подключить навыки разработки, сохранив языковые навыки продукта."""
    planned: list[tuple[Path, Path]] = []
    obsolete: list[Path] = []
    for name in LANGUAGE_SKILLS:
        for client in (".agents", ".claude"):
            path = root / client / "skills" / name
            if not (path / "SKILL.md").is_file() or path.is_symlink():
                raise ValueError(f"Не установлен языковой навык продукта: {path}")
    for client in (".agents", ".claude"):
        source = root / ".dev" / client / "skills"
        if not (source / "ait-routing" / "SKILL.md").is_file():
            raise ValueError(f"Не установлены навыки разработки: {source}")
        destination = root / client / "skills"
        for target in destination.iterdir():
            if target.is_symlink() and target.resolve().parent == source.resolve() \
                    and not (target / "SKILL.md").is_file():
                if check:
                    raise ValueError(f"Устаревшая ссылка на навык разработки: {target}")
                obsolete.append(target)
        for skill in sorted(source.iterdir()):
            if skill.name in LANGUAGE_SKILLS or not (skill / "SKILL.md").is_file():
                continue
            target = root / client / "skills" / skill.name
            if target.is_symlink() and target.resolve() == skill.resolve():
                continue
            if target.exists() or target.is_symlink():
                raise ValueError(f"Нельзя заменить существующий навык: {target}")
            if check:
                raise ValueError(f"Не подключён навык разработки: {target}")
            planned.append((target, skill))
    for target in obsolete:
        target.unlink()
    for target, skill in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(os.path.relpath(skill, target.parent), target_is_directory=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Проверить подключение навыков без установки.")
    args = parser.parse_args()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        if args.check:
            link_skills(ROOT, check=True)
            print("Навыки разработки подключены, языковые навыки принадлежат продукту.")
            return 0
        # Общий запускатель проверяет артефакты относительно каждой установки.
        validator_dir = ROOT / ".dev" / "tools"
        validator_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "tools" / "validate-python-artifacts.py", validator_dir)
        for project in (ROOT, ROOT / ".dev"):
            subprocess.run(
                [sys.executable, str(ROOT / "tools" / "run-apm-safe.py"),
                 "--project-root", str(project)],
                cwd=ROOT, env=env, check=True,
            )
        link_skills(ROOT)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Настройка APM не завершена: {error}", file=sys.stderr)
        return 1
    print("Продукт и средства разработки установлены. Навыки доступны из корня.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
