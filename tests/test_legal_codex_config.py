from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path

from taxax.legal.codex_config import (
    CodexConfigError,
    default_codex_config,
    register_codex,
    unregister_codex,
)

# 실제 사용자 config에서 관찰된 구조: 주석, 최상위 키, marketplaces/plugins 테이블,
# 따옴표가 포함된 projects 키, 기존 mcp_servers 항목이 섞여 있다.
_EXISTING = """# Codex 사용자 설정 (주석은 보존돼야 한다)
forced_login_method = "chatgpt"
model = "gpt-5.6-sol"

[marketplaces.openai-bundled]
source_type = "local"
source = '\\\\?\\C:\\Users\\tester\\.codex\\bundled'

[projects.'c:\\users\\tester']
trust_level = "trusted"

[mcp_servers.node_repl]
args = []
command = 'C:\\Tools\\node_repl.exe'
startup_timeout_sec = 120

[mcp_servers.node_repl.env]
CODEX_HOME = 'C:\\Users\\tester\\.codex'

[mcp_servers.openviking]
url = "http://127.0.0.1:1933/mcp"
"""


class CodexConfigTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path, Path]:
        config = root / "config.toml"
        executable = root / "Scripts" / ("taxax-legal-mcp.exe" if os.name == "nt" else "taxax-legal-mcp")
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"stub")
        data_dir = root / "legal"
        data_dir.mkdir(parents=True, exist_ok=True)
        return config, executable, data_dir

    def test_registers_into_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config, executable, data_dir = self._paths(Path(directory))
            result = register_codex(executable, data_dir, path=config, confirmed=True)
            self.assertTrue(result.changed)
            self.assertIsNone(result.backup_path)
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            entry = parsed["mcp_servers"]["taxax-legal"]
            self.assertEqual(entry["command"], str(executable.resolve()))
            self.assertEqual(entry["args"], ["--transport", "stdio"])
            self.assertEqual(entry["env"]["TAXAX_LEGAL_DATA_DIR"], str(data_dir.resolve()))

    def test_existing_user_settings_survive_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            config, executable, data_dir = self._paths(Path(directory))
            config.write_text(_EXISTING, encoding="utf-8")
            before = tomllib.loads(_EXISTING)

            result = register_codex(executable, data_dir, path=config, confirmed=True)
            self.assertTrue(result.changed)
            self.assertIsNotNone(result.backup_path)

            text = config.read_text(encoding="utf-8")
            after = tomllib.loads(text)
            # 주석과 기존 최상위/테이블 설정이 그대로 남는다.
            self.assertIn("# Codex 사용자 설정", text)
            self.assertEqual(after["model"], before["model"])
            self.assertEqual(after["forced_login_method"], before["forced_login_method"])
            self.assertEqual(after["marketplaces"], before["marketplaces"])
            self.assertEqual(after["projects"], before["projects"])
            # 기존 MCP server도 보존된다.
            self.assertEqual(after["mcp_servers"]["node_repl"], before["mcp_servers"]["node_repl"])
            self.assertEqual(after["mcp_servers"]["openviking"], before["mcp_servers"]["openviking"])
            self.assertIn("taxax-legal", after["mcp_servers"])
            # 백업은 원본과 동일하다.
            self.assertEqual(result.backup_path.read_text(encoding="utf-8"), _EXISTING)

    def test_extra_env_is_written_and_repeat_registration_stays_noop_only_if_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            config, executable, data_dir = self._paths(Path(directory))
            extra_env = {"TAXAX_NTS_ENABLED": "1", "TAXAX_NTS_TERMS_CONFIRMED": "1"}
            result = register_codex(executable, data_dir, path=config, confirmed=True, extra_env=extra_env)
            self.assertTrue(result.changed)
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            entry = parsed["mcp_servers"]["taxax-legal"]
            self.assertEqual(
                entry["env"],
                {
                    "TAXAX_LEGAL_DATA_DIR": str(data_dir.resolve()),
                    "TAXAX_NTS_ENABLED": "1",
                    "TAXAX_NTS_TERMS_CONFIRMED": "1",
                },
            )

            # 동일한 extra_env로 재등록하면 변경 없음.
            again = register_codex(executable, data_dir, path=config, confirmed=True, extra_env=extra_env)
            self.assertFalse(again.changed)

            # extra_env가 달라지면(OLTA 추가) 동일 등록으로 간주하지 않고 갱신이 필요하다.
            wider_env = dict(extra_env, TAXAX_OLTA_ENABLED="1", TAXAX_OLTA_TERMS_CONFIRMED="1")
            with self.assertRaises(CodexConfigError):
                register_codex(executable, data_dir, path=config, confirmed=True, extra_env=wider_env)
            updated = register_codex(
                executable, data_dir, path=config, confirmed=True, extra_env=wider_env, allow_update=True
            )
            self.assertTrue(updated.changed)
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(parsed["mcp_servers"]["taxax-legal"]["env"]["TAXAX_OLTA_ENABLED"], "1")

    def test_repeat_registration_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            config, executable, data_dir = self._paths(Path(directory))
            config.write_text(_EXISTING, encoding="utf-8")
            register_codex(executable, data_dir, path=config, confirmed=True)
            first = config.read_text(encoding="utf-8")
            result = register_codex(executable, data_dir, path=config, confirmed=True)
            self.assertFalse(result.changed)
            self.assertIsNone(result.backup_path)
            self.assertEqual(config.read_text(encoding="utf-8"), first)

    def test_conflicting_entry_requires_force_and_leaves_file_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, executable, data_dir = self._paths(root)
            config.write_text(
                _EXISTING + "\n[mcp_servers.taxax-legal]\ncommand = 'C:\\\\old\\\\taxax-legal-mcp.exe'\nargs = []\n",
                encoding="utf-8",
            )
            original = config.read_text(encoding="utf-8")
            with self.assertRaises(CodexConfigError):
                register_codex(executable, data_dir, path=config, confirmed=True)
            self.assertEqual(config.read_text(encoding="utf-8"), original)

            register_codex(executable, data_dir, path=config, confirmed=True, allow_update=True)
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(parsed["mcp_servers"]["taxax-legal"]["command"], str(executable.resolve()))
            # 갱신해도 기존 서버는 남는다.
            self.assertIn("node_repl", parsed["mcp_servers"])

    def test_registration_requires_consent(self):
        with tempfile.TemporaryDirectory() as directory:
            config, executable, data_dir = self._paths(Path(directory))
            with self.assertRaises(CodexConfigError):
                register_codex(executable, data_dir, path=config, confirmed=False)
            self.assertFalse(config.exists())

    def test_malformed_toml_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            config, executable, data_dir = self._paths(Path(directory))
            broken = "this is = not [valid toml\n"
            config.write_text(broken, encoding="utf-8")
            with self.assertRaises(CodexConfigError):
                register_codex(executable, data_dir, path=config, confirmed=True)
            self.assertEqual(config.read_text(encoding="utf-8"), broken)

    def test_unregister_removes_only_our_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            config, executable, data_dir = self._paths(Path(directory))
            config.write_text(_EXISTING, encoding="utf-8")
            register_codex(executable, data_dir, path=config, confirmed=True)

            result = unregister_codex(config, confirmed=True)
            self.assertTrue(result.changed)
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertNotIn("taxax-legal", parsed["mcp_servers"])
            self.assertIn("node_repl", parsed["mcp_servers"])
            self.assertEqual(parsed["mcp_servers"]["node_repl"]["command"], "C:\\Tools\\node_repl.exe")
            self.assertIn("openviking", parsed["mcp_servers"])
            self.assertIn("# Codex 사용자 설정", config.read_text(encoding="utf-8"))

            again = unregister_codex(config, confirmed=True)
            self.assertFalse(again.changed)

    def test_unregister_on_missing_file_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            result = unregister_codex(config, confirmed=True)
            self.assertFalse(result.changed)
            self.assertFalse(config.exists())

    def test_default_path_follows_codex_home(self):
        with tempfile.TemporaryDirectory() as directory:
            original = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = directory
            try:
                self.assertEqual(default_codex_config(), Path(directory) / "config.toml")
            finally:
                if original is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = original


if __name__ == "__main__":
    unittest.main()
