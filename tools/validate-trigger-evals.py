#!/usr/bin/env python3
"""Детерминированная проверка наборов срабатывания навыков."""

from __future__ import annotations

import argparse
import os

from run_skill_evals import iter_skill_dirs, load_trigger_cases, print_result, resolve_target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="Каталог всех навыков или одного навыка.")
    parser.add_argument("--require-all", action="store_true", help="Требовать evals/triggers.json у каждого навыка.")
    parser.add_argument("--case-id", help="Проверить наличие одного сценария. По умолчанию берётся APM_EVAL_CASE_ID.")
    args = parser.parse_args()

    target = resolve_target(args.target)
    case_id = args.case_id or os.environ.get("APM_EVAL_CASE_ID")
    matched_case = False
    ok = True

    try:
        for skill_dir in iter_skill_dirs(target):
            trigger_path = skill_dir / "evals" / "triggers.json"
            if not trigger_path.is_file():
                if args.require_all:
                    ok = False
                    print_result(False, f"{skill_dir.name}: проверки срабатывания", f"{trigger_path} не найден")
                continue
            skill_name, _, cases = load_trigger_cases(skill_dir, filter_case_id=case_id)
            if case_id is not None and not cases:
                continue
            matched_case = matched_case or case_id is None or bool(cases)
            print_result(True, f"{skill_name}: проверки срабатывания", f"примеров={len(cases)}")

        if case_id is not None and not matched_case:
            print_result(True, "проверки срабатывания", f"сценарий {case_id!r} не относится к этому набору")
    except Exception as exc:
        print_result(False, "проверки срабатывания", str(exc))
        return 1

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
