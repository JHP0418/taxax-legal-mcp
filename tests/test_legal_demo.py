from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taxax.legal.demo import DEMO_DOCUMENT_IDS, run_synthetic_demo
from taxax.legal.models import ResponseStatus
from taxax.legal.service import LegalKnowledgeService


class SyntheticDemoTests(unittest.TestCase):
    def test_demo_runs_offline_and_exposes_complete_synthetic_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = LegalKnowledgeService(root, data_dir=root / "state")
            response = run_synthetic_demo(service)
            self.assertEqual(response.status, ResponseStatus.OK)
            self.assertTrue(response.data["demo"]["synthetic"])
            self.assertFalse(response.data["demo"]["official_source"])
            self.assertEqual(set(response.data["demo"]["document_ids"]), set(DEMO_DOCUMENT_IDS))
            self.assertTrue(any("합성" in warning for warning in response.warnings))
            self.assertEqual(response.data["demo"]["search_status"], "ok")
            self.assertEqual(response.data["demo"]["citation_checks"][0]["status"], "verified")

            document = service.get_legal_document(document_id=DEMO_DOCUMENT_IDS[0])
            self.assertEqual(document.status, ResponseStatus.OK)
            self.assertTrue(document.data["metadata"]["synthetic_fixture"])
            self.assertFalse(document.data["metadata"]["official_source"])
            self.assertTrue(document.data["sections"])
            raw = service.snapshots.read_raw(document.sources[0].snapshot_ref)
            self.assertIn(b'"synthetic": true', raw)

            self.assertNotIn("report_id", response.data)

    def test_demo_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = LegalKnowledgeService(root, data_dir=root / "state")
            first = run_synthetic_demo(service)
            second = run_synthetic_demo(service)
            self.assertEqual(first.data["demo"]["document_ids"], second.data["demo"]["document_ids"])
            self.assertEqual(service.repository.source_status()["documents"], 2)


if __name__ == "__main__":
    unittest.main()
