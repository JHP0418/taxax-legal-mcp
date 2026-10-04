"""Claude Desktop 확장(.mcpb)을 Desktop과 같은 방식으로 실행해 본다.

번들을 풀고 매니페스트의 mcp_config 그대로(`uv run --directory <번들> src/server.py`)
서버를 띄운 뒤 MCP initialize·도구 목록·get_source_status를 확인한다. 외부 기관은
호출하지 않는다. uv가 PATH에 있어야 한다.

    python scripts/smoke_legal_mcpb.py <bundle.mcpb>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOLS = {"search_legal_sources", "get_legal_document", "get_applicable_law", "verify_legal_citations", "get_source_status", "search_knowledge"}


async def _smoke(extension: Path, home: Path) -> dict:
    manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
    config = manifest["server"]["mcp_config"]
    args = [arg.replace("${__dirname}", str(extension)) for arg in config["args"]]
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LOCALAPPDATA", "APPDATA") if key in os.environ}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "TAXAX_LEGAL_DATA_DIR": str(home / "data"), "TAXAX_LAW_GO_OC": "${user_config.law_go_oc}"})
    async with stdio_client(StdioServerParameters(command=config["command"], args=args, env=env)) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            status = await session.call_tool("get_source_status", {})
            payload = status.structured_content or json.loads(status.content[0].text)
    if info.server_info.version != manifest["version"]:
        raise SystemExit(f"버전 불일치: 서버 {info.server_info.version} / 매니페스트 {manifest['version']}")
    if tools != EXPECTED_TOOLS:
        raise SystemExit(f"도구 목록 불일치: {sorted(tools)}")
    if payload["status"] != "ok" or payload["data"]["official"]["credential_source"] != "none":
        raise SystemExit(f"상태 확인 실패: {payload['status']} {payload['data']['official']}")
    return {"server": info.server_info.name, "version": info.server_info.version, "tools": len(tools)}


def main(argv: list[str] | None = None) -> int:
    bundle = Path((argv or sys.argv[1:])[0]).resolve()
    with tempfile.TemporaryDirectory(prefix="taxax-mcpb-") as directory:
        extension, home = Path(directory) / "extension", Path(directory) / "home"
        home.mkdir()
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(extension)
        print(json.dumps(asyncio.run(_smoke(extension, home)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
