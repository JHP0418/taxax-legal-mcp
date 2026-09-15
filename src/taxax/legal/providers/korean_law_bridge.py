from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..models import ErrorCode
from .base import ProviderError

BRIDGE_PACKAGE = "korean-law-mcp"
BRIDGE_VERSION = "4.13.0"
BRIDGE_COMMAND = "korean-law-mcp"
NODE_MINIMUM = "20.19.0"
ALLOWED_TOOLS = frozenset({"search_law", "get_law_text", "get_annexes", "search_decisions", "get_decision_text", "ordinance_radar"})


@dataclass(frozen=True)
class BridgeResult:
    tool: str
    content: str
    auxiliary_only: bool = True
    requires_official_verification: bool = True


class KoreanLawBridge:
    def __init__(self, *, enabled: bool = False, timeout_seconds: float = 15.0, caller: Callable[[str, dict[str, Any]], Awaitable[str]] | None = None):
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self._caller = caller

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "package": BRIDGE_PACKAGE,
            "version": BRIDGE_VERSION,
            "node_minimum": NODE_MINIMUM,
            "allowlist": sorted(ALLOWED_TOOLS),
            "raw_source": False,
            "failure_isolated": True,
            "auto_install": False,
            "executable_available": bool(shutil.which(BRIDGE_COMMAND)),
        }

    async def call(self, tool: str, arguments: dict[str, Any]) -> BridgeResult:
        if not self.enabled:
            raise ProviderError(ErrorCode.POLICY_DISABLED, "korean-law-mcp bridge는 기본 비활성 상태입니다.")
        if tool not in ALLOWED_TOOLS:
            raise ProviderError(ErrorCode.ACCESS_DENIED, "bridge allowlist 밖의 도구는 호출할 수 없습니다.")
        caller = self._caller or self._call_self_hosted
        try:
            content = await asyncio.wait_for(caller(tool, arguments), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            raise ProviderError(ErrorCode.UPSTREAM_UNAVAILABLE, "보조 bridge 호출 시간이 초과되었습니다.", retryable=True) from None
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(ErrorCode.UPSTREAM_UNAVAILABLE, f"보조 bridge 실패: {type(exc).__name__}", retryable=True) from None
        return BridgeResult(tool=tool, content=content)

    @staticmethod
    async def _call_self_hosted(tool: str, arguments: dict[str, Any]) -> str:
        from mcp import Client, StdioServerParameters

        executable = shutil.which(BRIDGE_COMMAND)
        if executable is None:
            raise ProviderError(
                ErrorCode.POLICY_DISABLED,
                f"{BRIDGE_PACKAGE}@{BRIDGE_VERSION} 실행 파일을 운영자가 사전 설치해야 합니다.",
            )
        environment = {key: os.environ[key] for key in ("PATH", "Path", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "HOME", "USERPROFILE") if key in os.environ}
        parameters = StdioServerParameters(command=executable, args=[], env=environment)
        async with Client(parameters, raise_exceptions=True) as client:
            result = await client.call_tool(tool, arguments)
        texts = [item.text for item in result.content if getattr(item, "type", None) == "text"]
        return "\n".join(texts)
