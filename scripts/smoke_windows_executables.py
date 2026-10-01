from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

_EXPECTED_TOOLS = {
    "search_knowledge",
    "search_legal_sources",
    "get_legal_document",
    "get_applicable_law",
    "verify_legal_citations",
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


# frozen 실행파일 하나가 응답하지 않으면 job 전체가 GitHub의 6시간 한도까지
# 매달려 있게 되고, 로그만 봐서는 어느 명령에서 멈췄는지도 알 수 없다. 넉넉하되
# 유한한 한도를 두어 "멈췄다"가 곧바로 실패와 원인 표시로 드러나게 한다.
# onefile 실행파일은 실행할 때마다 자신을 임시 폴더에 풀기 때문에 느리다.
_COMMAND_TIMEOUT_SECONDS = 300.0
_MCP_TIMEOUT_SECONDS = 300.0
# 설치기 실행파일은 payload exe 두 개를 통째로 품고 있어 190MB에 가깝다.
# onefile은 실행할 때마다 그 전부를 임시 폴더에 풀고 Defender가 다시 훑으므로,
# 빠른 로컬 NVMe에서도 --help 한 번에 20초 넘게 걸린다. CI 러너에서는 이 값이
# 300초를 넘겨 "멈춘 것"처럼 보였다. 느린 것과 멈춘 것을 구분하려면 이쪽만
# 예산을 따로 줘야 한다.
_SETUP_TIMEOUT_SECONDS = 600.0


def _kill_process_tree(process: subprocess.Popen) -> None:
    """손자 프로세스까지 정리한다. onefile 실행파일은 자기 자신을 풀어 자식을 띄운다."""
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
        check=False,
        capture_output=True,
        timeout=60,
    )
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float = _COMMAND_TIMEOUT_SECONDS,
) -> str:
    # 출력을 파이프가 아니라 임시 파일로 받는다. capture_output=True로 파이프를
    # 쓰면 timeout이 걸린 뒤 subprocess가 프로세스를 죽이고 "파이프가 닫히기를"
    # 다시 기다리는데, onefile 실행파일이 띄운 손자가 그 파이프를 쥔 채 살아
    # 있으면 그 대기가 끝나지 않는다. 실제로 CI에서 명령별 300초 한도가 전혀
    # 발동하지 않고 job이 45분 한도에 잘려 나갔다. 파일이면 기다릴 파이프가 없다.
    print(f"[smoke] 실행: {' '.join(command)}", file=sys.stderr, flush=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="taxax-smoke-io-") as io_directory:
        out_path = Path(io_directory) / "stdout.txt"
        err_path = Path(io_directory) / "stderr.txt"
        with out_path.open("wb") as out_file, err_path.open("wb") as err_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                # 입력을 기다리다 멈추는 일이 없도록 stdin을 닫아 둔다.
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=err_file,
            )
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _kill_process_tree(process)
                # 멈추기 직전까지 무엇을 썼는지가 원인 판별의 거의 유일한 단서다.
                # 이걸 버리면 "멈췄다"는 사실만 남고 왜 멈췄는지는 알 수 없다.
                partial_out = out_path.read_text(encoding="utf-8", errors="replace")
                partial_err = err_path.read_text(encoding="utf-8", errors="replace")
                raise RuntimeError(
                    f"frozen command가 {timeout:.0f}초 안에 끝나지 "
                    f"않았습니다: {' '.join(command)}\n"
                    f"멈추기 전 stdout({len(partial_out)}자):\n{partial_out[:2000]}\n"
                    f"멈추기 전 stderr({len(partial_err)}자):\n{partial_err[:2000]}"
                ) from exc
        # 한글 Windows에서는 콘솔 코드페이지(cp949) 바이트가 섞여 들어온다.
        # 진단용 출력이므로 깨진 바이트는 대체 문자로 넘긴다.
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
    # 느린 것과 멈춘 것을 구분하려면 실제 소요 시간이 로그에 남아야 한다.
    print(f"[smoke]   완료 {time.monotonic() - started:.1f}초", file=sys.stderr, flush=True)
    if returncode != 0:
        raise RuntimeError(
            f"frozen command failed ({returncode}): {command[0]}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    return stdout


async def _mcp_smoke(executable: Path, environment: dict[str, str]) -> None:
    print(f"[smoke] 실행: frozen MCP stdio round trip ({executable.name})", file=sys.stderr, flush=True)
    try:
        await asyncio.wait_for(_mcp_exchange(executable, environment), _MCP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        # stdio 클라이언트는 서버가 침묵하면 그대로 영원히 기다린다.
        raise RuntimeError(
            f"frozen MCP 서버가 {_MCP_TIMEOUT_SECONDS:.0f}초 안에 응답하지 않았습니다: {executable.name}"
        ) from exc


async def _mcp_exchange(executable: Path, environment: dict[str, str]) -> None:
    parameters = StdioServerParameters(
        command=str(executable),
        args=["--transport", "stdio"],
        env=environment,
    )
    async with Client(parameters, raise_exceptions=True) as client:
        listed = await client.list_tools()
        if {tool.name for tool in listed.tools} != _EXPECTED_TOOLS:
            raise RuntimeError("frozen MCP tool 목록이 6개 공개 계약과 다릅니다.")
        status = await client.call_tool("get_source_status", {})
        if status.is_error:
            raise RuntimeError("frozen MCP 출처 상태 조회가 실패했습니다.")
        search = await client.call_tool("search_legal_sources", {"query": "대손금"})
        if search.is_error:
            raise RuntimeError("frozen MCP offline 검색이 실패했습니다.")


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
        # 파일로 결과를 쓰는 호출을 먼저 돌린다. --help는 stdout에 쓰는 유일한
        # 호출인데, CI에서 바로 이 명령이 600초를 넘겨도 아무것도 출력하지 않고
        # 멈췄다. 순서를 바꾸면 "이 exe가 CI에서 아예 못 뜨는 것"과 "stdout으로
        # 쓰는 경로만 막히는 것"이 갈린다. 앞의 둘이 통과하고 --help만 멈추면
        # 원인은 실행파일 기동이 아니라 stdout 쪽이다.
        paths_output = scratch / "setup-paths.json"
        _run(
            [str(setup), "--paths-output", str(paths_output)],
            cwd=first_cwd,
            environment=environment,
            timeout=_SETUP_TIMEOUT_SECONDS,
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
            timeout=_SETUP_TIMEOUT_SECONDS,
        )
        _run([str(setup), "--help"], cwd=first_cwd, environment=environment, timeout=_SETUP_TIMEOUT_SECONDS)
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
