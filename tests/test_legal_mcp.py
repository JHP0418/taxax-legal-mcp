from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client, StdioServerParameters

from taxax.mcp import server as server_module


class LegalMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_initialize_list_call_schema_and_error(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str((Path.cwd() / "src").resolve())
            environment["TAXAX_PROJECT_ROOT"] = str(Path.cwd())
            environment["TAXAX_LEGAL_DATA_DIR"] = directory
            environment.pop("TAXAX_LAW_GO_OC", None)
            # OC 미설정 상태를 재현하려면 환경변수뿐 아니라 로컬 protected secret
            # file 경로도 격리해야 한다. 그렇지 않으면 이 테스트를 실행하는 실제
            # 머신에 저장된 운영자 자격증명을 그대로 읽어버려 재현이 깨진다.
            environment["TAXAX_LEGAL_SECRET_FILE"] = str(Path(directory) / "unused-secret.json")
            parameters = StdioServerParameters(command=sys.executable, args=["-m", "taxax.mcp.server"], env=environment)
            async with Client(parameters, raise_exceptions=True) as client:
                listed = await client.list_tools()
                tools = {tool.name: tool for tool in listed.tools}
                expected = {
                    "search_knowledge",
                    "search_legal_sources",
                    "get_legal_document",
                    "get_applicable_law",
                    "verify_legal_citations",
                    "research_tax_issue",
                    "get_research_report",
                    "get_source_status",
                }
                self.assertEqual(set(tools), expected)
                for tool in tools.values():
                    self.assertEqual(tool.input_schema.get("type"), "object")
                    self.assertEqual(tool.output_schema.get("type"), "object")
                status = await client.call_tool("get_source_status", {})
                self.assertFalse(status.is_error)
                self.assertEqual(status.structured_content["status"], "ok")
                self.assertEqual(status.content[0].type, "text")
                research = await client.call_tool(
                    "research_tax_issue",
                    {
                        "issue": "법인세 대손금 손금산입",
                        "tax_type": "법인세",
                        "transaction_date": "2026-01-02",
                        "upstream": False,
                    },
                )
                self.assertFalse(research.is_error)
                self.assertEqual(research.structured_content["status"], "partial")
                report_id = research.structured_content["data"]["report_id"]
                report = await client.call_tool("get_research_report", {"report_id": report_id})
                self.assertFalse(report.is_error)
                self.assertEqual(report.structured_content["data"]["report_id"], report_id)
                blocked = await client.call_tool(
                    "search_legal_sources",
                    {"query": "민법", "target": "law", "upstream": True},
                )
                self.assertTrue(blocked.is_error)
                self.assertEqual(blocked.structured_content["status"], "blocked")
                self.assertEqual(blocked.structured_content["error"]["code"], "AUTH_REQUIRED")

    def test_http_without_auth_is_loopback_and_opt_in_only(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"TAXAX_LEGAL_DATA_DIR": directory},
            clear=False,
        ):
            with self.assertRaises(SystemExit) as non_loopback:
                server_module.main(["--transport", "streamable-http", "--host", "0.0.0.0"])
            self.assertIn("loopback", str(non_loopback.exception))
            with patch.dict(os.environ, {"TAXAX_MCP_ALLOW_LOCAL_HTTP": "0"}, clear=False):
                with self.assertRaises(SystemExit) as disabled:
                    server_module.main(["--transport", "streamable-http"])
            self.assertIn("TAXAX_MCP_ALLOW_LOCAL_HTTP", str(disabled.exception))


if __name__ == "__main__":
    unittest.main()
