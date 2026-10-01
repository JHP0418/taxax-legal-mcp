from __future__ import annotations

import re

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


def infer_tax_type(text: str) -> tuple[str | None, str]:
    """문서 제목에서 세목과 국세·지방세 범위를 추론하되 미확인은 유지한다."""
    compact = _compact(text)
    matches = [
        (name, scope)
        for name, scope, triggers in _TAX_TYPES
        if any(_compact(trigger) in compact for trigger in triggers)
    ]
    if not matches:
        return None, "unknown"
    scopes = {scope for _, scope in matches}
    if len(scopes) == 1:
        return matches[0][0], next(iter(scopes))
    return matches[0][0], "mixed"


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()
