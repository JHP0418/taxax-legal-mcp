from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from taxax.legal import maintenance as maintenance_module
from taxax.legal.demo import DEMO_DOCUMENT_IDS, run_synthetic_demo
from taxax.legal.maintenance import MaintenanceError, create_backup, restore_backup
from taxax.legal.models import ResearchReport, ResponseStatus
from taxax.legal.reports import ResearchReportRepository
from taxax.legal.service import LegalKnowledgeService


class LegalMaintenanceTests(unittest.TestCase):
    def test_new_install_backup_does_not_create_obsolete_private_report_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "state"
            service = LegalKnowledgeService(root, data_dir=data_dir)
            self.assertFalse((data_dir / "private" / "v1" / "reports.sqlite3").exists())
            archive = root / "backup.zip"
            create_backup(data_dir, archive)
            with zipfile.ZipFile(archive) as bundle:
                self.assertNotIn("private/v1/reports.sqlite3", bundle.namelist())
            restored = root / "restored"
            restore_backup(archive, restored)
            self.assertGreaterEqual(service.repository.source_status()["database_schema_version"], 1)
            self.assertFalse((restored / "private" / "v1" / "reports.sqlite3").exists())

    def test_backup_restore_preserves_databases_raw_hash_and_report_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "state"
            LegalKnowledgeService(root, data_dir=data_dir)
            report = ResearchReport(
                report_id="research-" + "b" * 32,
                issue="합성 backup 검증",
                created_at="2026-09-14T00:00:00Z",
                updated_at="2026-09-14T00:00:00Z",
            )
            ResearchReportRepository(data_dir / "private" / "v1" / "reports.sqlite3").save(
                report, principal_id="employee-1", org_id="office-1"
            )
            raw = data_dir / "v1" / "raw" / "law.go.kr" / "fixture.bin"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"synthetic-public-fixture")
            run = data_dir / "v1" / "runs" / "fixture.json"
            run.parent.mkdir(parents=True)
            run.write_text('{"synthetic": true}\n', encoding="utf-8")

            archive = root / "backup.zip"
            created = create_backup(data_dir, archive)
            self.assertEqual(created["status"], "ok")
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
            self.assertIn("backup-manifest.json", names)
            self.assertIn("v1/legal.sqlite3", names)
            self.assertIn("private/v1/reports.sqlite3", names)
            self.assertIn("v1/raw/law.go.kr/fixture.bin", names)
            self.assertFalse(any(name.endswith(("-wal", "-shm")) for name in names))

            restored = root / "restored"
            result = restore_backup(archive, restored)
            self.assertEqual(result["status"], "ok")
            self.assertEqual((restored / "v1" / "raw" / "law.go.kr" / "fixture.bin").read_bytes(), raw.read_bytes())
            restored_reports = ResearchReportRepository(restored / "private" / "v1" / "reports.sqlite3")
            same_scope = restored_reports.get(report.report_id, principal_id="employee-1", org_id="office-1")
            self.assertIsNotNone(same_scope)
            self.assertEqual(same_scope.report_id, report.report_id)
            self.assertIsNone(restored_reports.get(report.report_id, principal_id="employee-2", org_id="office-1"))

    def test_backup_restore_supports_synthetic_demo_extended_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "demo-state"
            service = LegalKnowledgeService(root, data_dir=data_dir)
            run_synthetic_demo(service)
            operations = root / ("backup-restore-" + "x" * 48)
            operations.mkdir()
            archive = operations / "demo-backup.zip"
            create_backup(data_dir, archive)
            with zipfile.ZipFile(archive) as bundle:
                longest = max(bundle.namelist(), key=len)
            staged_path = operations / ".taxax-legal-restore-xxxxxxxx" / "data" / Path(longest)
            self.assertGreater(len(str(staged_path.resolve())), 260)

            restored = operations / "restore-target"
            try:
                result = restore_backup(archive, restored)
                self.assertEqual(result["status"], "ok")
                restored_service = LegalKnowledgeService(root, data_dir=restored)
                document = restored_service.get_legal_document(document_id=DEMO_DOCUMENT_IDS[0])
                self.assertEqual(document.status, ResponseStatus.OK)
                self.assertTrue(document.data["metadata"]["synthetic_fixture"])
            finally:
                if os.path.exists(maintenance_module._native_path(restored)):
                    maintenance_module._remove_tree(restored)

    def test_backup_and_restore_never_overwrite_existing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "state"
            LegalKnowledgeService(root, data_dir=data_dir)
            archive = root / "backup.zip"
            create_backup(data_dir, archive)
            with self.assertRaises(MaintenanceError):
                create_backup(data_dir, archive)
            destination = root / "existing"
            destination.mkdir()
            with self.assertRaises(MaintenanceError):
                restore_backup(archive, destination)

    def test_backup_publication_does_not_overwrite_racing_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "state"
            LegalKnowledgeService(root, data_dir=data_dir)
            archive = root / "backup.zip"
            original_link = os.link

            def publish_after_competing_writer(source, destination):
                Path(destination).write_bytes(b"competing-backup")
                return original_link(source, destination)

            with patch.object(maintenance_module.os, "link", side_effect=publish_after_competing_writer):
                with self.assertRaises(MaintenanceError):
                    create_backup(data_dir, archive)
            self.assertEqual(archive.read_bytes(), b"competing-backup")

    def test_backup_supports_uri_significant_data_directory_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uri = maintenance_module._read_only_database_uri(root / "state?#%")
            self.assertIn("%3F%23%25", uri)
            data_dir = root / "state # percent%"
            LegalKnowledgeService(root, data_dir=data_dir)
            archive = root / "backup.zip"
            result = create_backup(data_dir, archive)
            self.assertEqual(result["status"], "ok")

    def test_restore_rejects_nonportable_windows_paths(self):
        invalid_names = (
            "v1/raw/note.txt:hidden",
            "v1/raw/CON",
            "v1/raw/aux.txt",
            "v1/raw/trailing.",
            "v1/raw/trailing ",
            "v1/raw/bad?.txt",
            "v1/raw//double.txt",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, name in enumerate(invalid_names):
                with self.subTest(name=name):
                    archive = root / f"malicious-{index}.zip"
                    with zipfile.ZipFile(archive, "w") as bundle:
                        bundle.writestr(name, "blocked")
                        bundle.writestr("backup-manifest.json", "{}")
                    with self.assertRaises(MaintenanceError):
                        restore_backup(archive, root / f"restored-{index}")

    def test_restore_rejects_case_insensitive_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "malicious.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("v1/raw/Record.txt", "first")
                bundle.writestr("v1/raw/record.txt", "second")
                bundle.writestr("backup-manifest.json", "{}")
            with self.assertRaises(MaintenanceError):
                restore_backup(archive, root / "restored")

    def test_restore_rejects_path_traversal_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "malicious.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside.txt", "blocked")
                bundle.writestr("backup-manifest.json", "{}")
            with self.assertRaises(MaintenanceError):
                restore_backup(archive, root / "restored")
            self.assertFalse((root / "outside.txt").exists())


if __name__ == "__main__":
    unittest.main()
