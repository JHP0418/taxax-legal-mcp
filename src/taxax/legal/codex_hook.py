"""Codex Stop 훅: 법률 답변을 한 번만 다시 검토하도록 요청한다.

최종 응답을 강제로 검증하거나 별도 모델을 호출하지 않는다. Codex가 훅을
신뢰하고 실행할 때만 한 번의 모델 continuation이 발생한다.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .codex_config import (
    CodexConfigError,
    CodexMergeResult,
    _private_backup,
    _write_atomic,
    default_codex_config,
)

_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_EVENT_BYTES = 512 * 1024
# 세목 이름만 보면 "비상장주식은 최근 매매가격을 그대로 쓰면 된다"처럼 세목 없이
# 개념으로 말한 세무 답변을 놓친다. 일반 개발 답변에 흔한 단어(평가·테스트 등)는 넣지 않는다.
_LEGAL_CONTENT = re.compile(
    r"법령|법률|판례|결정례|심판례|예규|유권해석|시행령|시행규칙|제\s*\d+\s*조|"
    r"세법|국세|지방세|과세|비과세|세무|세금|세율|세액|가산세|원천징수|경정청구|공제|감면|"
    r"법인세|소득세|부가가치세|취득세|상속|증여|양도세|양도소득|종합부동산세|"
    r"손금|익금|결손금|대손금|감가상각|시가|부당행위|특수관계|비상장\s*주식"
)
_STATUS_MESSAGE = "TAXax legal answer recheck"


def evaluate_stop(event: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    enabled = settings.get("enabled")
    count = settings.get("max_rechecks")
    if not isinstance(enabled, bool) or type(count) is not int or count not in (0, 1):
        raise ValueError(
            "훅 설정은 enabled(boolean), max_rechecks(0 또는 1)만 허용합니다."
        )
    if not enabled or count == 0 or event.get("stop_hook_active") is True:
        return {}
    if event.get("hook_event_name") != "Stop":
        return {}
    answer = event.get("last_assistant_message")
    if not isinstance(answer, str) or not _LEGAL_CONTENT.search(answer):
        return {}
    return {
        "decision": "block",
        "reason": (
            "TAXax 법률 답변 최종 점검을 한 번 수행하십시오. 주장마다 법제처·국세청 등 "
            "실제 열리는 공식 원문 링크, 해당 조문/사건, 시행·결정일과 인용 문구를 다시 확인하십시오. "
            "TAXax MCP 원문·verify_legal_citations 또는 공식 웹 원문을 재조회하고, "
            "원문이나 시점·링크가 확인되지 않으면 그 주장은 빼고 확인 불가로 표시하십시오. "
            "합성 자료를 실제 법령으로 인용하지 마십시오. 이미 충분히 검증했다면 기존 근거를 "
            "다시 대조하고 요점만 답하십시오. 고객 자료나 인증키를 새 도구에 보내지 마십시오."
        ),
    }


def _read_json_file(path: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    if path.is_symlink():
        raise CodexConfigError(f"Codex 훅 파일은 symbolic link일 수 없습니다: {path}")
    if not path.exists():
        return None, None
    if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
        raise CodexConfigError(f"Codex 훅 파일을 안전하게 읽을 수 없습니다: {path}")
    payload = path.read_bytes()
    try:
        parsed = json.loads(payload)
    except (UnicodeError, ValueError) as exc:
        raise CodexConfigError(
            f"Codex 훅 JSON이 올바르지 않아 변경하지 않았습니다: {path}"
        ) from exc
    if not isinstance(parsed, dict):
        raise CodexConfigError(f"Codex 훅 JSON 객체가 아닙니다: {path}")
    return parsed, payload


def _hook_command(cli: Path, settings: Path) -> str:
    parts = [str(cli.resolve()), "stop-hook", "--settings", str(settings.resolve())]
    return (
        subprocess.list2cmdline(parts) if sys.platform == "win32" else shlex.join(parts)
    )


def _our_handler(handler: Any) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and handler.get("statusMessage") == _STATUS_MESSAGE
    )


def _our_hook(group: Any) -> bool:
    return (
        isinstance(group, dict)
        and isinstance(group.get("hooks"), list)
        and any(_our_handler(handler) for handler in group["hooks"])
    )


def _without_our_handlers(groups: list[Any]) -> list[Any]:
    remaining = []
    for group in groups:
        if not _our_hook(group):
            remaining.append(group)
            continue
        handlers = [handler for handler in group["hooks"] if not _our_handler(handler)]
        if handlers:
            remaining.append({**group, "hooks": handlers})
    return remaining


def _backup_and_write(
    path: Path, original: bytes | None, payload: dict[str, Any]
) -> Path | None:
    backup = None
    try:
        if original is not None:
            backup = _private_backup(path, original)
        if original is not None and path.read_bytes() != original:
            raise CodexConfigError(
                "Codex 훅 파일이 동시에 변경되어 등록을 중단했습니다."
            )
        _write_atomic(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            existed=original is not None,
        )
    except Exception:
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise
    return backup


def register_stop_hook(
    cli: Path,
    *,
    path: Path | None = None,
    settings_path: Path | None = None,
    confirmed: bool,
    allow_update: bool = False,
) -> CodexMergeResult:
    if not confirmed:
        raise CodexConfigError(
            "Codex 답변 재검토 훅 등록에는 사용자의 동의가 필요합니다."
        )
    target = (path or default_codex_config().with_name("hooks.json")).expanduser()
    settings = (settings_path or target.with_name("taxax-legal-hook.json")).expanduser()
    if (
        not target.is_absolute()
        or not settings.is_absolute()
        or target.resolve() == settings.resolve()
        or not cli.resolve().is_file()
    ):
        raise CodexConfigError("Codex 훅 경로와 TAXax 실행 파일을 확인하십시오.")
    current, original = _read_json_file(target)
    current = current or {}
    hooks = current.get("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get("Stop", []), list):
        raise CodexConfigError("기존 Codex 훅 구조를 안전하게 병합할 수 없습니다.")
    groups = list(hooks.get("Stop", []))
    existing = [group for group in groups if _our_hook(group)]
    if len(existing) > 1:
        raise CodexConfigError("TAXax 훅이 여러 개이므로 자동 변경하지 않습니다.")
    command = _hook_command(cli, settings)
    entry = {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 20,
                "statusMessage": _STATUS_MESSAGE,
            }
        ]
    }
    matches = existing == [entry]
    if existing and not matches and not allow_update:
        raise CodexConfigError("기존 TAXax 훅이 달라 --force 없이 교체하지 않습니다.")
    saved_settings, _ = _read_json_file(settings)
    if saved_settings is not None:
        evaluate_stop({}, saved_settings)
    if matches:
        if saved_settings is None:
            _backup_and_write(settings, None, {"enabled": True, "max_rechecks": 1})
        return CodexMergeResult(changed=False, config_path=target, backup_path=None)
    merged = dict(current)
    merged_hooks = dict(hooks)
    merged_hooks["Stop"] = _without_our_handlers(groups) + [entry]
    merged["hooks"] = merged_hooks
    backup = _backup_and_write(target, original, merged)
    if saved_settings is None:
        _backup_and_write(settings, None, {"enabled": True, "max_rechecks": 1})
    return CodexMergeResult(changed=True, config_path=target, backup_path=backup)


def unregister_stop_hook(
    *, path: Path | None = None, confirmed: bool
) -> CodexMergeResult:
    if not confirmed:
        raise CodexConfigError(
            "Codex 답변 재검토 훅 제거에는 사용자의 동의가 필요합니다."
        )
    target = (path or default_codex_config().with_name("hooks.json")).expanduser()
    current, original = _read_json_file(target)
    if current is None:
        return CodexMergeResult(changed=False, config_path=target, backup_path=None)
    hooks = current.get("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get("Stop", []), list):
        raise CodexConfigError("기존 Codex 훅 구조를 안전하게 편집할 수 없습니다.")
    groups = hooks.get("Stop", [])
    if not any(_our_hook(group) for group in groups):
        return CodexMergeResult(changed=False, config_path=target, backup_path=None)
    merged = dict(current)
    merged_hooks = dict(hooks)
    remaining = _without_our_handlers(groups)
    if remaining:
        merged_hooks["Stop"] = remaining
    else:
        merged_hooks.pop("Stop", None)
    if merged_hooks:
        merged["hooks"] = merged_hooks
    else:
        merged.pop("hooks", None)
    backup = _backup_and_write(target, original, merged)
    return CodexMergeResult(changed=True, config_path=target, backup_path=backup)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="TAXax Codex Stop hook")
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        event_raw = sys.stdin.buffer.read(_MAX_EVENT_BYTES + 1)
        if len(event_raw) > _MAX_EVENT_BYTES:
            raise ValueError("훅 입력이 너무 큽니다.")
        event = json.loads(event_raw)
        if not isinstance(event, dict):
            raise ValueError("훅 입력이 JSON 객체가 아닙니다.")
        settings, _ = _read_json_file(args.settings)
        if settings is None:
            raise ValueError("훅 설정이 없습니다.")
        result = evaluate_stop(event, settings)
    except (OSError, ValueError) as exc:
        result = {
            "systemMessage": f"TAXax 재검토 훅을 실행하지 못했습니다: {exc.__class__.__name__}. 설정을 확인하십시오."
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0
