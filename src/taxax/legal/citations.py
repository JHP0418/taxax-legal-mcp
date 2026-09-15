from __future__ import annotations

import re
from collections.abc import Iterable

from .models import CitationCheck, CitationInput, CitationStatus, LegalDocument


def normalize_quote(value: str) -> str:
    return re.sub(r"\s+", "", value).strip()


def verify_citations(citations: Iterable[CitationInput], lookup) -> list[CitationCheck]:
    checks: list[CitationCheck] = []
    for citation in citations:
        document: LegalDocument | None = lookup(citation.document_id)
        if document is None:
            checks.append(CitationCheck(document_id=citation.document_id, status=CitationStatus.MISMATCH, document_exists=False, warnings=["문서를 찾지 못했습니다."]))
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
        if metadata_matches and quote_matches is not False and locator_matches is not False:
            status = CitationStatus.VERIFIED if citation.quote else CitationStatus.DOCUMENT_ONLY
        else:
            status = CitationStatus.MISMATCH
        if status == CitationStatus.DOCUMENT_ONLY:
            warnings.append("문서 존재만 확인했으며 인용문 일치와 법적 결론은 검증하지 않았습니다.")
        checks.append(CitationCheck(document_id=citation.document_id, status=status, document_exists=True, metadata_matches=metadata_matches, quote_matches=quote_matches, locator_matches=locator_matches, matched_section_id=matched_section_id, warnings=warnings))
    return checks
