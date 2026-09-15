from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from taxax.legal import installer as installer_module
from taxax.legal import local_config as local_config_module
from taxax.legal import windows_setup as windows_setup_module
from taxax.legal.installer import (
    InstallationError,
    install_local_application,
    merge_claude_desktop_config,
    server_configuration,
)
from taxax.legal.local_config import (
    LocalSecretError,
    delete_law_go_credential,
    load_law_go_credential,
    restricted_directory_permissions_verified,
    restricted_permissions_verified,
    save_law_go_credential,
)
from taxax.legal.maintenance import create_backup
from taxax.legal.providers.law_go import LawGoProvider
from taxax.legal.service import LegalKnowledgeService


class LocalSecretTests(unittest.TestCase):
    def test_secret_file_is_user_only_and_provider_reads_it_without_environment_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "설정 폴더" / "provider-secrets.json"
            fake_credential = "fixture-operator@example.invalid"
            save_law_go_credential(fake_credential, secret)
            self.assertTrue(restricted_directory_permissions_verified(secret.parent))
            self.assertTrue(restricted_permissions_verified(secret))
            self.assertEqual(load_law_go_credential(secret), fake_credential)
            with patch.dict(
                os.environ,
                {
                    "TAXAX_LAW_GO_OC": "",
                    "TAXAX_LEGAL_SECRET_FILE": str(secret),
                },
                clear=False,
            ):
                provider = LawGoProvider()
            self.assertEqual(provider.credential, fake_credential)
            self.assertEqual(provider.credential_source, "protected_file")
            self.assertIsNone(provider.credential_error)

    def test_acl_failure_never_replaces_existing_secret_or_exposes_value(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "config" / "provider-secrets.json"
            original = "fixture-original@example.invalid"
            replacement = "fixture-replacement@example.invalid"
            save_law_go_credential(original, secret)
            before = secret.read_bytes()
            # Windows에서는 저장 경로가 디렉터리 생성부터 ACL까지 전부
            # _windows_secret_write() 안에서 처리되므로(단일 프로세스 원칙,
            # local_config.py 참고), 실패를 시뮬레이션하려면 이 함수를 patch해야
            # 한다. POSIX는 여전히 restrict_file_to_current_user()를 별도로 호출한다.
            failing_target = "_windows_secret_write" if os.name == "nt" else "restrict_file_to_current_user"
            with patch.object(
                local_config_module,
                failing_target,
                side_effect=LocalSecretError("ACL fixture failure"),
            ):
                with self.assertRaises(LocalSecretError) as caught:
                    save_law_go_credential(replacement, secret)
            self.assertEqual(secret.read_bytes(), before)
            self.assertNotIn(replacement, str(caught.exception))

    def test_secret_delete_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "config" / "provider-secrets.json"
            save_law_go_credential("fixture-delete@example.invalid", secret)
            with self.assertRaises(LocalSecretError):
                delete_law_go_credential(secret)
            self.assertTrue(secret.exists())
            self.assertTrue(delete_law_go_credential(secret, confirmed=True))
            self.assertFalse(secret.exists())

    def test_unsafe_secret_file_disables_upstream_without_breaking_service_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "unsafe.json"
            secret.write_text(
                json.dumps({"format": "taxax-legal-secrets-v1", "law_go_oc": "fixture-unsafe"}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TAXAX_LAW_GO_OC": "", "TAXAX_LEGAL_SECRET_FILE": str(secret)},
                clear=False,
            ), patch.object(local_config_module, "restricted_permissions_verified", return_value=False):
                provider = LawGoProvider()
                service = LegalKnowledgeService(root, data_dir=root / "data", provider=provider)
                doctor = service.doctor()
            self.assertIsNone(provider.credential)
            self.assertIsNotNone(provider.credential_error)
            self.assertEqual(doctor.status.value, "ok")
            self.assertFalse(doctor.data["checks"]["law_go_credential_file_safe"])

    def test_general_data_backup_excludes_separate_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "TAXax" / "legal"
            secret = root / "TAXax" / "config" / "provider-secrets.json"
            LegalKnowledgeService(root, data_dir=data_dir)
            save_law_go_credential("fixture-backup@example.invalid", secret)
            archive = root / "backup.zip"
            create_backup(data_dir, archive)
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                payload = b"".join(bundle.read(name) for name in names)
            self.assertFalse(any("secret" in name.casefold() for name in names))
            self.assertNotIn(b"fixture-backup", payload)


class ClaudeDesktopConfigTests(unittest.TestCase):
    def _server(self, root: Path):
        executable = root / "한글 경로" / "taxax-legal-mcp.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"synthetic executable")
        return server_configuration(executable, root / "data")

    def test_merge_preserves_existing_settings_and_creates_exact_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "Claude" / "claude_desktop_config.json"
            config.parent.mkdir()
            original = {
                "preferences": {"theme": "dark"},
                "mcpServers": {"existing": {"command": "existing-server"}},
            }
            original_bytes = (json.dumps(original, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            config.write_bytes(original_bytes)
            server = self._server(root)
            result = merge_claude_desktop_config(config, server, confirmed=True)
            updated = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(updated["preferences"], original["preferences"])
            self.assertEqual(updated["mcpServers"]["existing"], original["mcpServers"]["existing"])
            self.assertEqual(updated["mcpServers"]["taxax-legal"], server)
            self.assertEqual(result.backup_path.read_bytes(), original_bytes)
            self.assertNotIn("fixture-operator", config.read_text(encoding="utf-8"))

            second = merge_claude_desktop_config(config, server, confirmed=True)
            self.assertFalse(second.changed)
            self.assertIsNone(second.backup_path)

    def test_conflict_and_malformed_json_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = self._server(root)
            conflict = root / "conflict.json"
            conflict.write_text(
                json.dumps({"mcpServers": {"taxax-legal": {"command": "other.exe"}}}),
                encoding="utf-8",
            )
            before = conflict.read_bytes()
            with self.assertRaises(InstallationError):
                merge_claude_desktop_config(conflict, server, confirmed=True)
            self.assertEqual(conflict.read_bytes(), before)

            malformed = root / "malformed.json"
            malformed.write_text("{not-json", encoding="utf-8")
            before = malformed.read_bytes()
            with self.assertRaises(InstallationError):
                merge_claude_desktop_config(malformed, server, confirmed=True)
            self.assertEqual(malformed.read_bytes(), before)
            self.assertEqual(list(root.glob("malformed.json.backup-*")), [])

    def test_concurrent_change_or_atomic_replace_failure_preserves_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = self._server(root)
            config = root / "claude.json"
            config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            before = config.read_bytes()
            with patch.object(installer_module, "_file_state", return_value=(999, 999, "changed")):
                with self.assertRaises(InstallationError):
                    merge_claude_desktop_config(config, server, confirmed=True)
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(list(root.glob("claude.json.backup-*")), [])

            with patch.object(installer_module.os, "replace", side_effect=OSError("fixture replace failure")):
                with self.assertRaises(OSError):
                    merge_claude_desktop_config(config, server, confirmed=True)
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(list(root.glob("claude.json.backup-*")), [])

    def test_new_config_requires_consent_and_contains_no_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "new" / "claude.json"
            server = self._server(root)
            with self.assertRaises(InstallationError):
                merge_claude_desktop_config(config, server, confirmed=False)
            self.assertFalse(config.exists())
            result = merge_claude_desktop_config(config, server, confirmed=True)
            self.assertTrue(result.changed)
            text = config.read_text(encoding="utf-8")
            self.assertIn("taxax-legal-mcp.exe", text)
            self.assertNotIn("TAXAX_LAW_GO_OC", text)


@unittest.skipUnless(os.name == "nt", "Windows local installer integration")
class LocalInstallerIntegrationTests(unittest.TestCase):
    def _bundle(self, root: Path) -> Path:
        bundle = root / "배포 파일"
        bundle.mkdir()
        (bundle / "taxax-legal.exe").write_bytes(b"synthetic cli executable")
        (bundle / "taxax-legal-mcp.exe").write_bytes(b"synthetic mcp executable")
        return bundle

    def test_doctor_failure_preserves_state_and_retry_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_app_data = root / "Local App Data"
            app_data = root / "Roaming App Data"
            secret = local_app_data / "TAXax" / "config" / "provider-secrets.json"
            config = app_data / "Claude" / "claude_desktop_config.json"
            data_dir = local_app_data / "TAXax" / "legal"
            data_dir.mkdir(parents=True)
            sentinel = data_dir / "existing-user-data.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "preferences": {"theme": "dark"},
                        "mcpServers": {"existing": {"command": "existing.exe"}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            save_law_go_credential("fixture-original@example.invalid", secret)
            config_before = config.read_bytes()
            secret_before = secret.read_bytes()
            bundle = self._bundle(root)
            environment = {
                "LOCALAPPDATA": str(local_app_data),
                "APPDATA": str(app_data),
                "TAXAX_LEGAL_SECRET_FILE": str(secret),
                "TAXAX_LAW_GO_OC": "",
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                installer_module,
                "run_doctor",
                side_effect=InstallationError("fixture doctor failure"),
            ):
                with self.assertRaises(InstallationError):
                    install_local_application(
                        bundle,
                        credential="fixture-replacement@example.invalid",
                        consent_to_configure_claude=True,
                    )
            self.assertEqual(config.read_bytes(), config_before)
            self.assertEqual(secret.read_bytes(), secret_before)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

            with patch.dict(os.environ, environment, clear=False), patch.object(
                installer_module,
                "run_doctor",
                return_value={"status": "ok"},
            ):
                installed = install_local_application(
                    bundle,
                    credential="fixture-replacement@example.invalid",
                    consent_to_configure_claude=True,
                )
                repeated = install_local_application(
                    bundle,
                    credential="fixture-replacement@example.invalid",
                    consent_to_configure_claude=True,
                )
            self.assertEqual(installed.doctor_status, "ok")
            self.assertTrue(installed.credential_configured)
            self.assertEqual(load_law_go_credential(secret), "fixture-replacement@example.invalid")
            self.assertIsNotNone(installed.backup_path)
            self.assertEqual(installed.backup_path.read_bytes(), config_before)
            self.assertIsNone(repeated.backup_path)
            updated = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(updated["preferences"], {"theme": "dark"})
            self.assertEqual(updated["mcpServers"]["existing"], {"command": "existing.exe"})
            server = updated["mcpServers"]["taxax-legal"]
            self.assertNotIn("TAXAX_LAW_GO_OC", server["env"])
            self.assertEqual(server["env"]["TAXAX_LEGAL_DATA_DIR"], str(data_dir.resolve()))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_consent_failure_creates_no_install_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_app_data = root / "Local App Data"
            app_data = root / "Roaming App Data"
            environment = {
                "LOCALAPPDATA": str(local_app_data),
                "APPDATA": str(app_data),
                "TAXAX_LEGAL_SECRET_FILE": str(root / "secret.json"),
            }
            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaises(InstallationError):
                    install_local_application(
                        self._bundle(root),
                        consent_to_configure_claude=False,
                    )
            self.assertFalse((local_app_data / "TAXax").exists())
            self.assertFalse((app_data / "Claude").exists())

    def test_setup_print_paths_does_not_modify_user_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_app_data = root / "Local App Data"
            app_data = root / "Roaming App Data"
            secret = root / "explicit-secret.json"
            environment = {
                "LOCALAPPDATA": str(local_app_data),
                "APPDATA": str(app_data),
                "TAXAX_LEGAL_SECRET_FILE": str(secret),
            }
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), redirect_stdout(output):
                self.assertEqual(windows_setup_module.main(["--print-paths"]), 0)
            paths = json.loads(output.getvalue())
            self.assertEqual(paths["install_root"], str(local_app_data / "TAXax" / "app"))
            self.assertEqual(paths["data_dir"], str(local_app_data / "TAXax" / "legal"))
            self.assertEqual(paths["secret_file"], str(secret))
            self.assertEqual(
                paths["claude_config"],
                str(app_data / "Claude" / "claude_desktop_config.json"),
            )
            self.assertFalse(local_app_data.exists())
            self.assertFalse(app_data.exists())
            self.assertFalse(secret.exists())


if __name__ == "__main__":
    unittest.main()
