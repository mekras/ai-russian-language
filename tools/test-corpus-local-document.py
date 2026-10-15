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
from datetime import UTC, datetime

import yaml


ROOT = Path(__file__).resolve().parent.parent
SOURCE = "knowledge/data/project-decision-dr-0004"


class LocalDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".corpus.tmp.", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in ("tools/corpus-local-document.py", "docs/DR/DR-0004.md",
                         "knowledge/corpus.yml",
                         ".agents/skills/kc-inventory/scripts/record-snapshot-verification.py"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        shutil.copytree(ROOT / SOURCE, self.root / SOURCE)

    def run_worker(self, *arguments):
        return subprocess.run([sys.executable, "tools/corpus-local-document.py", *arguments],
                              cwd=self.root, capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def fingerprint(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_verification_is_repeatable_and_preserves_canonical_document(self):
        document = self.root / "docs/DR/DR-0004.md"
        original = document.read_bytes()
        first = self.run_worker("verification")
        self.assertEqual(first.returncode, 0, first.stderr)
        snapshot = self.root / SOURCE / "documents/dr-0004/snapshot.md"
        self.assertEqual(snapshot.read_bytes(), original)
        verification = yaml.safe_load(snapshot.with_name("verification.yml").read_text())
        self.assertEqual(verification["acquisition"]["recorded_at"], datetime.now(UTC).date().isoformat())
        before = self.fingerprint()
        second = self.run_worker("verification")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(document.read_bytes(), original)

    def test_changed_canonical_document_refuses_without_writes(self):
        document = self.root / "docs/DR/DR-0004.md"
        document.write_text(document.read_text() + "\nИзменение\n")
        before = self.fingerprint()
        result = self.run_worker("verification")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.fingerprint(), before)

    def test_excerpt_mismatch_refuses_without_writes(self):
        card = self.root / SOURCE / "documents/dr-0004/statements.yml"
        card.write_text(card.read_text().replace("Проекту нужно проверяемое место", "Несуществующая выдержка"))
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
        card.write_text(card.read_text().replace('"docs/DR/DR-0004.md"', '"knowledge/corpus.yml"'))
        before = self.fingerprint()
        result = self.run_worker("fetch")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.fingerprint(), before)


if __name__ == "__main__":
    unittest.main()
