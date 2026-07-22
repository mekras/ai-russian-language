#!/usr/bin/env python3
"""Запуск проверок навыков через модельные вызовы по подписке Codex."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CODEX_SUBAGENT = ROOT / "tools" / "codex-model-subagent"
DEFAULT_SKILLS_ROOT = ROOT / ".apm" / "skills"
MODEL_CONFIG_PATH = ROOT / "evals.local.toml"
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
RU_LANG_OPTIONAL_SETS = {
    "технический": "references/technical-russian.md",
    "строгий": "references/strict-rules.md",
    "разработка": "references/development-rules.md",
}
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


def load_model_config() -> tuple[str, int]:
    """Return the selected Codex model and the bounded retry count."""
    try:
        with MODEL_CONFIG_PATH.open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError as error:
        raise ValueError(
            f"Не найден локальный файл настройки модели: {MODEL_CONFIG_PATH}. "
            "Создайте его с разделом [model]."
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{MODEL_CONFIG_PATH}: ошибка TOML: {error}") from error

    model = config.get("model")
    if not isinstance(model, dict):
        raise ValueError(f"{MODEL_CONFIG_PATH}: не задан раздел [model]")
    primary = model.get("primary")
    max_attempts = model.get("max_attempts")
    if not isinstance(primary, str) or not primary:
        raise ValueError(f"{MODEL_CONFIG_PATH}: model.primary должен быть непустой строкой")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 2:
        raise ValueError(f"{MODEL_CONFIG_PATH}: model.max_attempts должен быть целым числом от 1 до 2")
    return primary, max_attempts


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


def resolve_target(raw_target: str | None) -> Path:
    value = raw_target or os.environ.get("APM_EVAL_PATH") or ".apm/skills"
    target = Path(value)
    if not target.is_absolute():
        target = ROOT / target
    return target


def iter_skill_dirs(target: Path) -> list[Path]:
    if (target / "SKILL.md").is_file():
        return [target]
    if not target.is_dir():
        raise ValueError(f"{target}: каталог навыков не найден")
    skill_dirs = sorted(path for path in target.iterdir() if (path / "SKILL.md").is_file())
    if not skill_dirs:
        raise ValueError(f"{target}: не найдено ни одного каталога навыка с SKILL.md")
    return skill_dirs


def find_skill_dir(target: Path, name: str) -> Path | None:
    if target.name == name and (target / "SKILL.md").is_file():
        return target
    candidate = target / name
    if (candidate / "SKILL.md").is_file():
        return candidate
    return None


def load_trigger_cases(
    skill_dir: Path,
    *,
    filter_case_id: str | None = None,
) -> tuple[str, str, list[dict[str, object]]]:
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
    if filter_case_id is not None:
        cases = [case for case in cases if case["id"] == filter_case_id]

    return skill_name, skill_path.read_text(encoding="utf-8"), cases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Запуск проверок навыков через модельные вызовы Codex.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Каталог всех навыков или одного навыка. По умолчанию берётся APM_EVAL_PATH или .apm/skills.",
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
    parser.add_argument(
        "--case-id",
        help="Запустить один сценарий по id. Если не задано, используется APM_EVAL_CASE_ID.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Проверить структуру compose-сценариев без вызова модели.",
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


def load_compose_cases(compose_dataset: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_inputs: set[str] = set()
    decoder = json.JSONDecoder()
    content = compose_dataset.read_text(encoding="utf-8")
    position = 0
    while position < len(content):
        while position < len(content) and content[position].isspace():
            position += 1
        if position >= len(content):
            break
        try:
            record, position = decoder.raw_decode(content, position)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{compose_dataset}:{exc.lineno}: {exc.msg}") from exc
        line_number = content.count("\n", 0, position) + 1
        if not isinstance(record, dict):
            raise ValueError(f"{compose_dataset}:{line_number}: корень примера должен быть объектом")
        errors = []
        for field in COMPOSE_TEXT_FIELDS:
            if not isinstance(record.get(field), str) or not record[field].strip():
                errors.append(f"{field} должно быть непустой строкой")
        if errors:
            raise ValueError(f"{compose_dataset}:{line_number}: {', '.join(errors)}")
        case_id = str(record["id"])
        case_input = str(record["input"])
        if case_id in seen_ids:
            raise ValueError(
                f"{compose_dataset}:{line_number}: повторяющийся id {case_id!r}",
            )
        if case_input in seen_inputs:
            raise ValueError(
                f"{compose_dataset}:{line_number}: повторяющийся input",
            )
        seen_ids.add(case_id)
        seen_inputs.add(case_input)
        oracle = record.get("oracle")
        if not isinstance(oracle, dict):
            raise ValueError(f"{compose_dataset}:{line_number}: oracle должен быть объектом")
        normalized_oracle: dict[str, object] = {}
        for field in (
            "forbidden_substrings",
            "required_substrings",
            "required_any_substrings",
        ):
            values = validate_string_list(
                oracle.get(field),
                label=f"{compose_dataset}:{line_number}: oracle.{field}",
                allow_empty=True,
            )
            if values:
                normalized_oracle[field] = values
        required_any_groups = validate_string_groups(
            oracle.get("required_any_groups"),
            label=f"{compose_dataset}:{line_number}: oracle.required_any_groups",
        )
        if required_any_groups:
            normalized_oracle["required_any_groups"] = required_any_groups
        if not normalized_oracle:
            raise ValueError(
                f"{compose_dataset}:{line_number}: oracle должен содержать хотя бы одно правило",
            )
        unexpected_fields = sorted(set(oracle) - set(COMPOSE_ORACLE_FIELDS))
        if unexpected_fields:
            unexpected = ", ".join(unexpected_fields)
            raise ValueError(
                f"{compose_dataset}:{line_number}: неожиданные поля oracle: {unexpected}",
            )
        record["oracle"] = normalized_oracle
        cases.append(record)
    if not cases:
        raise ValueError(f"{compose_dataset}: набор данных должен содержать хотя бы один пример")
    return cases


def safe_name(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", case_id)


def active_ru_lang_sets(case_input: str) -> set[str]:
    """Return the rule sets selected by the marker in an evaluation input."""
    marker = re.search(r"`?ru-lang\s*:\s*([^`\n.]+)`?", case_input, flags=re.IGNORECASE)
    if marker is None:
        return set(RU_LANG_OPTIONAL_SETS)

    requested = {
        item.strip().casefold()
        for item in marker.group(1).split(",")
        if item.strip()
    }
    if not requested <= {"основной", *RU_LANG_OPTIONAL_SETS}:
        return set()
    return requested & set(RU_LANG_OPTIONAL_SETS)


def load_ru_lang_materials(ru_lang_dir: Path, case_input: str) -> str:
    parts = [
        ("SKILL.md", (ru_lang_dir / "SKILL.md").read_text(encoding="utf-8")),
        ("references/main-rules.md", (ru_lang_dir / "references" / "main-rules.md").read_text(encoding="utf-8")),
    ]
    active_sets = active_ru_lang_sets(case_input)
    for set_name, relative_path in RU_LANG_OPTIONAL_SETS.items():
        if set_name not in active_sets:
            continue
        parts.append((relative_path, (ru_lang_dir / relative_path).read_text(encoding="utf-8")))
        if set_name == "строгий":
            relative_path = "assets/strict-replacements.md"
            parts.append((relative_path, (ru_lang_dir / relative_path).read_text(encoding="utf-8")))
    return "\n\n".join(f"# {title}\n\n{text}" for title, text in parts)


def load_skill_materials(skill_dir: Path, case_input: str) -> tuple[str, str]:
    skill_name = read_skill_name(skill_dir / "SKILL.md")
    if not skill_name:
        raise ValueError(f"{skill_dir / 'SKILL.md'}: не найдено имя навыка во frontmatter")
    if skill_name == "ru-lang":
        return skill_name, load_ru_lang_materials(skill_dir, case_input)
    raise ValueError(f"{skill_dir}: сценарии результата не поддержаны для навыка {skill_name!r}")


def run_codex_prompt(name: str, prompt: str) -> tuple[str, str, str]:
    primary_model, max_attempts = load_model_config()
    env = os.environ.copy()
    env["CODEX_SUBAGENT_USAGE_LINE"] = "0"
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        label = name if attempt == 1 else f"{name} (повтор {attempt}/{max_attempts})"
        print_info(label, f"модель={primary_model}")
        result = subprocess.run(
            [str(CODEX_SUBAGENT), primary_model, safe_name(name), prompt],
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
        return final_path.read_text(encoding="utf-8"), str(final_path), primary_model

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


def validate_trigger_cases(skill_dir: Path, *, case_id: str | None = None) -> bool | None:
    skill_name, skill_text, cases = load_trigger_cases(skill_dir, filter_case_id=case_id)
    if not cases:
        return None
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


def run_compose_case(
    case: dict[str, object],
    *,
    with_skill: bool,
    skill_dir: Path,
) -> tuple[str, str, str]:
    if with_skill:
        case_input = str(case["input"])
        skill_name, skill_text = load_skill_materials(skill_dir, case_input)
        prompt = (
            f"Примени навык {skill_name} к пользовательскому запросу.\n"
            "Ответь только содержательным результатом, без пояснений про проверку.\n\n"
        )
        prompt += (
            f"Навык {skill_name}:\n{skill_text}\n\n"
            f"Запрос пользователя:\n{case['input']}"
        )
    else:
        prompt = (
            "Ответь на пользовательский запрос по-русски.\n"
            "Ответь только содержательным результатом, без пояснений про проверку.\n\n"
            f"Запрос пользователя:\n{case['input']}"
        )
    return run_codex_prompt(case["id"], prompt)


def collect_compose_failures(output: str, oracle: dict[str, object]) -> list[str]:
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
    return details


def validate_compose_cases(
    skill_dir: Path,
    *,
    with_skill: bool,
    compare_baseline: bool,
    case_id: str | None = None,
) -> bool | None:
    compose_dataset = skill_dir / "evals" / "compose.jsonl"
    if not compose_dataset.is_file():
        return None
    skill_name = read_skill_name(skill_dir / "SKILL.md") or skill_dir.name
    ok = True
    cases = load_compose_cases(compose_dataset)
    if case_id is not None:
        cases = [case for case in cases if case["id"] == case_id]
        if not cases:
            return None

    comparison = {
        "baseline_passed": 0,
        "skill_passed": 0,
        "improved": 0,
        "worsened": 0,
        "unchanged": 0,
    }
    for case in cases:
        oracle = case["oracle"]
        baseline_passed: bool | None = None
        if compare_baseline:
            baseline_output, baseline_path, baseline_model = run_compose_case(
                case,
                with_skill=False,
                skill_dir=skill_dir,
            )
            baseline_details = collect_compose_failures(baseline_output, oracle)
            baseline_passed = not baseline_details
            if baseline_passed:
                comparison["baseline_passed"] += 1
            print_result(
                baseline_passed,
                f"{case['id']}: базовая линия",
                (
                    f"модель={baseline_model}; файл={baseline_path}"
                    if baseline_passed
                    else f"{'; '.join(baseline_details)}; модель={baseline_model}; файл={baseline_path}"
                ),
            )

        output, final_path, used_model = run_compose_case(
            case,
            with_skill=with_skill,
            skill_dir=skill_dir,
        )
        details = collect_compose_failures(output, oracle)
        skill_passed = not details
        if with_skill and skill_passed:
            comparison["skill_passed"] += 1
        if baseline_passed is not None:
            if not baseline_passed and skill_passed:
                comparison["improved"] += 1
            elif baseline_passed and not skill_passed:
                comparison["worsened"] += 1
            else:
                comparison["unchanged"] += 1
        if details:
            ok = False
            print_result(
                False,
                case["id"],
                f"{'; '.join(details)}; модель={used_model}; файл={final_path}",
            )
            continue
        mode_label = "с навыком" if with_skill else "без навыка"
        print_result(
            True,
            case["id"],
            f"{mode_label}; модель={used_model}; файл={final_path}",
        )
    summary_label = (
        f"{skill_name}: проверка результата с навыком"
        if with_skill
        else f"{skill_name}: проверка результата без навыка"
    )
    print_result(ok, summary_label)
    if compare_baseline:
        print_info(
            f"{skill_name}: сравнение с базовой линией",
            (
                f"без навыка={comparison['baseline_passed']}/{len(cases)}; "
                f"с навыком={comparison['skill_passed']}/{len(cases)}; "
                f"исправлено={comparison['improved']}; "
                f"ухудшено={comparison['worsened']}; "
                f"без изменения={comparison['unchanged']}"
            ),
        )
    return ok


def main() -> int:
    args = parse_args()
    try:
        target = resolve_target(args.target)
        case_id = args.case_id or args.case or os.environ.get("APM_EVAL_CASE_ID")
        if args.validate_only:
            if args.checks != "compose":
                raise ValueError("Режим --validate-only поддержан только вместе с --checks compose")
            if args.without_skill:
                raise ValueError("Режим --without-skill не применяется к проверке структуры")
            checked = 0
            for skill_dir in iter_skill_dirs(target):
                compose_dataset = skill_dir / "evals" / "compose.jsonl"
                if not compose_dataset.is_file():
                    continue
                cases = load_compose_cases(compose_dataset)
                if case_id is not None:
                    cases = [case for case in cases if case["id"] == case_id]
                checked += len(cases)
            if not checked:
                if case_id is not None:
                    raise ValueError(f"{target}: сценарий {case_id!r} не найден")
                raise ValueError(f"{target}: compose-сценарии не найдены")
            print_result(True, "структура compose-сценариев", f"сценариев={checked}")
            return 0
        if args.without_skill and args.checks != "compose":
            raise ValueError("Режим --without-skill поддержан только вместе с --checks compose")
        if args.case and args.checks != "compose":
            raise ValueError("Фильтр --case поддержан только вместе с --checks compose")

        checks: list[bool] = []
        if args.checks in {"all", "triggers"}:
            for skill_dir in iter_skill_dirs(target):
                result = validate_trigger_cases(skill_dir, case_id=case_id)
                if result is not None:
                    checks.append(result)
        if args.checks in {"all", "compose"}:
            for skill_dir in iter_skill_dirs(target):
                result = validate_compose_cases(
                    skill_dir,
                    with_skill=not args.without_skill,
                    compare_baseline=not args.without_skill,
                    case_id=case_id,
                )
                if result is not None:
                    checks.append(result)
        if case_id is not None and not checks:
            raise ValueError(f"{target}: сценарий {case_id!r} не найден")
        if not checks:
            raise ValueError(f"{target}: для выбранного режима нет проверок")
    except Exception as exc:
        print_result(False, "проверки навыков", str(exc))
        return 1
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
