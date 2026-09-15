from __future__ import annotations

from datetime import date
from typing import Iterable

from .models import LegalDocument, TemporalStatus


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def temporal_candidates(documents: Iterable[LegalDocument], effective_on: str) -> tuple[list[LegalDocument], list[str]]:
    requested = parse_iso_date(effective_on)
    if requested is None:
        raise ValueError("effective_on은 YYYY-MM-DD 형식이어야 합니다.")
    candidates: list[LegalDocument] = []
    warnings: list[str] = []
    for document in documents:
        start = parse_iso_date(document.effective_from)
        end = parse_iso_date(document.effective_to)
        if start is None:
            continue
        if start <= requested and (end is None or requested <= end):
            candidates.append(document.model_copy(update={"temporal_status": TemporalStatus.CANDIDATE}))
    if not candidates:
        warnings.append("확인된 시행 범위에서 후보를 찾지 못했습니다.")
    if any(not any(section.kind == "addenda" for section in document.sections) for document in candidates):
        warnings.append("관련 부칙·적용례·경과조치 확인이 완료되지 않았습니다.")
    warnings.append("시행일 후보 확인은 해당 거래에 대한 법률 적용 확정을 의미하지 않습니다.")
    return candidates, warnings
