from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ResearchRoute


@dataclass(frozen=True)
class IssueVocabulary:
    name: str
    triggers: tuple[str, ...]
    expansions: tuple[str, ...]


_ISSUES = (
    IssueVocabulary("대손", ("대손", "회수불능", "부도"), ("대손", "대손금", "대손세액공제", "회수불능채권")),
    IssueVocabulary("상계", ("상계", "채권채무 상쇄", "채권 채무 상쇄"), ("상계", "채권채무 상계", "상계합의", "상계 적격")),
    IssueVocabulary("귀속시기", ("귀속시기", "손익 귀속", "수익 인식", "비용 인식"), ("귀속시기", "손익의 귀속사업연도", "권리의무확정주의")),
    IssueVocabulary("수정세금계산서", ("수정세금계산서", "수정 세금계산서", "세금계산서 수정"), ("수정세금계산서", "세금계산서 수정발급", "공급가액 변동")),
    IssueVocabulary("매입채무", ("매입채무", "외상매입금", "미지급금"), ("매입채무", "외상매입금", "채무면제이익")),
    IssueVocabulary("매출채권", ("매출채권", "외상매출금", "받을어음"), ("매출채권", "외상매출금", "대손충당금")),
    IssueVocabulary("비상장주식 평가", ("비상장주식", "비상장 주식", "보충적 평가"), ("비상장주식 평가", "보충적 평가방법", "순손익가치", "순자산가치")),
)

_TAX_TYPES = (
    ("부가가치세", "national", ("부가가치세", "부가세", "세금계산서", "매입세액", "매출세액")),
    ("법인세", "national", ("법인세", "손금", "익금", "사업연도")),
    ("소득세", "national", ("소득세", "필요경비", "종합소득", "양도소득")),
    ("원천세", "national", ("원천세", "원천징수")),
    ("상속세 및 증여세", "national", ("상속세", "증여세", "상증세")),
    ("취득세", "local", ("취득세", "간주취득")),
    ("재산세", "local", ("재산세",)),
    ("지방소득세", "local", ("지방소득세",)),
    ("등록면허세", "local", ("등록면허세",)),
    ("주민세", "local", ("주민세",)),
    ("자동차세", "local", ("자동차세",)),
)


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _scope_for_tax_type(tax_type: str | None, issue: str) -> tuple[str, str | None, list[str]]:
    combined = _compact(" ".join(part for part in (tax_type, issue) if part))
    matches = [(name, scope) for name, scope, triggers in _TAX_TYPES if any(_compact(trigger) in combined for trigger in triggers)]
    scopes = {scope for _, scope in matches}
    inferred = tax_type.strip() if tax_type and tax_type.strip() else (matches[0][0] if matches else None)
    rationale: list[str] = []
    if inferred and not tax_type:
        rationale.append(f"질의 표현에서 세목을 {inferred}(으)로 추론했습니다.")
    if scopes == {"national"}:
        return "national", inferred, rationale
    if scopes == {"local"}:
        return "local", inferred, rationale
    if len(scopes) > 1:
        rationale.append("국세와 지방세 표현이 함께 있어 양쪽 출처를 조사합니다.")
        return "mixed", inferred, rationale
    return "unknown", inferred, rationale


def route_tax_issue(
    issue: str,
    *,
    tax_type: str | None = None,
    jurisdiction: str = "KR",
    has_temporal_input: bool = False,
) -> tuple[ResearchRoute, list[str], list[str]]:
    normalized = _compact(issue)
    matched = next((entry for entry in _ISSUES if any(_compact(trigger) in normalized for trigger in entry.triggers)), None)
    scope, inferred_tax_type, rationale = _scope_for_tax_type(tax_type, issue)
    issue_terms = list(matched.expansions if matched else (issue.strip(),))
    search_terms: list[str] = []
    for term in [issue.strip(), inferred_tax_type or "", *issue_terms]:
        cleaned = re.sub(r"\s+", " ", term).strip()
        if cleaned and cleaned not in search_terms:
            search_terms.append(cleaned)
    providers = ["law.go.kr"]
    if scope in {"national", "mixed", "unknown"}:
        providers.append("taxlaw.nts.go.kr")
    if scope in {"local", "mixed", "unknown"}:
        providers.append("olta.re.kr")
    # 법령연혁 목록(lsHistory)은 법제처 API가 HTML로만 제공해 구조화 수집이
    # 불가능하다. 조사 대상에 넣으면 매 조사마다 실패 하나가 확정으로 붙으므로
    # 넣지 않는다. 시점 판단은 temporal_review와 get_applicable_law가 담당한다.
    official_targets = ["law", "admrul"]
    if scope in {"national", "mixed", "unknown"}:
        official_targets.append("ntsCgmExpc")
    official_targets.extend(["ttSpecialDecc", "prec"])
    missing_inputs: list[str] = []
    assumptions: list[str] = []
    if inferred_tax_type is None:
        missing_inputs.append("tax_type")
    if not has_temporal_input:
        missing_inputs.extend(["transaction_date 또는 tax_period"])
    if matched is None:
        assumptions.append("등록된 쟁점 템플릿과 일치하지 않아 입력 문구를 그대로 검색어로 사용했습니다.")
    if scope == "unknown":
        assumptions.append("세목 범위가 불명확해 국세·지방세 후보를 모두 검색 대상으로 두었습니다.")
    rationale.append("근거 유형 순서는 조사 순서이며 법적 효력의 일렬 순위를 의미하지 않습니다.")
    route = ResearchRoute(
        tax_type=inferred_tax_type,
        jurisdiction=jurisdiction,
        tax_scope=scope,
        matched_issue=matched.name if matched else None,
        issue_terms=issue_terms,
        search_terms=search_terms,
        providers=providers,
        official_targets=official_targets,
        rationale=rationale,
    )
    return route, missing_inputs, assumptions
