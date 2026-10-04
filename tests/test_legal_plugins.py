"""Claude·Codex 플러그인과 Claude Desktop 확장(.mcpb) 배포물 점검."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from taxax.legal.codex_hook import _LEGAL_CONTENT, evaluate_stop

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "taxax-legal"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class PluginManifestTests(unittest.TestCase):
    def test_every_version_pin_follows_pyproject(self):
        self.assertEqual(_json(PLUGIN / ".claude-plugin" / "plugin.json")["version"], VERSION)
        self.assertEqual(_json(PLUGIN / "plugin.json")["version"], VERSION)
        tag = f"/archive/refs/tags/v{VERSION}.zip"
        self.assertIn(tag, " ".join(_json(PLUGIN / "mcp.json")["mcpServers"]["taxax-legal"]["args"]))
        self.assertIn(tag, _json(PLUGIN / "hooks" / "codex-hooks.json")["hooks"]["Stop"][0]["hooks"][0]["command"])

    def test_marketplaces_point_at_the_plugin_folder(self):
        claude = _json(ROOT / ".claude-plugin" / "marketplace.json")
        codex = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
        self.assertEqual(claude["plugins"][0]["source"], "./plugins/taxax-legal")
        self.assertEqual(codex["plugins"][0]["source"], {"source": "local", "path": "./plugins/taxax-legal"})

    def test_plugin_has_no_bin_directory(self):
        # Cowork와 claude.ai는 최상위 bin/이 있는 플러그인을 설치하지 않는다.
        self.assertFalse((PLUGIN / "bin").exists())


class ClaudeStopHookScriptTests(unittest.TestCase):
    script = PLUGIN / "hooks" / "stop-recheck.sh"

    def test_shell_pattern_and_reason_match_the_python_hook(self):
        text = self.script.read_text(encoding="utf-8")
        shell_pattern = _LEGAL_CONTENT.pattern.replace(r"\s", "[[:space:]]").replace(r"\d", "[0-9]")
        self.assertIn(f"grep -Eq '{shell_pattern}'", text)
        reason = evaluate_stop({"hook_event_name": "Stop", "last_assistant_message": "법인세"}, {"enabled": True, "max_rechecks": 1})["reason"]
        self.assertIn(reason, text)

    @unittest.skipUnless(shutil.which("sh"), "sh가 없는 환경")
    def test_blocks_a_legal_answer_once_per_prompt(self):
        def run(event: dict, data: Path) -> str:
            completed = subprocess.run(
                ["sh", str(self.script)], input=json.dumps(event, ensure_ascii=False), capture_output=True, text=True,
                env={"PATH": "/usr/bin:/bin", "CLAUDE_PLUGIN_DATA": str(data)}, timeout=20, check=True,
            )
            return completed.stdout.strip()

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            legal = {"prompt_id": "p-1", "cwd": "/home/u/세무자료", "last_assistant_message": "특수관계인에게 싸게 팔면 증여세가 나옵니다.", "stop_hook_active": True}
            self.assertEqual(json.loads(run(legal, data))["decision"], "block")
            self.assertEqual(run(legal, data), "", "같은 질문에서 두 번 막지 않는다")
            self.assertEqual(run({**legal, "prompt_id": "p-2", "last_assistant_message": "빌드를 마쳤습니다."}, data), "")
            self.assertEqual(run({"prompt_id": "p-3", "cwd": "/x/세무", "last_assistant_message": "테스트 통과"}, data), "")
            self.assertEqual(run({"last_assistant_message": "가산세 감면", "stop_hook_active": True}, data), "")
            self.assertEqual(json.loads(run({"last_assistant_message": "가산세 감면", "stop_hook_active": False}, data))["decision"], "block")


class McpbBundleTests(unittest.TestCase):
    def test_bundle_holds_only_public_sources_and_a_uv_manifest(self):
        sys.path.insert(0, str(ROOT))
        from scripts import build_legal_mcpb

        with tempfile.TemporaryDirectory() as directory:
            staged = build_legal_mcpb.stage(Path(directory) / "bundle", lock=False)
            manifest = _json(staged / "manifest.json")
            self.assertEqual((manifest["manifest_version"], manifest["version"]), ("0.4", VERSION))
            self.assertEqual(manifest["server"]["type"], "uv")
            self.assertEqual(manifest["server"]["mcp_config"]["env"]["TAXAX_LAW_GO_OC"], "${user_config.law_go_oc}")
            self.assertTrue(manifest["user_config"]["law_go_oc"]["sensitive"])
            sources = {path.relative_to(staged).parts[:3] for path in staged.rglob("*.py")}
            self.assertEqual({part[2] for part in sources if len(part) == 3 and part[:2] == ("src", "taxax")}, {"legal", "mcp"})
            self.assertTrue((staged / "src" / "server.py").is_file())
            self.assertFalse(any(part in {"tests", "knowledge", ".venv"} for path in staged.rglob("*") for part in path.parts))
            project = tomllib.loads((staged / "pyproject.toml").read_text(encoding="utf-8"))["project"]
            self.assertNotIn("build-system", (staged / "pyproject.toml").read_text(encoding="utf-8"))
            self.assertEqual(project["dependencies"], tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"])


if __name__ == "__main__":
    unittest.main()
