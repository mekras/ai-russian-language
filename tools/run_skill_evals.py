#!/usr/bin/env python3
"""Запуск проверок навыков через модельные вызовы по подписке Codex."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


MODEL = "gpt-5.3-codex-spark"
ROOT = Path(__file__).resolve().parent.parent
COMPOSE_DATASET = ROOT / "skills" / "ru-lang" / "evals" / "compose.jsonl"
CODEX_SUBAGENT = ROOT / "tools" / "codex-model-subagent"
RU_LANG_SKILL = ROOT / "skills" / "ru-lang" / "SKILL.md"
TRIGGER_FIELDS = {
    "id": str,
    "prompt": str,
    "should_trigger": bool,
    "rationale": str,
}


def print_result(ok: bool, label: str, detail: str = "") -> None:
    status = "ok" if ok else "ne ok"
    suffix = f": {detail}" if detail else ""
    print(f"{status} - {label}{suffix}")


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


def load_compose_cases() -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    with COMPOSE_DATASET.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            errors = []
            for field in ("id", "input", "forbidden_terms"):
                if not isinstance(record.get(field), str) or not record[field].strip():
                    errors.append(f"{field} должно быть непустой строкой")
            if errors:
                raise ValueError(f"{COMPOSE_DATASET}:{line_number}: {', '.join(errors)}")
            cases.append(record)
    if not cases:
        raise ValueError(f"{COMPOSE_DATASET}: набор данных должен содержать хотя бы один пример")
    return cases


def safe_name(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", case_id)


def run_codex_prompt(name: str, prompt: str) -> tuple[str, str]:
    print_result(True, f"запуск {name}", f"модель={MODEL}")
    env = os.environ.copy()
    env["CODEX_SUBAGENT_USAGE_LINE"] = "0"
    result = subprocess.run(
        [str(CODEX_SUBAGENT), MODEL, safe_name(name), prompt],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout.strip())

    final_path = None
    for line in result.stdout.splitlines():
        if line.startswith("final="):
            final_path = Path(line.removeprefix("final="))
            break
    if final_path is None:
        raise RuntimeError(f"{name}: запускатель Codex не вернул путь к итоговому файлу")
    return final_path.read_text(encoding="utf-8"), str(final_path)


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
    output, final_path = run_codex_prompt(f"{skill_name}-triggers", prompt)
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
            print_result(False, case_id, f"в ответе модели нет решения; output={final_path}")
            continue
        if actual[case_id] != expected:
            ok = False
            print_result(
                False,
                case_id,
                f"ожидалось {expected}, получено {actual[case_id]}; output={final_path}",
            )
        else:
            print_result(True, case_id, f"модель={MODEL}")
    print_result(ok, f"{skill_name} triggers", f"output={final_path}")
    return ok


def run_compose_case(case: dict[str, str]) -> tuple[str, str]:
    skill_text = RU_LANG_SKILL.read_text(encoding="utf-8")
    prompt = (
        "Примени навык ru-lang к пользовательскому запросу.\n"
        "Ответь только содержательным результатом, без пояснений про проверку.\n\n"
        f"Навык ru-lang:\n{skill_text}\n\n"
        f"Запрос пользователя:\n{case['input']}"
    )
    return run_codex_prompt(case["id"], prompt)


def validate_compose_cases() -> bool:
    ok = True
    for case in load_compose_cases():
        output, final_path = run_compose_case(case)
        forbidden_terms = [
            term.strip() for term in case["forbidden_terms"].split("|") if term.strip()
        ]
        output_folded = output.casefold()
        found_terms = [term for term in forbidden_terms if term.casefold() in output_folded]
        if found_terms:
            ok = False
            print_result(
                False,
                case["id"],
                f"найдены запрещённые формы: {', '.join(found_terms)}; output={final_path}",
            )
        else:
            print_result(True, case["id"], f"модель={MODEL}; output={final_path}")
    print_result(ok, "ru-lang compose")
    return ok


def main() -> int:
    try:
        checks = [
            validate_trigger_cases(ROOT / "skills" / "ru-lang"),
            validate_trigger_cases(ROOT / "skills" / "ru-dev"),
            validate_compose_cases(),
        ]
    except Exception as exc:
        print_result(False, "skill evals", str(exc))
        return 1
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
