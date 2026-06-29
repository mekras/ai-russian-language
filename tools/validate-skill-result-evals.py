#!/usr/bin/env python3
"""Детерминированная проверка сценариев результата навыков."""

from __future__ import annotations

import argparse
import os

from run_skill_evals import iter_skill_dirs, load_compose_cases, print_result, read_skill_name, resolve_target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="Каталог всех навыков или одного навыка.")
    parser.add_argument("--case-id", help="Проверить наличие одного сценария. По умолчанию берётся APM_EVAL_CASE_ID.")
    args = parser.parse_args()

    target = resolve_target(args.target)
    case_id = args.case_id or os.environ.get("APM_EVAL_CASE_ID")

    try:
        found_any = False
        for skill_dir in iter_skill_dirs(target):
            compose_dataset = skill_dir / "evals" / "compose.jsonl"
            if not compose_dataset.is_file():
                continue
            cases = load_compose_cases(compose_dataset)
            if case_id is not None:
                cases = [case for case in cases if case["id"] == case_id]
                if not cases:
                    continue
            skill_name = read_skill_name(skill_dir / "SKILL.md") or skill_dir.name
            print_result(True, f"{skill_name}: сценарии результата", f"примеров={len(cases)}")
            found_any = True

        if not found_any:
            detail = (
                f"сценарий {case_id!r} не относится к этому набору"
                if case_id is not None
                else "для выбранного навыка не требуются"
            )
            print_result(True, "сценарии результата", detail)
    except Exception as exc:
        print_result(False, "сценарии результата", str(exc))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
