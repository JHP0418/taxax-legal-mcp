from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server import MCPServer

from taxax.legal.models import CitationInput, LegalToolResponse, ResponseStatus
from taxax.legal.service import LegalKnowledgeService

from .auth import current_request_scope


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
    @server.tool(structured_output=True)
    def search_knowledge(query: str, k_ar_id: str | None = None, limit: int = 10) -> LegalToolResponse:
        """기존 K-AR 지식을 읽기 전용으로 검색하며 승인 상태는 변경하지 않는다."""
        return _tool_response(service.search_knowledge(query=query, k_ar_id=k_ar_id, limit=limit))

    @server.tool(structured_output=True)
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
        """로컬 법률 색인을 검색하거나 명시적으로 공식 법제처 API를 조회한다."""
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

    @server.tool(structured_output=True)
    def get_legal_document(
        document_id: str | None = None,
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
        """저장된 문서 또는 공식 상세 원문을 절 단위 cursor로 조회한다."""
        return _tool_response(
            service.get_legal_document(
                document_id=document_id,
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

    @server.tool(structured_output=True)
    def get_applicable_law(
        effective_on: str,
        mst: str | None = None,
        law_id: str | None = None,
        document_id: str | None = None,
        refresh: bool = False,
    ) -> LegalToolResponse:
        """시행일 기준 법령 후보와 부칙 확인 상태를 반환하며 거래 적용을 확정하지 않는다."""
        return _tool_response(
            service.get_applicable_law(
                effective_on=effective_on,
                mst=mst,
                law_id=law_id,
                document_id=document_id,
                refresh=refresh,
            )
        )

    @server.tool(structured_output=True)
    def verify_legal_citations(citations: list[CitationInput]) -> LegalToolResponse:
        """문서·메타데이터·위치·인용문 일치를 분리해 확인한다."""
        payload: list[dict[str, Any]] = [citation.model_dump(mode="json") for citation in citations]
        return _tool_response(service.verify_legal_citations(citations=payload))

    @server.tool(structured_output=True)
    def research_tax_issue(
        issue: str,
        tax_type: str | None = None,
        transaction_date: str | None = None,
        tax_period: str | None = None,
        jurisdiction: str = "KR",
        research_as_of: str | None = None,
        knowledge_cutoff: str | None = None,
        budget: dict[str, int | float] | None = None,
        upstream: bool = True,
    ) -> LegalToolResponse:
        """결정론적 조사 계획으로 공식 근거를 수집·검증하며 세무 결론은 자동 확정하지 않는다. issue는 사실관계 서술 1~4000자."""
        scope = current_request_scope(auth_required=auth_required)
        return _tool_response(
            service.research_tax_issue(
                issue=issue,
                tax_type=tax_type,
                transaction_date=transaction_date,
                tax_period=tax_period,
                jurisdiction=jurisdiction,
                research_as_of=research_as_of,
                knowledge_cutoff=knowledge_cutoff,
                budget=budget,
                upstream=upstream,
                principal_id=scope.principal_id,
                org_id=scope.org_id,
            )
        )

    @server.tool(structured_output=True)
    def get_research_report(
        report_id: str,
        cursor: str | None = None,
        limit: int = 10,
    ) -> LegalToolResponse:
        """현재 인증 주체·조직 범위의 조사 보고서와 근거를 cursor 페이지로 조회한다."""
        scope = current_request_scope(auth_required=auth_required)
        return _tool_response(
            service.get_research_report(
                report_id=report_id,
                cursor=cursor,
                limit=limit,
                principal_id=scope.principal_id,
                org_id=scope.org_id,
            )
        )

    @server.tool(structured_output=True)
    def get_source_status() -> LegalToolResponse:
        """공식 출처와 로컬 저장소의 구현·활성화·검증 상태를 반환한다."""
        return _tool_response(service.get_source_status())
