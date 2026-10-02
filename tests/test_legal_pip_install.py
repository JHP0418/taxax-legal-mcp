from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.export_legal_public import _selected_files
from scripts.validate_legal_distribution import _source_tests
from taxax.legal import installer as installer_module
from taxax.legal.cli import main
from taxax.legal.installer import (
    InstallationError,
    console_script,
    install_pip_application,
    unregister_claude_desktop,
)

_SERVER = "taxax-legal"


class PublicPackageTests(unittest.TestCase):
    def test_public_test_selection_excludes_private_campaign_runner_dependency(self):
        selected = {path.name for path in _selected_files()}
        self.assertNotIn("test_legal_claude_e2e.py", selected)
        self.assertNotIn("tests/test_legal_claude_e2e.py", _source_tests())
        self.assertIn("test_legal_mcp.py", selected)


class PipInstallCommandTests(unittest.TestCase):
    """pip 설치본을 Claude Desktop에 등록하는 install/uninstall 경로."""

    def _environment(self, root: Path):
        scripts = root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        suffix = ".exe" if os.name == "nt" else ""
        for name in ("taxax-legal", "taxax-legal-mcp"):
            (scripts / f"{name}{suffix}").write_bytes(b"stub")
        data_dir = root / "data"
        config = root / "Claude" / "claude_desktop_config.json"
        return scripts, data_dir, config

    def _patches(self, scripts: Path, data_dir: Path):
        return (
            patch.object(installer_module, "_scripts_directories", return_value=[scripts]),
            patch.object(installer_module, "default_legal_data_dir", return_value=data_dir),
            patch.object(installer_module, "run_doctor", return_value={"status": "ok"}),
        )

    def test_install_registers_server_and_creates_data_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                result = install_pip_application(
                    config_path=config,
                    credential=None,
                    consent_to_configure_claude=True,
                )
            self.assertTrue(data_dir.is_dir())
            self.assertEqual(result.doctor_status, "ok")
            self.assertFalse(result.credential_configured)
            payload = json.loads(config.read_text(encoding="utf-8"))
            entry = payload["mcpServers"][_SERVER]
            self.assertTrue(entry["command"].endswith("taxax-legal-mcp" + (".exe" if os.name == "nt" else "")))
            self.assertEqual(entry["args"], ["--transport", "stdio"])
            self.assertEqual(entry["env"]["TAXAX_LEGAL_DATA_DIR"], str(data_dir.resolve()))

    def test_install_registers_selected_data_dir_instead_of_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, default_dir, _ = self._environment(root)
            environment_dir = root / "environment-data"
            explicit_dir = root / "explicit-data"
            a, b, c = self._patches(scripts, default_dir)
            with a, b, c, patch.dict(os.environ, {"TAXAX_LEGAL_DATA_DIR": str(environment_dir)}):
                for label, args, expected in (
                    ("environment", [], environment_dir),
                    ("explicit", ["--data-dir", str(explicit_dir)], explicit_dir),
                ):
                    with self.subTest(label=label):
                        config = root / f"{label}.json"
                        with redirect_stdout(io.StringIO()) as stream:
                            code = main([*args, "install", "--no-oc", "--config", str(config)])
                        self.assertEqual(code, 0)
                        self.assertEqual(json.loads(stream.getvalue())["data_dir"], str(expected.resolve()))
                        self.assertEqual(json.loads(config.read_text())["mcpServers"][_SERVER]["env"]["TAXAX_LEGAL_DATA_DIR"], str(expected.resolve()))
                        self.assertTrue(expected.is_dir())
            self.assertFalse(default_dir.exists())

    def test_credential_never_reaches_claude_config_or_stdout(self):
        secret = "test-oc-value-9137"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            a, b, c = self._patches(scripts, data_dir)
            saved: list[str] = []
            stream = io.StringIO()
            with a, b, c, patch.object(installer_module, "save_law_go_credential", side_effect=saved.append):
                with redirect_stdout(stream):
                    code = main(["install", "--oc", secret, "--config", str(config)])
            self.assertEqual(code, 0)
            self.assertEqual(saved, [secret])
            self.assertNotIn(secret, config.read_text(encoding="utf-8"))
            self.assertNotIn(secret, stream.getvalue())
            self.assertTrue(json.loads(stream.getvalue())["credential_configured"])

    def test_install_is_idempotent_and_preserves_other_servers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                json.dumps({"mcpServers": {"other": {"command": "keep-me"}}}),
                encoding="utf-8",
            )
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                install_pip_application(
                    config_path=config,
                    credential=None,
                    consent_to_configure_claude=True,
                )
                first = config.read_text(encoding="utf-8")
                # 두 번째 실행은 동일 등록이므로 --force 없이도 실패하지 않는다.
                install_pip_application(
                    config_path=config,
                    credential=None,
                    consent_to_configure_claude=True,
                )
            self.assertEqual(config.read_text(encoding="utf-8"), first)
            payload = json.loads(first)
            self.assertEqual(payload["mcpServers"]["other"], {"command": "keep-me"})
            self.assertIn(_SERVER, payload["mcpServers"])

    def test_codex_install_registers_one_stop_hook_and_user_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            codex_config = root / "codex" / "config.toml"
            hooks_file = codex_config.with_name("hooks.json")
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                first = install_pip_application(
                    config_path=config, credential=None, consent_to_configure_claude=False,
                    register_claude=False, register_codex_client=True, codex_config_path=codex_config,
                )
                second = install_pip_application(
                    config_path=config, credential=None, consent_to_configure_claude=False,
                    register_claude=False, register_codex_client=True, codex_config_path=codex_config,
                )
            self.assertEqual(first.codex_hook_path, hooks_file)
            self.assertEqual(second.codex_hook_path, hooks_file)
            self.assertEqual(len(json.loads(hooks_file.read_text())["hooks"]["Stop"]), 1)
            self.assertEqual(json.loads(codex_config.with_name("taxax-legal-hook.json").read_text()),
                             {"enabled": True, "max_rechecks": 1})

    @unittest.skipIf(os.name == "nt", "Linux Codex-only uninstall path")
    def test_codex_only_uninstall_preserves_user_hook_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, _ = self._environment(root)
            codex_config = root / "codex" / "config.toml"
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                install_pip_application(
                    credential=None, consent_to_configure_claude=False,
                    register_claude=False, register_codex_client=True, codex_config_path=codex_config,
                )
            stream = io.StringIO()
            with patch("taxax.legal.cli.default_claude_code_config", return_value=root / "claude.json"):
                with redirect_stdout(stream):
                    code = main(["uninstall", "--keep-credential", "--yes", "--codex-config", str(codex_config)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(stream.getvalue())["codex_hook_removed"])
            self.assertTrue(codex_config.with_name("taxax-legal-hook.json").exists())

    def test_conflicting_registration_requires_force(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                json.dumps({"mcpServers": {_SERVER: {"command": "stale-path"}}}),
                encoding="utf-8",
            )
            original = config.read_text(encoding="utf-8")
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                with self.assertRaises(InstallationError):
                    install_pip_application(
                        config_path=config,
                        credential=None,
                        consent_to_configure_claude=True,
                    )
                self.assertEqual(config.read_text(encoding="utf-8"), original)
                install_pip_application(
                    config_path=config,
                    credential=None,
                    consent_to_configure_claude=True,
                    allow_config_update=True,
                )
            entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][_SERVER]
            self.assertNotEqual(entry["command"], "stale-path")

    def test_skip_claude_still_prepares_data_dir_without_touching_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                result = install_pip_application(
                    config_path=config,
                    credential=None,
                    consent_to_configure_claude=False,
                    register_claude=False,
                )
            self.assertTrue(data_dir.is_dir())
            self.assertFalse(config.exists())
            self.assertEqual(result.doctor_status, "ok")

    def test_uninstall_removes_only_our_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                install_pip_application(
                    config_path=config,
                    credential=None,
                    consent_to_configure_claude=True,
                )
            payload = json.loads(config.read_text(encoding="utf-8"))
            payload["mcpServers"]["other"] = {"command": "keep-me"}
            config.write_text(json.dumps(payload), encoding="utf-8")

            merged = unregister_claude_desktop(config, confirmed=True)
            self.assertTrue(merged.changed)
            remaining = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
            self.assertNotIn(_SERVER, remaining)
            self.assertEqual(remaining["other"], {"command": "keep-me"})

            again = unregister_claude_desktop(config, confirmed=True)
            self.assertFalse(again.changed)

    def test_uninstall_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "claude_desktop_config.json"
            config.write_text(json.dumps({"mcpServers": {_SERVER: {"command": "x"}}}), encoding="utf-8")
            original = config.read_text(encoding="utf-8")
            with self.assertRaises(InstallationError):
                unregister_claude_desktop(config, confirmed=False)
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_console_script_found_when_sysconfig_path_is_wrong(self):
        """Microsoft Store 파이썬은 존재하지 않는 scripts 경로를 보고하고 PATH에도 없다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # console_script는 user site-packages 옆의 Scripts(Windows) 또는
            # bin(그 외)을 본다. 여기서 이름을 고정해 버리면 리눅스에서는 이
            # 가짜 경로가 후보에 걸리지 않아, 대신 진짜 인터프리터 옆에 설치된
            # 실제 실행파일을 찾아버려 시나리오 자체가 성립하지 않는다.
            scripts_dir = "Scripts" if os.name == "nt" else "bin"
            real = root / "LocalCache" / "local-packages" / "Python311" / scripts_dir
            real.mkdir(parents=True)
            suffix = ".exe" if os.name == "nt" else ""
            (real / f"taxax-legal-mcp{suffix}").write_bytes(b"stub")
            missing = root / "WindowsApps" / "Scripts"  # 존재하지 않는 경로

            with patch.object(installer_module.sysconfig, "get_path", return_value=str(missing)):
                with patch.object(installer_module.site, "getusersitepackages", return_value=str(real.parent / "site-packages")):
                    with patch.object(installer_module.shutil, "which", return_value=None):
                        resolved = console_script("taxax-legal-mcp")
            self.assertEqual(resolved, (real / f"taxax-legal-mcp{suffix}").resolve())

    def test_console_script_missing_reports_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = [Path(directory)]
            with patch.object(installer_module, "_scripts_directories", return_value=empty):
                with patch.object(installer_module.shutil, "which", return_value=None):
                    with self.assertRaises(InstallationError) as error:
                        console_script("taxax-legal-mcp")
            self.assertIn("pip install", str(error.exception))

    def test_cli_install_emits_json_and_next_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            a, b, c = self._patches(scripts, data_dir)
            stream = io.StringIO()
            with a, b, c, redirect_stdout(stream):
                code = main(["install", "--no-oc", "--config", str(config)])
            self.assertEqual(code, 0)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["data_dir"], str(data_dir.resolve()))
            self.assertFalse(payload["credential_configured"])
            self.assertTrue(any("재시작" in step for step in payload["next_steps"]))

    def test_legacy_consent_arguments_do_not_generate_provider_env_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                result = install_pip_application(
                    config_path=config,
                    credential=None,
                    consent_to_configure_claude=True,
                    nts_consent=True,
                    olta_consent=True,
                )
            self.assertFalse(result.credential_configured)
            entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][_SERVER]
            self.assertEqual(
                entry["env"],
                {"TAXAX_LEGAL_DATA_DIR": str(data_dir.resolve())},
            )

    def test_cli_install_is_noninteractive_and_providers_are_enabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            a, b, c = self._patches(scripts, data_dir)
            stream = io.StringIO()
            with (
                a,
                b,
                c,
                patch("sys.stdin.isatty", return_value=True),
                patch(
                    "sys.stdin.readline",
                    side_effect=AssertionError("install must not prompt"),
                ),
                redirect_stdout(stream),
            ):
                code = main(["install", "--no-oc", "--config", str(config)])
            self.assertEqual(code, 0)
            payload = json.loads(stream.getvalue())
            self.assertTrue(payload["nts_enabled"])
            self.assertTrue(payload["olta_enabled"])
            self.assertFalse(
                any("terms" in step.lower() or "약관" in step for step in payload["next_steps"])
            )
            entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][_SERVER]
            self.assertEqual(
                entry["env"],
                {"TAXAX_LEGAL_DATA_DIR": str(data_dir.resolve())},
            )

    def test_legacy_provider_and_terms_flags_are_accepted_as_noops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            a, b, c = self._patches(scripts, data_dir)
            stream = io.StringIO()
            with (
                a,
                b,
                c,
                patch(
                    "sys.stdin.readline",
                    side_effect=AssertionError("install must not prompt"),
                ),
                redirect_stdout(stream),
            ):
                code = main(
                    [
                        "install",
                        "--no-oc",
                        "--nts",
                        "--nts-terms-confirmed",
                        "--olta",
                        "--olta-terms-confirmed",
                        "--config",
                        str(config),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stream.getvalue())
            self.assertTrue(payload["nts_enabled"])
            self.assertTrue(payload["olta_enabled"])
            entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"][_SERVER]
            self.assertNotIn("TAXAX_NTS_ENABLED", entry["env"])
            self.assertNotIn("TAXAX_NTS_TERMS_CONFIRMED", entry["env"])
            self.assertNotIn("TAXAX_OLTA_ENABLED", entry["env"])
            self.assertNotIn("TAXAX_OLTA_TERMS_CONFIRMED", entry["env"])

    def test_claude_code_registration_preserves_other_settings_and_declares_transport(self):
        """`~/.claude.json`에는 MCP 외 설정도 함께 있으므로 우리 항목만 더해야 한다."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            code_config = root / ".claude.json"
            code_config.write_text(
                json.dumps(
                    {
                        "projects": {"C:\\work": {"history": ["이전 기록"]}},
                        "mcpServers": {"other": {"command": "keep-me"}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                result = install_pip_application(
                    config_path=config,
                    credential=None,
                    consent_to_configure_claude=True,
                    register_claude_code_client=True,
                    claude_code_config_path=code_config,
                    nts_consent=True,
                )
            self.assertTrue(result.claude_code_registered)
            self.assertEqual(result.claude_code_config_path, code_config)
            payload = json.loads(code_config.read_text(encoding="utf-8"))
            self.assertEqual(payload["projects"], {"C:\\work": {"history": ["이전 기록"]}})
            self.assertEqual(payload["mcpServers"]["other"], {"command": "keep-me"})
            entry = payload["mcpServers"][_SERVER]
            self.assertEqual(entry["type"], "stdio")
            self.assertEqual(entry["args"], ["--transport", "stdio"])
            self.assertEqual(
                entry["env"],
                {"TAXAX_LEGAL_DATA_DIR": str(data_dir.resolve())},
            )

    def test_claude_code_is_untouched_unless_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, data_dir, config = self._environment(root)
            code_config = root / ".claude.json"
            a, b, c = self._patches(scripts, data_dir)
            with a, b, c:
                result = install_pip_application(
                    config_path=config,
                    credential=None,
                    consent_to_configure_claude=True,
                    claude_code_config_path=code_config,
                )
            self.assertFalse(result.claude_code_registered)
            self.assertFalse(code_config.exists())

    def test_running_server_detection_ignores_processes_that_merely_mention_the_name(self):
        """이 검사가 느슨하면 stop-servers가 사용자의 셸을 종료할 수 있다."""
        self.assertNotIn(
            os.getpid(),
            [entry["pid"] for entry in installer_module.running_mcp_servers()],
            "자기 자신을 서버로 오인하면 안 됩니다.",
        )

    def test_stop_servers_requires_confirmation(self):
        with self.assertRaises(InstallationError):
            installer_module.stop_mcp_servers(confirmed=False)

    def test_cli_install_failure_returns_error_json_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(installer_module, "_scripts_directories", return_value=[Path(directory)]):
                with patch.object(installer_module.shutil, "which", return_value=None):
                    stream = io.StringIO()
                    with redirect_stdout(stream):
                        code = main(["install", "--no-oc"])
            self.assertEqual(code, 2)
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"]["code"], "INSTALL_FAILED")


if __name__ == "__main__":
    unittest.main()
