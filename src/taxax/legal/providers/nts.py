from __future__ import annotations

import json
import os
import re
import time
from dataclasses import replace
from datetime import date
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlparse

from ..models import ErrorCode
from ..transport import HttpResponse, SessionHttpTransport, decode_body
from .base import ProviderError, SupplementalResult

BASE_URL = "https://taxlaw.nts.go.kr"
SEARCH_ENTRY_URL = f"{BASE_URL}/qt/USEQTA001M.do?ntstDcmClCd=01"
ACTION_URL = f"{BASE_URL}/action.do"
SEARCH_ACTION = "ASEISA001MR01"
DETAIL_ACTION = "ASIQTB002PR01"
FILE_META_ACTION = "ACMCMA001MR02"
PARSER_VERSION = "nts-taxlaw-v2"

COLLECTIONS = {
    "form": "appendForm",
    "statute": "statute",
    "ruling": "question",
    "precedent": "precedent",
    "old_ruling": "formerLibrary",
    "intl": "intEpn",
    "hometax": "hometaxCnslThan",
}
SORT_OPTIONS = {"relevance": "SCORE/DESC", "date_desc": "DCM_RGT_DTM/DESC", "date_asc": "DCM_RGT_DTM/ASC"}
DOCUMENT_TYPES = {
    "appendForm": "nts_form",
    "statute": "nts_statute",
    "question": "nts_interpretation",
    "precedent": "nts_precedent",
    "formerLibrary": "nts_old_interpretation",
    "intEpn": "nts_international_interpretation",
    "hometaxCnslThan": "nts_hometax_consultation",
}


def _walk(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


_HIGHLIGHT_MARKUP = re.compile(r"<!HS>|<!HE>")


def strip_highlight_markup(value: str) -> str:
    """NTS 검색 API는 검색어와 일치한 부분을 <!HS>...<!HE>로 감싸 돌려준다.

    이건 우리 쪽 파싱 실수가 아니라 NTS 응답 JSON 문자열 값에 그 태그가
    말 그대로 들어있는 것이라, HTML 파서를 거치지 않는 이 경로에서는 따로
    벗겨내지 않으면 제목·요지·본문에 그대로 남는다.
    """
    return _HIGHLIGHT_MARKUP.sub("", value)


def _first(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return strip_highlight_markup(str(value).strip())
    return None


def _date(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)[:8]
    if len(digits) != 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:])).isoformat()
    except ValueError:
        return None


def _json(response: HttpResponse) -> dict[str, Any]:
    text = decode_body(response).lstrip("﻿\r\n\t ")
    if not text or text.startswith("<"):
        raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "NTS가 JSON 본문이 아닌 shell 또는 빈 응답을 반환했습니다.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise ProviderError(ErrorCode.PARSE_ERROR, "NTS JSON 응답 구조를 해석하지 못했습니다.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "NTS 응답의 data 구조가 변경됐습니다.")
    return payload


def _document_type(collection: str, item: Mapping[str, Any]) -> str:
    label = (_first(item, "NTST_DCM_CL_NM", "COLLECTION_NM") or "").replace(" ", "")
    if "심판" in label:
        return "national_tax_tribunal_decision"
    if "심사" in label or "이의" in label or "과세전" in label:
        return "nts_administrative_decision"
    if "판례" in label:
        return "court_precedent"
    if "사전답변" in label:
        return "nts_advance_ruling"
    if "서면" in label or "질의" in label:
        return "nts_interpretation"
    return DOCUMENT_TYPES.get(collection, "nts_public_document")


def _normalize_item(item: Mapping[str, Any], collection: str) -> dict[str, Any]:
    source_id = _first(item, "DOC_ID", "NTST_DCM_ID", "ntstDcmId", "RFRN_QUT_NTST_DCM_ID")
    title = _first(item, "TTL", "TITLE", "NTST_DCM_NM", "SJ")
    if not source_id or not title:
        raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "NTS 검색 항목에서 문서 ID 또는 제목을 확인하지 못했습니다.")
    registered = _date(_first(item, "NTST_DCM_RGT_DT", "DCM_RGT_DTM_S", "DATE", "DCM_RGT_DTM"))
    summary = _first(item, "GIST_CNTN", "CNTN")
    sections = []
    if summary:
        sections.append({"section_id": "summary-1", "kind": "summary", "heading": "검색 요약", "locator": "search-result", "text": summary, "derived": False, "source_field": "GIST_CNTN"})
    return {
        "source_document_id": source_id,
        "title": title,
        "document_type": _document_type(collection, item),
        "issuer": "국세청",
        "registered_at": registered,
        "interpreted_on": _date(_first(item, "EXPL_YD", "explYd", "interpretedOn")),
        "official_url": f"{BASE_URL}/qt/USEQTA002P.do?{urlencode({'ntstDcmId': source_id})}",
        "tax_type": _first(item, "NTST_TLAW_CL_NM"),
        "sections": sections,
        "metadata": {
            "collection": collection,
            "document_number": _first(item, "NTST_DCM_DSCM_CNTN", "ntstDcmDscmCntn"),
            "document_class": _first(item, "NTST_DCM_CL_NM"),
            "source_organization_code": _first(item, "NTST_DCM_SRCS_ORGN_CL_CD"),
            "file_id": _first(item, "NTST_FLE_ID"),
            "tax_scope": "national",
            "preview_only": True,
            "parser_version": PARSER_VERSION,
        },
    }


class NtsProvider:
    name = "taxlaw.nts.go.kr"

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        terms_confirmed: bool | None = None,
        transport: SessionHttpTransport | None = None,
        cache_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.enabled = (
            os.environ.get("TAXAX_NTS_ENABLED", "").strip() != "0"
            if enabled is None
            else bool(enabled)
        )
        self.transport = transport or SessionHttpTransport("taxlaw.nts.go.kr")
        self.cache_ttl_seconds = cache_ttl_seconds
        self.clock = clock
        self._bootstrapped = False
        self._cache: dict[str, tuple[float, SupplementalResult]] = {}

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "implemented": True,
            "fixture_verified": True,
            "enabled": self.enabled,
            "limited_query_only": True,
            "bulk_collection": False,
            "search_collections": dict(COLLECTIONS),
            "detail": True,
            "attachments": True,
            "date_filter_location": "client",
            "parser_version": PARSER_VERSION,
        }

    def search(
        self,
        query: str,
        *,
        collections: tuple[str, ...] = ("ruling", "precedent"),
        page: int = 1,
        display: int = 20,
        date_from: str | None = None,
        date_to: str | None = None,
        date_base: str = "DCM_RGT_DTM",
        sort: str = "relevance",
        max_pages: int = 3,
    ) -> SupplementalResult:
        self._require_enabled()
        if not query.strip() or len(query) > 500 or page < 1 or not 1 <= display <= 50 or not 1 <= max_pages <= 10:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "NTS 검색 입력 범위를 벗어났습니다.")
        unknown = sorted(set(collections) - set(COLLECTIONS))
        if unknown or not collections:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "지원하지 않는 NTS collection입니다.", details={"collections": unknown})
        if sort not in SORT_OPTIONS or date_base != "DCM_RGT_DTM":
            raise ProviderError(ErrorCode.INVALID_REQUEST, "NTS 정렬 또는 날짜 기준이 지원되지 않습니다.")
        start_date = self._parse_date(date_from)
        end_date = self._parse_date(date_to)
        if start_date and end_date and start_date > end_date:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "date_from은 date_to보다 늦을 수 없습니다.")
        request_parameters = {
            "query": query.strip(),
            "collections": sorted(collections),
            "page": page,
            "display": display,
            "date_from": date_from,
            "date_to": date_to,
            "date_base": date_base,
            "sort": sort,
            "max_pages": max_pages,
            "parser_version": PARSER_VERSION,
        }
        cache_key = json.dumps(request_parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cached = self._cache.get(cache_key)
        if cached and self.clock() - cached[0] <= self.cache_ttl_seconds:
            return replace(cached[1], warnings=(*cached[1].warnings, "동일 검색 cache를 재사용했습니다."))
        result = self._search(request_parameters, collections, start_date, end_date)
        self._cache[cache_key] = (self.clock(), result)
        return result

    def _search(self, request_parameters, collections, start_date, end_date) -> SupplementalResult:
        self._bootstrap()
        date_filtered = start_date is not None or end_date is not None
        fetch_size = min(max(int(request_parameters["display"]) * 5 if date_filtered else int(request_parameters["display"]), 50 if date_filtered else 1), 200)
        normalized: list[dict[str, Any]] = []
        raw_responses: list[HttpResponse] = []
        parsed_pages: list[dict[str, Any]] = []
        upstream_total: int | None = None
        scanned = 0
        for offset_page in range(int(request_parameters["max_pages"])):
            source_page = int(request_parameters["page"]) + offset_page
            start_count = (source_page - 1) * fetch_size + 1
            param_data = {
                "schVcb": request_parameters["query"],
                "startCount": start_count,
                "collection": ",".join(COLLECTIONS[key] for key in collections),
                "wnKey": "",
                "searchType": "",
                "sortField": SORT_OPTIONS[str(request_parameters["sort"])],
                "ntstTlawClCdList": [],
                "icldVcbCtl": [],
                "exclVcbCtl": [],
                "rltnStttCtl": [],
                "schDtBase": request_parameters["date_base"],
                "viewCount": str(fetch_size),
                "prtsSprcChiefJdgmYn": "",
                "prtsAttrYrCtl": [],
                "prtsPrgrStatCtl": [],
                "mainIdCtl": [],
                "useSynonymYn": "N",
            }
            response = self.transport.request(
                ACTION_URL,
                method="POST",
                form={"actionId": SEARCH_ACTION, "paramData": json.dumps(param_data, ensure_ascii=False, separators=(",", ":"))},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": SEARCH_ENTRY_URL},
            )
            raw_responses.append(response)
            payload = _json(response)
            parsed_pages.append(payload)
            search_result = payload["data"].get(SEARCH_ACTION, {}).get("searchResultVO")
            if not isinstance(search_result, Mapping) or not isinstance(search_result.get("collectionList"), list):
                raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "NTS 검색 응답 구조가 변경됐습니다.")
            collections_payload = search_result["collectionList"]
            page_total = 0
            raw_count = 0
            for collection_payload in collections_payload:
                if not isinstance(collection_payload, Mapping):
                    continue
                collection = str(collection_payload.get("nameEn") or "")
                total = int(str(collection_payload.get("totalCount") or 0).replace(",", ""))
                page_total += total
                results = collection_payload.get("resultList") or []
                if not isinstance(results, list):
                    raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "NTS resultList 구조가 변경됐습니다.")
                raw_count += len(results)
                for item in results:
                    if not isinstance(item, Mapping):
                        continue
                    candidate = _normalize_item(item, collection)
                    candidate["metadata"]["raw_response_index"] = len(raw_responses) - 1
                    candidate["metadata"]["source_page"] = source_page
                    item_date = self._parse_date(candidate.get("registered_at"))
                    if start_date and (item_date is None or item_date < start_date):
                        continue
                    if end_date and (item_date is None or item_date > end_date):
                        continue
                    normalized.append(candidate)
            upstream_total = page_total if upstream_total is None else max(upstream_total, page_total)
            scanned += raw_count
            if len(normalized) >= int(request_parameters["display"]) or raw_count < fetch_size or scanned >= page_total:
                break
        normalized = normalized[: int(request_parameters["display"])]
        partial = bool(date_filtered and upstream_total is not None and scanned < upstream_total and len(normalized) < int(request_parameters["display"]))
        warnings = ["NTS 기간 필터는 client-side이며 원천 total은 필터 전 건수입니다."] if date_filtered else []
        if partial:
            warnings.append("요청 예산 안에서 원천 검색 범위를 모두 탐색하지 못했습니다.")
        primary = raw_responses[-1]
        return SupplementalResult(
            provider=self.name,
            document_type="nts_search_result",
            request_parameters=request_parameters,
            response=primary,
            raw_responses=tuple(raw_responses),
            parsed={"pages": parsed_pages},
            items=normalized,
            total=upstream_total,
            page=int(request_parameters["page"]),
            page_size=int(request_parameters["display"]),
            warnings=tuple(warnings),
            partial=partial,
            date_filter_location="client" if date_filtered else "none",
        )

    def detail(self, document_id: str) -> SupplementalResult:
        self._require_enabled()
        if not re.fullmatch(r"[A-Za-z0-9]{1,64}", document_id):
            raise ProviderError(ErrorCode.INVALID_REQUEST, "NTS 문서 ID 형식이 올바르지 않습니다.")
        return self._detail(document_id)

    def _detail(self, document_id: str) -> SupplementalResult:
        page_url = f"{BASE_URL}/qt/USEQTA002P.do?{urlencode({'ntstDcmId': document_id})}"
        shell = self.transport.request(page_url)
        shell_text = decode_body(shell)
        if DETAIL_ACTION not in shell_text or "Req.doAction" not in shell_text:
            raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "NTS 상세 shell 구조 또는 세션 상태가 올바르지 않습니다.")
        response = self.transport.request(
            ACTION_URL,
            method="POST",
            form={"actionId": DETAIL_ACTION, "paramData": json.dumps({"dcmDVO": {"ntstDcmId": document_id}}, ensure_ascii=False, separators=(",", ":"))},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": page_url},
        )
        payload = _json(response)
        detail = payload["data"].get(DETAIL_ACTION, {}).get("dcmDVO")
        if not isinstance(detail, Mapping) or str(detail.get("ntstDcmId") or "") != document_id:
            raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "요청 NTS 문서 ID와 상세 응답이 일치하지 않습니다.")
        title = _first(detail, "ntstDcmTtl", "ntstDcmNm", "ttl", "TTL", "title", "sj") or f"NTS 공개 문서 {document_id}"
        sections = []
        # 실제 상세 응답(dcmDVO)에서 관찰된 필드명. 문서 유형(ntstDcmClCd)에 따라
        # 일부 필드가 비어 있을 수 있어 alias 여러 개를 순서대로 시도한다.
        aliases = (
            ("gist", ("ntstDcmGistCntn", "qsCntn", "qstnCntn", "질의요지")),
            ("answer", ("ntstDcmCntn", "ntstDcmRplyCntn", "ansCntn", "rplCntn", "회신내용", "replyCntn")),
            ("references", ("ntstDcmMatrCntn", "rltnStttCntn", "관련법령")),
        )
        flattened = list(_walk(detail))
        for kind, fields in aliases:
            for mapping in flattened:
                value = _first(mapping, *fields)
                if value:
                    sections.append({"section_id": f"{kind}-{len(sections) + 1}", "kind": kind, "heading": fields[0], "locator": fields[0], "text": value, "derived": False, "source_field": fields[0]})
                    break
        registered = _date(_first(detail, "ntstDcmRgtDt", "dcmRgtDtm", "DCM_RGT_DTM_S", "date"))
        item = {
            "source_document_id": document_id,
            "title": title,
            "document_type": _document_type("question", detail),
            "issuer": "국세청",
            "registered_at": registered,
            "interpreted_on": _date(_first(detail, "explYd", "interpretedOn")),
            "official_url": page_url,
            "tax_type": _first(detail, "ntstTlawClNm", "NTST_TLAW_CL_NM"),
            "sections": sections,
            "metadata": {
                "document_number": _first(detail, "ntstDcmDscmCntn", "NTST_DCM_DSCM_CNTN"),
                "file_id": _first(detail, "ntstFleId", "NTST_FLE_ID"),
                "tax_scope": "national",
                "preview_only": False,
                "raw_response_index": 1,
                "parser_version": PARSER_VERSION,
            },
        }
        return SupplementalResult(self.name, item["document_type"], {"document_id": document_id}, response, payload, [item], 1, 1, 1, raw_responses=(shell, response))

    def attachment(self, file_id: str) -> SupplementalResult:
        self._require_enabled()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", file_id):
            raise ProviderError(ErrorCode.INVALID_REQUEST, "NTS 첨부 ID 형식이 올바르지 않습니다.")
        return self._attachment(file_id)

    def _attachment(self, file_id: str) -> SupplementalResult:
        self._bootstrap()
        metadata_response = self.transport.request(
            ACTION_URL,
            method="POST",
            form={"actionId": FILE_META_ACTION, "paramData": json.dumps({"fleId": file_id}, separators=(",", ":"))},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": SEARCH_ENTRY_URL},
        )
        metadata = _json(metadata_response)["data"].get(FILE_META_ACTION)
        if not isinstance(metadata, Mapping):
            raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "NTS 첨부 metadata 구조가 변경됐습니다.")
        download_url = _first(metadata, "fleDwldUri")
        if not download_url:
            raise ProviderError(ErrorCode.NOT_FOUND, "NTS 첨부 다운로드 경로가 없습니다.")
        parsed = urlparse(download_url)
        if not parsed.scheme:
            download_url = BASE_URL + (download_url if download_url.startswith("/") else "/" + download_url)
        attachment = self.transport.request(download_url, max_bytes=10 * 1024 * 1024)
        content_type = attachment.headers.get("content-type", "").lower()
        if "html" in content_type or attachment.body.lstrip().lower().startswith((b"<html", b"<!doctype html")):
            raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "NTS 첨부 대신 HTML shell을 받았습니다.")
        item = {
            "source_document_id": file_id,
            "title": _first(metadata, "orgFleNm", "fleNm") or f"NTS attachment {file_id}",
            "document_type": "nts_attachment",
            "issuer": "국세청",
            "official_url": download_url,
            "sections": [],
            "metadata": {
                "media_type": content_type,
                "byte_length": len(attachment.body),
                "tax_scope": "national",
                "preview_only": False,
                "raw_response_index": 1,
                "parser_version": PARSER_VERSION,
            },
        }
        return SupplementalResult(self.name, "nts_attachment", {"file_id": file_id}, attachment, {"metadata": dict(metadata)}, [item], 1, 1, 1, raw_responses=(metadata_response, attachment))

    def _bootstrap(self) -> None:
        if self._bootstrapped:
            return
        response = self.transport.request(SEARCH_ENTRY_URL)
        text = decode_body(response)
        if "ASEISA001MR01" not in text and "검색" not in text:
            raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "NTS 공개 검색 세션 초기화 화면이 변경됐습니다.")
        self._bootstrapped = True

    @staticmethod
    def _parse_date(value: str | None) -> date | None:
        normalized = _date(value)
        if value and normalized is None:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "NTS 날짜는 YYYY-MM-DD 또는 YYYYMMDD 형식이어야 합니다.")
        return date.fromisoformat(normalized) if normalized else None

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ProviderError(
                ErrorCode.POLICY_DISABLED,
                "NTS 보완 adapter는 운영 설정에서 비활성 상태입니다.",
            )
