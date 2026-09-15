from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .budget import RemoteBudgetExceeded, RemoteRequestBudget, activate_remote_budget
from .citations import verify_citations
from .evidence import rank_documents
from .models import (
    CitationInput,
    ErrorCode,
    LegalDocument,
    ResearchBudget,
    ResearchReport,
    ResponseStatus,
)
from .providers.base import LegalTarget, ProviderError
from .providers.law_go import CONTRACTS
from .router import route_tax_issue
from .temporal import parse_iso_date
from .transport import HttpTransport, SessionHttpTransport

_DEFAULT_MAX_REMOTE_REQUESTS = 20
_DEFAULT_MAX_DETAIL_DOCUMENTS = 8
_DEFAULT_MAX_SECONDS = 60.0
_ALLOWED_BUDGET_KEYS = {"max_remote_requests", "max_detail_documents", "max_seconds"}


@dataclass
class ResearchExecution:
    report: ResearchReport
    documents: list[LegalDocument]
    warnings: list[str] = field(default_factory=list)
    queried_providers: list[str] = field(default_factory=list)
    succeeded_providers: list[str] = field(default_factory=list)
    failed_providers: list[str] = field(default_factory=list)
    disabled_providers: list[str] = field(default_factory=list)


def _iso_date(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if parse_iso_date(normalized) is None:
        raise ProviderError(ErrorCode.INVALID_REQUEST, f"{field_name}은 YYYY-MM-DD 형식이어야 합니다.")
    return normalized


def _tax_period(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", value.strip())
    if not normalized or len(normalized) > 100 or "\x00" in normalized:
        raise ProviderError(ErrorCode.INVALID_REQUEST, "tax_period는 1~100자여야 합니다.")
    return normalized


def _limits(value: Mapping[str, int | float] | int | None) -> tuple[int, int, float]:
    requested: dict[str, int | float]
    if value is None:
        requested = {}
    elif isinstance(value, int) and not isinstance(value, bool):
        requested = {"max_remote_requests": value}
    elif isinstance(value, Mapping):
        unknown = sorted(set(value) - _ALLOWED_BUDGET_KEYS)
        if unknown:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "지원하지 않는 research budget 항목입니다.", details={"keys": unknown})
        requested = dict(value)
    else:
        raise ProviderError(ErrorCode.INVALID_REQUEST, "budget은 정수 또는 허용된 숫자 필드 object여야 합니다.")
    parsed: dict[str, float] = {}
    for key, maximum in (
        ("max_remote_requests", float(_DEFAULT_MAX_REMOTE_REQUESTS)),
        ("max_detail_documents", float(_DEFAULT_MAX_DETAIL_DOCUMENTS)),
        ("max_seconds", _DEFAULT_MAX_SECONDS),
    ):
        raw = requested.get(key, maximum)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise ProviderError(ErrorCode.INVALID_REQUEST, f"budget.{key}는 0 이상의 숫자여야 합니다.")
        parsed[key] = min(float(raw), maximum)
    return int(parsed["max_remote_requests"]), int(parsed["max_detail_documents"]), parsed["max_seconds"]


# NTS/OLTA와 law.go.kr 검색은 각자 쿼리 문자열을 500자로 제한한다(각 provider
# 모듈의 독립적인 길이 검사). issue는 그보다 훨씬 긴 사실관계 서술일 수 있으므로,
# 업스트림 검색에 그대로 흘려보내면 provider별 길이 검사에서 개별적으로 실패한다.
# 조사 자체를 막지는 않지만(각 provider_error로 개별 기록될 뿐) 의미 없는 실패를
# 만드니, 검색어로 쓰는 자리에서는 항상 이 길이로 줄인 버전을 쓴다.
_UPSTREAM_QUERY_MAX_CHARS = 500


def _upstream_query(issue: str) -> str:
    return issue[:_UPSTREAM_QUERY_MAX_CHARS]


def _statute_query(tax_type: str | None, issue: str) -> str:
    mappings = {
        "법인세": "법인세법",
        "부가가치세": "부가가치세법",
        "소득세": "소득세법",
        "원천세": "소득세법 원천징수",
        "상속세 및 증여세": "상속세 및 증여세법",
        "취득세": "지방세법 취득세",
        "재산세": "지방세법 재산세",
        "지방소득세": "지방세법 지방소득세",
        "등록면허세": "지방세법 등록면허세",
        "주민세": "지방세법 주민세",
        "자동차세": "지방세법 자동차세",
    }
    return mappings.get(tax_type or "", tax_type or _upstream_query(issue))


def _documents_from_response(service, response) -> list[LegalDocument]:
    items = response.data.get("items", []) if isinstance(response.data, dict) else []
    documents: list[LegalDocument] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        document_id = item.get("document_id")
        if isinstance(document_id, str) and (document := service.repository.get_document(document_id)) is not None:
            documents.append(document)
    return documents


def _provider_error(provider: str, response) -> dict[str, Any]:
    error = response.error
    return {
        "provider": provider,
        "code": error.code.value if error else ErrorCode.UPSTREAM_UNAVAILABLE.value,
        "message": error.message if error else "출처 조회가 완료되지 않았습니다.",
        "retryable": error.retryable if error else False,
    }


def _record_provider_status(execution: ResearchExecution, provider: str, response) -> None:
    if provider not in execution.queried_providers:
        execution.queried_providers.append(provider)
    for name in response.coverage.succeeded_providers:
        if name not in execution.succeeded_providers:
            execution.succeeded_providers.append(name)
    for name in response.coverage.failed_providers:
        if name not in execution.failed_providers:
            execution.failed_providers.append(name)
    for name in response.coverage.disabled_providers:
        if name not in execution.disabled_providers:
            execution.disabled_providers.append(name)


def _uses_instrumented_transport(service, provider: str) -> bool:
    if provider == service.provider.name:
        adapter = service.provider
    elif provider == service.nts_provider.name:
        adapter = service.nts_provider
    else:
        adapter = service.olta_provider
    return isinstance(getattr(adapter, "transport", None), (HttpTransport, SessionHttpTransport))


def _claim_for_uninstrumented_transport(service, provider: str, budget: RemoteRequestBudget) -> None:
    if not _uses_instrumented_transport(service, provider):
        budget.consume()


def _citation_inputs(documents: list[LegalDocument]) -> list[CitationInput]:
    inputs: list[CitationInput] = []
    for document in documents:
        section = next((section for section in document.sections if section.text.strip()), None)
        if section is None:
            inputs.append(CitationInput(document_id=document.document_id, expected_provider=document.provider))
            continue
        quote = section.text.strip()[:240]
        inputs.append(
            CitationInput(
                document_id=document.document_id,
                quote=quote,
                locator=section.locator or section.section_id,
                expected_provider=document.provider,
            )
        )
    return inputs


def _detail_arguments(document: LegalDocument) -> dict[str, Any] | None:
    if document.provider == "law.go.kr":
        target_value = document.metadata.get("target")
        try:
            target = LegalTarget(str(target_value))
        except ValueError:
            return None
        contract = CONTRACTS[target]
        if not contract.detail_supported:
            return None
        if target == LegalTarget.PRECEDENT and str(document.metadata.get("데이터출처명") or "") == "국세법령정보시스템":
            # 국세청이 출처인 판례는 공식 가이드(precInfoGuide)상 본문을 HTML로만
            # 제공해, 구조화 상세조회를 요청하면 "일치하는 판례가 없습니다"가 온다.
            # 매번 실패를 만들지 말고 검색 단계 메타데이터까지만 근거로 쓴다.
            return None
        identifiers = document.metadata.get("upstream_identifiers")
        if not isinstance(identifiers, Mapping):
            return None
        for kind in ("ID", "MST", "LID", "LM"):
            values = identifiers.get(kind)
            if kind in contract.detail_identifiers and isinstance(values, list) and values and isinstance(values[0], str):
                return {
                    "provider": document.provider,
                    "target": target.value,
                    "source_document_id": values[0],
                    "identifier_kind": kind,
                    "refresh": True,
                }
        return None
    if document.provider == "taxlaw.nts.go.kr":
        return {"provider": document.provider, "source_document_id": document.source_document_id, "refresh": True}
    if document.provider == "olta.re.kr":
        category = document.metadata.get("category")
        if not document.metadata.get("detail_supported") or not isinstance(category, str):
            return None
        return {
            "provider": document.provider,
            "source_document_id": document.source_document_id,
            "source_category": category,
            "refresh": True,
        }
    return None


def _conflicts(service, documents: list[LegalDocument]) -> list[dict[str, Any]]:
    return [group for group in service._candidate_groups(documents) if group.get("conflicting_originals")]


def _temporal_review(documents: list[LegalDocument], transaction_date: str | None) -> dict[str, Any]:
    candidates = []
    for document in documents:
        if document.document_type not in {"law", "effective_law", "law_history"}:
            continue
        start = parse_iso_date(document.effective_from)
        end = parse_iso_date(document.effective_to)
        requested = parse_iso_date(transaction_date)
        in_range = bool(requested and start and start <= requested and (end is None or requested <= end))
        candidates.append(
            {
                "document_id": document.document_id,
                "effective_from": document.effective_from,
                "effective_to": document.effective_to,
                "in_stated_range": in_range,
                "addenda_present": any(section.kind == "addenda" and section.text.strip() for section in document.sections),
            }
        )
    return {
        "transaction_date": transaction_date,
        "candidates": candidates,
        "transaction_applicability_confirmed": False,
        "requires_addenda_and_transition_review": bool(transaction_date) and any(not item["addenda_present"] for item in candidates),
    }


def run_tax_research(
    service,
    *,
    issue: str,
    tax_type: str | None = None,
    transaction_date: str | None = None,
    tax_period: str | None = None,
    jurisdiction: str = "KR",
    research_as_of: str | None = None,
    knowledge_cutoff: str | None = None,
    budget: Mapping[str, int | float] | int | None = None,
    upstream: bool = True,
    now: str,
) -> ResearchExecution:
    transaction_date = _iso_date(transaction_date, "transaction_date")
    defaulted_research_as_of = research_as_of is None
    research_as_of = _iso_date(research_as_of or now[:10], "research_as_of")
    # research_as_of는 "이 시점 기준으로 조사했다"는 출처 표기다. 미래 날짜를
    # 그대로 받아들이면 아직 존재하지 않는 시점의 법 상태를 조사한 것처럼
    # 보고서에 남는다(2099년 기준 조사라고 적히는 식). 다만 UTC와 한국 시간이
    # 하루 차이 날 수 있어 하루치는 허용한다.
    if not defaulted_research_as_of and research_as_of:
        today = parse_iso_date(now[:10])
        requested = parse_iso_date(research_as_of)
        if today and requested and (requested - today).days > 1:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "research_as_of는 미래 날짜일 수 없습니다.")
    knowledge_cutoff = _iso_date(knowledge_cutoff, "knowledge_cutoff")
    if knowledge_cutoff and research_as_of and knowledge_cutoff > research_as_of:
        raise ProviderError(ErrorCode.INVALID_REQUEST, "knowledge_cutoff은 research_as_of 이후일 수 없습니다.")
    tax_period = _tax_period(tax_period)
    if tax_type is not None and (not tax_type.strip() or len(tax_type.strip()) > 100 or "\x00" in tax_type):
        raise ProviderError(ErrorCode.INVALID_REQUEST, "tax_type은 1~100자여야 합니다.")
    normalized_jurisdiction = jurisdiction.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", normalized_jurisdiction):
        raise ProviderError(ErrorCode.INVALID_REQUEST, "jurisdiction은 ISO 3166-1 alpha-2 형식이어야 합니다.")
    if normalized_jurisdiction != "KR":
        raise ProviderError(ErrorCode.INVALID_REQUEST, "현재 provider 계약은 KR 관할만 지원합니다.")
    max_requests, max_details, max_seconds = _limits(budget)
    remote_budget = RemoteRequestBudget(max_requests=max_requests, max_seconds=max_seconds)
    route, missing_inputs, assumptions = route_tax_issue(
        issue,
        tax_type=tax_type,
        jurisdiction=normalized_jurisdiction,
        has_temporal_input=bool(transaction_date or tax_period),
    )
    if defaulted_research_as_of:
        assumptions.append("research_as_of를 조사 실행일로 설정했습니다.")
    if not upstream:
        assumptions.append("offline 모드로 로컬 공개 법률 색인만 조사했습니다.")
    report = ResearchReport(
        report_id="research-" + uuid.uuid4().hex,
        status="needs_review",
        issue=issue,
        created_at=now,
        updated_at=now,
        transaction_date=transaction_date,
        tax_period=tax_period,
        research_as_of=research_as_of,
        knowledge_cutoff=knowledge_cutoff,
        route=route,
        research_plan=[
            "세목·쟁점·동의어를 결정론적으로 분류",
            "로컬 공개 법률 색인 예비 검색",
            "허용된 공식·보완 provider 목록 검색",
            "상위 후보의 지원되는 상세 원문 제한 조회",
            "시점·부칙·후속 자료 상태 분리",
            "근거 차원별 점수와 인용 일치 검증",
            "인증 주체·조직 범위의 보고서 저장",
        ],
        assumptions=assumptions,
        missing_inputs=missing_inputs,
        limitations=[
            "이 보고서는 법적·세무 결론을 자동 확정하지 않습니다.",
            "자료유형 점수 하나로 법적 효력이나 결론을 결정하지 않습니다.",
            "검색 결과 부재는 판례의 유효성·폐기 여부를 증명하지 않습니다.",
        ],
    )
    execution = ResearchExecution(report=report, documents=[])
    by_id: dict[str, LegalDocument] = {}

    for term in route.search_terms[:4]:
        local = service.search_legal_sources(query=_upstream_query(term), provider="all", jurisdiction=normalized_jurisdiction, limit=20)
        for document in _documents_from_response(service, local):
            by_id[document.document_id] = document

    operations: list[tuple[str, str | None, str]] = []
    for target in route.official_targets:
        query = _statute_query(route.tax_type, issue) if target == LegalTarget.LAW.value else _upstream_query(issue)
        operations.append((service.provider.name, target, query))
    if service.nts_provider.name in route.providers:
        operations.append((service.nts_provider.name, None, _upstream_query(issue)))
    if service.olta_provider.name in route.providers:
        operations.append((service.olta_provider.name, None, _upstream_query(issue)))

    detailed_ids: set[str] = {
        document.document_id
        for document in by_id.values()
        if document.sections and not bool(document.metadata.get("preview_only"))
    }
    consumed_details = 0
    blocked_providers: set[str] = set()
    with activate_remote_budget(remote_budget):
        if not upstream:
            report.unresearched_scope.extend(
                f"{provider}:{target or 'integrated-search'}"
                for provider, target, _ in operations
            )
        else:
            for index, (provider, target, query) in enumerate(operations):
                if provider in blocked_providers:
                    report.unresearched_scope.append(f"{provider}:{target or 'integrated-search'}")
                    continue
                try:
                    remote_budget.check_time()
                    _claim_for_uninstrumented_transport(service, provider, remote_budget)
                except RemoteBudgetExceeded:
                    report.unresearched_scope.extend(
                        f"{remaining_provider}:{remaining_target or 'integrated-search'}"
                        for remaining_provider, remaining_target, _ in operations[index:]
                    )
                    break
                filters = {"max_pages": "1"} if provider == service.nts_provider.name else None
                response = service.search_legal_sources(
                    query=query,
                    target=target,
                    provider=provider,
                    jurisdiction=normalized_jurisdiction,
                    filters=filters,
                    limit=5,
                    upstream=True,
                )
                _record_provider_status(execution, provider, response)
                for document in _documents_from_response(service, response):
                    by_id[document.document_id] = document
                if response.status in {ResponseStatus.ERROR, ResponseStatus.BLOCKED}:
                    report.provider_errors.append(_provider_error(provider, response))
                    report.unresearched_scope.append(f"{provider}:{target or 'integrated-search'}")
                    if response.error and response.error.code in {
                        ErrorCode.AUTH_REQUIRED,
                        ErrorCode.AUTH_FAILED,
                        ErrorCode.POLICY_DISABLED,
                    }:
                        blocked_providers.add(provider)
                    if response.error and response.error.code == ErrorCode.BUDGET_EXHAUSTED:
                        remote_budget.exhausted_reason = remote_budget.exhausted_reason or "remote_request_limit"
                        report.unresearched_scope.extend(
                            f"{remaining_provider}:{remaining_target or 'integrated-search'}"
                            for remaining_provider, remaining_target, _ in operations[index + 1 :]
                        )
                        break

        preliminary = rank_documents(
            by_id.values(),
            route,
            transaction_date=transaction_date,
            tax_period=tax_period,
            knowledge_cutoff=knowledge_cutoff,
        )
        detail_candidates = [by_id[item.document_id] for item in preliminary if _detail_arguments(by_id[item.document_id]) is not None]
        if upstream:
            for document in detail_candidates:
                if consumed_details >= max_details:
                    report.unresearched_scope.append("상세조회 후보 중 detail budget 이후 문서")
                    break
                if document.provider in blocked_providers:
                    report.unresearched_scope.append(f"상세조회 비활성 provider:{document.provider}")
                    continue
                try:
                    remote_budget.check_time()
                    _claim_for_uninstrumented_transport(service, document.provider, remote_budget)
                except RemoteBudgetExceeded:
                    report.unresearched_scope.append("상세조회 후보 중 remote/time budget 이후 문서")
                    break
                arguments = _detail_arguments(document)
                if arguments is None:
                    continue
                detail = service.get_legal_document(**arguments)
                consumed_details += 1
                _record_provider_status(execution, document.provider, detail)
                if detail.status in {ResponseStatus.ERROR, ResponseStatus.BLOCKED}:
                    report.provider_errors.append(_provider_error(document.provider, detail))
                    if detail.error and detail.error.code == ErrorCode.BUDGET_EXHAUSTED:
                        remote_budget.exhausted_reason = remote_budget.exhausted_reason or "remote_request_limit"
                        break
                    continue
                detail_id = detail.data.get("document_id") if isinstance(detail.data, dict) else None
                if isinstance(detail_id, str) and (full := service.repository.get_document(detail_id)) is not None:
                    by_id[full.document_id] = full
                    detailed_ids.add(full.document_id)

    execution.documents = sorted(by_id.values(), key=lambda document: document.document_id)
    checks = verify_citations(_citation_inputs(execution.documents), service.repository.get_document)
    rankings = rank_documents(
        execution.documents,
        route,
        citation_checks=checks,
        transaction_date=transaction_date,
        tax_period=tax_period,
        knowledge_cutoff=knowledge_cutoff,
        detailed_document_ids=detailed_ids,
    )
    elapsed = remote_budget.elapsed_seconds
    exhausted = remote_budget.exhausted
    report.candidate_documents = [item.document_id for item in rankings]
    report.evidence_ranking = rankings
    report.citation_checks = checks
    report.temporal_review = _temporal_review(execution.documents, transaction_date)
    report.conflicts = _conflicts(service, execution.documents)
    report.budget = ResearchBudget(
        max_remote_requests=max_requests,
        consumed_remote_requests=remote_budget.consumed_requests,
        max_detail_documents=max_details,
        consumed_detail_documents=consumed_details,
        max_seconds=max_seconds,
        elapsed_seconds=elapsed,
        exhausted=exhausted,
    )
    if knowledge_cutoff and any("post_knowledge_cutoff" in item.flags for item in rankings):
        report.limitations.append("knowledge_cutoff 이후 자료를 자동 배제하지 않고 후속 해석 자료로 표시했습니다.")
    if report.conflicts:
        report.limitations.append("동일·유사 제목의 상충 원문 후보가 있어 provenance별 검토가 필요합니다.")
    if missing_inputs:
        report.limitations.append("세목 또는 시점 입력이 부족해 예비 조사 범위로 종료했습니다.")
    if exhausted:
        report.limitations.append("요청 또는 시간 예산에 도달해 미조사 범위를 남겼습니다.")
    if not execution.documents:
        report.limitations.append("확보된 후보 문서가 없습니다.")
    if report.provider_errors or report.unresearched_scope or missing_inputs or not execution.documents:
        report.status = "partial"
    else:
        report.status = "needs_review"
    execution.warnings = [
        "구조화 보고서는 사람이 원문과 사실관계를 검토하기 위한 자료이며 자동 세무 확정이 아닙니다.",
        *(["일부 provider 또는 조사 단계가 완료되지 않았습니다."] if report.status == "partial" else []),
    ]
    return execution
