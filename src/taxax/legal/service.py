from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from .citations import heading_only_article, incomplete_law_articles, verify_citations
from .models import (
    CitationInput,
    CitationStatus,
    ContentCompleteness,
    Coverage,
    ErrorCode,
    LegalDocument,
    LegalToolResponse,
    Pagination,
    ResponseStatus,
    RetrievalStatus,
    ReviewState,
    SourceReference,
    TemporalStatus,
    TextSection,
    ToolError,
)
from .providers.base import LegalTarget, ProviderError, ProviderResult, SupplementalResult
from .providers.korean_law_bridge import KoreanLawBridge
from .providers.law_go import CONTRACTS, PARSER_VERSION, LawGoProvider
from .providers.nts import NtsProvider
from .providers.olta import OltaProvider
from .repository import AmbiguousDocumentVersion, LegalRepository
from .seeds import FORM_SPECS, LAW_GROUPS, NTS_SPECS
from .snapshots import SnapshotStore, sha256_bytes
from .paths import default_legal_data_dir
from .temporal import temporal_candidates
from .transport import TransportError, normalized_json_bytes
from .version import package_version

MAX_QUERY_CHARS = 500
DEFAULT_OUTPUT_CHARS = 16 * 1024
MAX_OUTPUT_CHARS = 64 * 1024
_PII_PATTERNS = (
    re.compile(r"(?<!\d)\d{6}-[1-8]\d{6}(?!\d)"),
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?!\d)"),
    re.compile(r"(?i)(계좌번호|주민등록번호|사업자등록번호|고객명|거래처명)\s*[:=]"),
)
_PROVIDER_ALIASES = {
    "law.go.kr": "law.go.kr",
    "law_go_kr": "law.go.kr",
    "www.law.go.kr": "law.go.kr",
    "official": "law.go.kr",
    "taxlaw.nts.go.kr": "taxlaw.nts.go.kr",
    "nts": "taxlaw.nts.go.kr",
    "olta.re.kr": "olta.re.kr",
    "olta": "olta.re.kr",
}
_PROVIDER_HOSTS = {
    "law.go.kr": frozenset({"www.law.go.kr", "open.law.go.kr"}),
    "taxlaw.nts.go.kr": frozenset({"taxlaw.nts.go.kr"}),
    "olta.re.kr": frozenset({"olta.re.kr"}),
}
# law.go.kr의 ntsCgmExpc(국세청 세법해석 색인) target은 원본이 국세청 사이트에
# 있어 응답의 법령해석상세링크가 taxlaw.nts.go.kr을 가리킨다. provider별
# allowlist를 그대로 쓰면 이 정상 교차 참조 링크까지 막히므로, "우리가 신뢰하는
# 공식 도메인 중 하나"인지로 검증하되 provider_name 자체가 등록된 provider인지는
# 그대로 확인한다.
_ALL_OFFICIAL_HOSTS = frozenset(host for hosts in _PROVIDER_HOSTS.values() for host in hosts)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def request_id() -> str:
    return "req-" + uuid.uuid4().hex




def _validate_query(value: str) -> str:
    # 세 가지 다른 잘못을 한 문구로 뭉뚱그리면 고칠 방법을 알 수 없다. 공백만
    # 넣은 사람에게 "1~500자여야 합니다"라고 하면, 세 글자를 넣었는데 왜
    # 걸렸는지 알 수 없다. 실제 이유는 다듬고 나니 아무것도 남지 않은 것이다.
    query = value.strip()
    if "\x00" in query:
        raise ProviderError(ErrorCode.INVALID_REQUEST, "query에 널 문자를 넣을 수 없습니다.")
    if not query:
        raise ProviderError(ErrorCode.INVALID_REQUEST, "query가 비어 있습니다. 공백만으로는 조회할 수 없습니다.")
    if len(query) > MAX_QUERY_CHARS:
        raise ProviderError(
            ErrorCode.INVALID_REQUEST,
            f"query가 {len(query)}자로 최대 {MAX_QUERY_CHARS}자를 넘었습니다.",
        )
    if any(pattern.search(query) for pattern in _PII_PATTERNS):
        raise ProviderError(ErrorCode.ACCESS_DENIED, "고객 식별정보가 포함될 수 있는 질의는 법률 출처로 전송하지 않습니다.")
    return query


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not re.fullmatch(r"\d+", cursor):
        raise ProviderError(ErrorCode.INVALID_REQUEST, "cursor 형식이 올바르지 않습니다.")
    offset = int(cursor)
    if offset > 2**63 - 1:
        raise ProviderError(ErrorCode.INVALID_REQUEST, "cursor가 지원 범위를 벗어났습니다.")
    return offset


def _provider_name(value: str | None, default: str) -> str:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized == "all":
        return normalized
    try:
        return _PROVIDER_ALIASES[normalized]
    except KeyError:
        raise ProviderError(
            ErrorCode.INVALID_REQUEST,
            "지원하지 않는 법률 출처 provider입니다. law.go.kr, taxlaw.nts.go.kr, olta.re.kr 또는 all을 사용하십시오.",
            details={"accepted_providers": ["law.go.kr", "taxlaw.nts.go.kr", "olta.re.kr", "all"]},
        ) from None


def _source(document: LegalDocument, locator: str | None = None) -> SourceReference:
    return SourceReference(
        provider=document.provider,
        document_id=document.document_id,
        version_id=document.version_id,
        title=document.title,
        document_number=document.document_number,
        official_url=document.official_url,
        version_url=document.version_url,
        retrieved_at=document.retrieved_at,
        raw_sha256=document.raw_sha256,
        snapshot_raw_sha256=document.metadata.get("snapshot_raw_sha256"),
        snapshot_ref=document.snapshot_ref,
        section_locator=locator,
    )


class LegalKnowledgeService:
    def __init__(
        self,
        project_root: Path,
        *,
        data_dir: Path | None = None,
        provider: LawGoProvider | None = None,
        nts_provider: NtsProvider | None = None,
        olta_provider: OltaProvider | None = None,
        bridge: KoreanLawBridge | None = None,
        repository: LegalRepository | None = None,
        snapshots: SnapshotStore | None = None,
        knowledge_dir: Path | None = None,
        legacy_snapshot_root: Path | None = None,
    ):
        self.project_root = project_root.resolve()
        configured = os.environ.get("TAXAX_LEGAL_DATA_DIR")
        configured_knowledge = os.environ.get("TAXAX_PRIVATE_KNOWLEDGE_DIR", "").strip()
        configured_legacy = os.environ.get("TAXAX_LEGACY_SNAPSHOT_ROOT", "").strip()
        self.data_dir = (data_dir or (Path(configured) if configured else default_legal_data_dir())).resolve()
        self.knowledge_dir = (
            knowledge_dir.resolve()
            if knowledge_dir is not None
            else Path(configured_knowledge).resolve()
            if configured_knowledge
            else None
        )
        self.legacy_snapshot_root = (
            legacy_snapshot_root.resolve()
            if legacy_snapshot_root is not None
            else Path(configured_legacy).resolve()
            if configured_legacy
            else None
        )
        self.provider = provider or LawGoProvider()
        self.nts_provider = nts_provider or NtsProvider()
        self.olta_provider = olta_provider or OltaProvider()
        self.bridge = bridge or KoreanLawBridge(enabled=os.environ.get("TAXAX_KOREAN_LAW_BRIDGE", "").lower() == "enabled")
        self.repository = repository or LegalRepository(self.data_dir / "v1" / "legal.sqlite3")
        self.snapshots = snapshots or SnapshotStore(self.data_dir, parser_version=PARSER_VERSION)

    def search_knowledge(self, *, query: str, k_ar_id: str | None = None, limit: int = 10) -> LegalToolResponse:
        rid = request_id()
        try:
            value = _validate_query(k_ar_id or query)
            if not 1 <= limit <= 50:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "limit는 1~50이어야 합니다.")
            if self.knowledge_dir is None:
                return LegalToolResponse(
                    request_id=rid,
                    status=ResponseStatus.OK,
                    data={"items": [], "approved_count": 0},
                    warnings=[
                        "K-AR은 선택 기능이며 TAXAX_PRIVATE_KNOWLEDGE_DIR가 설정되지 않아 비활성입니다.",
                        "공개 법률 조회와 세무 조사 workflow는 K-AR 없이 사용할 수 있습니다.",
                    ],
                    coverage=Coverage(
                        queried_providers=["private-k-ar"],
                        disabled_providers=["private-k-ar"],
                    ),
                    freshness={"catalog_read_only": True, "configured": False},
                )
            evidence_path = self.knowledge_dir / "k_ar_evidence.json"
            evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else {"evidence": []}
            evidence_by_id: dict[str, list[dict[str, Any]]] = {}
            for item in evidence_payload.get("evidence", []):
                evidence_by_id.setdefault(str(item.get("k_ar_id", "")), []).append(item)
            matches: list[dict[str, Any]] = []
            needle = value.casefold()
            for path in sorted(self.knowledge_dir.rglob("*.md")):
                text = path.read_text(encoding="utf-8-sig", errors="replace")
                lines = text.splitlines()
                for index, line in enumerate(lines):
                    ids = re.findall(r"K-AR-\d+", line)
                    if not ids:
                        continue
                    if k_ar_id and k_ar_id not in ids:
                        continue
                    start = max(0, index - 1)
                    end = min(len(lines), index + 3)
                    excerpt = "\n".join(lines[start:end]).strip()
                    if not k_ar_id and needle not in excerpt.casefold():
                        continue
                    for item_id in ids:
                        related = evidence_by_id.get(item_id, [])
                        matches.append(
                            {
                                "k_ar_id": item_id,
                                "excerpt": excerpt[:1200],
                                "document_path": path.relative_to(self.knowledge_dir).as_posix(),
                                "line": index + 1,
                                "evidence": related,
                                "approved": bool(related) and all(bool(item.get("approved")) for item in related),
                                "execution_enabled": False,
                            }
                        )
                        if len(matches) >= limit:
                            break
                    if len(matches) >= limit:
                        break
                if len(matches) >= limit:
                    break
            warnings = ["K-AR 조회는 읽기 전용이며 승인 상태를 변경하거나 장부 실행 규칙을 활성화하지 않습니다."]
            if not matches:
                warnings.append("일치하는 K-AR 항목을 찾지 못했습니다.")
            return LegalToolResponse(
                request_id=rid,
                status=ResponseStatus.OK,
                data={"items": matches, "approved_count": sum(1 for item in matches if item["approved"])},
                warnings=warnings,
                coverage=Coverage(queried_providers=["private-k-ar"], succeeded_providers=["private-k-ar"], fetched=len(matches)),
                freshness={"catalog_read_only": True, "configured": True, "generated_at": evidence_payload.get("generated_at")},
            )
        except (ProviderError, ValueError, OSError, json.JSONDecodeError) as exc:
            return self._error_response(rid, exc)

    def search_legal_sources(
        self,
        *,
        query: str,
        target: str | None = None,
        provider: str | None = None,
        document_type: str | None = None,
        jurisdiction: str | None = None,
        filters: Mapping[str, str] | None = None,
        page: int = 1,
        limit: int = 10,
        cursor: str | None = None,
        upstream: bool = False,
        response_type: str = "JSON",
        run_id: str | None = None,
    ) -> LegalToolResponse:
        rid = request_id()
        selected_provider = self.provider.name
        try:
            value = _validate_query(query)
            if not 1 <= limit <= 50 or page < 1:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "page는 1 이상, limit는 1~50이어야 합니다.")
            selected_provider = _provider_name(provider, self.provider.name)
            normalized_jurisdiction = jurisdiction.upper() if jurisdiction else None
            if normalized_jurisdiction and not re.fullmatch(r"[A-Z]{2}", normalized_jurisdiction):
                raise ProviderError(ErrorCode.INVALID_REQUEST, "jurisdiction은 ISO 3166-1 alpha-2 형식이어야 합니다.")
            if upstream:
                if cursor is not None:
                    if page != 1 or selected_provider == "all":
                        raise ProviderError(ErrorCode.INVALID_REQUEST, "upstream cursor는 개별 provider에서 page와 함께 지정할 수 없습니다.")
                    page = _cursor_offset(cursor)
                    if page < 1:
                        raise ProviderError(ErrorCode.INVALID_REQUEST, "upstream cursor의 page는 1 이상이어야 합니다.")
                result = self._search_upstream(
                    query=value,
                    selected_provider=selected_provider,
                    target=target,
                    filters=dict(filters or {}),
                    page=page,
                    limit=limit,
                    response_type=response_type,
                    run_id=run_id,
                )
                documents = result["documents"]
                unfiltered_count = len(documents)
                if document_type:
                    documents = [document for document in documents if document.document_type == document_type]
                if normalized_jurisdiction:
                    documents = [document for document in documents if document.jurisdiction == normalized_jurisdiction]
                filtered_count = unfiltered_count - len(documents)
                if filtered_count:
                    result["warnings"].append("upstream 결과에 document_type 또는 jurisdiction client-side 필터를 적용했습니다.")
                total = result["total"]
                next_cursor = str(page + 1) if total is not None and page * limit < total and selected_provider != "all" else None
                views = [self._metadata_view(document) for document in documents]
                return LegalToolResponse(
                    request_id=rid,
                    status=result["status"],
                    data={"items": views, "candidate_groups": self._candidate_groups(documents), "provider_errors": result["provider_errors"]},
                    sources=[_source(document) for document in documents],
                    warnings=result["warnings"],
                    pagination=Pagination(page=page, page_size=limit, total=total, next_cursor=next_cursor, truncated=result["truncated"]),
                    coverage=Coverage(
                        queried_providers=result["queried"],
                        succeeded_providers=result["succeeded"],
                        failed_providers=result["failed"],
                        disabled_providers=result["disabled"],
                        upstream_total=total,
                        fetched=len(documents),
                        filtered=filtered_count,
                        date_filter_location=result["date_filter_location"],
                        truncated=result["truncated"],
                        next_cursor=next_cursor,
                    ),
                    freshness={"retrieved_at": max((document.retrieved_at for document in documents), default=utc_now()), "upstream": True},
                    error=result["error"],
                )
            offset = _cursor_offset(cursor)
            local_provider = None if selected_provider == "all" or provider is None else selected_provider
            documents, total = self.repository.search_documents(
                value,
                provider=local_provider,
                document_type=document_type,
                jurisdiction=normalized_jurisdiction,
                limit=limit,
                offset=offset,
            )
            next_cursor = str(offset + limit) if offset + limit < total else None
            return LegalToolResponse(
                request_id=rid,
                status=ResponseStatus.OK,
                data={"items": [self._metadata_view(document) for document in documents], "candidate_groups": self._candidate_groups(documents)},
                sources=[_source(document) for document in documents],
                pagination=Pagination(page=(offset // limit) + 1, page_size=limit, total=total, next_cursor=next_cursor),
                coverage=Coverage(queried_providers=["local-legal-index"], succeeded_providers=["local-legal-index"], fetched=len(documents), next_cursor=next_cursor),
                freshness={"upstream": False},
                warnings=self._empty_index_warnings(total) + self._unapplied_filter_warnings(filters),
            )
        except (ProviderError, TransportError, ValueError, OSError) as exc:
            return self._error_response(rid, exc, provider=selected_provider if upstream else "local-legal-index")

    def get_legal_document(
        self,
        *,
        document_id: str | None = None,
        version_id: str | None = None,
        target: str | None = None,
        provider: str | None = None,
        source_document_id: str | None = None,
        source_category: str | None = None,
        attachment: bool = False,
        identifier_kind: str = "ID",
        effective_on: str | None = None,
        article: str | None = None,
        section_cursor: str | int = "0",
        max_chars: int = DEFAULT_OUTPUT_CHARS,
        refresh: bool = False,
        response_type: str = "JSON",
        run_id: str | None = None,
    ) -> LegalToolResponse:
        rid = request_id()
        selected_provider = self.provider.name
        try:
            section_index, character_offset = self._section_cursor(section_cursor)
            if not 1 <= max_chars <= MAX_OUTPUT_CHARS:
                raise ProviderError(ErrorCode.INVALID_REQUEST, f"max_chars는 1~{MAX_OUTPUT_CHARS}여야 합니다.")
            document = self._selected_document(document_id, version_id) if document_id else None
            if version_id and document is None:
                raise ProviderError(ErrorCode.NOT_FOUND, "요청한 version_id의 저장 문서가 없습니다. 다른 시행본으로 대체하지 않았습니다.")
            warnings: list[str] = []
            # article·attachment·response_type·effective_on·identifier_kind는 upstream
            # 조회에만 쓰인다. 캐시에 문서가 있으면 아래 블록을 통째로 건너뛰므로
            # 이 값들은 아무 효과가 없는데, 예전에는 그 사실을 알리지 않아 지정한
            # 쪽에서는 적용된 결과를 보고 있다고 오해했다. 받아놓고 조용히 무시하는
            # 것이 가장 나쁘다. 무엇이 반영되지 않았고 어떻게 하면 반영되는지 적는다.
            fetch_only = {
                "article": article,
                "attachment": attachment or None,
                "response_type": response_type if response_type.upper() != "JSON" else None,
                "effective_on": effective_on,
            }
            supplied = sorted(name for name, value in fetch_only.items() if value)
            if supplied and not refresh and document is not None:
                warnings.append(
                    f"{', '.join(supplied)}은(는) 원문을 새로 받아올 때만 적용됩니다. "
                    "저장된 문서를 그대로 돌려주었으므로 이 값들은 반영되지 않았습니다. "
                    "적용하려면 refresh=true로 다시 요청하십시오."
                )
            if refresh or document is None:
                selected_provider = _provider_name(provider, document.provider if document else self.provider.name)
                if selected_provider == "all":
                    raise ProviderError(ErrorCode.INVALID_REQUEST, "상세 조회는 provider 하나를 지정해야 합니다.")
                source_id = source_document_id or (document.source_document_id if document else None)
                if source_id is None:
                    raise ProviderError(ErrorCode.NOT_FOUND, "로컬 문서가 없으며 상세 조회 식별자가 제공되지 않았습니다.")
                if selected_provider == self.provider.name:
                    target_value = target or (str(document.metadata.get("target")) if document and document.metadata.get("target") else None)
                    if target_value is None:
                        raise ProviderError(ErrorCode.INVALID_REQUEST, "법제처 상세 조회에는 target이 필요합니다.")
                    if document is not None and source_document_id is None:
                        upstream_ids = document.metadata.get("upstream_identifiers") or {}
                        requested_mst = document.metadata.get("requested_mst")
                        known_mst = upstream_ids.get("MST") if isinstance(upstream_ids, Mapping) else None
                        pinned_mst = requested_mst or (
                            document.version_id if isinstance(known_mst, list) and document.version_id in known_mst else None
                        ) if version_id or requested_mst else None
                        if pinned_mst and identifier_kind.upper() == "ID":
                            identifier_kind = "MST"
                        candidates = upstream_ids.get(identifier_kind.upper()) if isinstance(upstream_ids, Mapping) else None
                        if pinned_mst and identifier_kind.upper() == "MST":
                            candidates = [pinned_mst]
                        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], str) or not candidates[0]:
                            raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "저장된 검색 항목에서 요청 식별자 종류를 유일하게 확인할 수 없습니다. 공식 ID 또는 MST를 명시하십시오.")
                        source_id = candidates[0]
                    try:
                        _, documents, reused = self._collect_detail(
                            LegalTarget(target_value),
                            identifier=source_id,
                            identifier_kind=identifier_kind,
                            effective_on=effective_on,
                            article=article,
                            response_type=response_type,
                            run_id=run_id,
                        )
                    except ProviderError as exc:
                        if not exc.details.get("precedent_body_unavailable"):
                            raise
                        documents, reused = self._nts_precedent_body(source_id, exc, run_id), False
                        warnings.append(
                            f"법제처 판례 {source_id}는 국세청 출처라 법제처 API에 본문이 없습니다. "
                            f"같은 사건번호의 국세청 원문({documents[0].document_id})을 반환했습니다. 인용은 이 document_id로 하십시오."
                        )
                else:
                    adapter = self.nts_provider if selected_provider == self.nts_provider.name else self.olta_provider
                    category = source_category or (str(document.metadata.get("category")) if document and document.metadata.get("category") else None)
                    action = "attachment" if attachment or (document is not None and document.document_type == "nts_attachment") else "detail"
                    _, documents, reused = self._collect_supplemental_detail(
                        adapter,
                        action=action,
                        source_document_id=source_id,
                        category=category,
                        run_id=run_id,
                    )
                if not documents:
                    raise ProviderError(ErrorCode.NOT_FOUND, "상세 응답에서 문서를 찾지 못했습니다.")
                document = documents[0]
                if version_id and document.version_id != version_id:
                    raise ProviderError(ErrorCode.TEMPORAL_UNRESOLVED, "상세 응답이 요청한 version_id와 일치하지 않습니다.")
                if reused:
                    warnings.append("같은 수집 창의 완료 run을 재사용했습니다.")
            if document is None:
                raise ProviderError(ErrorCode.NOT_FOUND, "법률 문서를 찾지 못했습니다.")
            if document.metadata.get("version_identity_source") == "request_mst":
                warnings.append("공식 응답에 MST가 없어 요청한 MST를 버전 식별자로 보존했습니다. 적용 시점과 버전 링크를 별도로 확인하십시오.")
            original_incomplete = (
                document.retrieval_status != RetrievalStatus.SUCCESS
                or document.content_completeness != ContentCompleteness.COMPLETE
                or not document.sections
                or incomplete_law_articles(document)
            )
            if original_incomplete:
                warnings.append("검색 메타데이터나 불완전한 원문만 저장되어 있습니다. 원문이 필요하면 공식 식별자를 확인하고 refresh=true로 다시 조회하십시오.")
            sections, next_cursor, truncated = self._page_sections(document.sections, section_index, character_offset, max_chars)
            data = document.model_dump(mode="json")
            data["sections"] = [section.model_dump(mode="json") for section in sections]
            if document.provider == "law.go.kr" and document.document_type == "law":
                data["temporal_status"] = TemporalStatus.UNRESOLVED.value
            if original_incomplete and data["content_completeness"] == ContentCompleteness.COMPLETE.value:
                data["content_completeness"] = ContentCompleteness.PARTIAL.value
            return LegalToolResponse(
                request_id=rid,
                status=ResponseStatus.PARTIAL if original_incomplete else ResponseStatus.OK,
                data=data,
                sources=[_source(document, section.locator) for section in sections] or [_source(document)],
                warnings=warnings,
                pagination=Pagination(page=section_index + 1, page_size=len(sections), total=len(document.sections), next_cursor=next_cursor, truncated=truncated),
                coverage=Coverage(queried_providers=[document.provider], succeeded_providers=[document.provider], fetched=1, truncated=truncated, next_cursor=next_cursor),
                freshness={"retrieved_at": document.retrieved_at},
            )
        except (ProviderError, TransportError, ValueError, OSError) as exc:
            return self._error_response(rid, exc, provider=selected_provider)

    def _selected_document(self, document_id: str, version_id: str | None) -> LegalDocument | None:
        try:
            return self.repository.get_document(document_id, version_id=version_id)
        except AmbiguousDocumentVersion as exc:
            raise ProviderError(
                ErrorCode.TEMPORAL_UNRESOLVED, str(exc), details={"available_version_ids": exc.versions}
            ) from exc

    def get_applicable_law(
        self,
        *,
        effective_on: str,
        mst: str | None = None,
        law_id: str | None = None,
        document_id: str | None = None,
        version_id: str | None = None,
        refresh: bool = False,
    ) -> LegalToolResponse:
        rid = request_id()
        try:
            if bool(mst) == bool(law_id) and document_id is None:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "MST 또는 ID 중 하나만 제공해야 합니다.")
            if version_id and not document_id:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "version_id에는 document_id가 필요합니다.")
            warnings: list[str] = []
            documents: list[LegalDocument] = []
            if document_id:
                document = self._selected_document(document_id, version_id)
                if version_id and document is None:
                    raise ProviderError(ErrorCode.NOT_FOUND, "요청한 version_id의 저장 문서가 없습니다.")
                if document:
                    documents.append(document)
            if refresh or not documents:
                identifier = mst or law_id
                if version_id and mst and mst != version_id:
                    raise ProviderError(ErrorCode.INVALID_REQUEST, "mst와 version_id가 서로 다릅니다.")
                if identifier is None and documents and version_id:
                    selected = documents[0]
                    upstream_ids = selected.metadata.get("upstream_identifiers") or {}
                    known_mst = upstream_ids.get("MST") if isinstance(upstream_ids, Mapping) else None
                    if selected.metadata.get("requested_mst") == version_id or (
                        isinstance(known_mst, list) and version_id in known_mst
                    ):
                        identifier = mst = version_id
                if identifier is None:
                    # document_id를 분명히 준 사람에게 "식별자가 없다"고 하면
                    # 무엇을 잘못했는지 알 수 없다. document_id는 우리 저장소의
                    # 이름이라 법제처에 그대로 물을 수 없다는 것이 실제 이유다.
                    raise ProviderError(
                        ErrorCode.NOT_FOUND,
                        "저장소에 그 문서가 없고, document_id만으로는 법제처에 시점 조회를 할 수 없습니다. "
                        "search_legal_sources로 법령을 찾아 mst(일련번호) 또는 law_id(법령ID)를 확인한 뒤 그 값으로 요청하십시오."
                        if document_id
                        else "시점 조회에는 mst(일련번호) 또는 law_id(법령ID)가 필요합니다.",
                        details={"provided": sorted(name for name, value in (("document_id", document_id), ("mst", mst), ("law_id", law_id)) if value)},
                    )
                if law_id and not mst:
                    raise ProviderError(ErrorCode.TEMPORAL_UNRESOLVED, "법령 ID 방식은 기준일 efYd를 적용하지 않습니다. 해당 시행 버전 MST를 찾아 다시 요청하십시오.")
                kind = "MST" if mst else "ID"
                _, documents, _ = self._collect_detail(LegalTarget.EFFECTIVE_LAW, identifier=identifier, identifier_kind=kind, effective_on=effective_on)
                if version_id and any(document.version_id != version_id for document in documents):
                    raise ProviderError(ErrorCode.TEMPORAL_UNRESOLVED, "시점 조회 결과가 요청한 version_id와 일치하지 않습니다.")
            candidates, temporal_warnings = temporal_candidates(documents, effective_on)
            warnings.extend(temporal_warnings)
            if law_id and not mst:
                candidates = [document.model_copy(update={"temporal_status": TemporalStatus.UNRESOLVED, "review_state": ReviewState.NEEDS_REVIEW}) for document in documents]
            incomplete_candidates = [
                document
                for document in candidates
                if document.retrieval_status != RetrievalStatus.SUCCESS
                or document.content_completeness != ContentCompleteness.COMPLETE
                or bool(document.metadata.get("preview_only"))
                or incomplete_law_articles(document)
            ]
            addenda_confirmed = bool(candidates) and not incomplete_candidates and all(
                any(section.kind == "addenda" and section.text.strip() for section in document.sections)
                for document in candidates
            )
            if incomplete_candidates:
                warnings.append("완전한 원문을 확보하지 못한 후보가 있어 부칙 확인을 완료할 수 없습니다.")
            status = ResponseStatus.OK if candidates else ResponseStatus.PARTIAL
            candidate_views = [
                self._metadata_view(
                    document.model_copy(update={"content_completeness": ContentCompleteness.PARTIAL})
                    if incomplete_law_articles(document) else document
                )
                for document in candidates
            ]
            return LegalToolResponse(
                request_id=rid,
                status=status,
                data={"effective_on": effective_on, "candidates": candidate_views, "addenda_confirmed": addenda_confirmed, "transaction_applicability_confirmed": False},
                sources=[_source(document) for document in candidates],
                warnings=warnings,
                coverage=Coverage(queried_providers=[self.provider.name], succeeded_providers=[self.provider.name] if candidates else [], failed_providers=[] if candidates else [self.provider.name], fetched=len(candidates)),
                freshness={"retrieved_at": max((document.retrieved_at for document in candidates), default=None)},
                error=None if candidates else ToolError(code=ErrorCode.TEMPORAL_UNRESOLVED, message="확인된 시행 버전 후보가 없습니다."),
            )
        except (ProviderError, TransportError, ValueError, OSError) as exc:
            return self._error_response(rid, exc, provider=self.provider.name)

    def verify_legal_citations(self, *, citations: Iterable[Mapping[str, Any]]) -> LegalToolResponse:
        rid = request_id()
        try:
            inputs = [CitationInput.model_validate(item) for item in citations]
            checks = verify_citations(inputs, self.repository.get_document)
            sources = []
            for item, check in zip(inputs, checks):
                if not check.document_exists or (check.status == CitationStatus.UNVERIFIED and check.version_id is None):
                    continue
                document = self.repository.get_document(item.document_id, version_id=check.version_id, locator=item.locator)
                if document:
                    sources.append(_source(document))
            unresolved = [
                check
                for check in checks
                if check.status in {CitationStatus.MISMATCH, CitationStatus.UNVERIFIED}
            ]
            return LegalToolResponse(
                request_id=rid,
                status=ResponseStatus.PARTIAL if unresolved else ResponseStatus.OK,
                data={"checks": [check.model_dump(mode="json") for check in checks], "legal_conclusion_verified": False},
                sources=sources,
                warnings=["인용 확인은 문서 존재·메타데이터·문구 일치 검사이며 법률 결론의 타당성 검증이 아닙니다."],
                coverage=Coverage(queried_providers=["local-legal-index"], succeeded_providers=["local-legal-index"], fetched=len(checks), filtered=len(unresolved)),
            )
        except (ValueError, TypeError) as exc:
            return self._error_response(rid, ProviderError(ErrorCode.INVALID_REQUEST, f"인용 입력이 올바르지 않습니다: {type(exc).__name__}"))

    def doctor(self) -> LegalToolResponse:
        rid = request_id()
        try:
            auth_names = (
                "TAXAX_MCP_AUTH_ISSUER",
                "TAXAX_MCP_AUTH_AUDIENCE",
                "TAXAX_MCP_AUTH_RESOURCE",
                "TAXAX_MCP_AUTH_JWKS_URL",
            )
            auth_presence = {name: bool(os.environ.get(name, "").strip()) for name in auth_names}
            auth_count = sum(auth_presence.values())
            auth_valid = False
            if auth_count == len(auth_names):
                try:
                    from taxax.mcp.auth import JwtAuthConfiguration

                    auth_valid = JwtAuthConfiguration.from_environment(required=True) is not None
                except ValueError:
                    auth_valid = False
            checks = {
                "python_3_11_or_newer": sys.version_info >= (3, 11),
                "public_legal_repository_ready": self.repository.source_status()["database_schema_version"] >= 1,
                "stdio_ready": True,
                "law_go_operator_credential_configured": bool(self.provider.credential),
                "law_go_credential_file_safe": self.provider.credential_error is None,
                "nts_enabled": bool(self.nts_provider.enabled),
                "olta_enabled": bool(self.olta_provider.enabled),
                "hosted_auth_complete": auth_valid,
                "hosted_auth_partial": 0 < auth_count < len(auth_names) or (auth_count == len(auth_names) and not auth_valid),
                "host_allowlist_configured": bool(os.environ.get("TAXAX_MCP_ALLOWED_HOSTS", "").strip()),
                "origin_allowlist_configured": bool(os.environ.get("TAXAX_MCP_ALLOWED_ORIGINS", "").strip()),
                "korean_law_bridge_enabled": bool(self.bridge.enabled),
            }
            failures = [name for name in ("python_3_11_or_newer", "public_legal_repository_ready") if not checks[name]]
            warnings = [
                "doctor는 설정과 로컬 저장소만 확인하며 공식 API·NTS·OLTA 실호출은 수행하지 않습니다.",
                "일반 직원용 hosted 연결에서는 중앙 운영 서버만 provider credential을 보관해야 합니다.",
            ]
            if checks["hosted_auth_partial"]:
                warnings.append("hosted auth 설정이 일부만 제공돼 non-loopback HTTP는 시작되지 않습니다.")
            if not checks["law_go_credential_file_safe"]:
                warnings.append("법제처 secret 파일 권한 또는 형식 검증에 실패해 credential을 사용하지 않습니다.")
            if not checks["law_go_operator_credential_configured"]:
                # 진단 도구가 "안 된다"만 말하고 끝나면 사용자는 다음에 뭘 할지 모른다.
                warnings.append("법제처 upstream은 credential이 없어 비활성이나 offline 조회와 stdio는 사용할 수 있습니다.")
                warnings.append(
                    "https://open.law.go.kr 에서 OC 인증키를 무료로 발급받은 뒤 "
                    "`taxax-legal install --oc <키>`로 등록하면 공식 원문 조회가 켜집니다. "
                    "명령을 찾지 못하면 `python -m taxax.legal install --oc <키>`를 쓰십시오."
                )
            if checks["law_go_operator_credential_configured"] and self.repository.source_status()["documents"] == 0:
                warnings.append(
                    "로컬 법률 색인이 비어 있습니다. `taxax-legal collect-seeds`"
                    "(또는 `python -m taxax.legal collect-seeds`)로 채우면 upstream 없이도 조회할 수 있습니다."
                )
            return LegalToolResponse(
                request_id=rid,
                status=ResponseStatus.ERROR if failures else ResponseStatus.OK,
                data={
                    "distribution": "taxax-legal-mcp",
                    "version": package_version(),
                    "checks": checks,
                    "ready": {
                        "stdio": not failures,
                        "authenticated_http": not failures
                        and checks["hosted_auth_complete"]
                        and checks["host_allowlist_configured"]
                        and checks["origin_allowlist_configured"],
                    },
                    "operational_api_calls": "not_run",
                },
                warnings=warnings,
                coverage=Coverage(
                    queried_providers=["local-diagnostics"],
                    succeeded_providers=[] if failures else ["local-diagnostics"],
                    failed_providers=["local-diagnostics"] if failures else [],
                ),
                error=ToolError(code=ErrorCode.UPSTREAM_UNAVAILABLE, message="로컬 진단 필수 항목이 실패했습니다.", details={"checks": failures}) if failures else None,
            )
        except (OSError, sqlite3.Error) as exc:
            return self._error_response(rid, exc, provider="local-diagnostics")

    def get_source_status(self) -> LegalToolResponse:
        rid = request_id()
        try:
            legacy = (
                self.snapshots.read_legacy_manifest(self.legacy_snapshot_root)
                if self.legacy_snapshot_root is not None
                else []
            )
            return LegalToolResponse(
                request_id=rid,
                status=ResponseStatus.OK,
                data={
                    "official": self.provider.capabilities(),
                    "supplemental": {
                        self.nts_provider.name: self.nts_provider.capabilities(),
                        self.olta_provider.name: self.olta_provider.capabilities(),
                    },
                    "repository": self.repository.source_status(),
                    "legacy_snapshot_records_read_only": len(legacy),
                    "legacy_snapshot_source_configured": self.legacy_snapshot_root is not None,
                    "private_knowledge_configured": self.knowledge_dir is not None,
                    "bridge": self.bridge.status(),
                    "layers": {"L1": "implemented_fixture_verified", "L2": "implemented_fixture_verified", "L3": "implemented_fixture_verified"},
                    "operational_validation": "not_run",
                },
                warnings=[
                    "fixture 검증과 공식 API 실호출·운영 배포 검증은 별도입니다.",
                    "NTS/OLTA는 명시적 opt-out이 없으면 제한 질의를 사용합니다.",
                    "조사 보고서는 공개 원문 cache와 분리된 인증 scope 저장소를 사용합니다.",
                ],
                coverage=Coverage(
                    queried_providers=[self.provider.name, self.nts_provider.name, self.olta_provider.name, "private-k-ar"],
                    succeeded_providers=[] if self.knowledge_dir is None else ["private-k-ar"],
                    disabled_providers=[
                        *([] if self.knowledge_dir is not None else ["private-k-ar"]),
                        *([] if self.nts_provider.enabled else [self.nts_provider.name]),
                        *([] if self.olta_provider.enabled else [self.olta_provider.name]),
                        *([] if self.bridge.enabled else ["korean-law-mcp-bridge"]),
                    ],
                ),
            )
        except (OSError, json.JSONDecodeError) as exc:
            return self._error_response(rid, exc)

    def collect_seed_profile(self, *, run_id_prefix: str | None = None) -> LegalToolResponse:
        rid = request_id()
        successes: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for group in LAW_GROUPS:
            name = str(group["law_name"])
            response = self.search_legal_sources(query=name, target=LegalTarget.LAW.value, upstream=True, limit=10, run_id=f"{run_id_prefix}-{group['law_id']}-law" if run_id_prefix else None)
            if response.status == ResponseStatus.OK:
                successes.append({"seed": group["law_id"], "action": "law", "count": response.coverage.fetched})
            else:
                failures.append({"seed": group["law_id"], "action": "law", "error": response.error.model_dump(mode="json") if response.error else None})
            history = self.search_legal_sources(query=name, target=LegalTarget.LAW_HISTORY.value, upstream=True, limit=10, run_id=f"{run_id_prefix}-{group['law_id']}-history" if run_id_prefix else None)
            if history.status == ResponseStatus.OK:
                successes.append({"seed": group["law_id"], "action": "history", "count": history.coverage.fetched})
            else:
                failures.append({"seed": group["law_id"], "action": "history", "error": history.error.model_dump(mode="json") if history.error else None})
        for spec in FORM_SPECS:
            response = self.search_legal_sources(query=str(spec["title_marker"]), target=LegalTarget.LAW_ATTACHMENT.value, upstream=True, limit=10, run_id=f"{run_id_prefix}-{spec['source_id']}" if run_id_prefix else None)
            if response.status == ResponseStatus.OK:
                successes.append({"seed": spec["source_id"], "action": "attachment-list", "count": response.coverage.fetched})
            else:
                failures.append({"seed": spec["source_id"], "action": "attachment-list", "error": response.error.model_dump(mode="json") if response.error else None})
        nts_attempted = False
        if self.nts_provider.enabled:
            nts_attempted = True
            for spec in NTS_SPECS:
                response = self.get_legal_document(
                    provider=self.nts_provider.name,
                    source_document_id=str(spec["document_id"]),
                    refresh=True,
                    run_id=f"{run_id_prefix}-{spec['source_id']}" if run_id_prefix else None,
                )
                if response.status == ResponseStatus.OK:
                    successes.append({"seed": spec["source_id"], "action": "nts-detail", "count": 1})
                else:
                    failures.append({"seed": spec["source_id"], "action": "nts-detail", "error": response.error.model_dump(mode="json") if response.error else None})
        queried = [self.provider.name, *([self.nts_provider.name] if nts_attempted else [])]
        failed_providers = sorted({self.provider.name if item["action"] != "nts-detail" else self.nts_provider.name for item in failures})
        return LegalToolResponse(
            request_id=rid,
            status=ResponseStatus.PARTIAL if failures else ResponseStatus.OK,
            data={
                "successes": successes,
                "failures": failures,
                "nts_seed_status": "attempted" if nts_attempted else "explicitly_disabled",
                "nts_seed_count": len(NTS_SPECS),
            },
            warnings=[
                "부칙·별표·서식 목록 확보와 실제 첨부 파일·추출 성공은 서로 다른 상태입니다.",
                "NTS 상세 seed는 명시적으로 비활성화하지 않은 경우 6건으로 제한해 조회합니다.",
            ],
            coverage=Coverage(
                queried_providers=queried,
                succeeded_providers=[name for name in queried if name not in failed_providers],
                failed_providers=failed_providers,
                disabled_providers=[] if nts_attempted else [self.nts_provider.name],
                fetched=sum(item["count"] for item in successes),
            ),
        )

    def _search_upstream(
        self,
        *,
        query: str,
        selected_provider: str,
        target: str | None,
        filters: dict[str, str],
        page: int,
        limit: int,
        response_type: str,
        run_id: str | None,
    ) -> dict[str, Any]:
        selected: list[tuple[str, Any]] = []
        warnings: list[str] = []
        if selected_provider in {self.provider.name, "all"}:
            if target is None and selected_provider == self.provider.name:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "법제처 upstream 검색에는 target이 필요합니다.")
            if target is not None:
                selected.append((self.provider.name, self.provider))
            elif selected_provider == "all":
                warnings.append("target이 없어 provider=all 검색에서 법제처 API는 조회하지 않았습니다.")
        if selected_provider in {self.nts_provider.name, "all"}:
            selected.append((self.nts_provider.name, self.nts_provider))
        if selected_provider in {self.olta_provider.name, "all"}:
            selected.append((self.olta_provider.name, self.olta_provider))
        if not selected:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "조회할 upstream provider가 없습니다.")
        if selected_provider == "all":
            allowed = set().union(
                CONTRACTS[LegalTarget(target)].allowed_search_filters if target else frozenset(),
                {"collections", "date_from", "date_to", "date_base", "sort", "max_pages", "category"},
            )
            unknown = sorted(set(filters) - allowed)
            if unknown:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "지원하지 않는 통합검색 filter입니다.", details={"filters": unknown})

        documents: list[LegalDocument] = []
        queried: list[str] = []
        succeeded: list[str] = []
        failed: list[str] = []
        disabled: list[str] = []
        provider_errors: list[dict[str, Any]] = []
        totals: list[int | None] = []
        date_locations: list[str] = []
        truncated = False
        for provider_name, adapter in selected:
            queried.append(provider_name)
            scoped_run_id = run_id
            if run_id and selected_provider == "all":
                scoped_run_id = f"{run_id}-{provider_name.replace('.', '-')}"
            try:
                if provider_name == self.provider.name:
                    legal_target = LegalTarget(target)
                    law_filters = filters
                    if selected_provider == "all":
                        law_filters = {key: value for key, value in filters.items() if key in CONTRACTS[legal_target].allowed_search_filters}
                    result, found, reused = self._collect_search(
                        legal_target,
                        query=query,
                        filters=law_filters,
                        page=page,
                        display=limit,
                        response_type=response_type,
                        run_id=scoped_run_id,
                    )
                    date_location = "upstream" if CONTRACTS[legal_target].date_filter in law_filters else "none"
                    is_partial = False
                else:
                    kwargs = self._supplemental_search_kwargs(provider_name, filters, selected_provider != "all")
                    result, found, reused = self._collect_supplemental_search(
                        adapter,
                        query=query,
                        page=page,
                        display=limit,
                        run_id=scoped_run_id,
                        **kwargs,
                    )
                    date_location = result.date_filter_location
                    is_partial = result.partial
                documents.extend(found)
                totals.append(result.total)
                date_locations.append(date_location)
                succeeded.append(provider_name)
                truncated = truncated or is_partial
                warnings.extend(f"[{provider_name}] {warning}" for warning in result.warnings)
                if reused:
                    warnings.append(f"[{provider_name}] 같은 수집 창의 완료 run을 재사용했습니다.")
            except (ProviderError, TransportError) as exc:
                if selected_provider != "all":
                    raise
                code = getattr(exc, "code", ErrorCode.UPSTREAM_UNAVAILABLE)
                target_list = disabled if code == ErrorCode.POLICY_DISABLED else failed
                target_list.append(provider_name)
                provider_errors.append({
                    "provider": provider_name,
                    "code": code.value,
                    "message": str(exc),
                    "retryable": bool(getattr(exc, "retryable", False)),
                })
                warnings.append(f"[{provider_name}] 조회 실패: {str(exc)}")

        total = None if any(value is None for value in totals) else sum(value or 0 for value in totals)
        date_filter_location = "client" if "client" in date_locations else "upstream" if "upstream" in date_locations else "none"
        error = None
        if succeeded:
            status = ResponseStatus.PARTIAL if failed or disabled or truncated else ResponseStatus.OK
        elif disabled and not failed:
            status = ResponseStatus.BLOCKED
            error = ToolError(code=ErrorCode.POLICY_DISABLED, message="선택한 보완 출처가 운영 정책에서 비활성입니다.", details={"providers": disabled})
        else:
            status = ResponseStatus.ERROR
            error = ToolError(code=ErrorCode.UPSTREAM_UNAVAILABLE, message="선택한 upstream 출처를 조회하지 못했습니다.", retryable=True, details={"providers": failed})
        return {
            "documents": documents,
            "total": total,
            "warnings": warnings,
            "provider_errors": provider_errors,
            "queried": queried,
            "succeeded": succeeded,
            "failed": failed,
            "disabled": disabled,
            "truncated": truncated,
            "date_filter_location": date_filter_location,
            "status": status,
            "error": error,
        }

    @staticmethod
    def _supplemental_search_kwargs(provider_name: str, filters: Mapping[str, str], strict: bool) -> dict[str, Any]:
        if provider_name == "taxlaw.nts.go.kr":
            allowed = {"collections", "date_from", "date_to", "date_base", "sort", "max_pages"}
            unknown = sorted(set(filters) - allowed)
            if strict and unknown:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "지원하지 않는 NTS filter입니다.", details={"filters": unknown})
            values: dict[str, Any] = {
                "collections": ("ruling", "precedent"),
                "date_from": None,
                "date_to": None,
                "date_base": "DCM_RGT_DTM",
                "sort": "relevance",
                "max_pages": 3,
            }
            if "collections" in filters:
                values["collections"] = tuple(value.strip() for value in filters["collections"].split(",") if value.strip())
            for key in ("date_from", "date_to", "date_base", "sort"):
                if key in filters:
                    values[key] = filters[key]
            if "max_pages" in filters:
                if not re.fullmatch(r"\d+", filters["max_pages"]):
                    raise ProviderError(ErrorCode.INVALID_REQUEST, "NTS max_pages는 1~10의 정수여야 합니다.")
                values["max_pages"] = int(filters["max_pages"])
            return values
        if provider_name == "olta.re.kr":
            allowed = {"category", "date_from", "date_to", "sort"}
            unknown = sorted(set(filters) - allowed)
            if strict and unknown:
                raise ProviderError(ErrorCode.INVALID_REQUEST, "지원하지 않는 OLTA filter입니다.", details={"filters": unknown})
            values: dict[str, Any] = {"category": None, "date_from": None, "date_to": None, "sort": "relevance"}
            values.update({key: filters[key] for key in allowed if key in filters})
            return values
        raise ProviderError(ErrorCode.INVALID_REQUEST, "지원하지 않는 보완 provider입니다.")

    def _nts_precedent_body(self, law_go_id: str, cause: ProviderError, run_id: str | None) -> list[LegalDocument]:
        """법제처에 본문이 없는 국세청 출처 판례를 사건번호로 국세청에서 받는다.

        사건번호(예: 수원고등법원-2025-누-688)가 국세청 문서번호와 정확히 같은
        판례 하나만 받아들인다. 비슷한 사건으로 대체하지 않는다.
        """
        stored = self.repository.get_document(f"law-go:{LegalTarget.PRECEDENT.value}:{law_go_id}")
        case_no = stored.case_no if stored else None
        parts = re.fullmatch(r".+?-(\d{4})-([가-힣]+)-(\d+)", case_no or "")
        if not self.nts_provider.enabled or not parts:
            reason = "국세청 출처가 비활성입니다" if parts else "저장된 검색 결과에서 국세청 형식 사건번호를 찾지 못했습니다"
            raise ProviderError(
                cause.code,
                f"{cause} {reason}. provider=taxlaw.nts.go.kr, target=precedent로 사건번호를 검색해 원문을 조회하십시오.",
                details=cause.details,
            )
        _, found, _ = self._collect_supplemental_search(
            self.nts_provider, query="".join(parts.groups()), page=1, display=10,
            run_id=f"{run_id}-nts-search" if run_id else None,
            **self._supplemental_search_kwargs(self.nts_provider.name, {"collections": "precedent"}, True),
        )
        matches = [document for document in found if document.metadata.get("document_number") == case_no]
        if len(matches) != 1:
            raise ProviderError(
                cause.code,
                f"{cause} 국세청에서 사건번호 {case_no}와 정확히 일치하는 판례를 {len(matches)}건 찾았습니다. 원문으로 대체하지 않았습니다.",
                details=cause.details,
            )
        _, documents, _ = self._collect_supplemental_detail(
            self.nts_provider, action="detail", source_document_id=matches[0].source_document_id, run_id=run_id,
        )
        return documents

    def _collect_supplemental_search(self, adapter, **kwargs) -> tuple[SupplementalResult, list[LegalDocument], bool]:
        run_id = kwargs.pop("run_id", None)
        request = {**kwargs, "parser_version": adapter.capabilities()["parser_version"], "collection_window": utc_now()[:10]}
        started = utc_now()
        run = self.repository.start_run(
            provider=adapter.name,
            action="search",
            request=request,
            started_at=started,
            run_id=run_id,
            resume=True,
        )
        if run["status"] == "completed":
            documents = self.repository.documents_for_run(run["run_id"])
            metadata = self.repository.collection_result(run["run_id"]) or {}
            cached = SupplementalResult(
                provider=adapter.name,
                document_type=str(metadata.get("document_type") or "supplemental_search_result"),
                request_parameters={key: value for key, value in request.items() if key != "collection_window"},
                response=self._cached_response(adapter.name),
                parsed={},
                items=[],
                total=metadata.get("total", len(documents)),
                page=int(metadata.get("page", kwargs.get("page", 1))),
                page_size=int(metadata.get("page_size", kwargs.get("display", 10))),
                warnings=tuple(metadata.get("warnings", [])),
                partial=bool(metadata.get("partial", False)),
                date_filter_location=str(metadata.get("date_filter_location", "none")),
            )
            return cached, documents, True
        if not run["claimed"]:
            raise ProviderError(ErrorCode.UPSTREAM_UNAVAILABLE, "같은 보완 출처 검색이 이미 실행 중입니다.", retryable=True)
        try:
            query = kwargs.pop("query")
            display = kwargs.pop("display")
            result = adapter.search(query, display=display, **kwargs)
            documents = self._persist_supplemental_result(result, run["run_id"], started)
            run_status = "partial" if result.partial else "completed"
            self.repository.save_collection_result(run["run_id"], {
                "document_type": result.document_type,
                "total": result.total,
                "page": result.page,
                "page_size": result.page_size,
                "warnings": list(result.warnings),
                "partial": result.partial,
                "date_filter_location": result.date_filter_location,
            })
            self.repository.finish_run(run["run_id"], status=run_status, finished_at=utc_now())
            if run_status == "completed":
                self.snapshots.save_run_manifest(run["run_id"], {
                    "run_id": run["run_id"],
                    "status": run_status,
                    "request": result.request_parameters,
                    "document_ids": [document.document_id for document in documents],
                })
            return result, documents, False
        except (ProviderError, TransportError, ValueError, OSError) as exc:
            code = getattr(exc, "code", ErrorCode.UPSTREAM_UNAVAILABLE)
            self.repository.finish_run(run["run_id"], status="failed", finished_at=utc_now(), error_code=code.value, error_message=str(exc))
            raise

    def _collect_supplemental_detail(self, adapter, *, action: str, source_document_id: str, category: str | None = None, run_id: str | None = None) -> tuple[SupplementalResult, list[LegalDocument], bool]:
        request = {
            "source_document_id": source_document_id,
            "category": category,
            "parser_version": adapter.capabilities()["parser_version"],
            "collection_window": utc_now()[:10],
        }
        started = utc_now()
        run = self.repository.start_run(provider=adapter.name, action=action, request=request, started_at=started, run_id=run_id, resume=True)
        if run["status"] == "completed":
            documents = self.repository.documents_for_run(run["run_id"])
            cached = SupplementalResult(adapter.name, documents[0].document_type if documents else "supplemental_document", request, self._cached_response(adapter.name), {}, [], len(documents), 1, len(documents))
            return cached, documents, True
        if not run["claimed"]:
            raise ProviderError(ErrorCode.UPSTREAM_UNAVAILABLE, "같은 보완 출처 상세 수집이 이미 실행 중입니다.", retryable=True)
        try:
            if action == "attachment":
                result = adapter.attachment(source_document_id)
            elif adapter.name == self.olta_provider.name:
                result = adapter.detail(category, source_document_id)
            else:
                result = adapter.detail(source_document_id)
            documents = self._persist_supplemental_result(result, run["run_id"], started)
            self.repository.save_collection_result(run["run_id"], {"document_type": result.document_type, "total": result.total, "page": 1, "page_size": len(documents), "partial": False, "date_filter_location": "none"})
            self.repository.finish_run(run["run_id"], status="completed", finished_at=utc_now())
            self.snapshots.save_run_manifest(run["run_id"], {"run_id": run["run_id"], "status": "completed", "request": result.request_parameters, "document_ids": [document.document_id for document in documents]})
            return result, documents, False
        except (ProviderError, TransportError, ValueError, OSError) as exc:
            code = getattr(exc, "code", ErrorCode.UPSTREAM_UNAVAILABLE)
            self.repository.finish_run(run["run_id"], status="failed", finished_at=utc_now(), error_code=code.value, error_message=str(exc))
            raise

    def _persist_supplemental_result(self, result: SupplementalResult, run_id: str, retrieved_at: str) -> list[LegalDocument]:
        raw_responses = result.raw_responses or (result.response,)
        primary_index = next((index for index, response in enumerate(raw_responses) if response is result.response), len(raw_responses) - 1)
        item_indexes: list[int] = []
        for item in result.items:
            metadata = item.get("metadata") or {}
            index = metadata.get("raw_response_index", primary_index)
            item_indexes.append(index if isinstance(index, int) and 0 <= index < len(raw_responses) else primary_index)
        documents: list[LegalDocument] = []
        fingerprint = self.repository.fingerprint(result.request_parameters)[:16]
        parser_version = str(
            result.request_parameters.get("parser_version")
            or next((item.get("metadata", {}).get("parser_version") for item in result.items if item.get("metadata", {}).get("parser_version")), None)
            or "supplemental-v1"
        )
        for index, response in enumerate(raw_responses):
            assigned = [item for item, item_index in zip(result.items, item_indexes) if item_index == index]
            completeness = self._items_completeness(assigned)
            source_key = str(assigned[0]["source_document_id"]) if len(assigned) == 1 else f"search-{fingerprint}-part-{index + 1}"
            snapshot = self.snapshots.save(
                provider=result.provider,
                document_type=result.document_type,
                source_document_id=source_key,
                response=response,
                parsed=self._supplemental_parsed_part(result, index, response),
                retrieved_at=retrieved_at,
                run_id=run_id,
                completeness=completeness,
                parser_version=parser_version,
            )
            current = [self._supplemental_document(result.provider, item, snapshot, run_id, retrieved_at) for item in assigned]
            self.repository.save_result(snapshot, current)
            documents.extend(current)
        return documents

    @staticmethod
    def _supplemental_parsed_part(result: SupplementalResult, index: int, response) -> Any:
        pages = result.parsed.get("pages") if isinstance(result.parsed, Mapping) else None
        if isinstance(pages, list) and index < len(pages):
            return pages[index]
        if response is result.response:
            return result.parsed
        return {"transport_response_index": index, "normalized_document_count": 0}

    @staticmethod
    def _items_completeness(items: list[Mapping[str, Any]]) -> ContentCompleteness:
        if not items:
            return ContentCompleteness.UNKNOWN
        if any(bool((item.get("metadata") or {}).get("preview_only")) for item in items):
            return ContentCompleteness.PARTIAL if any(item.get("sections") for item in items) else ContentCompleteness.METADATA_ONLY
        return ContentCompleteness.COMPLETE if all(item.get("sections") for item in items) else ContentCompleteness.METADATA_ONLY

    def _supplemental_document(self, provider_name: str, item: Mapping[str, Any], snapshot, run_id: str, retrieved_at: str) -> LegalDocument:
        source_id = str(item["source_document_id"])
        document_type = str(item.get("document_type") or "supplemental_public_document")
        metadata = dict(item.get("metadata") or {})
        official_url = self._safe_source_url(provider_name, item.get("official_url"))
        content_hash = self._item_content_hash(item)
        return LegalDocument(
            provider=provider_name,
            document_type=document_type,
            source_document_id=source_id,
            document_id=f"{provider_name}:{document_type}:{source_id}",
            version_id=item.get("version_id"),
            title=str(item["title"]),
            issuer=item.get("issuer"),
            court=item.get("court"),
            case_no=item.get("case_no"),
            document_number=metadata.get("document_number"),
            tax_type=item.get("tax_type"),
            promulgated_on=item.get("promulgated_on"),
            effective_from=item.get("effective_from"),
            effective_to=item.get("effective_to"),
            decided_on=item.get("decided_on"),
            interpreted_on=item.get("interpreted_on"),
            registered_at=item.get("registered_at"),
            retrieved_at=retrieved_at,
            official_url=official_url,
            raw_sha256=content_hash,
            normalized_sha256=content_hash,
            snapshot_ref=snapshot.snapshot_ref,
            parser_version=snapshot.parser_version,
            collection_run_id=run_id,
            content_completeness=self._items_completeness([item]),
            temporal_status=TemporalStatus.UNRESOLVED,
            review_state=ReviewState.NEEDS_REVIEW,
            sections=[TextSection.model_validate(section) for section in item.get("sections", [])],
            metadata={"source_class": "supplemental", "provenance_preserved": True, **metadata, "snapshot_raw_sha256": snapshot.raw_sha256},
        )

    @staticmethod
    def _safe_source_url(provider_name: str, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ProviderError(ErrorCode.ACCESS_DENIED, "공식 출처 URL 형식이 올바르지 않습니다.")
        parsed = urlparse(value)
        if provider_name not in _PROVIDER_HOSTS:
            raise ProviderError(ErrorCode.ACCESS_DENIED, "등록되지 않은 provider의 공식 출처 URL은 저장하지 않습니다.")
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALL_OFFICIAL_HOSTS or parsed.username or parsed.password:
            raise ProviderError(ErrorCode.ACCESS_DENIED, "provider allowlist 밖의 공식 출처 URL은 저장하지 않습니다.")
        if re.search(r"(?i)(?:[?&])(oc|authorization|cookie|api[_-]?key|token)=", value):
            raise ProviderError(ErrorCode.ACCESS_DENIED, "인증값 query가 포함된 공식 출처 URL은 저장하지 않습니다.")
        return value

    def _candidate_groups(self, documents: list[LegalDocument]) -> list[dict[str, Any]]:
        candidates: dict[str, list[LegalDocument]] = {}
        for document in documents:
            title_key = re.sub(r"[^0-9A-Za-z가-힣]", "", document.title).casefold()
            if title_key:
                candidates.setdefault(title_key, []).append(document)
        groups = []
        for title_key, grouped in candidates.items():
            for members in self._same_decision(grouped):
                if len(members) < 2:
                    continue
                groups.append({
                    "group_id": "dup-" + self.repository.fingerprint({"title": title_key, "case_no": members[0].case_no or ""})[:16],
                    "match_basis": "normalized_title_and_case_no",
                    "members": [
                        {
                            "document_id": member.document_id,
                            "provider": member.provider,
                            "title": member.title,
                            "case_no": member.case_no,
                            "decided_on": member.decided_on,
                            "raw_sha256": member.raw_sha256,
                        }
                        for member in members
                    ],
                    "conflicting_originals": len({member.raw_sha256 for member in members}) > 1,
                    "provenance_preserved": True,
                })
        return groups

    @staticmethod
    def _same_decision(members: list[LegalDocument]) -> list[list[LegalDocument]]:
        """제목이 같은 문서들을 사건번호로 다시 가른다.

        판결문 제목은 "상속세부과처분취소"처럼 일반적이라, 제목만으로 묶으면
        같은 사건의 1심·2심·대법원 판결이 하나의 중복 그룹이 된다. 세무사가
        확정판결과 하급심을 같은 것으로 볼 위험이 있다. 사건번호가 다르면
        서로 다른 결정이므로 중복이 아니다.

        사건번호가 비어 있는 문서는 상세조회 전 검색 stub이거나 사건번호를
        제공하지 않는 자료다. 같은 제목에 사건번호가 하나뿐이면 그 문서와
        같은 건으로 본다. 둘 이상이면 어느 쪽인지 알 수 없으므로 묶지 않는다.
        """
        by_case: dict[str, list[LegalDocument]] = {}
        unknown: list[LegalDocument] = []
        for member in members:
            case_no = (member.case_no or "").strip()
            if case_no:
                by_case.setdefault(case_no, []).append(member)
            else:
                unknown.append(member)
        if len(by_case) == 1:
            only = next(iter(by_case.values()))
            return [only + unknown]
        if not by_case:
            return [unknown]
        return [*by_case.values(), *([unknown] if len(unknown) > 1 else [])]

    def _collect_search(self, target: LegalTarget, **kwargs) -> tuple[ProviderResult, list[LegalDocument], bool]:
        run_id = kwargs.pop("run_id", None)
        request = {"target": target.value, **kwargs, "collection_window": utc_now()[:10]}
        started = utc_now()
        run = self.repository.start_run(
            provider=self.provider.name,
            action="search",
            request=request,
            started_at=started,
            run_id=run_id,
            resume=True,
        )
        if run["status"] == "completed":
            documents = self.repository.documents_for_run(run["run_id"])
            previous = self.repository.collection_result(run["run_id"])
            if previous is None or "total" not in previous or "page" not in previous:
                raise ProviderError(ErrorCode.INCOMPLETE_RESULT, "완료된 검색의 상류 페이지 정보가 없어 총 건수를 확인할 수 없습니다.")
            empty_response = ProviderResult(target, {key: value for key, value in request.items() if key != "collection_window"}, self._cached_response(), {}, [], previous["total"], previous["page"], kwargs.get("display", 10))
            return empty_response, documents, True
        if not run["claimed"]:
            raise ProviderError(ErrorCode.UPSTREAM_UNAVAILABLE, "같은 수집 요청이 이미 실행 중입니다.", retryable=True)
        try:
            result = self.provider.search(target, **kwargs)
            documents = self._persist_result(result, run["run_id"], started)
            self.repository.save_collection_result(run["run_id"], {"total": result.total, "page": result.page})
            self.repository.finish_run(run["run_id"], status="completed", finished_at=utc_now())
            self.snapshots.save_run_manifest(run["run_id"], {"run_id": run["run_id"], "status": "completed", "request": result.request_parameters, "document_ids": [document.document_id for document in documents]})
            return result, documents, False
        except (ProviderError, TransportError, ValueError, OSError) as exc:
            code = getattr(exc, "code", ErrorCode.UPSTREAM_UNAVAILABLE)
            self.repository.finish_run(run["run_id"], status="failed", finished_at=utc_now(), error_code=code.value, error_message=str(exc))
            raise

    def _collect_detail(self, target: LegalTarget, **kwargs) -> tuple[ProviderResult, list[LegalDocument], bool]:
        run_id = kwargs.pop("run_id", None)
        request = {"target": target.value, **kwargs, "parser_version": PARSER_VERSION, "collection_window": utc_now()[:10]}
        started = utc_now()
        run = self.repository.start_run(
            provider=self.provider.name,
            action="detail",
            request=request,
            started_at=started,
            run_id=run_id,
            resume=True,
        )
        if run["status"] == "completed":
            documents = self.repository.documents_for_run(run["run_id"])
            empty_response = ProviderResult(target, {key: value for key, value in request.items() if key != "collection_window"}, self._cached_response(), {}, [], len(documents), 1, len(documents))
            return empty_response, documents, True
        if not run["claimed"]:
            raise ProviderError(ErrorCode.UPSTREAM_UNAVAILABLE, "같은 상세 수집 요청이 이미 실행 중입니다.", retryable=True)
        try:
            result = self.provider.detail(target, **kwargs)
            documents = self._persist_result(result, run["run_id"], started)
            self.repository.finish_run(run["run_id"], status="completed", finished_at=utc_now())
            self.snapshots.save_run_manifest(run["run_id"], {"run_id": run["run_id"], "status": "completed", "request": result.request_parameters, "document_ids": [document.document_id for document in documents]})
            return result, documents, False
        except (ProviderError, TransportError, ValueError, OSError) as exc:
            code = getattr(exc, "code", ErrorCode.UPSTREAM_UNAVAILABLE)
            self.repository.finish_run(run["run_id"], status="failed", finished_at=utc_now(), error_code=code.value, error_message=str(exc))
            raise

    def _persist_result(self, result: ProviderResult, run_id: str, retrieved_at: str) -> list[LegalDocument]:
        section_counts = [bool(item.get("sections")) for item in result.items]
        full_sections = [
            bool(item.get("sections")) and not (
                result.target in {LegalTarget.LAW, LegalTarget.EFFECTIVE_LAW}
                and any(heading_only_article(section["text"]) for section in item["sections"] if section["kind"] == "articles")
            )
            for item in result.items
        ]
        completeness = (
            ContentCompleteness.COMPLETE
            if full_sections and all(full_sections)
            else ContentCompleteness.PARTIAL
            if any(section_counts)
            else ContentCompleteness.METADATA_ONLY
        )
        source_key = result.items[0]["source_document_id"] if len(result.items) == 1 else f"search-{result.target.value}-{self.repository.fingerprint(result.request_parameters)[:16]}"
        snapshot = self.snapshots.save(
            provider=self.provider.name,
            document_type=CONTRACTS[result.target].document_type,
            source_document_id=source_key,
            response=result.response,
            parsed=result.parsed,
            retrieved_at=retrieved_at,
            run_id=run_id,
            completeness=completeness,
            secrets=(self.provider.credential or "",),
        )
        documents = [self._document_from_item(result.target, item, snapshot, run_id, retrieved_at, completeness) for item in result.items]
        self.repository.save_result(snapshot, documents)
        return documents

    @staticmethod
    def _unapplied_filter_warnings(filters: Mapping[str, str] | None) -> list[str]:
        """filters는 upstream 검색에만 전달된다. 로컬 색인에는 적용되지 않는다.

        upstream 경로는 지원하지 않는 필터를 INVALID_REQUEST로 거부하지만,
        로컬 경로는 값을 받아놓고 아무 데도 쓰지 않은 채 결과를 돌려줬다.
        지정한 쪽에서는 걸러진 결과를 보고 있다고 오해하게 된다. 받아놓고
        조용히 무시하는 것이 가장 나쁘다.
        """
        if not filters:
            return []
        return [
            f"filters({', '.join(sorted(filters))})는 upstream 검색에만 적용됩니다. "
            "로컬 색인 검색 결과에는 반영되지 않았습니다. 적용하려면 upstream=true로 요청하십시오."
        ]

    def _empty_index_warnings(self, total: int) -> list[str]:
        """로컬 색인이 비어서 0건인 경우와 검색어가 안 맞아 0건인 경우를 구분해 알린다.

        upstream 기본값이 false라서, 갓 설치한 사용자는 무엇을 검색하든 이
        경로로 들어와 0건을 받는다. 그때 status=ok에 warning이 하나도 없으면
        호출하는 LLM은 "그런 법령은 없습니다"라고 단정하게 된다. 아직 아무것도
        수집하지 않았다는 사실과 다음에 할 일을 응답 자체에 담아야 한다.
        """
        if total != 0:
            return []
        try:
            if self.repository.source_status()["documents"] != 0:
                return []
        except (sqlite3.Error, OSError):
            return []
        return [
            "로컬 법률 색인이 비어 있어 0건입니다. 해당 법령이 없다는 뜻이 아니라 아직 수집하지 않은 상태입니다.",
            "공식 원문은 upstream=true로 요청하거나, taxax-legal collect-seeds로 색인을 먼저 채운 뒤 조회하십시오.",
        ]

    @staticmethod
    def _item_content_hash(item: Mapping[str, Any]) -> str:
        """개별 문서 항목 자체의 내용 해시.

        검색 결과 한 페이지에 문서가 여러 건 들어있으면 snapshot.raw_sha256은
        그 페이지(HTTP 응답 원문) 하나의 해시다. 그 값을 각 문서에 그대로
        복사하면, 서로 완전히 다른 문서(예: 법인세법 / 시행령 / 시행규칙)가
        우연이 아니라 항상 같은 raw_sha256을 갖게 되어 "이 문서의 원문 해시"
        라는 필드 의미가 깨진다. 항목 자체 데이터로 따로 계산해야 한다.
        """
        return sha256_bytes(normalized_json_bytes(item))

    def _document_from_item(self, target: LegalTarget, item: Mapping[str, Any], snapshot, run_id: str, retrieved_at: str, completeness: ContentCompleteness) -> LegalDocument:
        source_id = str(item["source_document_id"])
        official_url = self._safe_source_url(
            self.provider.name,
            item.get("official_url") or LawGoProvider.detail_url(target, source_id),
        )
        temporal = TemporalStatus.UNRESOLVED
        content_hash = self._item_content_hash(item)
        metadata = dict(item.get("metadata") or {})
        upstream_ids = metadata.get("upstream_identifiers") or {}
        mst = metadata.get("requested_mst") or (
            item.get("version_id")
            if isinstance(upstream_ids, Mapping) and item.get("version_id") in upstream_ids.get("MST", [])
            else None
        )
        return LegalDocument(
            provider=self.provider.name,
            document_type=CONTRACTS[target].document_type,
            source_document_id=source_id,
            document_id=f"law-go:{target.value}:{source_id}",
            version_id=item.get("version_id"),
            title=str(item["title"]),
            issuer=item.get("issuer"),
            court=item.get("court"),
            case_no=item.get("case_no"),
            promulgated_on=item.get("promulgated_on"),
            effective_from=item.get("effective_from"),
            decided_on=item.get("decided_on"),
            interpreted_on=item.get("interpreted_on"),
            registered_at=item.get("registered_at"),
            retrieved_at=retrieved_at,
            official_url=official_url,
            version_url=(f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={mst}"
                         if target in {LegalTarget.LAW, LegalTarget.EFFECTIVE_LAW}
                         and isinstance(mst, str) and mst.isdigit() else None),
            raw_sha256=content_hash,
            normalized_sha256=content_hash,
            snapshot_ref=snapshot.snapshot_ref,
            parser_version=snapshot.parser_version,
            collection_run_id=run_id,
            content_completeness=completeness,
            temporal_status=temporal,
            review_state=ReviewState.UNREVIEWED,
            sections=[TextSection.model_validate(section) for section in item.get("sections", [])],
            metadata={"target": target.value, "official_guide": LawGoProvider.guide_url(target), **metadata, "snapshot_raw_sha256": snapshot.raw_sha256},
        )

    @staticmethod
    def _section_cursor(cursor: str | int) -> tuple[int, int]:
        value = str(cursor)
        match = re.fullmatch(r"(\d+)(?::(\d+))?", value)
        if not match:
            raise ProviderError(ErrorCode.INVALID_REQUEST, "section_cursor는 절번호 또는 절번호:문자offset 형식이어야 합니다.")
        return int(match.group(1)), int(match.group(2) or 0)

    @staticmethod
    def _page_sections(sections: list[TextSection], index: int, character_offset: int, max_chars: int) -> tuple[list[TextSection], str | None, bool]:
        if index > len(sections) or (index == len(sections) and character_offset):
            raise ProviderError(ErrorCode.INVALID_REQUEST, "section_cursor가 문서 범위를 벗어났습니다.")
        selected: list[TextSection] = []
        used = 0
        while index < len(sections):
            original = sections[index]
            text = original.text[character_offset:]
            remaining = max_chars - used
            if not text:
                index += 1
                character_offset = 0
                continue
            if len(text) > remaining:
                selected.append(original.model_copy(update={"text": text[:remaining]}))
                next_cursor = f"{index}:{character_offset + remaining}"
                return selected, next_cursor, True
            selected.append(original.model_copy(update={"text": text}) if character_offset else original)
            used += len(text)
            index += 1
            character_offset = 0
            if used == max_chars and index < len(sections):
                return selected, str(index), True
        return selected, None, False

    @staticmethod
    def _metadata_view(document: LegalDocument) -> dict[str, Any]:
        return {
            "document_id": document.document_id,
            "source_document_id": document.source_document_id,
            "version_id": document.version_id,
            "provider": document.provider,
            "document_type": document.document_type,
            "title": document.title,
            "issuer": document.issuer,
            "court": document.court,
            "case_no": document.case_no,
            "document_number": document.document_number,
            "promulgated_on": document.promulgated_on,
            "effective_from": document.effective_from,
            "decided_on": document.decided_on,
            "interpreted_on": document.interpreted_on,
            "retrieved_at": document.retrieved_at,
            "content_completeness": document.content_completeness.value,
            "temporal_status": (TemporalStatus.UNRESOLVED.value
                                if document.provider == "law.go.kr" and document.document_type == "law"
                                and document.temporal_status == TemporalStatus.CURRENT
                                else document.temporal_status.value),
            "citation_status": document.citation_status.value,
            "review_state": document.review_state.value,
            "official_url": document.official_url,
            "version_url": document.version_url,
        }

    @staticmethod
    def _cached_response(provider_name: str = "law.go.kr"):
        from .transport import HttpResponse

        urls = {
            "law.go.kr": "https://www.law.go.kr/DRF/",
            "taxlaw.nts.go.kr": "https://taxlaw.nts.go.kr/",
            "olta.re.kr": "https://olta.re.kr/",
        }
        return HttpResponse(url=urls[provider_name], status=200, headers={"content-type": "application/json"}, body=b"{}")

    @staticmethod
    def _error_response(rid: str, exc: Exception, provider: str | None = None) -> LegalToolResponse:
        code = getattr(exc, "code", None)
        if not isinstance(code, ErrorCode):
            if isinstance(exc, PermissionError):
                code = ErrorCode.ACCESS_DENIED
            else:
                code = ErrorCode.INVALID_REQUEST if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)) else ErrorCode.UPSTREAM_UNAVAILABLE
        retryable = bool(getattr(exc, "retryable", False))
        details = getattr(exc, "details", {})
        if isinstance(exc, sqlite3.Error):
            message = "로컬 저장소 작업에 실패했습니다."
        elif isinstance(exc, OSError):
            message = "로컬 파일 작업에 실패했습니다."
        else:
            message = str(exc)
        status = ResponseStatus.BLOCKED if code in {ErrorCode.AUTH_REQUIRED, ErrorCode.AUTH_FAILED, ErrorCode.ACCESS_DENIED, ErrorCode.POLICY_DISABLED} else ResponseStatus.ERROR
        disabled = [provider] if provider and code == ErrorCode.POLICY_DISABLED else []
        failed = [provider] if provider and code != ErrorCode.POLICY_DISABLED else []
        return LegalToolResponse(
            request_id=rid,
            status=status,
            error=ToolError(code=code, message=message, retryable=retryable, details=details),
            coverage=Coverage(queried_providers=[provider] if provider else [], failed_providers=failed, disabled_providers=disabled),
        )
