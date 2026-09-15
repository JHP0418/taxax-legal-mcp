from __future__ import annotations

import re
from datetime import date
from typing import Iterable

from .models import CitationCheck, CitationStatus, EvidenceScore, LegalDocument, ResearchRoute
from .temporal import parse_iso_date

_NORMATIVE_TYPES = {"law", "effective_law", "law_history", "administrative_rule", "nts_statute"}
_COURT_TYPES = {"precedent", "court_precedent", "local_tax_court_precedent", "local_tax_constitutional_decision"}
_TRIBUNAL_TYPES = {"tax_tribunal_decision", "national_tax_tribunal_decision", "local_tax_tribunal_decision"}
_INTERPRETATION_TYPES = {
    "nts_interpretation",
    "nts_advance_ruling",
    "nts_old_interpretation",
    "nts_international_interpretation",
    "local_tax_moi_interpretation",
    "local_tax_moleg_interpretation",
    "local_tax_government_interpretation",
}


def _tokens(values: Iterable[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        compact = re.sub(r"[^0-9A-Za-z가-힣 ]", " ", value).casefold()
        tokens.update(token for token in compact.split() if len(token) >= 2)
    return tokens


def _document_date(document: LegalDocument) -> date | None:
    for value in (
        document.effective_from,
        document.decided_on,
        document.interpreted_on,
        document.promulgated_on,
        document.registered_at,
    ):
        parsed = parse_iso_date(value)
        if parsed:
            return parsed
    return None


def _relevance(document: LegalDocument, route: ResearchRoute) -> tuple[int, str]:
    query_tokens = _tokens([*route.search_terms, *route.issue_terms, route.tax_type or ""])
    title_tokens = _tokens([document.title, document.tax_type or ""])
    body_tokens = _tokens(section.text for section in document.sections)
    title_hits = len(query_tokens & title_tokens)
    body_hits = len(query_tokens & body_tokens)
    value = min(5, title_hits * 2 + min(body_hits, 3))
    return value, f"제목 일치 {title_hits}개, 본문 일치 {body_hits}개"


def _material_type(document: LegalDocument) -> tuple[int, str]:
    if document.document_type in _NORMATIVE_TYPES:
        return 5, "법령·행정규칙 유형"
    if document.document_type in _COURT_TYPES:
        return 4, "법원·헌법재판 유형"
    if document.document_type in _TRIBUNAL_TYPES:
        return 3, "조세 불복 결정 유형"
    if document.document_type in _INTERPRETATION_TYPES:
        return 2, "과세관청·행정기관 해석 유형"
    return 1, "기타 공개 법률 자료 유형"


def _normative_basis(document: LegalDocument) -> tuple[int, str]:
    text = " ".join(section.text for section in document.sections)
    if document.document_type in {"law", "effective_law"}:
        return 5, "법령 원문"
    if document.document_type == "administrative_rule":
        if any(term in text for term in ("위임", "근거", "제정")):
            return 4, "행정규칙 본문에서 위임·근거 표현 확인"
        return 2, "행정규칙이나 위임 근거는 별도 확인 필요"
    if any(term in text for term in ("법 제", "시행령 제", "시행규칙 제")):
        return 3, "본문에 법령 조문 참조가 있음"
    return 1, "법규성·위임 근거를 본문에서 확인하지 못함"


def _case_relation(document: LegalDocument) -> tuple[int, str]:
    if document.document_type in _COURT_TYPES:
        if document.court and document.case_no:
            return 5, "법원과 사건번호가 모두 있음"
        if document.court or document.case_no:
            return 3, "법원 또는 사건번호만 있음"
        return 1, "판례 유형이나 법원·사건번호가 불완전함"
    if document.document_type in _TRIBUNAL_TYPES:
        return (3 if document.case_no else 2), "불복 결정의 사건 관계 정보"
    return 1, "법원·사건 관계를 적용하지 않는 자료 유형"


def _temporal_fit(
    document: LegalDocument,
    *,
    transaction_date: str | None,
    tax_period: str | None,
    knowledge_cutoff: str | None,
) -> tuple[int, str, list[str]]:
    flags: list[str] = []
    target = parse_iso_date(transaction_date)
    if target is None and tax_period:
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", tax_period)
        target = parse_iso_date(match.group(0)) if match else None
    source_date = _document_date(document)
    cutoff = parse_iso_date(knowledge_cutoff)
    if cutoff and source_date and source_date > cutoff:
        flags.append("post_knowledge_cutoff")
    if target is None:
        return 1, "거래일·과세기간이 없어 시점 적합성을 확정하지 못함", flags
    if document.document_type in {"law", "effective_law", "law_history"}:
        start = parse_iso_date(document.effective_from)
        end = parse_iso_date(document.effective_to)
        if start and start <= target and (end is None or target <= end):
            return 5, "표시된 시행 범위에 거래일이 포함됨", flags
        if start:
            flags.append("outside_stated_effective_range")
            return 1, "표시된 시행일과 거래일이 맞지 않음", flags
        flags.append("effective_range_unresolved")
        return 1, "시행 범위 정보가 없어 부칙 확인 필요", flags
    if source_date is None:
        flags.append("source_date_missing")
        return 1, "자료 일자를 확인하지 못함", flags
    if source_date > target:
        flags.append("post_transaction_material")
        return 3, "거래 후 자료이며 과거 규정 해석 자료로 별도 검토 필요", flags
    return 4, "자료 일자가 거래일 이전 또는 당일임", flags


def _citation_dimension(check: CitationCheck | None) -> tuple[int, str]:
    if check is None:
        return 0, "인용 검증을 수행하지 못함"
    if check.status == CitationStatus.VERIFIED:
        return 5, "문서·위치·인용문 일치 확인"
    if check.status == CitationStatus.DOCUMENT_ONLY:
        return 2, "문서 존재만 확인"
    if check.status == CitationStatus.MISMATCH:
        return 0, "인용 검증 불일치"
    return 1, "인용 상태 미확정"


def _factual_similarity(document: LegalDocument, route: ResearchRoute) -> tuple[int, str]:
    issue_tokens = _tokens(route.issue_terms)
    body_tokens = _tokens([document.title, *(section.text for section in document.sections)])
    hits = len(issue_tokens & body_tokens)
    if not document.sections:
        return min(2, hits), f"본문 미확보 상태에서 표현 {hits}개 일치"
    return min(5, hits + (1 if hits else 0)), f"쟁점 표현 {hits}개 일치"


def rank_document(
    document: LegalDocument,
    route: ResearchRoute,
    *,
    citation_check: CitationCheck | None = None,
    transaction_date: str | None = None,
    tax_period: str | None = None,
    knowledge_cutoff: str | None = None,
    selected_for_detail: bool = False,
) -> EvidenceScore:
    relevance, relevance_reason = _relevance(document, route)
    material_type, type_reason = _material_type(document)
    normative_basis, normative_reason = _normative_basis(document)
    case_relation, case_reason = _case_relation(document)
    temporal_fit, temporal_reason, flags = _temporal_fit(
        document,
        transaction_date=transaction_date,
        tax_period=tax_period,
        knowledge_cutoff=knowledge_cutoff,
    )
    citation, citation_reason = _citation_dimension(citation_check)
    factual_similarity, factual_reason = _factual_similarity(document, route)
    dimensions = {
        "relevance": relevance,
        "material_type": material_type,
        "normative_basis": normative_basis,
        "court_case_relation": case_relation,
        "temporal_fit": temporal_fit,
        "citation_verification": citation,
        "factual_similarity": factual_similarity,
    }
    score = round(sum(dimensions.values()) / (len(dimensions) * 5) * 100)
    if document.content_completeness.value in {"metadata_only", "partial", "unknown"}:
        flags.append("content_incomplete")
    if bool(document.metadata.get("preview_only")):
        flags.append("preview_only")
    return EvidenceScore(
        document_id=document.document_id,
        score=score,
        dimensions=dimensions,
        reasons=[
            relevance_reason,
            type_reason,
            normative_reason,
            case_reason,
            temporal_reason,
            citation_reason,
            factual_reason,
        ],
        flags=sorted(set(flags)),
        selected_for_detail=selected_for_detail,
    )


def rank_documents(
    documents: Iterable[LegalDocument],
    route: ResearchRoute,
    *,
    citation_checks: Iterable[CitationCheck] = (),
    transaction_date: str | None = None,
    tax_period: str | None = None,
    knowledge_cutoff: str | None = None,
    detailed_document_ids: set[str] | None = None,
) -> list[EvidenceScore]:
    checks = {check.document_id: check for check in citation_checks}
    detailed = detailed_document_ids or set()
    rankings = [
        rank_document(
            document,
            route,
            citation_check=checks.get(document.document_id),
            transaction_date=transaction_date,
            tax_period=tax_period,
            knowledge_cutoff=knowledge_cutoff,
            selected_for_detail=document.document_id in detailed,
        )
        for document in documents
    ]
    return sorted(rankings, key=lambda item: (-item.score, item.document_id))
