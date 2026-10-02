from __future__ import annotations

import re
from collections.abc import Iterable

from .models import (
    CitationCheck,
    CitationInput,
    CitationStatus,
    ContentCompleteness,
    LegalDocument,
    RetrievalStatus,
)
from .repository import AmbiguousDocumentVersion


def normalize_quote(value: str) -> str:
    return re.sub(r"\s+", "", value).strip()


def heading_only_article(text: str) -> bool:
    """조문 내용 없이 '제80조' 또는 '제80조(제목)'만 남은 절이다."""
    return bool(re.fullmatch(r"제\d+조(?:의\d+)?(?:\([^\n]*\))?", text.strip()))


def incomplete_law_articles(document: LegalDocument) -> bool:
    """하나라도 본문이 빠진 조문이 있으면 법령 전체를 완전으로 판정하지 않는다."""
    return (
        document.provider == "law.go.kr"
        and document.document_type in {"law", "effective_law"}
        and any(heading_only_article(section.text) for section in document.sections if section.kind == "articles")
    )


def verify_citations(citations: Iterable[CitationInput], lookup) -> list[CitationCheck]:
    checks: list[CitationCheck] = []
    for citation in citations:
        try:
            document: LegalDocument | None = lookup(citation.document_id, version_id=citation.version_id, locator=citation.locator)
        except AmbiguousDocumentVersion as exc:
            checks.append(CitationCheck(
                document_id=citation.document_id, status=CitationStatus.UNVERIFIED,
                document_exists=True, warnings=[str(exc)],
            ))
            continue
        if document is None:
            checks.append(CitationCheck(document_id=citation.document_id, version_id=citation.version_id, status=CitationStatus.MISMATCH, document_exists=False, warnings=["요청한 문서 또는 시행본을 찾지 못했습니다."]))
            continue
        metadata_matches = True
        warnings: list[str] = []
        if citation.expected_provider and document.provider != citation.expected_provider:
            metadata_matches = False
            warnings.append("provider가 인용과 일치하지 않습니다.")
        if citation.expected_date:
            dates = {document.promulgated_on, document.effective_from, document.decided_on, document.interpreted_on}
            if citation.expected_date not in dates:
                metadata_matches = False
                warnings.append("문서 일자가 인용과 일치하지 않습니다.")
        locator_matches = None
        sections = document.sections
        if citation.locator:
            matching = [section for section in sections if citation.locator in {section.locator, section.section_id, section.heading}]
            locator_matches = bool(matching)
            sections = matching
            if not matching:
                warnings.append("인용 위치를 확인하지 못했습니다.")
        quote_matches = None
        matched_section_id = None
        if citation.quote:
            needle = normalize_quote(citation.quote)
            for section in sections:
                if needle and needle in normalize_quote(section.text):
                    quote_matches = True
                    matched_section_id = section.section_id
                    break
            if quote_matches is None:
                quote_matches = False
                warnings.append("인용문이 해당 원문 절과 일치하지 않습니다.")
        complete_source = (
            document.retrieval_status == RetrievalStatus.SUCCESS
            and document.content_completeness == ContentCompleteness.COMPLETE
            and not bool(document.metadata.get("preview_only"))
            and not incomplete_law_articles(document)
        )
        if not complete_source:
            warnings.append("완전한 원문을 확보하지 못해 인용 검증을 완료할 수 없습니다.")
        if not metadata_matches:
            status = CitationStatus.MISMATCH
        elif not complete_source:
            status = CitationStatus.UNVERIFIED
        elif quote_matches is not False and locator_matches is not False:
            status = CitationStatus.VERIFIED if citation.quote else CitationStatus.DOCUMENT_ONLY
        else:
            status = CitationStatus.MISMATCH
        if status == CitationStatus.DOCUMENT_ONLY:
            warnings.append("문서 존재만 확인했으며 인용문 일치와 법적 결론은 검증하지 않았습니다.")
        checks.append(CitationCheck(document_id=citation.document_id, version_id=document.version_id, status=status, document_exists=True, metadata_matches=metadata_matches, quote_matches=quote_matches, locator_matches=locator_matches, matched_section_id=matched_section_id, warnings=warnings))
    return checks
