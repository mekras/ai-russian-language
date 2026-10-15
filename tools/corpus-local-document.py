#!/usr/bin/env python3
"""Maintain permitted local decision-record snapshots without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml


ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "knowledge"
INVENTORY = ROOT / ".agents/skills/kc-inventory/scripts"


class MaintenanceError(Exception):
    pass


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MaintenanceError("Ожидалась карточка YAML.")
    return data


def inside(path: Path, boundary: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(boundary.resolve()):
        raise MaintenanceError("Путь выходит за разрешённую область.")
    return resolved


def atomic_write(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise MaintenanceError("Запись через символическую ссылку запрещена.")
    if path.exists() and path.read_bytes() == content:
        return
    descriptor, name = tempfile.mkstemp(prefix=".corpus.tmp.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save(path: Path, data: dict) -> None:
    atomic_write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode())


def source_context(source_dir: Path, source_id: str | None = None,
                   locator: str | None = None) -> tuple[dict, Path, Path, dict]:
    source_dir = inside(source_dir, CORPUS / "data")
    source = load(inside(source_dir / "source.yml", source_dir))
    if source_id is not None and source.get("id") != source_id:
        raise MaintenanceError("Идентификатор не совпадает с карточкой источника.")
    if locator is not None and source.get("locator") != locator:
        raise MaintenanceError("Локатор не совпадает с карточкой источника.")
    if source.get("status") != "approved":
        raise MaintenanceError("Источник не одобрен для обработки.")
    if (source.get("requires_access_check") or
            source.get("processing_scope") == "metadata_only" or
            source.get("copy_policy") != "full_copy_allowed"):
        raise MaintenanceError("Требуется решение о допустимости получения материала.")
    if source.get("source_kind") != "decision_record":
        raise MaintenanceError("Поддерживаются только локальные записи решений.")
    document = inside(ROOT / source["locator"], ROOT / "docs/DR")
    if not document.is_file():
        raise MaintenanceError("Канонический документ недоступен.")
    items = sorted(source_dir.glob("documents/*/item.yml"))
    if len(items) != 1:
        raise MaintenanceError("Для локальной записи решения нужна ровно одна единица.")
    item_path = inside(items[0], source_dir)
    item = load(item_path)
    if item.get("source_id") != source.get("id") or item.get("workflow_stage") == "blocked":
        raise MaintenanceError("Единица заблокирована или относится к другому источнику.")
    if (item.get("requires_access_check") or
            item.get("copy_policy", source.get("copy_policy")) != "full_copy_allowed"):
        raise MaintenanceError("Ограничения единицы запрещают получение материала.")
    if item.get("processing_scope", source.get("processing_scope")) != "full":
        raise MaintenanceError("Для этой команды нужна разрешённая полная обработка.")
    return source, document, item_path, item


def pinned_content(source: dict, document: Path) -> bytes:
    content = document.read_bytes()
    if (source.get("content_hash_algorithm") != "sha256" or
            hashlib.sha256(content).hexdigest() != source.get("content_hash")):
        raise MaintenanceError("Документ изменён. Сначала нужна содержательная сверка утверждений.")
    return content


def check_excerpts(item_path: Path, content: bytes) -> dict | None:
    statements_path = item_path.parent / "statements.yml"
    if statements_path.is_symlink():
        raise MaintenanceError("Карточка утверждений не должна быть символической ссылкой.")
    if not statements_path.exists():
        return None
    data = load(statements_path)
    text = " ".join(content.decode("utf-8").split())
    for statement in data.get("statements", []):
        excerpt = statement.get("excerpt")
        artifact = statement.get("artifact")
        if not excerpt or " ".join(excerpt.split()) not in text or not artifact:
            raise MaintenanceError("Выдержка утверждения не совпадает с каноническим документом.")
        artifact_path = inside(item_path.parent / artifact, ROOT)
        if not artifact_path.is_file() or artifact_path.read_bytes() != content:
            raise MaintenanceError("Артефакт утверждения не совпадает с каноническим документом.")
    return data


def fetch(source: dict, document: Path, item_path: Path, item: dict) -> Path:
    content = pinned_content(source, document)
    check_excerpts(item_path, content)
    snapshot = item_path.parent / "snapshot.md"
    if snapshot.is_symlink():
        raise MaintenanceError("Снимок не должен быть символической ссылкой.")
    inside(snapshot, item_path.parent)
    atomic_write(snapshot, content)
    files = item.setdefault("files", {})
    tracked = files.setdefault("tracked", [])
    if "snapshot.md" not in tracked:
        tracked.append("snapshot.md")
    if item.get("workflow_stage") in {"indexed", "needs_fetch"}:
        item["workflow_stage"] = "fetched"
    save(item_path, item)
    return snapshot


def verify(source: dict, document: Path, item_path: Path) -> Path:
    content = pinned_content(source, document)
    check_excerpts(item_path, content)
    snapshot = inside(item_path.parent / "snapshot.md", item_path.parent)
    if not snapshot.is_file() or snapshot.read_bytes() != content:
        raise MaintenanceError("Снимок отсутствует или отличается. Выполните получение заново.")
    command = [sys.executable, str(INVENTORY / "record-snapshot-verification.py"),
               str(item_path.parent), str(snapshot),
               "--acquisition-method", "local_file",
               "--verification-method", "direct_reopen",
               "--content-scope", "full_text", "--content-match", "verified",
               "--scope-completeness", "complete", "--overall-result", "verified",
               "--metadata", "locator", "--metadata-result", "locator=verified",
               "--checked-by-role", "local-document-executor",
               "--limitation", "Проверен локальный канонический файл проекта. Актуальность удалённого репозитория не проверялась."]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise MaintenanceError("Не удалось записать проверку снимка: " + result.stderr.strip())
    return item_path.parent / "verification.yml"


def adapter(args: argparse.Namespace) -> dict:
    result = {"contract_version": 2, "operation": args.operation,
              "source_id": args.source_id, "adapter": "builtin.local-file",
              "artifacts": []}
    source_path = inside(ROOT / args.source_dir, CORPUS / "data")
    source = load(source_path / "source.yml")
    if source.get("id") != args.source_id or source.get("locator") != args.locator:
        raise MaintenanceError("Аргументы не совпадают с карточкой источника.")
    if source.get("requires_access_check") or source.get("processing_scope") == "metadata_only":
        result.update(status="terms-decision-required" if args.operation == "probe" else "access-limited",
                      message="Обработка ограничена до решения владельца.")
        return result
    source, document, item_path, item = source_context(source_path, args.source_id, args.locator)
    content = pinned_content(source, document)
    check_excerpts(item_path, content)
    if args.operation == "probe":
        result.update(status="ready", message="Разрешённый локальный документ доступен.")
    elif args.operation == "index":
        result.update(status="unchanged", message="Локальная единица и её канонический документ сверены.")
    elif args.operation == "fetch":
        artifact = fetch(source, document, item_path, item)
        result["artifacts"] = [artifact.relative_to(ROOT).as_posix()]
        result.update(status="synced", message="Снимок разрешённого документа получен.")
    elif args.operation == "verify":
        artifact = verify(source, document, item_path)
        result["artifacts"] = [artifact.relative_to(ROOT).as_posix()]
        result.update(status="verified", message="Снимок совпадает с локальным каноническим документом.")
    return result


def stage(operation: str) -> None:
    if operation == "corpus_validation":
        command = [sys.executable, str(INVENTORY / "validate-corpus-layout.py"),
                   str(CORPUS), "--project-root", str(ROOT), "--strict-admission",
                   "--operational", "--strict-statements",
                   "--strict-concepts", "--strict-verification"]
        subprocess.run(command, cwd=ROOT, check=True)
        return
    count = 0
    for source_path in sorted((CORPUS / "data").glob("*/source.yml")):
        source = load(source_path)
        if source.get("requires_access_check") or source.get("processing_scope") == "metadata_only":
            continue
        source, document, item_path, item = source_context(source_path.parent)
        content = pinned_content(source, document)
        statements = check_excerpts(item_path, content)
        if operation == "traceability":
            if statements:
                changed = False
                for statement in statements.get("statements", []):
                    status = statement.setdefault("processing_status", {})
                    if status.get("traceability") != "passed":
                        status["traceability"] = "passed"
                        changed = True
                if changed:
                    save(item_path.parent / "statements.yml", statements)
        else:
            fetch(source, document, item_path, item)
            if operation in {"source_check", "verification"}:
                verify(source, document, item_path)
        count += 1
    print(f"{operation}: проверено локальных источников — {count}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["probe", "index", "fetch", "verify",
                                              "traceability", "source_check",
                                              "verification", "corpus_validation"])
    parser.add_argument("--source-id")
    parser.add_argument("--source-dir")
    parser.add_argument("--locator")
    args = parser.parse_args()
    adapter_mode = any(value is not None for value in (args.source_id, args.source_dir, args.locator))
    if adapter_mode and (None in (args.source_id, args.source_dir, args.locator) or
                         args.operation not in {"probe", "index", "fetch", "verify"}):
        parser.error("Адаптеру нужны операция, идентификатор, каталог и локатор источника.")
    if not adapter_mode and args.operation in {"probe", "index", "verify"}:
        parser.error("Эта операция требует аргументов адаптера.")
    try:
        if adapter_mode:
            print(json.dumps(adapter(args), ensure_ascii=False))
        else:
            stage(args.operation)
        return 0
    except (MaintenanceError, OSError, KeyError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        print(f"Ошибка механической обработки корпуса: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
