#!/usr/bin/env python3
"""Запуск проверок навыков через модельные вызовы по подписке Codex."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


PRIMARY_MODEL = "gpt-5.3-codex-spark"
ROOT = Path(__file__).resolve().parent.parent
COMPOSE_DATASET = ROOT / "skills" / "ru-lang" / "evals" / "compose.jsonl"
CODEX_SUBAGENT = ROOT / "tools" / "codex-model-subagent"
RU_LANG_SKILL = ROOT / "skills" / "ru-lang" / "SKILL.md"
RU_LANG_BASE_LANGUAGE = ROOT / "skills" / "ru-lang" / "references" / "base-language.md"
RU_LANG_TECHNICAL = ROOT / "skills" / "ru-lang" / "references" / "technical-russian.md"
RU_LANG_REPLACEMENTS = ROOT / "skills" / "ru-lang" / "assets" / "term-replacements.md"
RU_LANG_HYBRIDS = ROOT / "skills" / "ru-lang" / "assets" / "hybrid-examples.md"
TRIGGER_FIELDS = {
    "id": str,
    "prompt": str,
    "should_trigger": bool,
    "rationale": str,
}
COMPOSE_TEXT_FIELDS = ("id", "input")
COMPOSE_ORACLE_FIELDS = (
    "forbidden_substrings",
    "required_substrings",
    "required_any_substrings",
    "required_any_groups",
)
MAX_MODEL_ATTEMPTS = 3
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"


def use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def colorize(text: str, color: str) -> str:
    if not use_color():
        return text
    return f"{color}{text}{ANSI_RESET}"


def print_info(label: str, detail: str = "") -> None:
    suffix = f": {detail}" if detail else ""
    print(f"Запуск - {label}{suffix}", flush=True)


def print_result(ok: bool, label: str, detail: str = "") -> None:
    status = colorize("Пройден", ANSI_GREEN) if ok else colorize("Провален", ANSI_RED)
    suffix = f": {detail}" if detail else ""
    print(f"{status} - {label}{suffix}", flush=True)


def read_skill_name(skill_path: Path) -> str | None:
    in_frontmatter = False
    for line in skill_path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            break
        if in_frontmatter and line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def load_trigger_cases(skill_dir: Path) -> tuple[str, str, list[dict[str, object]]]:
    skill_path = skill_dir / "SKILL.md"
    trigger_path = skill_dir / "evals" / "triggers.json"
    skill_name = read_skill_name(skill_path)
    if not skill_name:
        raise ValueError(f"{skill_path}: не найдено имя навыка во frontmatter")

    data = json.loads(trigger_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{trigger_path}: корень JSON должен быть объектом")
    if data.get("skill_name") != skill_name:
        raise ValueError(f"{trigger_path}: skill_name должен быть равен {skill_name!r}")

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{trigger_path}: cases должен быть непустым массивом")

    seen_ids: set[str] = set()
    seen_prompts: set[str] = set()
    trigger_values: set[bool] = set()
    for index, case in enumerate(cases):
        label = f"{trigger_path}: cases[{index}]"
        if not isinstance(case, dict):
            raise ValueError(f"{label}: значение должно быть объектом")
        for field, expected_type in TRIGGER_FIELDS.items():
            value = case.get(field)
            if expected_type is bool:
                if not isinstance(value, bool):
                    raise ValueError(f"{label}: поле {field} должно быть булевым")
                trigger_values.add(value)
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label}: поле {field} должно быть непустой строкой")
        case_id = str(case["id"])
        prompt = str(case["prompt"])
        if not case_id.startswith(f"{skill_name}-"):
            raise ValueError(f"{label}: id должен начинаться с {skill_name!r}")
        if case_id in seen_ids:
            raise ValueError(f"{label}: повторяющийся id {case_id!r}")
        if prompt in seen_prompts:
            raise ValueError(f"{label}: повторяющийся prompt")
        seen_ids.add(case_id)
        seen_prompts.add(prompt)
    if trigger_values != {False, True}:
        raise ValueError(
            f"{trigger_path}: нужны примеры и с should_trigger=true, и с should_trigger=false",
        )

    return skill_name, skill_path.read_text(encoding="utf-8"), cases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Запуск проверок навыков через модельные вызовы Codex.",
    )
    parser.add_argument(
        "--checks",
        choices=("all", "triggers", "compose"),
        default="all",
        help="Какие проверки запускать: все, только triggers или только compose.",
    )
    parser.add_argument(
        "--without-skill",
        action="store_true",
        help="Запускать compose-проверки без подстановки текста навыка ru-lang.",
    )
    parser.add_argument(
        "--case",
        help="Запустить один compose-сценарий по id. Используется только с --checks compose.",
    )
    return parser.parse_args()


def validate_string_list(
    value: object,
    *,
    label: str,
    allow_empty: bool = False,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label}: значение должно быть массивом строк")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label}[{index}]: значение должно быть непустой строкой")
        items.append(item)
    if not allow_empty and not items:
        raise ValueError(f"{label}: массив не должен быть пустым")
    return items


def validate_string_groups(value: object, *, label: str) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label}: значение должно быть массивом массивов строк")
    groups: list[list[str]] = []
    for index, group in enumerate(value):
        groups.append(validate_string_list(group, label=f"{label}[{index}]"))
    return groups


def load_compose_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    decoder = json.JSONDecoder()
    content = COMPOSE_DATASET.read_text(encoding="utf-8")
    position = 0
    while position < len(content):
        while position < len(content) and content[position].isspace():
            position += 1
        if position >= len(content):
            break
        try:
            record, position = decoder.raw_decode(content, position)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{COMPOSE_DATASET}:{exc.lineno}: {exc.msg}") from exc
        line_number = content.count("\n", 0, position) + 1
        if not isinstance(record, dict):
            raise ValueError(f"{COMPOSE_DATASET}:{line_number}: корень примера должен быть объектом")
        errors = []
        for field in COMPOSE_TEXT_FIELDS:
            if not isinstance(record.get(field), str) or not record[field].strip():
                errors.append(f"{field} должно быть непустой строкой")
        if errors:
            raise ValueError(f"{COMPOSE_DATASET}:{line_number}: {', '.join(errors)}")
        oracle = record.get("oracle")
        if not isinstance(oracle, dict):
            raise ValueError(f"{COMPOSE_DATASET}:{line_number}: oracle должен быть объектом")
        normalized_oracle: dict[str, object] = {}
        for field in (
            "forbidden_substrings",
            "required_substrings",
            "required_any_substrings",
        ):
            values = validate_string_list(
                oracle.get(field),
                label=f"{COMPOSE_DATASET}:{line_number}: oracle.{field}",
                allow_empty=True,
            )
            if values:
                normalized_oracle[field] = values
        required_any_groups = validate_string_groups(
            oracle.get("required_any_groups"),
            label=f"{COMPOSE_DATASET}:{line_number}: oracle.required_any_groups",
        )
        if required_any_groups:
            normalized_oracle["required_any_groups"] = required_any_groups
        if not normalized_oracle:
            raise ValueError(
                f"{COMPOSE_DATASET}:{line_number}: oracle должен содержать хотя бы одно правило",
            )
        unexpected_fields = sorted(set(oracle) - set(COMPOSE_ORACLE_FIELDS))
        if unexpected_fields:
            unexpected = ", ".join(unexpected_fields)
            raise ValueError(
                f"{COMPOSE_DATASET}:{line_number}: неожиданные поля oracle: {unexpected}",
            )
        record["oracle"] = normalized_oracle
        cases.append(record)
    if not cases:
        raise ValueError(f"{COMPOSE_DATASET}: набор данных должен содержать хотя бы один пример")
    return cases


def safe_name(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", case_id)


def load_ru_lang_materials() -> str:
    parts = [
        ("SKILL.md", RU_LANG_SKILL.read_text(encoding="utf-8")),
        ("references/base-language.md", RU_LANG_BASE_LANGUAGE.read_text(encoding="utf-8")),
        ("references/technical-russian.md", RU_LANG_TECHNICAL.read_text(encoding="utf-8")),
        ("assets/term-replacements.md", RU_LANG_REPLACEMENTS.read_text(encoding="utf-8")),
        ("assets/hybrid-examples.md", RU_LANG_HYBRIDS.read_text(encoding="utf-8")),
    ]
    return "\n\n".join(f"# {title}\n\n{text}" for title, text in parts)


def run_codex_prompt(name: str, prompt: str) -> tuple[str, str, str]:
    env = os.environ.copy()
    env["CODEX_SUBAGENT_USAGE_LINE"] = "0"
    last_error = ""
    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        label = name if attempt == 1 else f"{name} (повтор {attempt}/{MAX_MODEL_ATTEMPTS})"
        print_info(label, f"модель={PRIMARY_MODEL}")
        result = subprocess.run(
            [str(CODEX_SUBAGENT), PRIMARY_MODEL, safe_name(name), prompt],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode != 0:
            last_error = result.stdout.strip() or (
                f"процесс Codex завершился с кодом {result.returncode} без вывода"
            )
            continue

        final_path = None
        for line in result.stdout.splitlines():
            if line.startswith("final="):
                final_path = Path(line.removeprefix("final="))
                break
        if final_path is None:
            last_error = "процесс Codex не вернул путь к итоговому файлу"
            continue
        return final_path.read_text(encoding="utf-8"), str(final_path), PRIMARY_MODEL

    raise RuntimeError(f"{name}: {last_error}")


def extract_json_object(text: str) -> dict[str, object]:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if match:
        return json.loads(match.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("ответ модели не содержит JSON-объект")
    return json.loads(text[start : end + 1])


def validate_trigger_cases(skill_dir: Path) -> bool:
    skill_name, skill_text, cases = load_trigger_cases(skill_dir)
    prompt_cases = [
        {"id": case["id"], "prompt": case["prompt"]}
        for case in cases
    ]
    prompt = (
        "Определи, должен ли агент применить навык к каждому запросу.\n"
        "Верни только JSON без Markdown: "
        "{\"results\":[{\"id\":\"...\",\"should_trigger\":true,\"reason\":\"...\"}]}.\n\n"
        f"Навык:\n{skill_text}\n\n"
        f"Запросы:\n{json.dumps(prompt_cases, ensure_ascii=False, indent=2)}"
    )
    output, final_path, used_model = run_codex_prompt(f"{skill_name}-triggers", prompt)
    response = extract_json_object(output)
    results = response.get("results")
    if not isinstance(results, list):
        raise ValueError(f"{skill_name}: ответ модели должен содержать массив results")

    actual: dict[str, bool] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        case_id = result.get("id")
        should_trigger = result.get("should_trigger")
        if isinstance(case_id, str) and isinstance(should_trigger, bool):
            actual[case_id] = should_trigger

    ok = True
    for case in cases:
        case_id = str(case["id"])
        expected = bool(case["should_trigger"])
        if case_id not in actual:
            ok = False
            print_result(False, case_id, f"в ответе модели нет решения; файл={final_path}")
            continue
        if actual[case_id] != expected:
            ok = False
            print_result(
                False,
                case_id,
                f"ожидалось {expected}, получено {actual[case_id]}; файл={final_path}",
            )
        else:
            print_result(True, case_id, f"модель={used_model}")
    print_result(ok, f"{skill_name}: проверка срабатывания", f"файл={final_path}")
    return ok


def run_compose_case(case: dict[str, object], *, with_skill: bool) -> tuple[str, str, str]:
    if with_skill:
        skill_text = load_ru_lang_materials()
        prompt = (
            "Примени навык ru-lang к пользовательскому запросу.\n"
            "Ответь только содержательным результатом, без пояснений про проверку.\n\n"
            f"Навык ru-lang:\n{skill_text}\n\n"
            f"Запрос пользователя:\n{case['input']}"
        )
    else:
        prompt = (
            "Ответь на пользовательский запрос по-русски.\n"
            "Ответь только содержательным результатом, без пояснений про проверку.\n\n"
            f"Запрос пользователя:\n{case['input']}"
        )
    return run_codex_prompt(case["id"], prompt)


def validate_compose_cases(*, with_skill: bool, case_id: str | None = None) -> bool:
    ok = True
    cases = load_compose_cases()
    if case_id is not None:
        cases = [case for case in cases if case["id"] == case_id]
        if not cases:
            raise ValueError(f"{COMPOSE_DATASET}: compose-сценарий {case_id!r} не найден")

    for case in cases:
        output, final_path, used_model = run_compose_case(case, with_skill=with_skill)
        oracle = case["oracle"]
        output_folded = output.casefold()
        found_forbidden = [
            term
            for term in oracle.get("forbidden_substrings", [])
            if term.casefold() in output_folded
        ]
        missing_required = [
            term
            for term in oracle.get("required_substrings", [])
            if term.casefold() not in output_folded
        ]
        required_any = oracle.get("required_any_substrings", [])
        missing_required_any = bool(required_any) and not any(
            term.casefold() in output_folded for term in required_any
        )
        required_any_groups = oracle.get("required_any_groups", [])
        missing_required_any_groups = [
            group
            for group in required_any_groups
            if not any(term.casefold() in output_folded for term in group)
        ]

        details = []
        if found_forbidden:
            details.append(f"найдены запрещённые формы: {', '.join(found_forbidden)}")
        if missing_required:
            details.append(f"нет обязательных фрагментов: {', '.join(missing_required)}")
        if missing_required_any:
            details.append(
                "нет ни одного допустимого фрагмента из набора: "
                + ", ".join(required_any),
            )
        if missing_required_any_groups:
            details.extend(
                "нет ни одного допустимого фрагмента из группы: "
                + ", ".join(group)
                for group in missing_required_any_groups
            )

        if details:
            ok = False
            print_result(
                False,
                case["id"],
                f"{'; '.join(details)}; файл={final_path}",
            )
        else:
            mode_label = "с навыком" if with_skill else "без навыка"
            print_result(True, case["id"], f"{mode_label}; модель={used_model}; файл={final_path}")
    summary_label = (
        "ru-lang: проверка русских формулировок с навыком"
        if with_skill
        else "ru-lang: проверка русских формулировок без навыка"
    )
    print_result(ok, summary_label)
    return ok


def main() -> int:
    args = parse_args()
    try:
        if args.without_skill and args.checks != "compose":
            raise ValueError("Режим --without-skill поддержан только вместе с --checks compose")
        if args.case and args.checks != "compose":
            raise ValueError("Фильтр --case поддержан только вместе с --checks compose")

        checks = []
        if args.checks in {"all", "triggers"}:
            checks.extend(
                [
                    validate_trigger_cases(ROOT / "skills" / "ru-lang"),
                    validate_trigger_cases(ROOT / "skills" / "ru-dev"),
                ],
            )
        if args.checks in {"all", "compose"}:
            checks.append(validate_compose_cases(with_skill=not args.without_skill, case_id=args.case))
    except Exception as exc:
        print_result(False, "проверки навыков", str(exc))
        return 1
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
