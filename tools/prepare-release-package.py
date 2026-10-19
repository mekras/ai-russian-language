#!/usr/bin/env python3
"""Собрать публичный состав коллекции из текущих исходников."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parent.parent


def public_manifest(text: str) -> str:
    # Удаляем служебные секции целиком, сохраняя остальные поля без пересборки YAML.
    lines: list[str] = []
    skip = False
    for line in text.splitlines(keepends=True):
        field = re.match(r"^([A-Za-z][A-Za-z0-9_]*):", line)
        if field:
            skip = field[1] in {"scripts", "devDependencies"}
        if not skip:
            lines.append(line)
    return "".join(lines)


def prepare(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source / ".apm", destination / ".apm")
    for name in ("README.md", "CHANGELOG.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(source / name, destination / name)
    (destination / "apm.yml").write_text(
        public_manifest((source / "apm.yml").read_text(encoding="utf-8")),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Собрать публичный пакет в новом каталоге.")
    parser.add_argument("destination", type=Path, help="Новый каталог публичного пакета.")
    args = parser.parse_args()
    try:
        prepare(ROOT, args.destination)
    except OSError as error:
        parser.exit(1, f"Не удалось собрать публичный пакет: {error}\n")


if __name__ == "__main__":
    main()
