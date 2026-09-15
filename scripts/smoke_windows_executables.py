from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters

_EXPECTED_TOOLS = {
    "search_knowledge",
    "search_legal_sources",
    "get_legal_document",
    "get_applicable_law",
    "verify_legal_citations",
    "research_tax_issue",
    "get_research_report",
    "get_source_status",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_manifest(root: Path, setup_name: str) -> None:
    manifest_path = root / "SHA256SUMS.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Windows installer manifest를 읽지 못했습니다.") from exc
    expected = {
        setup_name,
        "windows-payload/taxax-legal.exe",
        "windows-payload/taxax-legal-mcp.exe",
    }
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "taxax-legal-windows-installer-v1"
        or manifest.get("code_signed") is not False
        or manifest.get("smartscreen_reputation") != "not_established"
        or set(manifest.get("files") or {}) != expected
    ):
        raise RuntimeError("Windows installer manifest 계약이 올바르지 않습니다.")
    for relative, metadata in manifest["files"].items():
        path = root / Path(relative)
        if (
            not path.is_file()
            or metadata.get("sha256") != _sha256(path)
            or metadata.get("bytes") != path.stat().st_size
        ):
            raise RuntimeError(f"Windows installer hash 검증에 실패했습니다: {relative}")


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"frozen command failed ({completed.returncode}): {command[0]}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


async def _mcp_smoke(executable: Path, environment: dict[str, str]) -> None:
    parameters = StdioServerParameters(
        command=str(executable),
        args=["--transport", "stdio"],
        env=environment,
    )
    async with Client(parameters, raise_exceptions=True) as client:
        listed = await client.list_tools()
        if {tool.name for tool in listed.tools} != _EXPECTED_TOOLS:
            raise RuntimeError("frozen MCP tool 목록이 8개 공개 계약과 다릅니다.")
        research = await client.call_tool(
            "research_tax_issue",
            {
                "issue": "법인세 대손금 손금산입",
                "tax_type": "법인세",
                "transaction_date": "2025-06-30",
                "upstream": False,
            },
        )
        if research.is_error:
            raise RuntimeError("frozen MCP offline research가 실패했습니다.")
        report_id = research.structured_content["data"]["report_id"]
        report = await client.call_tool("get_research_report", {"report_id": report_id})
        if report.is_error or report.structured_content["data"]["report_id"] != report_id:
            raise RuntimeError("frozen MCP report round trip이 실패했습니다.")


def smoke_windows_executables(bundle: Path) -> dict[str, object]:
    if os.name != "nt":
        raise ValueError("Windows frozen executable smoke는 Windows에서 실행해야 합니다.")
    root = bundle.resolve()
    payload = root / "windows-payload"
    cli = payload / "taxax-legal.exe"
    mcp = payload / "taxax-legal-mcp.exe"
    setup_candidates = list(root.glob("taxax-legal-setup-*.exe"))
    if not cli.is_file() or not mcp.is_file() or len(setup_candidates) != 1:
        raise ValueError("Windows installer bundle의 실행파일 구성이 올바르지 않습니다.")
    setup = setup_candidates[0]
    _verify_manifest(root, setup.name)

    with tempfile.TemporaryDirectory(prefix="taxax-frozen-smoke-") as directory:
        scratch = Path(directory)
        first_cwd = scratch / "첫 실행 위치"
        second_cwd = scratch / "second-cwd"
        first_cwd.mkdir()
        second_cwd.mkdir()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONHOME", "PYTHONPATH", "TAXAX_PROJECT_ROOT", "TAXAX_LEGAL_DATA_DIR", "TAXAX_LAW_GO_OC"}
            and not key.startswith("TAXAX_MCP_AUTH_")
        }
        environment.update(
            {
                "PYTHONUTF8": "1",
                "LOCALAPPDATA": str(scratch / "Local App Data"),
                "APPDATA": str(scratch / "Roaming App Data"),
                "TAXAX_LEGAL_SECRET_FILE": str(scratch / "missing-secret.json"),
            }
        )
        _run([str(setup), "--help"], cwd=first_cwd, environment=environment)
        paths_output = scratch / "setup-paths.json"
        _run(
            [str(setup), "--paths-output", str(paths_output)],
            cwd=first_cwd,
            environment=environment,
        )
        paths = json.loads(paths_output.read_text(encoding="utf-8"))
        if paths != {
            "install_root": str(Path(environment["LOCALAPPDATA"]) / "TAXax" / "app"),
            "data_dir": str(Path(environment["LOCALAPPDATA"]) / "TAXax" / "legal"),
            "secret_file": str(Path(environment["TAXAX_LEGAL_SECRET_FILE"])),
            "claude_config": str(
                Path(environment["APPDATA"]) / "Claude" / "claude_desktop_config.json"
            ),
        }:
            raise RuntimeError("frozen setup의 사용자별 기본 경로 계산이 올바르지 않습니다.")
        self_check_output = scratch / "setup-self-check.json"
        _run(
            [str(setup), "--self-check-output", str(self_check_output)],
            cwd=second_cwd,
            environment=environment,
        )
        self_check = json.loads(self_check_output.read_text(encoding="utf-8"))
        if self_check != {
            "status": "ok",
            "payload": ["taxax-legal.exe", "taxax-legal-mcp.exe"],
        }:
            raise RuntimeError("frozen setup 내장 payload self-check가 실패했습니다.")
        if (Path(environment["APPDATA"]) / "Claude" / "claude_desktop_config.json").exists():
            raise RuntimeError("setup smoke가 동의 없이 Claude Desktop 설정을 변경했습니다.")

        _run([str(cli), "--help"], cwd=first_cwd, environment=environment)
        first_doctor = json.loads(_run([str(cli), "doctor"], cwd=first_cwd, environment=environment))
        second_doctor = json.loads(_run([str(cli), "doctor"], cwd=second_cwd, environment=environment))
        if first_doctor["status"] != "ok" or second_doctor["status"] != "ok":
            raise RuntimeError("cwd-independent frozen doctor가 실패했습니다.")
        default_data = Path(environment["LOCALAPPDATA"]) / "TAXax" / "legal"
        if not (default_data / "v1" / "legal.sqlite3").is_file():
            raise RuntimeError("frozen executable이 LOCALAPPDATA 기본 data directory를 만들지 않았습니다.")

        demo_environment = {**environment, "TAXAX_LEGAL_DATA_DIR": str(scratch / "demo-data")}
        demo = json.loads(_run([str(cli), "demo"], cwd=first_cwd, environment=demo_environment))
        if not demo["data"]["demo"]["synthetic"]:
            raise RuntimeError("frozen offline demo 표시가 올바르지 않습니다.")
        archive = scratch / "backup.zip"
        restored = scratch / "restored"
        _run([str(cli), "backup", str(archive)], cwd=second_cwd, environment=demo_environment)
        _run([str(cli), "restore", str(archive), str(restored)], cwd=second_cwd, environment=demo_environment)
        asyncio.run(_mcp_smoke(mcp, {**environment, "TAXAX_LEGAL_DATA_DIR": str(restored)}))

        return {
            "status": "ok",
            "setup": setup.name,
            "setup_payload": "verified",
            "manifest_hashes": "verified",
            "cli": cli.name,
            "mcp": mcp.name,
            "tools": 8,
            "cwd_independent_data": str(default_data),
            "backup_restore": "verified",
            "python_install_required_by_executables": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows frozen CLI/MCP executable을 독립 실행 검증합니다.")
    parser.add_argument("bundle", type=Path)
    arguments = parser.parse_args()
    try:
        result = smoke_windows_executables(arguments.bundle)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "error", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
