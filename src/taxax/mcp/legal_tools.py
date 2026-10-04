from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server import MCPServer

from taxax.legal.budget import RemoteRequestBudget, activate_remote_budget
from taxax.legal.models import CitationInput, LegalToolResponse, ResponseStatus
from taxax.legal.service import LegalKnowledgeService

_LOCAL_READ = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_UPSTREAM_READ = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

# 클라이언트(Codex·Claude Desktop)는 도구 호출을 약 60초에 끊는다. 그보다 먼저 끝내야
# 원인 안내와 저장본 대체가 사용자에게 닿는다. 소켓 대기도 남은 시간으로 줄어든다.
TOOL_SECONDS = 45.0
TOOL_REQUESTS = 30
SEARCH_LIMIT = 10


def _bounded(call):
    with activate_remote_budget(RemoteRequestBudget(max_requests=TOOL_REQUESTS, max_seconds=TOOL_SECONDS)):
        return call()


def _tool_response(response: LegalToolResponse) -> LegalToolResponse:
    if response.status not in {ResponseStatus.ERROR, ResponseStatus.BLOCKED}:
        return response
    payload = response.model_dump(mode="json")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=response.model_dump_json())],
        structuredContent=payload,
        isError=True,
    )  # type: ignore[return-value]


def register_legal_tools(
    server: MCPServer,
    service: LegalKnowledgeService,
    *,
    auth_required: bool = False,
) -> None:
    @server.tool(structured_output=True, annotations=_LOCAL_READ)
    def search_knowledge(query: str, k_ar_id: str | None = None, limit: int = 10) -> LegalToolResponse:
        """기존 K-AR 지식을 읽기 전용으로 검색하며 승인 상태는 변경하지 않는다."""
        return _tool_response(service.search_knowledge(query=query, k_ar_id=k_ar_id, limit=limit))

    @server.tool(structured_output=True, annotations=_UPSTREAM_READ)
    def search_legal_sources(
        query: str,
        target: str | None = None,
        provider: str | None = None,
        document_type: str | None = None,
        jurisdiction: str | None = None,
        filters: dict[str, str] | None = None,
        page: int = 1,
        limit: int = 10,
        cursor: str | None = None,
        upstream: bool = False,
        response_type: str = "JSON",
    ) -> LegalToolResponse:
        """세법·조문·판례 질문에 우선 사용한다. provider는 law.go.kr·nts·olta·all(국세청·법제처 같은 한글명도 됨), 법제처 target은 law·prec·ttSpecialDecc 등. 상증법·조특법 같은 약칭도 된다. 검색 요약은 원문이 아니다."""
        # 검색 1건은 출처 정보까지 약 1,700자다(들여쓴 JSON 기준). 10건을 넘으면 클라이언트 응답 한도에 가까워져 줄여서 돌려준다.
        clamped = limit > SEARCH_LIMIT
        limit = min(limit, SEARCH_LIMIT)
        response = _bounded(lambda: service.search_legal_sources(
                query=query,
                target=target,
                provider=provider,
                document_type=document_type,
                jurisdiction=jurisdiction,
                filters=filters,
                page=page,
                limit=limit,
                cursor=cursor,
                upstream=upstream,
                response_type=response_type,
            ))
        if clamped:
            response.warnings.append(f"한 번에 {SEARCH_LIMIT}건까지 돌려줍니다. 더 보려면 page나 pagination.next_cursor로 다음 쪽을 요청하십시오.")
        return _tool_response(response)

    @server.tool(structured_output=True, annotations=_UPSTREAM_READ)
    def get_legal_document(
        document_id: str | None = None,
        version_id: str | None = None,
        target: str | None = None,
        provider: str | None = None,
        source_document_id: str | None = None,
        source_category: str | None = None,
        attachment: bool = False,
        identifier_kind: str = "ID",
        effective_on: str | None = None,
        article: str | None = None,
        section_cursor: str = "0",
        max_chars: int = 16 * 1024,
        refresh: bool = False,
        response_type: str = "JSON",
    ) -> LegalToolResponse:
        """공식 상세 원문을 절 단위 cursor로 조회한다. article은 '제60조'·'제2조의3'처럼 써도 된다. 법제처에 본문이 없는 국세청 출처 판례는 국세청 판결문으로 받아 온다. 미확보 절을 확인 완료로 표시하지 않는다."""
        return _tool_response(_bounded(lambda: service.get_legal_document(
                document_id=document_id,
                version_id=version_id,
                target=target,
                provider=provider,
                source_document_id=source_document_id,
                source_category=source_category,
                attachment=attachment,
                identifier_kind=identifier_kind,
                effective_on=effective_on,
                article=article,
                section_cursor=section_cursor,
                max_chars=max_chars,
                refresh=refresh,
                response_type=response_type,
            ))
        )

    @server.tool(structured_output=True, annotations=_UPSTREAM_READ)
    def get_applicable_law(
        effective_on: str,
        mst: str | None = None,
        law_id: str | None = None,
        document_id: str | None = None,
        version_id: str | None = None,
        refresh: bool = False,
    ) -> LegalToolResponse:
        """기준일(effective_on)에 시행 중이던 법령 버전을 찾는다. 법령은 law_id(법령ID), mst, 또는 document_id에 법령명(예: '부가가치세법')으로 지정한다. 거래 적용을 확정하지 않는다."""
        return _tool_response(_bounded(lambda: service.get_applicable_law(
                effective_on=effective_on,
                mst=mst,
                law_id=law_id,
                document_id=document_id,
                version_id=version_id,
                refresh=refresh,
            ))
        )

    @server.tool(structured_output=True, annotations=_LOCAL_READ)
    def verify_legal_citations(citations: list[CitationInput]) -> LegalToolResponse:
        """문서·메타데이터·위치·인용문 일치를 분리해 확인한다."""
        payload: list[dict[str, Any]] = [citation.model_dump(mode="json") for citation in citations]
        return _tool_response(service.verify_legal_citations(citations=payload))

    @server.tool(structured_output=True, annotations=_LOCAL_READ)
    def get_source_status() -> LegalToolResponse:
        """공식 출처와 로컬 저장소의 구현·활성화·검증 상태를 반환한다."""
        return _tool_response(service.get_source_status())
