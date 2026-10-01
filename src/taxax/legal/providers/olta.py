from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode

from ..models import ErrorCode
from ..transport import HttpResponse, SessionHttpTransport, decode_body
from ..router import infer_tax_type
from .base import ProviderError, SupplementalResult

BASE_URL = "https://olta.re.kr"
ENTRY_URL = f"{BASE_URL}/explainInfo/decisionList.do?menuNo=90010100&upperMenuId=90010000"
SEARCH_URL = f"{BASE_URL}/search/PU_0003_search.jsp"
PARSER_VERSION = "olta-v1"

CATEGORIES = {
    "court": ("법원판례", "10000", "sentencing"),
    "moi_ruling": ("행정안전부 유권해석", "20000", "authoritative"),
    "mole_ruling": ("법제처 유권해석", "30000", "legal"),
    "tax_tribunal": ("조세심판원 결정례", "40000", "screen"),
    "audit": ("감사원 심사결정례", "60000", "evaluation"),
    "constitutional": ("헌법재판소 결정례", "70000", "ordinance"),
    "local_gov_ruling": ("자치단체 질의회신", "80000", None),
}
# 검색 결과 화면의 category heading 표기는 CATEGORIES의 기관/자료 명칭과 다르다
# (예: "법제처 유권해석"이 화면에는 "법제처해석", "감사원 심사결정례"가 "감사원
# 결정례"로 나온다). 표기가 달라도 category를 놓치지 않도록 별칭을 함께 둔다.
_HEADING_ALIASES = {
    "moi_ruling": ("행정안전부해석", "행안부 유권해석"),
    "mole_ruling": ("법제처해석",),
    "audit": ("감사원 결정례",),
}
DETAIL_PATHS = {
    "tax_tribunal": "/explainInfo/judgeDecisionDetail.do",
    "constitutional": "/explainInfo/constitutionDcnDetail.do",
}
DOCUMENT_TYPES = {
    "court": "local_tax_court_precedent",
    "moi_ruling": "local_tax_moi_interpretation",
    "mole_ruling": "local_tax_moleg_interpretation",
    "tax_tribunal": "local_tax_tribunal_decision",
    "audit": "local_tax_audit_decision",
    "constitutional": "local_tax_constitutional_decision",
    "local_gov_ruling": "local_tax_government_interpretation",
}


_TEXT_TAG = "#text"


@dataclass(eq=False)
class _Node:
    """트리 노드는 값이 아니라 정체(identity)로 비교해야 한다.

    dataclass 기본 __eq__는 parent와 children까지 재귀 비교하는데, 부모와 자식이
    서로를 참조하므로 tag/attrs가 같은 노드끼리 비교하는 순간(예: 형제인 두 개의
    텍스트 노드) 무한 재귀에 빠진다. children.index(node) 한 번으로도 터진다.
    """

    tag: str
    attrs: dict[str, str]
    parent: "_Node | None" = None
    children: list["_Node"] = field(default_factory=list)
    data: list[str] = field(default_factory=list)


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag.lower(), {key: value or "" for key, value in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.lower() not in {"br", "img", "input", "meta", "link", "hr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag):
        target = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == target:
                del self.stack[index:]
                return

    def handle_data(self, data):
        # 텍스트를 형제 요소와 같은 children 리스트에 순서대로 끼워 넣어야
        # _text()가 실제 문서 순서를 보존할 수 있다. 별도 data 리스트에 모으면
        # "이 노드의 텍스트 전부 먼저, 자식 텍스트는 나중"이 되어, 텍스트와
        # 자식 요소가 번갈아 나오는 마크업(예: "(총<span>4,009</span>건)")에서
        # 순서가 뒤섞인다.
        current = self.stack[-1]
        text_node = _Node(_TEXT_TAG, {}, current, data=[data])
        current.children.append(text_node)


def _classes(node: _Node) -> set[str]:
    return set(node.attrs.get("class", "").split())


def _text(node: _Node) -> str:
    if node.tag == _TEXT_TAG:
        return "".join(node.data)
    values = [_text(child) for child in node.children]
    return re.sub(r"\s+", " ", " ".join(values)).strip()


def _text_excluding(node: _Node, exclude_classes: set[str]) -> str:
    """지정한 class를 가진 하위 요소를 뺀 나머지 텍스트만 모은다."""
    parts: list[str] = []
    for child in node.children:
        if child.tag == _TEXT_TAG:
            parts.append("".join(child.data))
        elif not (_classes(child) & exclude_classes):
            parts.append(_text_excluding(child, exclude_classes))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _find_all(node: _Node, *, tag: str | None = None, class_name: str | None = None) -> list[_Node]:
    found: list[_Node] = []
    for child in node.children:
        if (tag is None or child.tag == tag) and (class_name is None or class_name in _classes(child)):
            found.append(child)
        found.extend(_find_all(child, tag=tag, class_name=class_name))
    return found


def _first(node: _Node, *, tag: str | None = None, class_name: str | None = None) -> _Node | None:
    values = _find_all(node, tag=tag, class_name=class_name)
    return values[0] if values else None


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


def _category_for_heading(heading: str) -> str | None:
    compact = re.sub(r"\s+", "", heading)
    for key, (label, _, _) in CATEGORIES.items():
        aliases = (label, *_HEADING_ALIASES.get(key, ()))
        if any(re.sub(r"\s+", "", alias) in compact for alias in aliases):
            return key
    return None


def _listing_after_heading(heading: _Node) -> _Node | None:
    """heading 다음에 나오는 `<ul class="search_out">` 목록을 찾는다.

    페이지 레이아웃에 따라 목록이 heading과 같은 형제 레벨이 아니라, heading의
    부모(`div.search_tit`)와 형제 레벨에 있는 경우도 있다(category 전체보기
    페이지가 그렇다). 그래서 형제 레벨에서 못 찾으면 한 단계씩 위로 올라가며
    찾되, 다음 category heading을 만나면 그 category 영역을 벗어난 것이므로
    멈춘다.
    """
    node = heading
    while node.parent is not None:
        siblings = node.parent.children
        try:
            position = siblings.index(node)
        except ValueError:
            return None
        for candidate in siblings[position + 1 :]:
            if candidate.tag == "ul" and "search_out" in _classes(candidate):
                return candidate
            if (candidate.tag == "p" and "se_title" in _classes(candidate)) or _find_all(
                candidate, tag="p", class_name="se_title"
            ):
                return None
        node = node.parent
    return None


def _is_search_page(text: str) -> bool:
    """결과가 없어도 남아 있는 category 안내 문구로 검색 화면인지 확인한다.

    실측으로 결과 0건 응답에도 일곱 개 기관명이 모두 페이지에 남아 있다.
    과반이 보이면 우리가 아는 검색 화면으로 본다.
    """
    present = sum(1 for label, _, _ in CATEGORIES.values() if label.split()[0] in text)
    return present >= (len(CATEGORIES) + 1) // 2


def parse_search_html(response: HttpResponse) -> tuple[list[dict[str, Any]], dict[str, int]]:
    text = decode_body(response)
    lowered = text[:2048].lower()
    if "<html" not in lowered and "se_title" not in text:
        raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "OLTA HTML 검색 응답이 아닙니다.")
    if any(marker in lowered for marker in ("login", "access denied", "captcha")):
        raise ProviderError(ErrorCode.ACCESS_DENIED, "OLTA 로그인 또는 차단 화면을 받았습니다.")
    parser = _TreeParser()
    parser.feed(text)
    headings = _find_all(parser.root, tag="p", class_name="se_title")
    if not headings:
        # 검색 결과가 하나도 없으면 category heading도 없다. 그때마다 "구조가
        # 변경됐다"고 하면, 지방세 심판례가 없는 국세 주제를 물었을 뿐인데
        # 시스템이 고장난 것처럼 보인다. 실제로 "가업상속공제 사후관리 위반"은
        # 85KB짜리 멀쩡한 페이지를 받고도 이 오류가 났다.
        # category 안내 문구는 결과 유무와 무관하게 남아 있으므로, 그것으로
        # "검색 페이지는 맞다"를 확인하고 0건으로 돌려준다. 그 문구마저 없으면
        # 그때는 정말 우리가 모르는 화면이므로 기존대로 실패시킨다.
        if _is_search_page(text):
            return [], {}
        raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "OLTA category heading 구조가 변경됐습니다.")
    items: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    for heading in headings:
        heading_text = _text(heading)
        category = _category_for_heading(heading_text)
        if category is None:
            continue
        # "(총6건)"처럼 숫자가 괄호에 붙어 있는 경우도, "(총 4,009 건)"처럼
        # 공백이 섞여 있는 경우도 있어 괄호 안 어디든 있는 숫자를 찾는다.
        parenthetical = re.search(r"\(([^)]*)\)", heading_text)
        count_match = re.search(r"[\d,]+", parenthetical.group(1)) if parenthetical else None
        totals[category] = int(count_match.group(0).replace(",", "")) if count_match else 0
        listing = _listing_after_heading(heading)
        if listing is None:
            continue
        for row in [child for child in listing.children if child.tag == "li"]:
            title_node = _first(row, class_name="tt")
            summary_node = _first(row, class_name="txt")
            title = _text(title_node) if title_node else ""
            if not title:
                continue
            summary = _text(summary_node) if summary_node else ""
            metadata_node = _first(row, class_name="top")
            if metadata_node is None:
                metadata_node = next((node for node in _find_all(row, tag="p") if "tt" not in _classes(node) and "txt" not in _classes(node)), None)
            metadata_text = _text(metadata_node) if metadata_node else ""
            date_match = re.search(r"(\d{4}[.\-/]?\d{2}[.\-/]?\d{2})", metadata_text)
            normalized_date = _date(date_match.group(1)) if date_match else None
            # 이 줄에는 세목(span.part)과 결정 결과(span.label)가 사건번호와 나란히
            # 들어 있다. 두 값은 각각 별도 필드로 뽑으므로, 사건번호에는 섞지 않는다.
            doc_no_source = _text_excluding(metadata_node, {"part", "label", "part_r01"}) if metadata_node else ""
            doc_no = doc_no_source
            doc_no_date = re.search(r"(\d{4}[.\-/]?\d{2}[.\-/]?\d{2})", doc_no_source)
            if doc_no_date:
                doc_no = doc_no_source[: doc_no_date.start()].strip(" ()-,")
            anchors = _find_all(row, tag="a")
            identifiers = []
            for anchor in anchors:
                identifiers.extend(re.findall(r"\d{6,}", anchor.attrs.get("onclick", "")))
            source_id = identifiers[-1] if identifiers else "preview-" + hashlib.sha256(f"{category}|{title}|{doc_no}|{normalized_date}".encode()).hexdigest()[:20]
            sections = []
            if summary:
                sections.append({"section_id": "summary-1", "kind": "summary", "heading": "검색 요약", "locator": "search-result", "text": summary, "derived": False, "source_field": "p.txt"})
            # 상세 endpoint가 확인된 유형은 검색 단계에서도 원문 주소를 만들 수
            # 있다. 상세조회를 해야만 링크가 생기면, 검색 결과만 받아 본 쪽은
            # 출처를 가리킬 방법이 없어 "근거는 있는데 어디서 왔는지 모르는"
            # 인용이 된다. 주소 규칙은 _detail()이 쓰는 것과 같다.
            official_url = (
                BASE_URL + DETAIL_PATHS[category] + "?" + urlencode({"num": source_id})
                if category in DETAIL_PATHS and not source_id.startswith("preview-")
                else None
            )
            # OLTA는 지방세 연구 포털이지만 법원판례·헌재결정례 섹션에는 국세
            # 사건도 색인한다. 출처만 보고 "지방세"라고 적으면 상속세 대법원
            # 판결(2007두16493 등)이 지방세 자료로 표시된다. 실제로 그렇게
            # 나가고 있었다. 세목은 제목에서 읽히는 경우에만 단정하고, 못 읽으면
            # unknown으로 남긴다. 모르는 것을 "지방세"로 채우지 않는다.
            declared_tax_type = _text(_first(row, class_name="part")) if _first(row, class_name="part") else None
            title_tax_type, title_scope = infer_tax_type(f"{title} {doc_no or ''}")
            declared_name, declared_scope = infer_tax_type(declared_tax_type or "")
            if declared_name:
                # OLTA가 세목을 분명히 적어 준 경우. 제목 추론보다 이쪽이 낫다.
                tax_type, tax_scope = declared_name, declared_scope
            else:
                # OLTA는 국세 사건에 "기타"라고만 적는다. 아무 정보도 아니므로
                # 제목에서 읽는다. 그마저 안 읽히면 표기를 그대로 두고 scope는
                # unknown으로 남긴다.
                tax_type, tax_scope = title_tax_type or declared_tax_type, title_scope
            items.append(
                {
                    "source_document_id": source_id,
                    "title": title,
                    "document_type": DOCUMENT_TYPES[category],
                    "issuer": CATEGORIES[category][0],
                    "official_url": official_url,
                    "case_no": doc_no or None,
                    "decided_on": normalized_date,
                    "tax_type": tax_type,
                    "sections": sections,
                    "metadata": {
                        "category": category,
                        "category_code": CATEGORIES[category][1],
                        "result": _text(_first(row, class_name="label")) if _first(row, class_name="label") else None,
                        "court_level": _text(_first(row, class_name="part_r01")) if _first(row, class_name="part_r01") else None,
                        "preview_only": True,
                        "detail_supported": category in DETAIL_PATHS,
                        "derived_source_id": not bool(identifiers),
                        "tax_scope": tax_scope,
                        "source_portal_scope": "local",
                        "raw_response_index": 0,
                        "parser_version": PARSER_VERSION,
                    },
                }
            )
    if not totals:
        raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "OLTA 알려진 category를 확인하지 못했습니다.")
    return items, totals


def _detail_heading(category: str, document_id: str, content: str) -> tuple[str, str | None]:
    """상세 본문 머리말에서 제목과 사건번호를 뽑는다.

    결정문 본문은 "조심2025지2521(20260828) 취득세 기각 이 건 토지는 ..." 처럼
    사건번호로 시작한다. 뽑지 못하면 기관명 + 문서번호로 돌아간다.
    """
    fallback = f"{CATEGORIES[category][0]} {document_id}"
    head = re.sub(r"\s+", " ", content[:400]).strip()
    # 사건번호 표기는 기관마다 달라 "조심2025지2521(20260828)"처럼 한글로 시작하기도
    # 하고 "2019헌바107(20220331)"처럼 숫자로 시작하기도 한다.
    match = re.match(r"([0-9가-힣A-Za-z]+\(\d{8}\))\s*(.+)", head)
    if not match:
        return fallback, None
    remainder = match.group(2).strip()
    return (remainder[:120].strip() or fallback), match.group(1)


def parse_detail_html(response: HttpResponse) -> str:
    text = decode_body(response)
    lowered = text[:4096].lower()
    if any(marker in lowered for marker in ("login", "access denied", "captcha")):
        raise ProviderError(ErrorCode.ACCESS_DENIED, "OLTA 로그인 또는 차단 화면을 받았습니다.")
    parser = _TreeParser()
    parser.feed(text)
    # 인쇄용 본문 영역(div#print_contents)에는 결정문 원문만 들어 있다. 더 바깥
    # container로 내려가면 "다운로드 / 프린트 / 워터마크 안내" 같은 화면 UI 문구가
    # 본문 앞에 섞여 근거 텍스트로 저장된다.
    container = next((node for node in _find_all(parser.root, tag="div") if node.attrs.get("id") == "print_contents"), None)
    if container is None:
        container = _first(parser.root, tag="div", class_name="exp_ifview_cont")
    if container is None:
        container = _first(parser.root, tag="div", class_name="cont")
    if container is None:
        container = next((node for node in _find_all(parser.root, tag="div") if node.attrs.get("id") == "content"), None)
    if container is None:
        container = _first(parser.root, tag="body")
    content = _text(container) if container else ""
    if len(content) < 100:
        raise ProviderError(ErrorCode.NOT_FOUND, "OLTA 상세 원문 container를 확인하지 못했습니다.")
    return content


class OltaProvider:
    name = "olta.re.kr"

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
            os.environ.get("TAXAX_OLTA_ENABLED", "").strip() != "0"
            if enabled is None
            else bool(enabled)
        )
        self.transport = transport or SessionHttpTransport("olta.re.kr", min_interval_seconds=1.0)
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
            "categories": {key: {"label": value[0], "code": value[1], "collection": value[2], "detail": key in DETAIL_PATHS} for key, value in CATEGORIES.items()},
            "integrated_search_preview_only": True,
            "attachments": False,
            "parser_version": PARSER_VERSION,
        }

    def search(
        self,
        query: str,
        *,
        category: str | None = None,
        page: int = 1,
        display: int = 10,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "relevance",
    ) -> SupplementalResult:
        self._require_enabled()
        if not query.strip() or len(query) > 500 or page < 1 or not 1 <= display <= 50 or sort not in {"relevance", "date_desc"}:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "OLTA 검색 입력 범위를 벗어났습니다.")
        if category is not None and category not in CATEGORIES:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "지원하지 않는 OLTA category입니다.")
        if category == "local_gov_ruling" and (page != 1 or date_from or date_to):
            raise ProviderError(ErrorCode.INVALID_REQUEST, "자치단체 질의회신은 preview 검색만 지원합니다.")
        start = self._compact_date(date_from)
        end = self._compact_date(date_to)
        if start and end and start > end:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "date_from은 date_to보다 늦을 수 없습니다.")
        request_parameters = {
            "query": query.strip(), "category": category, "page": page, "display": display,
            "date_from": date_from, "date_to": date_to, "sort": sort, "parser_version": PARSER_VERSION,
        }
        key = repr(sorted(request_parameters.items()))
        cached = self._cache.get(key)
        if cached and self.clock() - cached[0] <= self.cache_ttl_seconds:
            return replace(cached[1], warnings=(*cached[1].warnings, "동일 검색 cache를 재사용했습니다."))
        result = self._search(request_parameters, start, end)
        self._cache[key] = (self.clock(), result)
        return result

    def _search(self, request_parameters, start, end) -> SupplementalResult:
        self._bootstrap()
        category = request_parameters["category"]
        collection = CATEGORIES[category][2] if category else None
        if category and collection:
            form = {
                "searchType": "1", "detailSearchIsOnOff": "on", "query": request_parameters["query"],
                "startCount": str((int(request_parameters["page"]) - 1) * 10),
                "sort": "DATE" if request_parameters["sort"] == "date_desc" else "RANK",
                "collection": collection, "startDate": self._dotted(start), "endDate": self._dotted(end),
                "searchField": "ALL", "reQuery": "", "taxTitleStr": "", "range": "ALL", "brdNm": "",
            }
            partial = int(request_parameters["display"]) > 10
            date_location = "upstream" if start or end else "none"
            warnings = ["OLTA collection의 upstream page size는 10건이며 추가 결과는 다음 page로 조회해야 합니다."] if partial else []
        else:
            if start or end:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "OLTA 통합 preview 검색에는 기간 필터를 적용할 수 없습니다.")
            form = {"csrfToken": "null", "query": request_parameters["query"], "querySub": request_parameters["query"]}
            partial = True
            date_location = "none"
            warnings = ["OLTA 통합검색은 category별 일부 preview만 제공하며 전체검색이 아닙니다."]
        response = self.transport.request(SEARCH_URL, method="POST", form=form, headers={"Referer": ENTRY_URL})
        items, totals = parse_search_html(response)
        if category:
            items = [item for item in items if item["metadata"]["category"] == category]
            total = totals.get(category, 0)
        else:
            total = sum(totals.values())
        items = items[: int(request_parameters["display"])]
        if total > len(items):
            partial = True
        return SupplementalResult(
            self.name,
            DOCUMENT_TYPES.get(category, "local_tax_search_result"),
            request_parameters,
            response,
            {"category_totals": totals},
            items,
            total,
            int(request_parameters["page"]),
            int(request_parameters["display"]),
            tuple(warnings),
            partial,
            date_location,
            (response,),
        )

    def detail(self, category: str, document_id: str) -> SupplementalResult:
        self._require_enabled()
        if category not in DETAIL_PATHS:
            raise ProviderError(ErrorCode.POLICY_DISABLED, "이 OLTA 유형은 확인된 상세 endpoint가 없어 검색 요약만 제공합니다.")
        if not re.fullmatch(r"\d{6,20}", document_id):
            raise ProviderError(ErrorCode.INVALID_REQUEST, "OLTA 상세 문서 ID 형식이 올바르지 않습니다.")
        return self._detail(category, document_id)

    def _detail(self, category: str, document_id: str) -> SupplementalResult:
        url = BASE_URL + DETAIL_PATHS[category] + "?" + urlencode({"num": document_id})
        response = self.transport.request(url, headers={"Referer": ENTRY_URL})
        content = parse_detail_html(response)
        title, case_no = _detail_heading(category, document_id, content)
        item = {
            "source_document_id": document_id,
            "title": title,
            "case_no": case_no,
            "document_type": DOCUMENT_TYPES[category],
            "issuer": CATEGORIES[category][0],
            "official_url": url,
            "sections": [{"section_id": "body-1", "kind": "body", "heading": "원문", "locator": "div.cont", "text": content, "derived": False, "source_field": "div.cont"}],
            "metadata": {
                "category": category,
                "category_code": CATEGORIES[category][1],
                "tax_scope": "local",
                "preview_only": False,
                "raw_response_index": 0,
                "parser_version": PARSER_VERSION,
            },
        }
        return SupplementalResult(self.name, DOCUMENT_TYPES[category], {"category": category, "document_id": document_id}, response, {"content": content}, [item], 1, 1, 1, raw_responses=(response,))

    def attachment(self, *args, **kwargs):
        raise ProviderError(ErrorCode.POLICY_DISABLED, "OLTA 참고 구현에서 검증된 첨부 endpoint가 없어 비활성입니다.")

    def _bootstrap(self) -> None:
        if self._bootstrapped:
            return
        response = self.transport.request(ENTRY_URL)
        text = decode_body(response)
        if "decisionList" not in text and "지방세" not in text and "결정례" not in text:
            raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "OLTA 공개 세션 초기화 화면이 변경됐습니다.")
        self._bootstrapped = True

    @staticmethod
    def _compact_date(value: str | None) -> str | None:
        if not value:
            return None
        normalized = _date(value)
        if normalized is None:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "OLTA 날짜는 YYYY-MM-DD 또는 YYYYMMDD 형식이어야 합니다.")
        return normalized.replace("-", "")

    @staticmethod
    def _dotted(value: str | None) -> str:
        return f"{value[:4]}.{value[4:6]}.{value[6:]}" if value else ""

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ProviderError(
                ErrorCode.POLICY_DISABLED,
                "OLTA 보완 adapter는 운영 설정에서 비활성 상태입니다.",
            )
