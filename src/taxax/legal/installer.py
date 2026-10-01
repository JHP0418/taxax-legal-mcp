from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import site
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex_config import CodexConfigError, register_codex
from .codex_hook import register_stop_hook
from .local_config import (
    LocalSecretError,
    copy_file_permissions,
    default_secret_file,
    delete_law_go_credential,
    restrict_file_to_current_user,
    restricted_permissions_verified,
    save_law_go_credential,
)
from .service import default_legal_data_dir
from .version import package_version

SERVER_NAME = "taxax-legal"
# 설치 경로를 버전별로 나누는 값이므로 배포 버전과 어긋나면 안 된다.
APPLICATION_VERSION = package_version()
_EXECUTABLES = ("taxax-legal.exe", "taxax-legal-mcp.exe")
_MAX_CLAUDE_CONFIG_BYTES = 4 * 1024 * 1024


class InstallationError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigMergeResult:
    changed: bool
    config_path: Path
    backup_path: Path | None


@dataclass(frozen=True)
class InstallationResult:
    install_dir: Path
    data_dir: Path
    config_path: Path
    backup_path: Path | None
    credential_configured: bool
    doctor_status: str
    codex_config_path: Path | None = None
    codex_registered: bool = False
    codex_hook_path: Path | None = None
    claude_code_config_path: Path | None = None
    claude_code_registered: bool = False


def default_install_root() -> Path:
    if os.name != "nt":
        raise InstallationError("Windows local installer는 Windows에서만 사용할 수 있습니다.")
    configured = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / "AppData" / "Local"
    return base / "TAXax" / "app"


def default_claude_desktop_config() -> Path:
    if os.name != "nt":
        raise InstallationError("Claude Desktop 자동 등록은 Windows installer에서만 지원합니다.")
    configured = os.environ.get("APPDATA", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / "AppData" / "Roaming"
    return base / "Claude" / "claude_desktop_config.json"


def default_claude_code_config() -> Path:
    """Claude Code는 OS와 무관하게 홈 디렉터리의 `.claude.json`을 쓴다.

    Claude Desktop 설정과 달리 이 파일에는 프로젝트 기록 등 MCP 외 설정도 함께
    들어 있으므로, 우리 항목만 병합하고 나머지는 그대로 보존해야 한다.
    """
    return Path.home() / ".claude.json"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_path(path: Path) -> None:
    if path.is_symlink():
        raise InstallationError("설치 또는 설정 파일은 symbolic link일 수 없습니다.")
    current = path.parent
    while current != current.parent:
        if current.is_symlink():
            raise InstallationError("설치 또는 설정 경로에 symbolic link directory가 포함될 수 없습니다.")
        current = current.parent


def _read_config(path: Path) -> tuple[dict[str, Any], bytes | None, tuple[int, int, str] | None]:
    _validate_path(path)
    if not path.exists():
        return {}, None, None
    if not path.is_file() or path.stat().st_size > _MAX_CLAUDE_CONFIG_BYTES:
        raise InstallationError("Claude Desktop 설정 파일 형식 또는 크기가 올바르지 않습니다.")
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    state = (after.st_size, after.st_mtime_ns, _sha256_bytes(payload))
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise InstallationError("Claude Desktop 설정 파일이 읽는 동안 변경됐습니다.")
    try:
        parsed = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallationError("Claude Desktop 설정 JSON이 올바르지 않아 변경하지 않았습니다.") from exc
    if not isinstance(parsed, dict):
        raise InstallationError("Claude Desktop 설정 root는 JSON object여야 합니다.")
    servers = parsed.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise InstallationError("Claude Desktop mcpServers는 JSON object여야 합니다.")
    return parsed, payload, state


def _file_state(path: Path) -> tuple[int, int, str] | None:
    if not path.exists():
        return None
    metadata = path.stat()
    return metadata.st_size, metadata.st_mtime_ns, _sha256_file(path)


def _write_new_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _backup_config(path: Path, payload: bytes) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.backup-{timestamp}-{uuid.uuid4().hex[:8]}")
    try:
        _write_new_file(backup, payload)
        copy_file_permissions(path, backup)
    except Exception:
        backup.unlink(missing_ok=True)
        raise
    return backup


def server_configuration(
    mcp_executable: Path,
    data_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    declare_transport_type: bool = False,
) -> dict[str, Any]:
    executable = mcp_executable.resolve()
    storage = data_dir.resolve()
    if not executable.is_absolute() or not storage.is_absolute():
        raise InstallationError("MCP executable과 data directory는 절대경로여야 합니다.")
    environment = {"TAXAX_LEGAL_DATA_DIR": str(storage)}
    environment.update(extra_env or {})
    server: dict[str, Any] = {
        "command": str(executable),
        "args": ["--transport", "stdio"],
        "env": environment,
    }
    if declare_transport_type:
        # Claude Code는 `claude mcp add`로 등록할 때 transport 종류를 항목에 남긴다.
        # 같은 형태로 써야 우리가 병합한 항목이 CLI가 만든 것과 구분되지 않는다.
        return {"type": "stdio", **server}
    return server


def merge_claude_desktop_config(
    path: Path,
    server: dict[str, Any],
    *,
    confirmed: bool,
    allow_update: bool = False,
    client_label: str = "Claude Desktop",
) -> ConfigMergeResult:
    """`mcpServers` 키를 쓰는 MCP client 설정에 우리 항목만 병합한다.

    Claude Desktop과 Claude Code(`~/.claude.json`)가 같은 구조를 쓰므로 한
    구현을 공유한다. client_label은 오류 메시지에만 쓰인다.
    """
    if not confirmed:
        raise InstallationError(f"{client_label} 설정 등록에는 사용자의 명시적 동의가 필요합니다.")
    target = path.expanduser()
    if not target.is_absolute():
        raise InstallationError(f"{client_label} 설정 파일은 절대경로여야 합니다.")
    configuration, original_payload, original_state = _read_config(target)
    servers = dict(configuration.get("mcpServers") or {})
    existing = servers.get(SERVER_NAME)
    if existing == server:
        return ConfigMergeResult(changed=False, config_path=target, backup_path=None)
    if existing is not None and not allow_update:
        raise InstallationError("동일한 taxax-legal MCP 등록이 이미 있어 확인 없이 덮어쓰지 않습니다.")
    servers[SERVER_NAME] = server
    updated = dict(configuration)
    updated["mcpServers"] = servers
    payload = (json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    target.parent.mkdir(parents=True, exist_ok=True)
    _validate_path(target)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    backup: Path | None = None
    try:
        _write_new_file(temporary, payload)
        if target.exists():
            copy_file_permissions(target, temporary)
        else:
            restrict_file_to_current_user(temporary)
            if not restricted_permissions_verified(temporary):
                raise InstallationError(f"새 {client_label} 설정 파일 권한을 검증하지 못했습니다.")
        if original_payload is not None:
            backup = _backup_config(target, original_payload)
        if _file_state(target) != original_state:
            raise InstallationError(f"{client_label} 설정 파일이 동시에 변경돼 병합을 중단했습니다.")
        if original_state is None:
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise InstallationError(f"{client_label} 설정 파일이 동시에 생성돼 병합을 중단했습니다.") from exc
            temporary.unlink()
        else:
            os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise
    return ConfigMergeResult(changed=True, config_path=target, backup_path=backup)


def install_executables(source_dir: Path, install_dir: Path) -> Path:
    source_root = source_dir.resolve()
    destination = install_dir.expanduser()
    if not destination.is_absolute():
        raise InstallationError("설치 directory는 절대경로여야 합니다.")
    _validate_path(destination)
    missing = [name for name in _EXECUTABLES if not (source_root / name).is_file()]
    if missing:
        raise InstallationError("설치 bundle에 필수 실행파일이 없습니다: " + ", ".join(missing))
    if destination.exists():
        if not destination.is_dir():
            raise InstallationError("설치 대상 경로가 directory가 아닙니다.")
        matches = all(
            (destination / name).is_file()
            and _sha256_file(destination / name) == _sha256_file(source_root / name)
            for name in _EXECUTABLES
        )
        if not matches:
            raise InstallationError("같은 version 설치 directory의 파일이 달라 덮어쓰지 않습니다.")
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".taxax-legal-install-", dir=destination.parent) as directory:
        staged = Path(directory) / "app"
        staged.mkdir()
        for name in _EXECUTABLES:
            shutil.copy2(source_root / name, staged / name)
            if _sha256_file(source_root / name) != _sha256_file(staged / name):
                raise InstallationError("설치 실행파일 hash 검증에 실패했습니다.")
        try:
            staged.rename(destination)
        except FileExistsError as exc:
            raise InstallationError("설치 directory가 동시에 생성돼 덮어쓰지 않습니다.") from exc
    return destination


_SERVER_PROCESS_MARKER = "taxax-legal-mcp"


def running_mcp_servers() -> list[dict[str, Any]]:
    """실행 중인 taxax-legal MCP 서버 프로세스를 찾는다.

    MCP client가 서버를 띄워 둔 상태에서는 Windows가 실행파일을 잠그기 때문에
    `pip install --upgrade`가 WinError 32로 실패한다. 무엇을 닫아야 하는지
    알려주려면 먼저 누가 붙잡고 있는지 알아야 한다.
    """
    current = os.getpid()
    try:
        if os.name == "nt":
            # 찾는 문자열을 스크립트에 직접 쓰면 이 조회용 powershell.exe 자신의
            # 명령줄에도 그 문자열이 들어가 스스로를 서버로 잡는다. 환경변수로
            # 넘기면 명령줄에는 변수명만 남는다.
            # 명령줄에 이 문자열이 들어갔다는 이유만으로 고르면, 그 문자열을
            # 인자로 가진 셸이나 스크립트까지 잡혀 사용자의 터미널을 죽일 수 있다.
            # 실행 이미지가 우리 서버이거나, 그 실행파일을 stdio 전송으로 직접
            # 띄운 python 프로세스만 고른다.
            script = (
                "Get-CimInstance Win32_Process"
                " | Where-Object {"
                "   $_.Name -eq ($env:TAXAX_SERVER_MARKER + '.exe') -or"
                "   (($_.Name -like 'python*') -and"
                "    ($_.CommandLine -like ('*' + $env:TAXAX_SERVER_MARKER + '.exe*')) -and"
                "    ($_.CommandLine -like '*--transport*'))"
                " }"
                " | Select-Object ProcessId, Name"
                " | ConvertTo-Json -Compress"
            )
            executable = shutil.which("powershell.exe") or shutil.which("powershell")
            if executable is None:
                return []
            environment = dict(os.environ)
            environment["TAXAX_SERVER_MARKER"] = _SERVER_PROCESS_MARKER
            completed = subprocess.run(
                [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            payload = (completed.stdout or "").strip().lstrip("﻿")
            if not payload:
                return []
            parsed = json.loads(payload)
            entries = parsed if isinstance(parsed, list) else [parsed]
            found = [
                {"pid": int(entry["ProcessId"]), "name": str(entry.get("Name") or "")}
                for entry in entries
                if isinstance(entry, dict) and entry.get("ProcessId")
            ]
        else:
            completed = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            found = []
            for line in (completed.stdout or "").splitlines():
                stripped = line.strip()
                if _SERVER_PROCESS_MARKER not in stripped:
                    continue
                pid_text, _, args = stripped.partition(" ")
                argv = args.strip()
                if not pid_text.isdigit() or not argv:
                    continue
                # Windows와 같은 이유로, 실행 이미지 자체가 우리 서버이거나 그
                # 실행파일을 stdio 전송으로 띄운 python인 경우만 센다.
                program = os.path.basename(argv.split()[0])
                hosted_by_python = (
                    program.startswith("python")
                    and _SERVER_PROCESS_MARKER in argv
                    and "--transport" in argv
                )
                if program.startswith(_SERVER_PROCESS_MARKER) or hosted_by_python:
                    found.append({"pid": int(pid_text), "name": argv[:120]})
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return []
    # 자신과 자신을 띄운 부모는 절대 후보에 넣지 않는다. 이 명령을 실행한
    # 셸이나 래퍼 스크립트를 스스로 종료하는 일을 막기 위한 안전장치다.
    protected = {current, os.getppid()} if hasattr(os, "getppid") else {current}
    return [entry for entry in found if entry["pid"] not in protected]


def stop_mcp_servers(*, confirmed: bool) -> list[dict[str, Any]]:
    """실행 중인 taxax-legal MCP 서버를 종료한다.

    서버는 stdio로만 상태를 주고받고 영속 상태는 DB에 있으므로 종료해도 잃는
    자료가 없다. MCP client를 재시작하면 다시 떠오른다.
    """
    if not confirmed:
        raise InstallationError("실행 중인 MCP 서버 종료에는 사용자의 명시적 확인이 필요합니다.")
    stopped: list[dict[str, Any]] = []
    for entry in running_mcp_servers():
        try:
            os.kill(entry["pid"], signal.SIGTERM)
        except (OSError, ValueError):
            continue
        stopped.append(entry)
    return stopped


def run_doctor(cli_executable: Path, data_dir: Path) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["TAXAX_LEGAL_DATA_DIR"] = str(data_dir.resolve())
    environment.pop("PYTHONPATH", None)
    # 자식이 한국어 JSON을 콘솔 기본 인코딩(Windows cp949)으로 쓰면 부모의 UTF-8 디코딩이 깨진다.
    environment["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [str(cli_executable.resolve()), "doctor"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise InstallationError("설치 후 doctor 진단에 실패했습니다.")
    try:
        result = json.loads(completed.stdout or "")
    except json.JSONDecodeError as exc:
        raise InstallationError("설치 후 doctor 결과를 읽지 못했습니다.") from exc
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise InstallationError("설치 후 doctor 진단이 정상 상태가 아닙니다.")
    return result


def install_local_application(
    source_dir: Path,
    *,
    config_path: Path | None = None,
    credential: str | None = None,
    consent_to_configure_claude: bool,
    allow_config_update: bool = False,
) -> InstallationResult:
    if not consent_to_configure_claude:
        raise InstallationError("Claude Desktop 설정 등록에는 사용자의 명시적 동의가 필요합니다.")
    data_dir = default_legal_data_dir().resolve()
    target_install_dir = default_install_root() / APPLICATION_VERSION
    target_config = (config_path or default_claude_desktop_config()).expanduser()
    if not target_config.is_absolute():
        raise InstallationError("Claude Desktop 설정 파일은 절대경로여야 합니다.")
    server = server_configuration(target_install_dir / "taxax-legal-mcp.exe", data_dir)
    configuration, _, _ = _read_config(target_config)
    existing = (configuration.get("mcpServers") or {}).get(SERVER_NAME)
    if existing is not None and existing != server and not allow_config_update:
        raise InstallationError("동일한 taxax-legal MCP 등록이 이미 있어 확인 없이 덮어쓰지 않습니다.")

    data_dir.mkdir(parents=True, exist_ok=True)
    install_dir = install_executables(source_dir, target_install_dir)
    doctor = run_doctor(install_dir / "taxax-legal.exe", data_dir)
    credential_configured = False
    if credential is not None and credential.strip():
        try:
            save_law_go_credential(credential)
        except LocalSecretError as exc:
            raise InstallationError(str(exc)) from exc
        credential_configured = True
    merged = merge_claude_desktop_config(
        target_config,
        server,
        confirmed=True,
        allow_update=allow_config_update,
    )
    return InstallationResult(
        install_dir=install_dir,
        data_dir=data_dir,
        config_path=merged.config_path,
        backup_path=merged.backup_path,
        credential_configured=credential_configured,
        doctor_status=str(doctor["status"]),
    )


def _scripts_directories() -> list[Path]:
    """console script가 설치될 수 있는 디렉터리를 우선순위대로 모은다.

    Microsoft Store 파이썬은 sysconfig의 기본 scripts 경로를 존재하지 않는
    `%ProgramFiles%\\WindowsApps\\...\\Scripts`로 보고하고, 실제 파일은 사용자
    LocalCache 아래에 둔다. PATH에도 없어서 shutil.which로도 찾지 못한다.
    """
    candidates: list[Path] = []

    def add(value: str | None) -> None:
        if not value:
            return
        path = Path(value)
        if path not in candidates:
            candidates.append(path)

    add(sysconfig.get_path("scripts"))
    for scheme in ("nt_user", "posix_user"):
        if scheme in sysconfig.get_scheme_names():
            try:
                add(sysconfig.get_path("scripts", scheme=scheme))
            except KeyError:
                pass
    try:
        add(str(Path(site.getusersitepackages()).parent / ("Scripts" if os.name == "nt" else "bin")))
    except (AttributeError, TypeError):
        pass
    add(str(Path(sys.executable).parent))
    add(str(Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")))
    return candidates


def console_script(name: str) -> Path:
    """현재 파이썬 환경에 설치된 console script 경로를 찾는다."""
    filename = f"{name}.exe" if os.name == "nt" else name
    for directory in _scripts_directories():
        candidate = directory / filename
        if candidate.is_file():
            return candidate.resolve()
    located = shutil.which(name)
    if located:
        return Path(located).resolve()
    raise InstallationError(
        f"{name} 실행 파일을 찾지 못했습니다. `pip install taxax-legal-mcp`가 끝났는지 확인하십시오."
    )


def _supplemental_provider_env(*, nts_consent: bool, olta_consent: bool) -> dict[str, str]:
    return {}


def install_pip_application(
    *,
    config_path: Path | None = None,
    credential: str | None = None,
    consent_to_configure_claude: bool,
    allow_config_update: bool = False,
    register_claude: bool = True,
    register_codex_client: bool = False,
    codex_config_path: Path | None = None,
    register_claude_code_client: bool = False,
    claude_code_config_path: Path | None = None,
    nts_consent: bool = False,
    olta_consent: bool = False,
) -> InstallationResult:
    """pip로 설치된 console script를 Claude Desktop에 등록한다.

    exe 배포본을 복사하는 install_local_application과 달리 이미 설치된
    entry point를 그대로 가리키므로 파일을 옮기지 않는다.
    """
    if register_claude and not consent_to_configure_claude:
        raise InstallationError("Claude Desktop 설정 등록에는 사용자의 명시적 동의가 필요합니다.")
    data_dir = default_legal_data_dir().resolve()
    mcp_executable = console_script("taxax-legal-mcp")
    cli_executable = console_script("taxax-legal")

    data_dir.mkdir(parents=True, exist_ok=True)
    credential_configured = False
    if credential is not None and credential.strip():
        try:
            save_law_go_credential(credential)
        except LocalSecretError as exc:
            raise InstallationError(str(exc)) from exc
        credential_configured = True

    doctor = run_doctor(cli_executable, data_dir)
    extra_env = _supplemental_provider_env(nts_consent=nts_consent, olta_consent=olta_consent)

    codex_path: Path | None = None
    codex_registered = False
    codex_hook_path: Path | None = None
    if register_codex_client:
        try:
            codex = register_codex(
                mcp_executable,
                data_dir,
                path=codex_config_path,
                confirmed=True,
                allow_update=allow_config_update,
                extra_env=extra_env,
            )
            hook = register_stop_hook(
                cli_executable,
                path=codex.config_path.with_name("hooks.json"),
                confirmed=True,
                allow_update=allow_config_update,
            )
        except CodexConfigError as exc:
            raise InstallationError(str(exc)) from exc
        codex_path = codex.config_path
        codex_registered = True
        codex_hook_path = hook.config_path

    claude_code_path: Path | None = None
    claude_code_registered = False
    if register_claude_code_client:
        target = (claude_code_config_path or default_claude_code_config()).expanduser()
        if not target.is_absolute():
            raise InstallationError("Claude Code 설정 파일은 절대경로여야 합니다.")
        merged_code = merge_claude_desktop_config(
            target,
            server_configuration(mcp_executable, data_dir, extra_env=extra_env, declare_transport_type=True),
            confirmed=True,
            allow_update=allow_config_update,
            client_label="Claude Code",
        )
        claude_code_path = merged_code.config_path
        claude_code_registered = True

    if not register_claude:
        return InstallationResult(
            install_dir=mcp_executable.parent,
            data_dir=data_dir,
            config_path=data_dir,
            backup_path=None,
            credential_configured=credential_configured,
            doctor_status=str(doctor["status"]),
            codex_config_path=codex_path,
            codex_registered=codex_registered,
            codex_hook_path=codex_hook_path,
            claude_code_config_path=claude_code_path,
            claude_code_registered=claude_code_registered,
        )

    target_config = (config_path or default_claude_desktop_config()).expanduser()
    if not target_config.is_absolute():
        raise InstallationError("Claude Desktop 설정 파일은 절대경로여야 합니다.")
    server = server_configuration(mcp_executable, data_dir, extra_env=extra_env)
    merged = merge_claude_desktop_config(
        target_config,
        server,
        confirmed=True,
        allow_update=allow_config_update,
    )
    return InstallationResult(
        install_dir=mcp_executable.parent,
        data_dir=data_dir,
        config_path=merged.config_path,
        backup_path=merged.backup_path,
        credential_configured=credential_configured,
        doctor_status=str(doctor["status"]),
        codex_config_path=codex_path,
        codex_registered=codex_registered,
        codex_hook_path=codex_hook_path,
        claude_code_config_path=claude_code_path,
        claude_code_registered=claude_code_registered,
    )


def unregister_claude_desktop(
    path: Path | None = None,
    *,
    confirmed: bool,
    client_label: str = "Claude Desktop",
) -> ConfigMergeResult:
    """`mcpServers`를 쓰는 client 설정에서 taxax-legal 항목만 제거한다."""
    if not confirmed:
        raise InstallationError(f"{client_label} 설정 변경에는 사용자의 명시적 동의가 필요합니다.")
    target = (path or default_claude_desktop_config()).expanduser()
    if not target.is_absolute():
        raise InstallationError(f"{client_label} 설정 파일은 절대경로여야 합니다.")
    configuration, original_payload, original_state = _read_config(target)
    servers = dict(configuration.get("mcpServers") or {})
    if SERVER_NAME not in servers:
        return ConfigMergeResult(changed=False, config_path=target, backup_path=None)
    del servers[SERVER_NAME]
    updated = dict(configuration)
    updated["mcpServers"] = servers
    payload = (json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    _validate_path(target)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    backup: Path | None = None
    try:
        _write_new_file(temporary, payload)
        copy_file_permissions(target, temporary)
        if original_payload is not None:
            backup = _backup_config(target, original_payload)
        if _file_state(target) != original_state:
            raise InstallationError(f"{client_label} 설정 파일이 동시에 변경돼 제거를 중단했습니다.")
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise
    return ConfigMergeResult(changed=True, config_path=target, backup_path=backup)


def remove_local_credential(*, confirmed: bool) -> bool:
    try:
        return delete_law_go_credential(default_secret_file(), confirmed=confirmed)
    except LocalSecretError as exc:
        raise InstallationError(str(exc)) from exc
