from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from taxax.legal.service import LegalKnowledgeService
from taxax.legal.version import package_version

from .auth import JwtAuthConfiguration, JwtTokenVerifier
from .legal_tools import register_legal_tools


def create_server(
    service: LegalKnowledgeService | None = None,
    *,
    auth_configuration: JwtAuthConfiguration | None = None,
) -> MCPServer:
    if service is None:
        project_root = Path(os.environ.get("TAXAX_PROJECT_ROOT", Path.cwd()))
        data_dir_value = os.environ.get("TAXAX_LEGAL_DATA_DIR")
        service = LegalKnowledgeService(project_root, data_dir=Path(data_dir_value) if data_dir_value else None)
    server = MCPServer(
        name="taxax-legal",
        title="TAXax Legal Knowledge",
        description="공식 출처 원문과 검증 상태를 제공하는 TAXax 법률 지식 MCP",
        instructions=(
            "세법·조문·판례 등 법적 내용의 질문에는 TAXax MCP를 우선 사용하십시오. "
            "공식 법령 검색은 provider=law.go.kr, target=law로 시작하고, "
            "목록의 공식 ID/MST 및 시행일을 확인해 해당 원문을 조회하십시오. "
            "답변 직전 주장마다 독립적으로 근거를 재점검하십시오. "
            "MCP 검색 요약만으로 인용하지 말고 get_legal_document의 원문과 "
            "verify_legal_citations의 문구·위치를 확인하십시오. 웹 자료는 공식 원문 페이지를 다시 여십시오. "
            "각 법적 주장에 해당 기관의 실제 원문 링크와 확인한 조문/사건·시행/결정일을 붙이십시오. "
            "링크가 열리지 않거나 원문·버전과 맞지 않으면 주장하지 말고 확인 불가로 표시하십시오. "
            "RATE_LIMITED, AUTH_FAILED, ACCESS_DENIED 또는 시간초과 뒤에는 같은 출처를 즉시 다시 호출하지 마십시오. "
            "실패한 출처와 미확보 원문을 밝히고 기존에 검증된 자료만 사용하십시오. "
            "기준일·부칙·적용 사정이 미확인인 경우 결론을 보류하십시오. "
            "합성 fixture를 실제 법적 근거로 인용하지 마십시오. "
            "초안의 모든 주장과 링크를 마지막으로 재대조하고 틀리거나 누락된 주장을 제거하십시오."
        ),
        version=package_version(),
        log_level="ERROR",
        token_verifier=JwtTokenVerifier(auth_configuration) if auth_configuration else None,
        auth=auth_configuration.settings() if auth_configuration else None,
    )
    register_legal_tools(server, service, auth_required=auth_configuration is not None)
    return server


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _comma_values(name: str) -> list[str]:
    return [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]


def _authenticated_transport_security() -> TransportSecuritySettings:
    allowed_hosts = _comma_values("TAXAX_MCP_ALLOWED_HOSTS")
    allowed_origins = _comma_values("TAXAX_MCP_ALLOWED_ORIGINS")
    if not allowed_hosts or not allowed_origins:
        raise ValueError("인증 HTTP에는 TAXAX_MCP_ALLOWED_HOSTS와 TAXAX_MCP_ALLOWED_ORIGINS가 필요합니다.")
    for value in allowed_hosts:
        if not re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::\d{1,5})?", value):
            raise ValueError("TAXAX_MCP_ALLOWED_HOSTS 항목 형식이 올바르지 않습니다.")
    for value in allowed_origins:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("TAXAX_MCP_ALLOWED_ORIGINS는 origin만 포함한 HTTPS URL이어야 합니다.")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[value.rstrip("/") for value in allowed_origins],
    )


def _local_transport_security(port: int) -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"],
        allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="taxax-legal-mcp")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.transport == "stdio":
        create_server().run(transport="stdio")
        return 0
    loopback = arguments.host in _LOOPBACK_HOSTS
    try:
        auth_configuration = JwtAuthConfiguration.from_environment(required=not loopback)
        if auth_configuration is not None:
            security = _authenticated_transport_security()
            resource_host = urlparse(auth_configuration.resource_server_url).netloc.lower()
            if resource_host not in {value.lower() for value in security.allowed_hosts}:
                raise ValueError("auth resource host가 TAXAX_MCP_ALLOWED_HOSTS에 없습니다.")
        else:
            if not loopback:
                raise ValueError("인증 없는 HTTP 모드는 loopback 주소에만 bind할 수 있습니다.")
            if os.environ.get("TAXAX_MCP_ALLOW_LOCAL_HTTP") != "1":
                raise ValueError("local HTTP를 사용하려면 TAXAX_MCP_ALLOW_LOCAL_HTTP=1을 명시해야 합니다.")
            security = _local_transport_security(arguments.port)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    server = create_server(auth_configuration=auth_configuration)
    server.run(
        transport="streamable-http",
        host=arguments.host,
        port=arguments.port,
        streamable_http_path="/mcp",
        max_request_body_size=128 * 1024,
        max_sessions=100,
        session_idle_timeout=300,
        transport_security=security,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
