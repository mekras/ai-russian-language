#!/usr/bin/env python3
"""Exercise write boundaries and refusal paths of the local corpus worker."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import json
import hashlib
from datetime import UTC, datetime

import yaml


ROOT = Path(__file__).resolve().parent.parent
SOURCE = "knowledge/data/test-decision"
DOCUMENT = "docs/DR/test-decision.md"


class LocalDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".corpus.tmp.", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in ("tools/corpus-local-document.py",
                         "knowledge/corpus.yml",
                         ".agents/skills/kc-inventory/scripts/record-snapshot-verification.py"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        document = self.root / DOCUMENT
        document.parent.mkdir(parents=True, exist_ok=True)
        content = "# Тестовый документ\n\nТестовое правило для проверки снимка.\n"
        document.write_text(content)
        folder = self.root / SOURCE
        unit = folder / "documents/test-decision"
        unit.mkdir(parents=True)
        records = {
            folder / "source.yml": {
                "id": "TEST", "status": "approved", "locator": DOCUMENT,
                "source_kind": "decision_record", "processing_scope": "full",
                "copy_policy": "full_copy_allowed", "requires_access_check": False,
                "content_hash_algorithm": "sha256",
                "content_hash": hashlib.sha256(document.read_bytes()).hexdigest(),
            },
            unit / "item.yml": {
                "id": "TEST-DOCUMENT", "source_id": "TEST",
                "workflow_stage": "source_checked", "processing_scope": "full",
                "copy_policy": "full_copy_allowed", "requires_access_check": False,
                "files": {"tracked": ["snapshot.md"], "local": []},
            },
            unit / "statements.yml": {
                "statements": [{"id": "TEST-001",
                    "excerpt": "Тестовое правило для проверки снимка.",
                    "artifact": "../../../../../" + DOCUMENT}],
            },
        }
        for path, data in records.items():
            path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        (unit / "snapshot.md").write_bytes(document.read_bytes())

    def run_worker(self, *arguments):
        return subprocess.run([sys.executable, "tools/corpus-local-document.py", *arguments],
                              cwd=self.root, capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def fingerprint(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_verification_is_repeatable_and_preserves_canonical_document(self):
        document = self.root / DOCUMENT
        original = document.read_bytes()
        first = self.run_worker("verification")
        self.assertEqual(first.returncode, 0, first.stderr)
        snapshot = self.root / SOURCE / "documents/test-decision/snapshot.md"
        self.assertEqual(snapshot.read_bytes(), original)
        verification = yaml.safe_load(snapshot.with_name("verification.yml").read_text())
        self.assertEqual(verification["acquisition"]["recorded_at"], datetime.now(UTC).date().isoformat())
        before = self.fingerprint()
        second = self.run_worker("verification")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(document.read_bytes(), original)

    def test_changed_canonical_document_refuses_without_writes(self):
        document = self.root / DOCUMENT
        document.write_text(document.read_text() + "\nИзменение\n")
        before = self.fingerprint()
        result = self.run_worker("verification")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.fingerprint(), before)

    def test_excerpt_mismatch_refuses_without_writes(self):
        card = self.root / SOURCE / "documents/test-decision/statements.yml"
        card.write_text(card.read_text().replace("Тестовое правило для проверки снимка.", "Несуществующая выдержка"))
        before = self.fingerprint()
        result = self.run_worker("verification")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.fingerprint(), before)

    def test_restricted_source_probe_never_reads_or_copies_material(self):
        folder = self.root / "knowledge/data/restricted"
        folder.mkdir()
        (folder / "source.yml").write_text("id: restricted\nlocator: local:restricted\nrequires_access_check: true\nprocessing_scope: metadata_only\n")
        before = self.fingerprint()
        result = self.run_worker("probe", "--source-id", "restricted",
                                 "--source-dir", "knowledge/data/restricted",
                                 "--locator", "local:restricted")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "terms-decision-required")
        self.assertEqual(self.fingerprint(), before)

    def test_locator_cannot_escape_decision_directory(self):
        card = self.root / SOURCE / "source.yml"
        data = yaml.safe_load(card.read_text())
        data["locator"] = "knowledge/corpus.yml"
        card.write_text(yaml.safe_dump(data, allow_unicode=True))
        before = self.fingerprint()
        result = self.run_worker("fetch")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.fingerprint(), before)

    def test_book_probe_checks_local_snapshot_without_writes(self):
        folder = self.root / SOURCE
        card = folder / "source.yml"
        source = yaml.safe_load(card.read_text())
        source.update(source_kind="book", locator="local:test-book", storage_strategy="local_only",
                      copy_policy="fragments_only", processing_scope="selected_fragments")
        content = b"test local book snapshot"
        snapshot = folder / "source.local.epub"
        snapshot.write_bytes(content)
        source.update(content_hash=hashlib.sha256(content).hexdigest(),
                      file_size_bytes=len(content))
        card.write_text(yaml.safe_dump(source))
        item_path = folder / "documents/test-decision/item.yml"
        item = yaml.safe_load(item_path.read_text())
        item.update(workflow_stage="fetched", copy_policy="fragments_only",
                    processing_scope="selected_fragments")
        item["files"] = {"tracked": [], "local": ["../../source.local.epub"]}
        item_path.write_text(yaml.safe_dump(item))
        for case in ("ready", "changed", "outside", "blocked"):
            with self.subTest(case=case):
                snapshot.write_bytes(content if case != "changed" else b"changed")
                item["workflow_stage"] = "blocked" if case == "blocked" else "fetched"
                item["files"]["local"] = ["../../../../corpus.yml"] if case == "outside" else ["../../source.local.epub"]
                item_path.write_text(yaml.safe_dump(item))
                before = self.fingerprint()
                result = self.run_worker("probe", "--source-id", "TEST",
                                         "--source-dir", SOURCE, "--locator", "local:test-book")
                self.assertEqual(result.returncode, 0 if case == "ready" else 2, result.stderr)
                if case == "ready":
                    self.assertEqual(json.loads(result.stdout)["status"], "ready")
                self.assertEqual(self.fingerprint(), before)

    def prepare_normalized_book(self):
        import zipfile
        folder = self.root / SOURCE
        source_path = folder / "source.yml"
        source = yaml.safe_load(source_path.read_text())
        source.update(source_kind="book", locator="local:test-book",
                      storage_strategy="local_only", retrieved_at="2026-08-12",
                      copy_policy="fragments_only", processing_scope="selected_fragments")
        original = b"<html><body>Test source fragment</body></html>"
        document = folder / "source.local.epub"
        with zipfile.ZipFile(document, "w") as archive:
            archive.writestr("OEBPS/Text/test.xhtml", original)
        source.update(content_hash=hashlib.sha256(document.read_bytes()).hexdigest(),
                      file_size_bytes=document.stat().st_size)
        source_path.write_text(yaml.safe_dump(source))
        normalized = folder / "normalized.local/test.local.md"
        normalized.parent.mkdir()
        normalized.write_text("Test source fragment\n")
        manifest = {"source_hash": source["content_hash"], "files": [{
            "path": "normalized.local/test.local.md",
            "sha256": hashlib.sha256(normalized.read_bytes()).hexdigest(),
            "epub_entry": "OEBPS/Text/test.xhtml",
            "entry_sha256": hashlib.sha256(original).hexdigest()}]}
        (normalized.parent / "manifest.local.yml").write_text(yaml.safe_dump(manifest))
        unit = folder / "documents/test-decision"
        item_path = unit / "item.yml"
        item = yaml.safe_load(item_path.read_text())
        item.update(workflow_stage="statements_extracted", copy_policy="fragments_only",
                    processing_scope="selected_fragments")
        item["files"] = {"tracked": ["statements.yml"], "local": [
            "../../source.local.epub", "../../normalized.local/test.local.md",
            "../../normalized.local/manifest.local.yml"]}
        item_path.write_text(yaml.safe_dump(item))
        (unit / "statements.yml").write_text(yaml.safe_dump({"statements": [{
            "id": "TEST-001", "excerpt": "Test source fragment",
            "artifact": "../../normalized.local/test.local.md",
            "processing_status": {"traceability": "pending"}}]}))
        return folder, unit, normalized

    def test_book_verification_records_local_integrity_without_claiming_source_match(self):
        folder, unit, normalized = self.prepare_normalized_book()
        original = (folder / "source.local.epub").read_bytes()
        probe = self.run_worker("probe", "--source-id", "TEST", "--source-dir", SOURCE,
                                "--locator", "local:test-book")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        trace = self.run_worker("traceability")
        self.assertEqual(trace.returncode, 0, trace.stderr)
        statement = yaml.safe_load((unit / "statements.yml").read_text())["statements"][0]
        self.assertEqual(statement["processing_status"]["traceability"], "passed")
        result = self.run_worker("verification")
        self.assertEqual(result.returncode, 0, result.stderr)
        certificate = yaml.safe_load((unit / "verification.yml").read_text())
        self.assertEqual(certificate["verification"]["method"], "local_integrity_only")
        self.assertEqual(certificate["verification"]["result"]["content_match"], "unverified")
        self.assertEqual(certificate["acquisition"]["method"], "user_provided")
        self.assertEqual(certificate["acquisition"]["recorded_at"], "2026-08-12")
        self.assertEqual((unit / "snapshot.local.epub").read_bytes(), original)
        before = self.fingerprint()
        repeat = self.run_worker("verification")
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual((folder / "source.local.epub").read_bytes(), original)

    def test_book_coverage_link_must_resolve_to_statement_scope(self):
        folder, unit, normalized = self.prepare_normalized_book()
        source_map = {"structure": {"units": [{"id": "unit-1"}]},
                      "coverage": {"units": [{"unit_id": "unit-1", "status": "extracted",
                                               "statement_ids": ["TEST-001"]}]}}
        (folder / "source-map.yml").write_text(yaml.safe_dump(source_map))
        before = self.fingerprint()
        result = self.run_worker("verification")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.fingerprint(), before)
        path = unit / "statements.yml"
        data = yaml.safe_load(path.read_text())
        data["statements"][0]["scope"] = {"unit_ids": ["unit-1"]}
        path.write_text(yaml.safe_dump(data))
        result = self.run_worker("traceability")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_book_changed_normalization_or_excerpt_refuses_without_writes(self):
        folder, unit, normalized = self.prepare_normalized_book()
        original = normalized.read_bytes()
        statement_path = unit / "statements.yml"
        for case in ("normalization", "excerpt", "manifest-escape", "entry-hash"):
            with self.subTest(case=case):
                normalized.write_bytes(original)
                data = yaml.safe_load(statement_path.read_text())
                data["statements"][0]["excerpt"] = "missing" if case == "excerpt" else "Test source fragment"
                statement_path.write_text(yaml.safe_dump(data))
                manifest_path = normalized.parent / "manifest.local.yml"
                manifest = yaml.safe_load(manifest_path.read_text())
                manifest["files"][0]["path"] = "../../../../" + DOCUMENT if case == "manifest-escape" else "normalized.local/test.local.md"
                manifest["files"][0]["entry_sha256"] = "0" * 64 if case == "entry-hash" else hashlib.sha256(b"<html><body>Test source fragment</body></html>").hexdigest()
                manifest_path.write_text(yaml.safe_dump(manifest))
                if case == "normalization":
                    normalized.write_text("changed")
                before = self.fingerprint()
                result = self.run_worker("verification")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.fingerprint(), before)


if __name__ == "__main__":
    unittest.main()
