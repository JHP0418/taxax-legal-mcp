"""Codex(`~/.codex/config.toml`)에 stdio MCP server를 등록·해제한다.

표준 라이브러리에는 TOML writer가 없다. 전체를 파싱해 다시 쓰면 사용자의 주석과
서식이 사라지고 기존 marketplaces/plugins/hooks 설정을 잃을 위험이 있으므로,
이 모듈은 우리 섹션만 텍스트로 덧붙이거나 잘라낸다.
"""

from __future__ import annotations

import os
import re
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .local_config import copy_file_permissions, restrict_file_to_current_user

SERVER_NAME = "taxax-legal"
_SECTION = f"[mcp_servers.{SERVER_NAME}]"
_ENV_SECTION = f"[mcp_servers.{SERVER_NAME}.env]"
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_SECTION_HEADER = re.compile(r"^\s*\[", re.MULTILINE)


class CodexConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CodexMergeResult:
    changed: bool
    config_path: Path
    backup_path: Path | None


def default_codex_config() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return base / "config.toml"


def _toml_string(value: str) -> str:
    """TOML literal string. Windows 경로의 역슬래시를 이스케이프하지 않아도 된다."""
    if "'" in value or "\n" in value or "\r" in value:
        raise CodexConfigError("TOML literal string으로 표현할 수 없는 값입니다.")
    return f"'{value}'"


def _read(path: Path) -> tuple[str, dict[str, Any]]:
    if not path.exists():
        return "", {}
    if path.is_symlink() or not path.is_file():
        raise CodexConfigError("Codex 설정 경로가 일반 파일이 아닙니다.")
    if path.stat().st_size > _MAX_CONFIG_BYTES:
        raise CodexConfigError("Codex 설정 파일이 허용 크기를 초과합니다.")
    text = path.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CodexConfigError("Codex 설정 TOML이 올바르지 않아 변경하지 않았습니다.") from exc
    return text, parsed


def _expected_env(data_dir: Path, extra_env: dict[str, str] | None) -> dict[str, str]:
    environment = {"TAXAX_LEGAL_DATA_DIR": str(data_dir.resolve())}
    environment.update(extra_env or {})
    return environment


def server_block(mcp_executable: Path, data_dir: Path, *, extra_env: dict[str, str] | None = None) -> str:
    executable = mcp_executable.resolve()
    storage = data_dir.resolve()
    if not executable.is_absolute() or not storage.is_absolute():
        raise CodexConfigError("MCP executable과 data directory는 절대경로여야 합니다.")
    environment = _expected_env(data_dir, extra_env)
    env_lines = "\n".join(f"{key} = {_toml_string(value)}" for key, value in environment.items())
    return (
        f"{_SECTION}\n"
        f"command = {_toml_string(str(executable))}\n"
        'args = ["--transport", "stdio"]\n'
        f"\n{_ENV_SECTION}\n"
        f"{env_lines}\n"
    )


def _existing_entry(parsed: dict[str, Any]) -> dict[str, Any] | None:
    servers = parsed.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    entry = servers.get(SERVER_NAME)
    return entry if isinstance(entry, dict) else None


def _matches(
    entry: dict[str, Any],
    mcp_executable: Path,
    data_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> bool:
    environment = entry.get("env")
    return (
        entry.get("command") == str(mcp_executable.resolve())
        and list(entry.get("args") or []) == ["--transport", "stdio"]
        and environment == _expected_env(data_dir, extra_env)
    )


def _strip_sections(text: str) -> str:
    """`[mcp_servers.taxax-legal]`과 그 하위 테이블만 제거한다."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        if _SECTION_HEADER.match(line):
            header = line.strip()
            skipping = header.startswith(_SECTION[:-1]) and (
                header == _SECTION or header.startswith(f"[mcp_servers.{SERVER_NAME}.")
            )
        if not skipping:
            kept.append(line)
    return "".join(kept)


def _write_atomic(path: Path, payload: str, *, existed: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8", newline="")
        if existed:
            copy_file_permissions(path, temporary)
        else:
            restrict_file_to_current_user(temporary)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def register_codex(
    mcp_executable: Path,
    data_dir: Path,
    *,
    path: Path | None = None,
    confirmed: bool,
    allow_update: bool = False,
    extra_env: dict[str, str] | None = None,
) -> CodexMergeResult:
    if not confirmed:
        raise CodexConfigError("Codex 설정 등록에는 사용자의 명시적 동의가 필요합니다.")
    target = (path or default_codex_config()).expanduser()
    if not target.is_absolute():
        raise CodexConfigError("Codex 설정 파일은 절대경로여야 합니다.")
    text, parsed = _read(target)
    existing = _existing_entry(parsed)
    if existing is not None and _matches(existing, mcp_executable, data_dir, extra_env=extra_env):
        return CodexMergeResult(changed=False, config_path=target, backup_path=None)
    if existing is not None and not allow_update:
        raise CodexConfigError("동일한 taxax-legal Codex 등록이 이미 있어 확인 없이 덮어쓰지 않습니다.")

    existed = bool(text)
    backup: Path | None = None
    if existed:
        backup = target.with_name(f"{target.name}.taxax-backup-{uuid.uuid4().hex[:8]}")
        backup.write_text(text, encoding="utf-8", newline="")

    body = _strip_sections(text) if existing is not None else text
    if body and not body.endswith("\n"):
        body += "\n"
    block = server_block(mcp_executable, data_dir, extra_env=extra_env)
    payload = f"{body}\n{block}" if body else block
    try:
        tomllib.loads(payload)
    except tomllib.TOMLDecodeError as exc:
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise CodexConfigError("생성된 Codex 설정이 올바른 TOML이 아니어서 변경하지 않았습니다.") from exc
    try:
        _write_atomic(target, payload, existed=existed)
    except Exception:
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise
    return CodexMergeResult(changed=True, config_path=target, backup_path=backup)


def unregister_codex(path: Path | None = None, *, confirmed: bool) -> CodexMergeResult:
    if not confirmed:
        raise CodexConfigError("Codex 설정 변경에는 사용자의 명시적 동의가 필요합니다.")
    target = (path or default_codex_config()).expanduser()
    if not target.is_absolute():
        raise CodexConfigError("Codex 설정 파일은 절대경로여야 합니다.")
    text, parsed = _read(target)
    if _existing_entry(parsed) is None:
        return CodexMergeResult(changed=False, config_path=target, backup_path=None)
    backup = target.with_name(f"{target.name}.taxax-backup-{uuid.uuid4().hex[:8]}")
    backup.write_text(text, encoding="utf-8", newline="")
    payload = _strip_sections(text)
    try:
        tomllib.loads(payload)
    except tomllib.TOMLDecodeError as exc:
        backup.unlink(missing_ok=True)
        raise CodexConfigError("제거 결과가 올바른 TOML이 아니어서 변경하지 않았습니다.") from exc
    try:
        _write_atomic(target, payload, existed=True)
    except Exception:
        backup.unlink(missing_ok=True)
        raise
    return CodexMergeResult(changed=True, config_path=target, backup_path=backup)
