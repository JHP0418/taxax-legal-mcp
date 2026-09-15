from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import venv
from pathlib import Path

_SENSITIVE_ENVIRONMENT_PREFIXES = (
    "TAXAX_LAW_GO_",
    "TAXAX_MCP_AUTH_",
    "TAXAX_MCP_ALLOWED_",
)
_SENSITIVE_ENVIRONMENT_NAMES = {
    "PYTHONPATH",
    "TAXAX_PROJECT_ROOT",
    "TAXAX_PRIVATE_KNOWLEDGE_DIR",
    "TAXAX_LEGACY_SNAPSHOT_ROOT",
    "TAXAX_NTS_ENABLED",
    "TAXAX_NTS_TERMS_CONFIRMED",
    "TAXAX_OLTA_ENABLED",
    "TAXAX_OLTA_TERMS_CONFIRMED",
}


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
            f"command failed ({completed.returncode}): {command[0]}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def _entrypoint(venv_root: Path, name: str) -> Path:
    scripts = venv_root / ("Scripts" if os.name == "nt" else "bin")
    executable = name + (".exe" if os.name == "nt" else "")
    path = scripts / executable
    if not path.is_file():
        raise RuntimeError(f"설치된 entrypoint를 찾지 못했습니다: {name}")
    return path


def _wheel(path: Path) -> Path:
    if path.is_file() and path.suffix == ".whl":
        return path.resolve()
    wheels = sorted(path.glob("taxax_legal_mcp-*.whl"))
    if len(wheels) != 1:
        raise ValueError("wheel 파일 또는 wheel 하나가 있는 directory를 지정해야 합니다.")
    return wheels[0].resolve()


def smoke_install(distribution: Path) -> dict[str, object]:
    wheel = _wheel(distribution)
    with tempfile.TemporaryDirectory(prefix="taxax-legal-clean-") as directory:
        scratch = Path(directory)
        venv_root = scratch / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_root)
        python = _entrypoint(venv_root, "python")

        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in _SENSITIVE_ENVIRONMENT_NAMES
            and not any(key.startswith(prefix) for prefix in _SENSITIVE_ENVIRONMENT_PREFIXES)
        }
        environment.update(
            {
                "PYTHONUTF8": "1",
                "TAXAX_LEGAL_DATA_DIR": str(scratch / "data"),
                "TAXAX_KOREAN_LAW_BRIDGE": "disabled",
            }
        )
        _run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", str(wheel)],
            cwd=scratch,
            environment=environment,
        )
        legal_cli = _entrypoint(venv_root, "taxax-legal")
        mcp_cli = _entrypoint(venv_root, "taxax-legal-mcp")

        _run([str(legal_cli), "--help"], cwd=scratch, environment=environment)
        namespace = _run(
            [
                str(python),
                "-c",
                "import taxax; assert taxax.__file__ is None; "
                "import taxax.legal.service, taxax.legal.maintenance, taxax.mcp.server",
            ],
            cwd=scratch,
            environment=environment,
        )
        if namespace.strip():
            raise RuntimeError("clean import가 예상하지 않은 stdout을 출력했습니다.")

        doctor = json.loads(_run([str(legal_cli), "doctor"], cwd=scratch, environment=environment))
        if doctor["status"] != "ok" or not doctor["data"]["ready"]["stdio"]:
            raise RuntimeError("installed doctor가 stdio ready 상태가 아닙니다.")
        demo = json.loads(_run([str(legal_cli), "demo"], cwd=scratch, environment=environment))
        if not demo["data"]["demo"]["synthetic"]:
            raise RuntimeError("offline demo가 합성 자료로 표시되지 않았습니다.")
        report_id = demo["data"]["report_id"]
        document_id = "fixture:law:corporate-tax-bad-debt-v1"
        detail = json.loads(
            _run(
                [str(legal_cli), "get-legal-document", "--document-id", document_id],
                cwd=scratch,
                environment=environment,
            )
        )
        if detail["data"]["document_id"] != document_id:
            raise RuntimeError("installed CLI가 합성 원문을 조회하지 못했습니다.")
        report = json.loads(
            _run([str(legal_cli), "get-research-report", report_id], cwd=scratch, environment=environment)
        )
        if report["data"]["report_id"] != report_id:
            raise RuntimeError("installed CLI가 저장된 report를 조회하지 못했습니다.")

        archive = scratch / "backup.zip"
        restored = scratch / "restored"
        _run([str(legal_cli), "backup", str(archive)], cwd=scratch, environment=environment)
        _run([str(legal_cli), "restore", str(archive), str(restored)], cwd=scratch, environment=environment)
        restored_environment = {**environment, "TAXAX_LEGAL_DATA_DIR": str(restored)}
        restored_doctor = json.loads(
            _run([str(legal_cli), "doctor"], cwd=scratch, environment=restored_environment)
        )
        if restored_doctor["status"] != "ok":
            raise RuntimeError("복원된 data directory의 doctor가 실패했습니다.")
        restored_report = json.loads(
            _run(
                [str(legal_cli), "get-research-report", report_id],
                cwd=scratch,
                environment=restored_environment,
            )
        )
        if restored_report["data"]["report_id"] != report_id:
            raise RuntimeError("복원된 report scope 조회가 실패했습니다.")

        mcp_smoke = scratch / "mcp_smoke.py"
        mcp_smoke.write_text(
            textwrap.dedent(
                """
                import asyncio
                import os
                import sys
                from mcp import Client, StdioServerParameters

                EXPECTED = {
                    "search_knowledge",
                    "search_legal_sources",
                    "get_legal_document",
                    "get_applicable_law",
                    "verify_legal_citations",
                    "research_tax_issue",
                    "get_research_report",
                    "get_source_status",
                }

                async def main():
                    parameters = StdioServerParameters(
                        command=sys.argv[1],
                        args=["--transport", "stdio"],
                        env=dict(os.environ),
                    )
                    async with Client(parameters, raise_exceptions=True) as client:
                        listed = await client.list_tools()
                        assert {tool.name for tool in listed.tools} == EXPECTED
                        research = await client.call_tool(
                            "research_tax_issue",
                            {
                                "issue": "법인세 대손금 손금산입",
                                "tax_type": "법인세",
                                "transaction_date": "2025-06-30",
                                "upstream": False,
                            },
                        )
                        assert not research.is_error
                        report_id = research.structured_content["data"]["report_id"]
                        report = await client.call_tool(
                            "get_research_report",
                            {"report_id": report_id},
                        )
                        assert not report.is_error
                        assert report.structured_content["data"]["report_id"] == report_id

                asyncio.run(main())
                """
            ).lstrip(),
            encoding="utf-8",
        )
        _run([str(python), str(mcp_smoke), str(mcp_cli)], cwd=scratch, environment=restored_environment)

        return {
            "status": "ok",
            "wheel": wheel.name,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "tools": 8,
            "demo_document": document_id,
            "backup_restore": "verified",
            "taxax_checkout_required": False,
            "pythonpath_required": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="새 virtualenv에서 taxax-legal wheel을 설치·검증합니다.")
    parser.add_argument("distribution", type=Path)
    arguments = parser.parse_args()
    try:
        result = smoke_install(arguments.distribution.resolve())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "error", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
