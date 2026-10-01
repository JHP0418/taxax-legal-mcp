from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlencode, urljoin

from ..local_config import LocalSecretError, load_law_go_credential
from ..models import ErrorCode
from ..transport import (
    HttpResponse,
    HttpTransport,
    decode_body,
    redact_text,
    response_media_type,
    strip_secret_query_params,
)
from .base import LegalTarget, ProviderError, ProviderResult, TargetContract

SEARCH_ENDPOINT = "https://www.law.go.kr/DRF/lawSearch.do"
DETAIL_ENDPOINT = "https://www.law.go.kr/DRF/lawService.do"
GUIDE_BASE = "https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName="
PARSER_VERSION = "law-go-v3"

COMMON_LIST_FILTERS = frozenset({"search", "query", "display", "page", "sort", "gana", "popYn"})
# 법제처가 부여한 숫자 식별자만 받는 자리. admrul의 LM은 법령명을 받으므로 뺀다.
_NUMERIC_IDENTIFIER_KINDS = frozenset({"ID", "MST", "LID"})
CONTRACTS: dict[LegalTarget, TargetContract] = {
    LegalTarget.LAW: TargetContract(
        LegalTarget.LAW,
        "law",
        True,
        True,
        COMMON_LIST_FILTERS | {"date", "efYd", "ancYd", "ancNo", "rrClsCd", "nb", "org", "knd", "lsChapNo"},
        "efYd",
        frozenset({"ID", "MST"}),
    ),
    LegalTarget.LAW_HISTORY: TargetContract(
        LegalTarget.LAW_HISTORY,
        "law_history",
        True,
        False,
        COMMON_LIST_FILTERS | {"date", "efYd", "ancYd", "ancNo", "rrClsCd", "org", "knd", "lsChapNo"},
        "efYd",
    ),
    LegalTarget.EFFECTIVE_LAW: TargetContract(
        LegalTarget.EFFECTIVE_LAW,
        "effective_law",
        False,
        True,
        detail_identifiers=frozenset({"ID", "MST"}),
        notes=("MST 조회에는 efYd가 필수이며 ID 조회에서는 efYd가 무시됩니다.",),
    ),
    LegalTarget.PRECEDENT: TargetContract(
        LegalTarget.PRECEDENT,
        "precedent",
        True,
        True,
        COMMON_LIST_FILTERS | {"org", "curt", "JO", "date", "prncYd", "nb", "datSrcNm"},
        "prncYd",
    ),
    LegalTarget.TAX_TRIBUNAL: TargetContract(
        LegalTarget.TAX_TRIBUNAL,
        "tax_tribunal_decision",
        True,
        True,
        COMMON_LIST_FILTERS | {"cls", "date", "dpaYd", "rslYd", "fields"},
        "rslYd",
    ),
    LegalTarget.NTS_INTERPRETATION: TargetContract(
        LegalTarget.NTS_INTERPRETATION,
        "nts_interpretation",
        True,
        # 법제처는 이 종류를 색인만 제공하고 본문은 갖고 있지 않다. 검색 응답의
        # 각 항목은 법령해석상세링크로 taxlaw.nts.go.kr을 가리킨다(그 링크는
        # official_url에 그대로 보존된다). 그런데도 detail을 True로 두면
        # lawService.do에 본문을 요청하게 되고, 법제처는 제공하지 않는 요청에
        # "미신청된 목록/본문에 대한 접근입니다" 안내 화면을 돌려준다. 우리는
        # 그것을 AUTH_FAILED로 읽어, 신청이 모두 완료된 계정에서도 조사마다
        # 인증 실패가 쌓였다. 실제로 이 오류를 보고 "라우팅이 고장났다"거나
        # "API 신청이 빠졌다"는 잘못된 진단이 각각 나왔다.
        # 본문이 필요하면 NTS provider가 원본 사이트에서 가져온다.
        False,
        COMMON_LIST_FILTERS | {"explYd", "inq", "rpl", "itmno", "fields"},
        "explYd",
        notes=(
            "itmno가 있으면 upstream은 query를 무시합니다.",
            "본문은 법제처가 아니라 국세청 원본(official_url)에 있습니다.",
        ),
    ),
    LegalTarget.ADMIN_RULE: TargetContract(
        LegalTarget.ADMIN_RULE,
        "administrative_rule",
        True,
        True,
        COMMON_LIST_FILTERS | {"nw", "org", "knd", "date", "prmlYd", "modYd", "nb"},
        "prmlYd",
        frozenset({"ID", "LID", "LM"}),
    ),
    LegalTarget.LAW_ATTACHMENT: TargetContract(
        LegalTarget.LAW_ATTACHMENT,
        "law_attachment",
        True,
        False,
        COMMON_LIST_FILTERS | {"org", "mulOrg", "knd"},
    ),
}

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "source_document_id": (
        "법령일련번호", "판례일련번호", "판례정보일련번호", "특별행정심판재결례일련번호",
        "특별행정심판재결례 일련번호", "법령해석일련번호", "행정규칙일련번호", "별표일련번호",
        "법령ID", "행정규칙ID", "id", "ID", "법령일련번호",
    ),
    "version_id": ("법령일련번호", "MST", "lsiSeq", "행정규칙일련번호"),
    "title": ("법령명한글", "법령명_한글", "사건명", "안건명", "행정규칙명", "별표명", "재결례명", "법령해석명", "title"),
    "issuer": ("소관부처명", "해석기관명", "재결청", "발령기관명", "issuer"),
    "court": ("법원명", "court"),
    "case_no": ("사건번호", "안건번호", "청구번호", "caseNo"),
    "promulgated_on": ("공포일자", "발령일자", "promulgatedOn"),
    "effective_from": ("시행일자", "effectiveDate", "effective_from"),
    "decided_on": ("선고일자", "의결일자", "decidedOn"),
    "interpreted_on": ("해석일자", "interpretedOn"),
    "registered_at": ("등록일시", "데이터기준일시", "registeredAt"),
    "official_url": ("법령상세링크", "판례상세링크", "행정심판재결례상세링크", "법령해석상세링크", "행정규칙상세링크", "별표법령상세링크", "국세법령정보시스템원문링크", "officialUrl"),
}

_SECTION_FIELDS = (
    ("articles", ("조문내용", "조문", "법령내용", "본문")),
    ("addenda", ("부칙내용", "부칙")),
    ("holdings", ("판시사항", "재결요지", "질의요지")),
    ("summary", ("판결요지", "회답", "주문", "재결요지")),
    ("reasoning", ("판례내용", "이유", "회신")),
    ("references", ("참조조문", "참조판례", "관련법령")),
    ("attachments", ("별표내용", "첨부파일명", "별표서식파일링크", "별표서식PDF파일링크")),
)

# 행정규칙은 공식 가이드(admrulInfoGuide)상 ID 파라미터가 "행정규칙일련번호"이고
# LID 파라미터가 "행정규칙ID"다. 이름만 보면 반대로 짝지어지기 쉬우니 주의한다.
_IDENTIFIER_ALIASES: dict[str, tuple[str, ...]] = {
    "ID": (
        "법령ID", "판례일련번호", "판례정보일련번호", "특별행정심판재결례일련번호",
        "특별행정심판재결례 일련번호", "법령해석일련번호", "행정규칙일련번호", "별표일련번호", "ID", "id",
    ),
    "MST": ("법령일련번호", "MST", "lsiSeq"),
    "LID": ("행정규칙ID", "LID"),
    "LM": ("행정규칙명", "행정규칙법령명", "LM"),
}


def _clean_key(value: str) -> str:
    return re.sub(r"[\s_\-]", "", value)


def _normalize_date(value: Any) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
    except ValueError:
        return None


def _scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            scalar = _scalar(item)
            if scalar:
                return scalar
        return None
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text or None
    return None


def _lookup(mapping: Mapping[str, Any], aliases: tuple[str, ...]) -> Any:
    normalized = {_clean_key(str(key)): value for key, value in mapping.items()}
    for alias in aliases:
        value = normalized.get(_clean_key(alias))
        if value not in (None, "", [], {}):
            return value
    return None


def _flatten_xml(element: ET.Element) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for child in element:
        value: Any = _flatten_xml(child) if list(child) else (child.text or "").strip()
        if child.tag in result:
            current = result[child.tag]
            result[child.tag] = current + [value] if isinstance(current, list) else [current, value]
        else:
            result[child.tag] = value
    return result


def _html_notice(text: str, secrets: tuple[str, ...]) -> str | None:
    """오류·안내 HTML에서 사람이 읽을 안내 문구만 뽑는다.

    법제처는 권한이 없는 target을 조회하면 HTTP 200으로 "미신청된 목록/본문에
    대한 접근입니다 ... OPEN API 신청 후 법령종류를 체크해 주세요" 같은 안내
    페이지를 돌려준다. 이 문구를 버리면 호출자는 무엇을 조치해야 하는지 알 수
    없으므로 인증값만 지우고 그대로 전달한다.
    """
    stripped = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if not stripped:
        return None
    return redact_text(stripped, secrets)[:300]


def parse_response(response: HttpResponse, expected_type: str, *, secrets: tuple[str, ...] = ()) -> Any:
    text = decode_body(response).lstrip("﻿\r\n\t ")
    lowered = text[:1024].lower()
    media_type = response_media_type(response)
    if not text or len(text.strip()) < 2:
        raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "공식 API가 빈 응답을 반환했습니다.")
    if "<html" in lowered or "<!doctype html" in lowered:
        notice = _html_notice(text, secrets)
        suffix = f" (법제처 안내: {notice})" if notice else ""
        if any(marker in lowered for marker in ("error", "오류", "login", "인증", "접근")):
            raise ProviderError(ErrorCode.AUTH_FAILED, f"HTTP 200 오류 또는 인증 화면을 받았습니다.{suffix}")
        raise ProviderError(ErrorCode.INCOMPLETE_RESULT, f"본문이 아닌 HTML shell을 받았습니다.{suffix}")
    expected = expected_type.upper()
    try:
        if expected == "JSON" or "json" in media_type or text.startswith(("{", "[")):
            payload = json.loads(text)
        elif expected == "XML" or "xml" in media_type or text.startswith("<"):
            if re.search(r"<!DOCTYPE|<!ENTITY", text, re.IGNORECASE):
                raise ProviderError(ErrorCode.PARSE_ERROR, "외부 엔티티 선언이 포함된 XML은 거부합니다.")
            root = ET.fromstring(text)
            payload = {root.tag: _flatten_xml(root)}
        else:
            raise ProviderError(ErrorCode.PARSE_ERROR, "지원되지 않는 공식 API 응답 형식입니다.")
    except ProviderError:
        raise
    except (json.JSONDecodeError, ET.ParseError) as exc:
        raise ProviderError(ErrorCode.PARSE_ERROR, f"공식 API 응답 파싱 실패: {type(exc).__name__}") from None
    text_error = json.dumps(payload, ensure_ascii=False)[:4000].lower()
    if any(marker in text_error for marker in ('"error"', "인증키", "인증오류", "서비스 오류")):
        raise ProviderError(ErrorCode.AUTH_FAILED, "공식 API 오류 응답을 받았습니다.")
    return payload


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _collapse_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    collapsed: dict[str, Any] = {}

    def add(key: str, value: Any) -> None:
        if value in (None, "", [], {}):
            return
        if key not in collapsed:
            collapsed[key] = value
            return
        current = collapsed[key]
        existing = current if isinstance(current, list) else [current]
        additions = value if isinstance(value, list) else [value]
        for item in additions:
            if item not in existing:
                existing.append(item)
        collapsed[key] = existing

    for key, value in mapping.items():
        if isinstance(value, Mapping):
            for child_key, child_value in _collapse_mapping(value).items():
                add(str(child_key), child_value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    for child_key, child_value in _collapse_mapping(item).items():
                        add(str(child_key), child_value)
                else:
                    add(str(key), item)
        else:
            add(str(key), value)
    return collapsed


def _identifier_values(mapping: Mapping[str, Any]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for kind, aliases in _IDENTIFIER_ALIASES.items():
        found: list[str] = []
        normalized = {_clean_key(str(key)): value for key, value in mapping.items()}
        for alias in aliases:
            raw = normalized.get(_clean_key(alias))
            candidates = raw if isinstance(raw, list) else [raw]
            for candidate in candidates:
                scalar = _scalar(candidate)
                if scalar and scalar not in found:
                    found.append(scalar)
        if found:
            values[kind] = found
    return values


def _article_sections(payload: Any) -> list[dict[str, Any]]:
    """법령 상세 API의 조문단위 안에 들어 있는 항·호·목을 순서대로 보존한다."""
    if not isinstance(payload, Mapping):
        return []
    law = payload.get("법령")
    if not isinstance(law, Mapping):
        return []
    articles = law.get("조문")
    if not isinstance(articles, Mapping):
        return []
    units = articles.get("조문단위")
    units = units if isinstance(units, list) else [units]
    sections = []
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            continue
        text = []

        def collect(node: Mapping[str, Any], field: str, child: str | None) -> None:
            value = _scalar(node.get(field))
            if value:
                text.append(value)
            if child:
                nested = node.get(child)
                nested = nested if isinstance(nested, list) else [nested]
                next_field = {"항": "항내용", "호": "호내용", "목": "목내용"}[child]
                next_child = {"항": "호", "호": "목", "목": None}[child]
                for entry in nested:
                    if isinstance(entry, Mapping):
                        collect(entry, next_field, next_child)

        collect(unit, "조문내용", "항")
        if not text:
            continue
        number = _scalar(unit.get("조문번호"))
        branch = _scalar(unit.get("조문가지번호"))
        locator = f"제{number}조" + (f"의{branch}" if branch and branch != "0" else "") if number else "조문내용"
        sections.append({
            "section_id": f"articles-{index + 1}", "kind": "articles",
            "heading": _scalar(unit.get("조문제목")), "locator": locator,
            "text": "\n".join(text), "derived": False, "source_field": "조문단위",
        })
    return sections


def extract_items(payload: Any, target: LegalTarget) -> tuple[list[dict[str, Any]], int | None, int | None]:
    mappings = list(_walk_mappings(payload))
    if isinstance(payload, Mapping):
        mappings.insert(0, _collapse_mapping(payload))
    total = None
    page = None
    for mapping in mappings:
        total_value = _lookup(mapping, ("totalCnt", "total", "총건수"))
        page_value = _lookup(mapping, ("page", "페이지"))
        if total_value is not None:
            try:
                total = int(str(total_value).replace(",", ""))
            except ValueError:
                pass
        if page_value is not None:
            try:
                page = int(page_value)
            except (TypeError, ValueError):
                pass
    candidates: list[dict[str, Any]] = []
    for mapping in mappings:
        source_id = _scalar(_lookup(mapping, _FIELD_ALIASES["source_document_id"]))
        title = _scalar(_lookup(mapping, _FIELD_ALIASES["title"]))
        if source_id and title:
            candidates.append(normalize_item(mapping, target))
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in candidates:
        key = (item["source_document_id"], item["title"])
        existing = unique.get(key)
        if existing is None or len(item.get("sections", [])) > len(existing.get("sections", [])):
            unique[key] = item
    items = list(unique.values())
    if len(items) == 1 and target in {LegalTarget.LAW, LegalTarget.EFFECTIVE_LAW}:
        articles = _article_sections(payload)
        if articles:
            item = items[0]
            item["sections"] = articles + [section for section in item["sections"] if section["kind"] != "articles"]
    return items, total, page


def normalize_item(mapping: Mapping[str, Any], target: LegalTarget) -> dict[str, Any]:
    item: dict[str, Any] = {"target": target.value, "document_type": CONTRACTS[target].document_type}
    for field, aliases in _FIELD_ALIASES.items():
        value = _scalar(_lookup(mapping, aliases))
        if field.endswith("_on") or field in {"effective_from"}:
            value = _normalize_date(value)
        # law.go.kr은 다음 조회용 상세링크를 스킴/호스트 없는 상대경로로, 게다가
        # 이번 요청에 쓰인 OC를 그대로 실어 돌려준다. 상대경로 그대로면 출처 URL
        # 검증(https 필수)에 걸리고, OC를 남겨두면 사용자가 자기 인증키가 박힌
        # URL을 그대로 공유하게 되므로 항상 절대경로로 만들고 OC를 제거한다.
        if field == "official_url" and value:
            value = strip_secret_query_params(urljoin(SEARCH_ENDPOINT, value))
        item[field] = value
    item["source_document_id"] = item["source_document_id"] or ""
    item["title"] = item["title"] or ""
    sections: list[dict[str, Any]] = []
    for kind, aliases in _SECTION_FIELDS:
        value = _lookup(mapping, aliases)
        values = value if isinstance(value, list) else [value]
        for index, part in enumerate(values):
            text = _scalar(part)
            if text:
                sections.append({"section_id": f"{kind}-{index + 1}", "kind": kind, "heading": aliases[0], "locator": aliases[0], "text": text, "derived": False, "source_field": aliases[0]})
    item["sections"] = sections
    item["metadata"] = {
        **{str(key): value for key, value in mapping.items() if not isinstance(value, (dict, list))},
        "upstream_identifiers": _identifier_values(mapping),
    }
    return item


class LawGoProvider:
    name = "law.go.kr"

    def __init__(self, *, credential: str | None = None, transport: HttpTransport | None = None):
        self.credential_error: str | None = None
        if credential is not None:
            self.credential = credential
            self.credential_source = "explicit"
        else:
            environment_credential = os.environ.get("TAXAX_LAW_GO_OC", "").strip()
            if environment_credential:
                self.credential = environment_credential
                self.credential_source = "environment"
            else:
                try:
                    self.credential = load_law_go_credential()
                    self.credential_source = "protected_file" if self.credential else "none"
                except LocalSecretError as exc:
                    self.credential = None
                    self.credential_source = "none"
                    self.credential_error = str(exc)
        self.transport = transport or HttpTransport()

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "enabled": bool(self.credential),
            "credential_source": self.credential_source,
            "credential_file_error": self.credential_error is not None,
            "targets": {target.value: {"search": contract.search_supported, "detail": contract.detail_supported, "date_filter": contract.date_filter} for target, contract in CONTRACTS.items()},
            "official_api": True,
            "verified_on": "2026-09-13",
        }

    def search(self, target: LegalTarget, *, query: str | None = None, page: int = 1, display: int = 10, filters: Mapping[str, str] | None = None, response_type: str = "JSON") -> ProviderResult:
        contract = self._contract(target)
        if not contract.search_supported:
            raise ProviderError(ErrorCode.INVALID_REQUEST, f"{target.value}는 목록 검색 target이 아닙니다.")
        if target == LegalTarget.LAW_HISTORY:
            # 법제처 공식 가이드(lsHstListGuide)상 lsHistory는 출력 형태가 HTML로
            # 고정돼 있어 JSON/XML을 요청해도 서버가 HTML 페이지를 그대로 돌려준다.
            # 구조화된 근거 수집 대상이 아니므로 명확히 차단한다.
            raise ProviderError(ErrorCode.INVALID_REQUEST, "법령연혁 목록(lsHistory)은 법제처 API가 HTML 형식만 제공해 구조화 조회를 지원하지 않습니다.")
        self._require_credential()
        if page < 1 or not 1 <= display <= 50:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "page는 1 이상, display는 1~50이어야 합니다.")
        request_filters = dict(filters or {})
        unsupported = sorted(set(request_filters) - contract.allowed_search_filters)
        if unsupported:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "지원하지 않는 필터입니다.", details={"filters": unsupported})
        if target == LegalTarget.NTS_INTERPRETATION and request_filters.get("itmno") and query:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "itmno 사용 시 upstream이 query를 무시하므로 둘을 함께 보낼 수 없습니다.")
        params: dict[str, str | int] = {"OC": self.credential or "", "target": target.value, "type": self._response_type(response_type), "display": display, "page": page}
        if query:
            params["query"] = query
        params.update(request_filters)
        response = self.transport.request(SEARCH_ENDPOINT, params=params, secrets=(self.credential or "",))
        parsed = parse_response(response, response_type, secrets=(self.credential or "",))
        items, total, parsed_page = extract_items(parsed, target)
        public_params = {key: value for key, value in params.items() if key != "OC"}
        warnings = list(contract.notes)
        if total and not items:
            raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "전체 건수는 있으나 결과 항목을 파싱하지 못했습니다.")
        return ProviderResult(target, public_params, response, parsed, items, total, parsed_page or page, display, tuple(warnings))

    def detail(self, target: LegalTarget, *, identifier: str, identifier_kind: str = "ID", effective_on: str | None = None, article: str | None = None, response_type: str = "JSON") -> ProviderResult:
        contract = self._contract(target)
        if not contract.detail_supported:
            raise ProviderError(ErrorCode.INVALID_REQUEST, f"{target.value}는 상세 조회 target이 아닙니다.")
        self._require_credential()
        kind = identifier_kind.upper()
        if kind not in contract.detail_identifiers:
            raise ProviderError(ErrorCode.INVALID_REQUEST, f"{target.value}에서 허용되지 않는 식별자 종류입니다.")
        if kind in _NUMERIC_IDENTIFIER_KINDS:
            # 한글을 허용하는 넓은 규칙을 숫자 식별자에까지 그대로 쓰고 있었다.
            # 그래서 law_id="소득세법"처럼 사람이 자연스럽게 넣는 값이 검증을
            # 통과해 ID=소득세법으로 법제처에 그대로 전송됐고, 거기서 실패한
            # 결과가 upstream 오류로 돌아와 "이 기능은 통째로 죽었다"는 진단이
            # 나왔다. 여기서 막으면 무엇이 잘못됐고 무엇을 해야 하는지 바로
            # 알 수 있다. (LM은 법령명을 받는 자리라 이 규칙에서 제외한다.)
            if not re.fullmatch(r"\d{1,20}", identifier):
                raise ProviderError(
                    ErrorCode.INVALID_REQUEST,
                    f"{kind}는 법제처가 부여한 숫자 식별자여야 합니다. "
                    "법령명으로 찾으려면 search_legal_sources로 먼저 조회해 식별자를 확인하십시오.",
                    details={"identifier_kind": kind},
                )
        elif not re.fullmatch(r"[A-Za-z0-9가-힣().·\- ]{1,200}", identifier):
            raise ProviderError(ErrorCode.INVALID_REQUEST, "식별자 형식이 올바르지 않습니다.")
        warnings = list(contract.notes)
        params: dict[str, str] = {"OC": self.credential or "", "target": target.value, "type": self._response_type(response_type), kind: identifier}
        if target == LegalTarget.EFFECTIVE_LAW:
            if kind == "MST":
                if not effective_on:
                    raise ProviderError(ErrorCode.TEMPORAL_UNRESOLVED, "MST 방식의 시행일 기준 조회에는 efYd가 필요합니다.")
                params["efYd"] = self._compact_date(effective_on)
            elif effective_on:
                raise ProviderError(ErrorCode.TEMPORAL_UNRESOLVED, "ID 방식은 기준일 efYd를 적용하지 않습니다. 시행일 버전의 MST를 확인해 요청하십시오.")
        elif effective_on:
            raise ProviderError(ErrorCode.INVALID_REQUEST, f"{target.value} 상세 조회에는 efYd를 사용할 수 없습니다.")
        if article:
            if target not in {LegalTarget.LAW, LegalTarget.EFFECTIVE_LAW} or not re.fullmatch(r"\d{6}", article):
                raise ProviderError(ErrorCode.INVALID_REQUEST, "JO는 law/eflaw의 6자리 조문 코드만 허용됩니다.")
            params["JO"] = article
        response = self.transport.request(DETAIL_ENDPOINT, params=params, secrets=(self.credential or "",))
        parsed = parse_response(response, response_type, secrets=(self.credential or "",))
        items, _, _ = extract_items(parsed, target)
        if not items:
            # 본문 대신 짧은 안내 문자열만 오는 경우가 있다. 예를 들어 국세청이
            # 출처인 판례는 공식 가이드상 HTML로만 본문을 제공해서, JSON/XML로
            # 요청하면 "일치하는 판례가 없습니다"라는 문자열이 돌아온다. 그
            # 문구를 그대로 실어야 호출자가 원인을 알 수 있다.
            upstream_message = next(
                (value.strip() for value in parsed.values() if isinstance(value, str) and value.strip()),
                None,
            ) if isinstance(parsed, Mapping) else None
            message = "상세 응답에서 문서 식별자와 제목을 확인하지 못했습니다."
            if upstream_message:
                message = f"{message} (법제처 응답: {upstream_message[:200]})"
            raise ProviderError(ErrorCode.INCOMPLETE_RESULT, message)
        available = sorted({key for item in items for key in item.get("metadata", {}).get("upstream_identifiers", {})})
        matching = [
            item
            for item in items
            if identifier in item.get("metadata", {}).get("upstream_identifiers", {}).get(kind, [])
        ]
        if not matching and kind not in available and len(items) == 1:
            # law.go.kr 상세조회 응답은 요청에 쓰인 식별자 종류를 그대로 돌려주지
            # 않는 경우가 있다(예: MST로 요청해도 응답 기본정보에는 법령ID만 있고
            # 법령일련번호 필드가 없음). 그 종류의 필드 자체가 응답에 없다면 대조
            # 불가능한 것이지 불일치가 아니므로, 결과가 정확히 하나면 그대로 신뢰한다.
            matching = items
        if not matching:
            raise ProviderError(
                ErrorCode.INCOMPLETE_RESULT,
                "요청 식별자 종류와 상세 응답 식별자가 일치하지 않습니다.",
                details={"requested_identifier_kind": kind, "available_identifier_kinds": available},
            )
        items = matching
        if target in {LegalTarget.LAW, LegalTarget.EFFECTIVE_LAW}:
            for item in items:
                metadata = item["metadata"]
                if kind == "MST":
                    # 상세 응답은 요청한 일련번호를 생략하고 법령ID만 반환할 수 있다.
                    # 그때도 서로 다른 시행본을 하나의 버전으로 합치지 않는다.
                    item["version_id"] = identifier
                    metadata["requested_mst"] = identifier
                    metadata["version_identity_source"] = (
                        "response_mst" if kind in available else "request_mst"
                    )
                elif not item.get("version_id"):
                    # ID 방식은 특정 시행본을 지정하지 않으므로, 원문에 실린
                    # 공포 키가 있을 때만 그 개정본을 구별한다.
                    revision_key = _scalar(metadata.get("법령키"))
                    if revision_key:
                        item["version_id"] = revision_key
                        metadata["version_identity_source"] = "response_revision_key"
        public_params = {key: value for key, value in params.items() if key != "OC"}
        return ProviderResult(target, public_params, response, parsed, items, len(items), 1, len(items), tuple(warnings))

    @staticmethod
    def detail_url(target: LegalTarget, identifier: str, identifier_kind: str = "ID") -> str:
        return DETAIL_ENDPOINT + "?" + urlencode({"target": target.value, identifier_kind.upper(): identifier})

    @staticmethod
    def guide_url(target: LegalTarget) -> str:
        names = {
            LegalTarget.LAW: "lsNwListGuide",
            LegalTarget.LAW_HISTORY: "lsHstListGuide",
            LegalTarget.EFFECTIVE_LAW: "lsEfYdInfoGuide",
            LegalTarget.PRECEDENT: "precListGuide",
            LegalTarget.TAX_TRIBUNAL: "specialDeccTtListGuide",
            LegalTarget.NTS_INTERPRETATION: "cgmExpcNtsListGuide",
            LegalTarget.ADMIN_RULE: "admrulListGuide",
            LegalTarget.LAW_ATTACHMENT: "lsBylListGuide",
        }
        return GUIDE_BASE + names[target]

    @staticmethod
    def _response_type(value: str) -> str:
        upper = value.upper()
        if upper not in {"JSON", "XML"}:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "응답 형식은 JSON 또는 XML이어야 합니다.")
        return upper

    @staticmethod
    def _compact_date(value: str) -> str:
        digits = re.sub(r"\D", "", value)
        if len(digits) != 8 or _normalize_date(digits) is None:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "날짜는 YYYY-MM-DD 또는 YYYYMMDD 형식이어야 합니다.")
        return digits

    @staticmethod
    def _contract(target: LegalTarget) -> TargetContract:
        try:
            return CONTRACTS[target]
        except KeyError:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "지원하지 않는 법제처 target입니다.") from None

    def _require_credential(self) -> None:
        if not self.credential:
            raise ProviderError(ErrorCode.AUTH_REQUIRED, "운영자가 관리하는 TAXAX_LAW_GO_OC가 필요합니다.")
