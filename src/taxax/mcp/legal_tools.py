from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server import MCPServer

from taxax.legal.models import CitationInput, LegalToolResponse, ResponseStatus
from taxax.legal.service import LegalKnowledgeService

_LOCAL_READ = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_UPSTREAM_READ = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

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
        """세법·조문·판례 질문에 우선 사용한다. provider는 law.go.kr/nts/olta/all, target은 law/precedent 등. 검색 요약은 원문이 아니다."""
        return _tool_response(
            service.search_legal_sources(
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
            )
        )

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
        """AI가 필요할 때 공식 상세 원문을 절 단위 cursor로 조회한다. 미확보 절을 확인 완료로 표시하지 않는다."""
        return _tool_response(
            service.get_legal_document(
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
            )
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
        """시행일 기준 법령 후보와 부칙 확인 상태를 반환하며 거래 적용을 확정하지 않는다."""
        return _tool_response(
            service.get_applicable_law(
                effective_on=effective_on,
                mst=mst,
                law_id=law_id,
                document_id=document_id,
                version_id=version_id,
                refresh=refresh,
            )
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
