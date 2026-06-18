#!/usr/bin/env python3
"""Детерминированная проверка сценариев результата навыков."""

from __future__ import annotations

import argparse
import os

from run_skill_evals import find_skill_dir, load_compose_cases, print_result, resolve_target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="Каталог всех навыков или одного навыка.")
    parser.add_argument("--case-id", help="Проверить наличие одного сценария. По умолчанию берётся APM_EVAL_CASE_ID.")
    args = parser.parse_args()

    target = resolve_target(args.target)
    case_id = args.case_id or os.environ.get("APM_EVAL_CASE_ID")

    try:
        ru_lang_dir = find_skill_dir(target, "ru-lang")
        if ru_lang_dir is None:
            detail = (
                f"сценарий {case_id!r} не относится к этому набору"
                if case_id is not None
                else "для выбранного навыка не требуются"
            )
            print_result(True, "сценарии результата", detail)
            return 0

        compose_dataset = ru_lang_dir / "evals" / "compose.jsonl"
        cases = load_compose_cases(compose_dataset)
        if case_id is not None:
            cases = [case for case in cases if case["id"] == case_id]
            if not cases:
                print_result(True, "сценарии результата", f"сценарий {case_id!r} не относится к этому набору")
                return 0
        print_result(True, "ru-lang: сценарии результата", f"примеров={len(cases)}")
    except Exception as exc:
        print_result(False, "сценарии результата", str(exc))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
