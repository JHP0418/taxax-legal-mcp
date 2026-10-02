from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taxax.legal.models import (
    ContentCompleteness,
    ErrorCode,
    LegalDocument,
    TextSection,
    ResponseStatus,
    RetrievalStatus,
    SourceSnapshot,
)
from taxax.legal.providers.base import LegalTarget, ProviderError
from taxax.legal.providers.korean_law_bridge import KoreanLawBridge
from taxax.legal.providers.law_go import PARSER_VERSION, LawGoProvider
from taxax.legal.service import LegalKnowledgeService
from taxax.legal.transport import HttpResponse

FIXTURES = Path(__file__).parent / "legal_fixtures"


class RoutedTransport:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def request(self, url, *, params=None, secrets=()):
        parameters = dict(params or {})
        self.calls.append(parameters)
        fixture = "law_detail.json" if url.endswith("lawService.do") else "law_search.json"
        return HttpResponse(url=url, status=200, headers={"content-type": "application/json"}, body=(FIXTURES / fixture).read_bytes())


class LegalServiceTests(unittest.TestCase):
    def make_service(self, root: Path, *, knowledge_dir: Path | None = None):
        transport = RoutedTransport()
        provider = LawGoProvider(credential="operator-secret", transport=transport)
        service = LegalKnowledgeService(
            root,
            data_dir=root / "state",
            provider=provider,
            knowledge_dir=knowledge_dir,
        )
        return service, transport

    def test_default_data_directory_is_user_scoped_and_cwd_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"TAXAX_LEGAL_DATA_DIR": ""}
            if os.name == "nt":
                environment["LOCALAPPDATA"] = str(root / "local-app-data")
                expected = root / "local-app-data" / "TAXax" / "legal"
            elif sys.platform == "darwin":
                environment["HOME"] = str(root / "home")
                expected = root / "home" / "Library" / "Application Support" / "TAXax" / "legal"
            else:
                environment["XDG_DATA_HOME"] = str(root / "xdg-data")
                expected = root / "xdg-data" / "taxax" / "legal"
            with patch.dict(os.environ, environment, clear=False):
                first = LegalKnowledgeService(root / "first-project")
                second = LegalKnowledgeService(root / "second-project")
            self.assertEqual(first.data_dir, expected.resolve())
            self.assertEqual(second.data_dir, expected.resolve())

    def test_search_collect_store_and_exact_run_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            service, transport = self.make_service(Path(directory))
            first = service.search_legal_sources(query="법인세법", target="law", upstream=True)
            second = service.search_legal_sources(query="법인세법", target="law", upstream=True)
            self.assertEqual(first.status, ResponseStatus.OK)
            self.assertEqual(second.status, ResponseStatus.OK)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(first.data["items"][0]["document_type"], "law")
            self.assertTrue(any("재사용" in warning for warning in second.warnings))
            local = service.search_legal_sources(query="법인세법")
            self.assertEqual(local.pagination.total, 1)
            self.assertEqual(local.sources[0].provider, "law.go.kr")
            manifests = list((Path(directory) / "state" / "v1" / "runs").glob("*.json"))
            manifest = manifests[0].read_text(encoding="utf-8")
            self.assertNotIn("operator-secret", manifest)
            self.assertNotIn('"OC"', manifest)

    def test_observed_law_go_provider_name_routes_to_official_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            service, transport = self.make_service(Path(directory))
            response = service.search_legal_sources(query="소득세법", target="law", provider="law_go_kr", upstream=True)
            self.assertEqual(response.status, ResponseStatus.OK)
            self.assertEqual(response.sources[0].provider, "law.go.kr")
            self.assertEqual(len(transport.calls), 1)

    def test_upstream_cursor_advances_and_reused_run_keeps_official_total(self):
        class PagedTransport:
            def __init__(self):
                self.pages: list[int] = []

            def request(self, url, *, params=None, secrets=()):
                page = int(params["page"])
                self.pages.append(page)
                payload = {"LawSearch": {"totalCnt": 2, "page": page, "law": [{
                    "법령ID": f"000{page}", "법령일련번호": str(100 + page), "법령명한글": f"합성 법률 {page}",
                }]}}
                return HttpResponse(url=url, status=200, headers={"content-type": "application/json"}, body=json.dumps(payload, ensure_ascii=False).encode())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = PagedTransport()
            service = LegalKnowledgeService(root, data_dir=root / "state", provider=LawGoProvider(credential="operator-secret", transport=transport))
            first = service.search_legal_sources(query="합성 법률", target="law", upstream=True, limit=1)
            reused = service.search_legal_sources(query="합성 법률", target="law", upstream=True, limit=1)
            self.assertEqual(first.pagination.total, 2)
            self.assertEqual(reused.pagination.total, 2)
            self.assertEqual(reused.pagination.next_cursor, "2")
            second = service.search_legal_sources(query="합성 법률", target="law", upstream=True, limit=1, cursor=reused.pagination.next_cursor)
            self.assertEqual(second.pagination.page, 2)
            self.assertEqual(second.data["items"][0]["title"], "합성 법률 2")
            self.assertEqual(transport.pages, [1, 2])
            invalid = service.search_legal_sources(query="합성 법률", target="law", upstream=True, limit=1, page=2, cursor="2")
            self.assertEqual(invalid.error.code, ErrorCode.INVALID_REQUEST)
            self.assertEqual(transport.pages, [1, 2])

    def test_search_hit_is_not_original_and_refresh_uses_official_id(self):
        with tempfile.TemporaryDirectory() as directory:
            service, transport = self.make_service(Path(directory))
            search = service.search_legal_sources(query="법인세법", target="law", upstream=True)
            document_id = search.data["items"][0]["document_id"]
            cached = service.get_legal_document(document_id=document_id)
            self.assertEqual(cached.status, ResponseStatus.PARTIAL)
            self.assertFalse(cached.data["sections"])
            self.assertTrue(any("refresh=true" in warning for warning in cached.warnings))
            self.assertEqual(len(transport.calls), 1, "로컬 메타데이터 조회가 원격 호출을 해서는 안 됩니다")
            detailed = service.get_legal_document(document_id=document_id, refresh=True)
            self.assertEqual(detailed.status, ResponseStatus.OK)
            self.assertEqual(transport.calls[-1]["ID"], "000123")
            self.assertEqual(detailed.data["document_id"], document_id)

    def test_search_and_detail_share_id_when_detail_omits_mst(self):
        class MissingMstTransport(RoutedTransport):
            def request(self, url, *, params=None, secrets=()):
                response = super().request(url, params=params, secrets=secrets)
                payload = json.loads(response.body)
                if url.endswith("lawService.do"):
                    payload["법령"]["기본정보"].pop("법령일련번호")
                else:
                    payload["LawSearch"]["law"][0]["id"] = "1"
                return HttpResponse(url, 200, response.headers, json.dumps(payload, ensure_ascii=False).encode())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = MissingMstTransport()
            service = LegalKnowledgeService(root, data_dir=root / "state", provider=LawGoProvider(credential="operator-secret", transport=transport))
            listed = service.search_legal_sources(query="법인세법", target="law", upstream=True)
            hit = listed.data["items"][0]
            self.assertEqual(hit["document_id"], "law-go:law:000123")
            self.assertEqual(hit["version_id"], "12345")
            self.assertEqual(service.repository.get_document(hit["document_id"], version_id=hit["version_id"]).metadata["upstream_identifiers"]["ID"], ["000123"])
            detail = service.get_legal_document(document_id=hit["document_id"], version_id=hit["version_id"], refresh=True)
            self.assertEqual(transport.calls[-1]["MST"], "12345")
            self.assertEqual(detail.data["document_id"], hit["document_id"])
            self.assertEqual(detail.data["version_id"], hit["version_id"])
            cached = service.get_legal_document(document_id=hit["document_id"], version_id=hit["version_id"])
            self.assertEqual(cached.data["sections"], detail.data["sections"])
            checked = service.verify_legal_citations(citations=[
                {"document_id": hit["document_id"], "version_id": hit["version_id"], "locator": "조문내용", "quote": "법인세 과세"}
            ])
            self.assertEqual(checked.data["checks"][0]["status"], "verified")

    def test_missing_official_id_cannot_reuse_search_serial_as_id(self):
        class SerialOnlyTransport:
            def __init__(self):
                self.calls: list[dict] = []

            def request(self, url, *, params=None, secrets=()):
                self.calls.append(dict(params or {}))
                payload = {"LawSearch": {"totalCnt": 1, "page": 1, "law": [{"법령일련번호": "12345", "법령명한글": "합성 법률"}]}}
                return HttpResponse(url=url, status=200, headers={"content-type": "application/json"}, body=json.dumps(payload, ensure_ascii=False).encode())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = SerialOnlyTransport()
            service = LegalKnowledgeService(root, data_dir=root / "state", provider=LawGoProvider(credential="operator-secret", transport=transport))
            search = service.search_legal_sources(query="합성 법률", target="law", upstream=True)
            detail = service.get_legal_document(document_id=search.data["items"][0]["document_id"], refresh=True)
            self.assertEqual(detail.status, ResponseStatus.ERROR)
            self.assertEqual(detail.error.code, ErrorCode.INCOMPLETE_RESULT)
            self.assertEqual(len(transport.calls), 1)

    def test_effective_law_id_and_date_does_not_silently_ignore_date(self):
        with tempfile.TemporaryDirectory() as directory:
            service, transport = self.make_service(Path(directory))
            result = service.get_applicable_law(effective_on="2026-01-02", law_id="000123", refresh=True)
            self.assertEqual(result.status, ResponseStatus.ERROR)
            self.assertEqual(result.error.code, ErrorCode.TEMPORAL_UNRESOLVED)
            self.assertEqual(transport.calls, [])

    def test_each_document_in_one_search_page_gets_its_own_content_hash(self):
        """검색 한 페이지에 여러 문서가 있을 때 raw_sha256이 문서별로 달라야 한다.

        snapshot.raw_sha256은 HTTP 응답 페이지 전체의 해시라, 그걸 각 문서에
        그대로 복사하면 법인세법 / 시행령 / 시행규칙처럼 완전히 다른 법령이
        항상 같은 해시를 갖게 된다. "이 문서의 원문 해시"로 읽히는 필드라
        문서별로 따로 계산해야 한다.
        """

        class MultiItemTransport:
            def request(self, url, *, params=None, secrets=()):
                body = json.dumps(
                    {
                        "LawSearch": {
                            "totalCnt": 3,
                            "page": 1,
                            "law": [
                                {"법령ID": "000111", "법령일련번호": "111", "법령명한글": "법인세법", "공포일자": "20260101", "시행일자": "20260102"},
                                {"법령ID": "000222", "법령일련번호": "222", "법령명한글": "법인세법 시행령", "공포일자": "20260101", "시행일자": "20260102"},
                                {"법령ID": "000333", "법령일련번호": "333", "법령명한글": "법인세법 시행규칙", "공포일자": "20260101", "시행일자": "20260102"},
                            ],
                        }
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                return HttpResponse(url=url, status=200, headers={"content-type": "application/json"}, body=body)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = LawGoProvider(credential="operator-secret", transport=MultiItemTransport())
            service = LegalKnowledgeService(root, data_dir=root / "state", provider=provider)
            result = service.search_legal_sources(query="법인세법", target="law", provider="law.go.kr", upstream=True, limit=3)
            self.assertEqual(len(result.sources), 3)
            hashes = [source.raw_sha256 for source in result.sources]
            self.assertEqual(len(set(hashes)), 3, "서로 다른 법령이 같은 raw_sha256을 공유하면 안 됩니다")
            # 같은 페이지에서 왔다는 출처(snapshot_ref)는 공유하는 게 맞다.
            self.assertEqual(len({source.snapshot_ref for source in result.sources}), 1)
            for source in result.sources:
                self.assertEqual(source.snapshot_raw_sha256, hashlib.sha256(service.snapshots.read_raw(source.snapshot_ref)).hexdigest())

    def test_empty_local_index_says_so_instead_of_reporting_zero_results(self):
        """갓 설치한 사용자가 받는 응답이 "그런 법령 없음"처럼 보이면 안 된다.

        upstream 기본값이 false라 신규 사용자는 무엇을 검색하든 로컬 경로로
        들어와 0건을 받는다. status=ok에 warning이 없으면 호출하는 LLM이
        없는 법령이라고 단정한다. 반대로 색인에 문서가 있는데 검색어가 안 맞아
        0건인 경우까지 이 경고를 붙이면 그건 거짓이므로 구분해야 한다.
        """
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))

            empty = service.search_legal_sources(query="법인세법 대손금")
            self.assertEqual(empty.status, ResponseStatus.OK)
            self.assertEqual(empty.pagination.total, 0)
            self.assertTrue(
                any("아직 수집하지 않은" in warning for warning in empty.warnings),
                f"비어 있는 색인이 그 사실을 알리지 않았습니다: {empty.warnings}",
            )

            # 색인을 채운 뒤에는 매칭 실패에 이 경고가 붙으면 안 된다.
            service.search_legal_sources(query="법인세법", target="law", upstream=True)
            missed = service.search_legal_sources(query="존재하지않는법령명xyz")
            self.assertEqual(missed.pagination.total, 0)
            self.assertEqual(
                [w for w in missed.warnings if "아직 수집하지 않은" in w],
                [],
                "색인에 문서가 있는데도 비어 있다고 알렸습니다.",
            )

    def test_title_matches_outrank_documents_that_only_mention_the_term(self):
        """로컬 검색이 "가장 최근에 수집한 문서" 순이던 문제.

        매칭이 본문 substring까지 허용하므로, 질의어를 본문 어딘가에서 한 번
        언급했을 뿐인 법령도 후보가 된다. 정렬까지 수집 시각순이면 직전 조사가
        방금 가져다 놓은 그런 법령이 제목에 그 말이 들어간 예규보다 앞선다.
        밖에서는 이것이 "직전 질의 결과가 다음 질의에 샌다"로 관측됐고 동시성
        버그로 오해됐다. 실제로는 순차 실행에서도 그대로 재현된다.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _ = self.make_service(root)
            repository = service.repository

            def document(document_id: str, title: str, body: str, retrieved_at: str):
                return LegalDocument(
                    provider="law.go.kr",
                    document_type="law",
                    source_document_id=document_id.split(":")[-1],
                    document_id=document_id,
                    title=title,
                    retrieved_at=retrieved_at,
                    snapshot_ref=f"ref/{document_id}",
                    parser_version=PARSER_VERSION,
                    collection_run_id="run-seed",
                    raw_sha256="0" * 64,
                    sections=[TextSection(section_id="s1", kind="articles", text=body)],
                )

            repository.start_run(
                provider="law.go.kr",
                action="search",
                request={"query": "seed"},
                started_at="2026-09-15T00:00:00Z",
                run_id="run-seed",
            )
            snapshot = SourceSnapshot(
                snapshot_id="snap-1",
                provider="law.go.kr",
                source_document_id="seed",
                raw_sha256="0" * 64,
                snapshot_ref="ref/seed",
                parser_version=PARSER_VERSION,
                retrieved_at="2026-09-15T00:00:00Z",
                retrieval_status=RetrievalStatus.SUCCESS,
                content_completeness=ContentCompleteness.COMPLETE,
                run_id="run-seed",
                byte_length=1,
            )
            # 제목에 질의어가 있는 오래된 문서, 본문에만 스치는 최신 문서.
            repository.save_result(
                snapshot,
                [
                    document("law-go:law:on-topic", "신용카드 소득공제 특례", "본문", "2026-09-01T00:00:00Z"),
                    document("law-go:law:off-topic", "국제조세조정에 관한 법률", "신용카드 관련 언급이 한 번 있음", "2026-09-15T00:00:00Z"),
                ],
            )

            found = service.search_legal_sources(query="신용카드", limit=10)
            ids = [item["document_id"] for item in found.data["items"]]
            self.assertEqual(len(ids), 2, f"두 문서 모두 매칭돼야 합니다: {ids}")
            self.assertEqual(
                ids[0],
                "law-go:law:on-topic",
                "제목이 일치하는 문서가 본문만 스친 최신 문서보다 앞서야 합니다.",
            )

    def test_different_court_levels_are_not_reported_as_duplicates(self):
        """같은 사건의 1심·2심·대법원 판결이 하나의 중복 그룹이 되던 문제.

        판결문 제목은 "상속세부과처분취소"처럼 일반적이라 제목만으로 묶으면
        심급이 다른 별개 판결이 "중복"으로 표시된다. 세무사가 확정판결과
        하급심을 같은 것으로 볼 위험이 있다. 반대로 같은 문서를 검색 stub과
        상세본으로 두 번 가진 경우나, 같은 예규를 법제처와 국세청 양쪽에서
        가져온 경우는 진짜 중복이므로 계속 묶여야 한다.
        """

        def decision(document_id: str, title: str, case_no: str | None, provider: str = "law.go.kr"):
            return LegalDocument(
                provider=provider,
                document_type="prec",
                source_document_id=document_id.split(":")[-1],
                document_id=document_id,
                title=title,
                case_no=case_no,
                retrieved_at="2026-09-15T00:00:00Z",
                snapshot_ref=f"ref/{document_id}",
                parser_version=PARSER_VERSION,
                collection_run_id="run-seed",
                raw_sha256="0" * 64,
            )

        levels = [
            decision("law-go:prec:1", "상속세부과처분취소", "2019두1234"),
            decision("law-go:prec:2", "상속세부과처분취소", "2018누5678"),
            decision("law-go:prec:3", "상속세부과처분취소", "2017구합999"),
        ]
        self.assertEqual(
            [part for part in LegalKnowledgeService._same_decision(levels) if len(part) >= 2],
            [],
            "사건번호가 다른 별개 판결을 중복으로 묶었습니다.",
        )

        same_case = [
            decision("law-go:ntsCgmExpc:85204", "신용카드 소득공제 여부", "서면상담1팀-348"),
            decision("taxlaw.nts.go.kr:nts_interpretation:1", "신용카드 소득공제 여부", None, provider="taxlaw.nts.go.kr"),
        ]
        merged = [part for part in LegalKnowledgeService._same_decision(same_case) if len(part) >= 2]
        self.assertEqual(len(merged), 1, "같은 자료의 provider별 사본이 묶이지 않았습니다.")

    def test_a_search_rejection_says_which_rule_was_broken(self):
        """두 가지 다른 잘못을 한 문구로 뭉뚱그리던 문제.

        공백만 넣은 사람에게 "1~500자여야 합니다"라고 하면, 세 글자를 넣었는데
        왜 걸렸는지 알 수 없다. 실제 이유는 다듬고 나니 아무것도 남지 않은 것이다.
        """
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))

            blank = service.search_legal_sources(query="   ")
            self.assertEqual(blank.error.code, ErrorCode.INVALID_REQUEST)
            self.assertIn("비어 있습니다", blank.error.message)

            too_long = service.search_legal_sources(query="가" * 600)
            self.assertEqual(too_long.error.code, ErrorCode.INVALID_REQUEST)
            self.assertIn("600자", too_long.error.message)

    def test_cursor_above_sqlite_integer_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            cursor = str(2**63)

            search = service.search_legal_sources(query="법인세", cursor=cursor)
            initial = service.get_legal_document(
                target="law",
                source_document_id="000123",
                refresh=True,
            )
            detail = service.get_legal_document(
                document_id=initial.data["document_id"],
                section_cursor=cursor,
            )

            self.assertEqual(search.status, ResponseStatus.ERROR)
            self.assertEqual(search.error.code, ErrorCode.INVALID_REQUEST)
            self.assertIn("지원 범위", search.error.message)
            self.assertEqual(detail.status, ResponseStatus.ERROR)
            self.assertEqual(detail.error.code, ErrorCode.INVALID_REQUEST)
            self.assertIn("범위", detail.error.message)

    def test_detail_pages_long_section_without_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            initial = service.get_legal_document(target="law", source_document_id="000123", identifier_kind="ID", refresh=True, max_chars=10)
            self.assertEqual(initial.status, ResponseStatus.OK)
            document_id = initial.data["document_id"]
            cursor = initial.pagination.next_cursor
            chunks = [initial.data["sections"][0]["text"]]
            while cursor is not None:
                page = service.get_legal_document(document_id=document_id, section_cursor=cursor, max_chars=10)
                chunks.extend(section["text"] for section in page.data["sections"])
                cursor = page.pagination.next_cursor
            stored = service.repository.get_document(document_id)
            self.assertEqual("".join(chunks), "".join(section.text for section in stored.sections))
            self.assertGreater(len(chunks), 2)

    def test_applicable_law_is_candidate_not_transaction_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            response = service.get_applicable_law(effective_on="2026-01-02", mst="12345", refresh=True)
            self.assertEqual(response.status, ResponseStatus.OK)
            self.assertEqual(response.data["candidates"][0]["temporal_status"], "candidate")
            self.assertTrue(response.data["addenda_confirmed"])
            self.assertFalse(response.data["transaction_applicability_confirmed"])
            self.assertTrue(any("확정" in warning for warning in response.warnings))

    def test_incomplete_preview_does_not_confirm_addenda(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            run_id = "run-partial-addenda"
            document_id = "law-go:law:partial-addenda"
            service.repository.start_run(
                provider="law.go.kr",
                action="detail",
                request={"document_id": document_id},
                started_at="2026-09-16T00:00:00Z",
                run_id=run_id,
            )
            service.repository.save_result(
                SourceSnapshot(
                    snapshot_id="snap-partial-addenda",
                    provider="law.go.kr",
                    source_document_id="partial-addenda",
                    raw_sha256="2" * 64,
                    snapshot_ref="v1/raw/partial-addenda.json",
                    parser_version="fixture-v1",
                    retrieved_at="2026-09-16T00:00:00Z",
                    retrieval_status=RetrievalStatus.PARTIAL,
                    content_completeness=ContentCompleteness.PARTIAL,
                    run_id=run_id,
                    byte_length=1,
                ),
                [
                    LegalDocument(
                        provider="law.go.kr",
                        document_type="law",
                        source_document_id="partial-addenda",
                        document_id=document_id,
                        title="부분 법령",
                        effective_from="2026-01-01",
                        effective_to="2026-12-31",
                        retrieved_at="2026-09-16T00:00:00Z",
                        raw_sha256="2" * 64,
                        snapshot_ref="v1/raw/partial-addenda.json",
                        parser_version="fixture-v1",
                        collection_run_id=run_id,
                        retrieval_status=RetrievalStatus.PARTIAL,
                        content_completeness=ContentCompleteness.PARTIAL,
                        sections=[
                            TextSection(
                                section_id="addenda",
                                kind="addenda",
                                text="부칙 일부 미리보기",
                            )
                        ],
                        metadata={"preview_only": True},
                    )
                ],
            )

            response = service.get_applicable_law(
                effective_on="2026-06-30",
                document_id=document_id,
            )

            self.assertEqual(response.status, ResponseStatus.OK)
            self.assertEqual(response.data["candidates"][0]["temporal_status"], "candidate")
            self.assertEqual(response.data["candidates"][0]["content_completeness"], "partial")
            self.assertFalse(response.data["addenda_confirmed"])
            self.assertFalse(response.data["transaction_applicability_confirmed"])
            self.assertTrue(any("완전한 원문" in warning for warning in response.warnings))

    def test_citation_states_document_quote_and_locator_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            detail = service.get_legal_document(target="law", source_document_id="000123", refresh=True)
            document_id = detail.data["document_id"]
            checked = service.verify_legal_citations(citations=[
                {"document_id": document_id, "locator": "조문내용", "quote": "법인세 과세"},
                {"document_id": document_id, "locator": "없는 위치", "quote": "없는 문구"},
                {"document_id": "law-go:law:missing"},
            ])
            statuses = [item["status"] for item in checked.data["checks"]]
            self.assertEqual(statuses, ["verified", "mismatch", "mismatch"])
            self.assertFalse(checked.data["legal_conclusion_verified"])
            self.assertEqual(checked.status, ResponseStatus.PARTIAL)

    def test_cached_law_with_only_article_heading_is_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            document_id = "law-go:law:001563"
            service.repository.start_run(
                provider="law.go.kr", action="detail", request={"document_id": document_id},
                started_at="2026-09-17T00:00:00Z", run_id="run-title-only",
            )
            service.repository.save_result(
                SourceSnapshot(
                    snapshot_id="snap-title-only", provider="law.go.kr", source_document_id="001563",
                    raw_sha256="3" * 64, snapshot_ref="v1/raw/title-only.json", parser_version="law-go-v1",
                    retrieved_at="2026-09-17T00:00:00Z", retrieval_status=RetrievalStatus.SUCCESS,
                    content_completeness=ContentCompleteness.COMPLETE, run_id="run-title-only", byte_length=1,
                ),
                [LegalDocument(
                    provider="law.go.kr", document_type="law", source_document_id="001563",
                    document_id=document_id, title="법인세법", retrieved_at="2026-09-17T00:00:00Z",
                    raw_sha256="3" * 64, snapshot_ref="v1/raw/title-only.json", parser_version="law-go-v1",
                    collection_run_id="run-title-only", content_completeness=ContentCompleteness.COMPLETE,
                    sections=[TextSection(section_id="articles-1", kind="articles", locator="조문내용",
                                          text="제19조의2(대손금의 손금불산입)")],
                )],
            )
            detail = service.get_legal_document(document_id=document_id)
            self.assertEqual(detail.status, ResponseStatus.PARTIAL)
            self.assertEqual(detail.data["content_completeness"], "partial")
            checked = service.verify_legal_citations(citations=[
                {"document_id": document_id, "locator": "조문내용", "quote": "제19조의2(대손금의 손금불산입)"}
            ])
            self.assertEqual(checked.data["checks"][0]["status"], "unverified")

    def test_law_heading_and_addenda_do_not_confirm_missing_article_body(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            service.repository.start_run(
                provider="law.go.kr", action="detail", request={"source_document_id": "000123"},
                started_at="2026-09-17T00:00:00Z", run_id="run-title-with-addenda",
            )
            source = service.provider.detail(LegalTarget.LAW, identifier="000123", identifier_kind="ID")
            source.items[0]["sections"] = [
                {"section_id": "articles-1", "kind": "articles", "text": "제19조의2(대손금)", "locator": "조문내용"},
                {"section_id": "addenda-1", "kind": "addenda", "text": "부칙 제1조 시행일", "locator": "부칙"},
            ]
            documents = service._persist_result(source, "run-title-with-addenda", "2026-09-17T00:00:00Z")
            self.assertEqual(documents[0].content_completeness, ContentCompleteness.PARTIAL)
            applicable = service.get_applicable_law(effective_on="2026-09-17", document_id=documents[0].document_id)
            self.assertFalse(applicable.data["addenda_confirmed"])
            self.assertEqual(applicable.data["candidates"][0]["content_completeness"], "partial")

    def test_one_heading_only_article_downgrades_mixed_law_and_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            service.repository.start_run(
                provider="law.go.kr", action="detail", request={"source_document_id": "000123"},
                started_at="2026-09-17T00:00:00Z", run_id="run-mixed-article",
            )
            source = service.provider.detail(LegalTarget.LAW, identifier="000123", identifier_kind="ID")
            source.items[0]["sections"] = [
                {"section_id": "articles-1", "kind": "articles", "text": "제79조(합병요건)\n① 합병 요건은 다음과 같다.", "locator": "제79조"},
                {"section_id": "articles-2", "kind": "articles", "text": "제80조", "locator": "제80조"},
                {"section_id": "addenda-1", "kind": "addenda", "text": "부칙 제1조 시행일", "locator": "부칙"},
            ]
            document = service._persist_result(source, "run-mixed-article", "2026-09-17T00:00:00Z")[0]
            self.assertEqual(document.content_completeness, ContentCompleteness.PARTIAL)
            detail = service.get_legal_document(document_id=document.document_id)
            self.assertEqual(detail.status, ResponseStatus.PARTIAL)
            citation = service.verify_legal_citations(citations=[
                {"document_id": document.document_id, "locator": "제80조", "quote": "제80조"}
            ])
            self.assertEqual(citation.data["checks"][0]["status"], "unverified")
            applicable = service.get_applicable_law(effective_on="2026-09-17", document_id=document.document_id)
            self.assertFalse(applicable.data["addenda_confirmed"])

    def test_shared_law_id_requires_explicit_mst_for_cached_detail_and_citation(self):
        class VersionTransport:
            def __init__(self):
                self.calls = []

            def request(self, url, *, params=None, secrets=()):
                self.calls.append(dict(params or {}))
                mst = str(params["MST"])
                payload = {"법령": {"기본정보": {
                    "법령ID": "003608", "법령명_한글": "합성 시행령",
                    "시행일자": "20250228" if mst == "269543" else "20260227",
                }, "조문": {"조문단위": {"조문번호": "19", "조문내용":
                    f"제19조(합성)\n① {mst} 버전의 본문이다."}}}}
                return HttpResponse(url, 200, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = VersionTransport()
            service = LegalKnowledgeService(root, data_dir=root / "state", provider=LawGoProvider(credential="operator-secret", transport=transport))
            for mst in ("283635", "269543"):
                result = service.get_legal_document(target="law", source_document_id=mst, identifier_kind="MST", refresh=True)
                self.assertEqual(result.status, ResponseStatus.OK)
                self.assertEqual(result.data["version_id"], mst)
                self.assertEqual(result.data["temporal_status"], "unresolved")
                self.assertIn(f"lsiSeq={mst}", result.data["version_url"])
            document_id = "law-go:law:003608"
            ambiguous = service.get_legal_document(document_id=document_id)
            self.assertEqual(ambiguous.error.code, ErrorCode.TEMPORAL_UNRESOLVED)
            self.assertEqual(ambiguous.error.details["available_version_ids"], ["269543", "283635"])
            older = service.get_legal_document(document_id=document_id, version_id="269543")
            self.assertIn("269543 버전", older.data["sections"][0]["text"])
            self.assertEqual(service.get_legal_document(document_id=document_id, version_id="missing").error.code, ErrorCode.NOT_FOUND)
            checked = service.verify_legal_citations(citations=[
                {"document_id": document_id, "quote": "269543 버전"},
                {"document_id": document_id, "version_id": "269543", "locator": "제19조", "quote": "269543 버전"},
                {"document_id": document_id, "version_id": "283635", "locator": "제19조", "quote": "269543 버전"},
            ])
            self.assertEqual([check["status"] for check in checked.data["checks"]], ["unverified", "verified", "mismatch"])
            self.assertEqual(checked.sources[0].version_id, "269543")
            refreshed = service.get_legal_document(document_id=document_id, version_id="269543", refresh=True)
            self.assertEqual(refreshed.status, ResponseStatus.OK)
            self.assertEqual(transport.calls[-1]["MST"], "269543")
            applicable = service.get_applicable_law(
                effective_on="2025-02-28", document_id=document_id, version_id="269543", refresh=True,
            )
            self.assertEqual(applicable.status, ResponseStatus.OK)
            self.assertEqual(transport.calls[-1]["MST"], "269543")
            self.assertEqual(transport.calls[-1]["efYd"], "20250228")
            self.assertEqual(applicable.data["candidates"][0]["version_id"], "269543")
            calls_before = len(transport.calls)
            mismatch = service.get_applicable_law(
                effective_on="2025-02-28", document_id=document_id,
                version_id="269543", mst="283635", refresh=True,
            )
            self.assertEqual(mismatch.error.code, ErrorCode.INVALID_REQUEST)
            self.assertEqual(len(transport.calls), calls_before)

    def test_partial_article_cache_selects_cited_snapshot_without_merging_versions(self):
        from taxax.legal.repository import AmbiguousDocumentVersion

        for order in ((35, 60), (60, 35)):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                service, transport = self.make_service(Path(directory))
                document_id = "law-go:law:article-cache"
                originals = {}

                def save_article(article, version, index):
                    run_id = f"run-{index}"
                    retrieved_at = f"2026-09-17T00:00:0{index}Z"
                    digest = hashlib.sha256(f"{article}:{version}".encode()).hexdigest()
                    snapshot_ref = f"v1/raw/{digest}.json"
                    service.repository.start_run(
                        provider="law.go.kr", action="detail", request={"article": article, "version": version},
                        started_at=retrieved_at, run_id=run_id,
                    )
                    document = LegalDocument(
                        provider="law.go.kr", document_type="law", source_document_id="article-cache",
                        document_id=document_id, version_id=version, title="합성 법률",
                        retrieved_at=retrieved_at, raw_sha256=digest, snapshot_ref=snapshot_ref,
                        parser_version="fixture-v1", collection_run_id=run_id,
                        retrieval_status=RetrievalStatus.PARTIAL, content_completeness=ContentCompleteness.PARTIAL,
                        sections=[TextSection(section_id="articles-1", kind="articles", locator=f"제{article}조",
                                              text=f"제{article}조(합성)\n① {version} 시행본 제{article}조의 본문이다.")],
                    )
                    service.repository.save_result(
                        SourceSnapshot(
                            snapshot_id=f"snap-{index}", provider="law.go.kr", source_document_id="article-cache",
                            raw_sha256=digest, snapshot_ref=snapshot_ref, parser_version="fixture-v1",
                            retrieved_at=retrieved_at, retrieval_status=RetrievalStatus.PARTIAL,
                            content_completeness=ContentCompleteness.PARTIAL, run_id=run_id, byte_length=1,
                        ), [document],
                    )
                    return document

                for index, article in enumerate(order, 1):
                    originals[article] = save_article(article, "100", index)
                for article in order:
                    selected = service.repository.get_document(document_id, version_id="100", locator=f"제{article}조")
                    self.assertEqual(selected.model_dump(), originals[article].model_dump())
                checked = service.verify_legal_citations(citations=[
                    {"document_id": document_id, "version_id": "100", "locator": f"제{article}조",
                     "quote": f"100 시행본 제{article}조의 본문이다."} for article in order
                ])
                self.assertEqual(len(checked.data["checks"]), 2)
                self.assertEqual(len(checked.sources), 2)
                for check, source, article in zip(checked.data["checks"], checked.sources, order):
                    self.assertTrue(check["quote_matches"])
                    self.assertTrue(check["locator_matches"])
                    self.assertEqual(check["status"], "unverified")
                    self.assertEqual(source.raw_sha256, originals[article].raw_sha256)
                    self.assertEqual(source.snapshot_ref, originals[article].snapshot_ref)
                self.assertFalse(checked.data["legal_conclusion_verified"])
                cached = service.get_legal_document(document_id=document_id, version_id="100")
                self.assertEqual(cached.status, ResponseStatus.PARTIAL)
                self.assertEqual(cached.data["content_completeness"], "partial")
                self.assertEqual(len(cached.data["sections"]), 1)
                save_article(60, "200", 3)
                with self.assertRaises(AmbiguousDocumentVersion):
                    service.repository.get_document(document_id, locator="제35조")
                ambiguous = service.verify_legal_citations(citations=[
                    {"document_id": document_id, "locator": "제35조", "quote": "100 시행본"}
                ])
                self.assertEqual(ambiguous.data["checks"][0]["status"], "unverified")
                self.assertIsNone(ambiguous.data["checks"][0]["quote_matches"])
                self.assertFalse(ambiguous.sources)
                selected = service.repository.get_document(document_id, version_id="100", locator="제60조")
                self.assertEqual(selected.raw_sha256, originals[60].raw_sha256)
                other_version = service.verify_legal_citations(citations=[
                    {"document_id": document_id, "version_id": "200", "locator": "제60조", "quote": "100 시행본"}
                ])
                self.assertFalse(other_version.data["checks"][0]["quote_matches"])
                self.assertEqual(other_version.sources[0].version_id, "200")
                for article in order:
                    saved = service.repository.documents_for_run(originals[article].collection_run_id)
                    self.assertEqual(saved[0].model_dump(), originals[article].model_dump())
                missing_locator = service.repository.get_document(document_id, version_id="100", locator="제99조")
                self.assertEqual(missing_locator.raw_sha256, originals[order[-1]].raw_sha256)
                self.assertIsNone(service.repository.get_document(document_id, version_id="missing", locator="제60조"))
                self.assertEqual(service.repository.source_status()["documents"], 3)
                self.assertEqual(service.repository.source_status()["snapshots"], 3)
                self.assertEqual(transport.calls, [])

    def test_incomplete_preview_citation_is_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            run_id = "run-preview"
            document_id = "olta:tax_tribunal:preview-1"
            service.repository.start_run(
                provider="olta.re.kr",
                action="search",
                request={"query": "쟁점 문구"},
                started_at="2026-09-16T00:00:00Z",
                run_id=run_id,
            )
            service.repository.save_result(
                SourceSnapshot(
                    snapshot_id="snap-preview",
                    provider="olta.re.kr",
                    source_document_id="preview-1",
                    raw_sha256="0" * 64,
                    snapshot_ref="v1/raw/preview.html",
                    parser_version="fixture-v1",
                    retrieved_at="2026-09-16T00:00:00Z",
                    retrieval_status=RetrievalStatus.PARTIAL,
                    content_completeness=ContentCompleteness.PARTIAL,
                    run_id=run_id,
                    byte_length=1,
                ),
                [
                    LegalDocument(
                        provider="olta.re.kr",
                        document_type="tax_tribunal",
                        source_document_id="preview-1",
                        document_id=document_id,
                        title="미리보기",
                        retrieved_at="2026-09-16T00:00:00Z",
                        raw_sha256="0" * 64,
                        snapshot_ref="v1/raw/preview.html",
                        parser_version="fixture-v1",
                        collection_run_id=run_id,
                        retrieval_status=RetrievalStatus.PARTIAL,
                        content_completeness=ContentCompleteness.PARTIAL,
                        sections=[
                            TextSection(
                                section_id="gist",
                                kind="summary",
                                locator="요약",
                                text="쟁점 문구가 포함된 검색 미리보기",
                            )
                        ],
                        metadata={"preview_only": True},
                    )
                ],
            )

            checked = service.verify_legal_citations(
                citations=[
                    {
                        "document_id": document_id,
                        "locator": "요약",
                        "quote": "쟁점 문구",
                    }
                ]
            )

            self.assertEqual(checked.status, ResponseStatus.PARTIAL)
            self.assertEqual(checked.data["checks"][0]["status"], "unverified")
            self.assertTrue(checked.data["checks"][0]["quote_matches"])
            self.assertTrue(checked.data["checks"][0]["locator_matches"])
            self.assertTrue(
                any("완전한 원문" in warning for warning in checked.data["checks"][0]["warnings"])
            )
            self.assertEqual(checked.coverage.filtered, 1)

    def test_k_ar_search_is_read_only_and_preserves_unapproved_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            curated = root / "knowledge" / "curated"
            curated.mkdir(parents=True)
            (curated / "sample.md").write_text("# 대손 검토\nK-AR-900 대손 요건을 확인한다.\n", encoding="utf-8")
            evidence = {"generated_at": "2026-09-14", "approved_count": 0, "evidence": [{"k_ar_id": "K-AR-900", "approved": False, "evidence_id": "EV-900"}]}
            evidence_path = curated / "k_ar_evidence.json"
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            before = evidence_path.read_bytes()
            service, _ = self.make_service(root, knowledge_dir=curated)
            response = service.search_knowledge(query="대손", k_ar_id="K-AR-900")
            self.assertEqual(response.status, ResponseStatus.OK)
            self.assertFalse(response.data["items"][0]["approved"])
            self.assertFalse(response.data["items"][0]["execution_enabled"])
            self.assertEqual(response.data["approved_count"], 0)
            self.assertEqual(evidence_path.read_bytes(), before)

    def test_private_knowledge_requires_explicit_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            curated = root / "knowledge" / "curated"
            curated.mkdir(parents=True)
            (curated / "sample.md").write_text("K-AR-900 대손 요건", encoding="utf-8")
            service, _ = self.make_service(root)
            response = service.search_knowledge(query="대손")
            self.assertEqual(response.status, ResponseStatus.OK)
            self.assertEqual(response.data["items"], [])
            self.assertEqual(response.coverage.disabled_providers, ["private-k-ar"])

    def test_optional_bridge_never_auto_installs_runtime_package(self):
        bridge = KoreanLawBridge(enabled=True)
        with patch(
            "taxax.legal.providers.korean_law_bridge.shutil.which",
            return_value=None,
        ):
            status = bridge.status()
            with self.assertRaises(ProviderError) as caught:
                asyncio.run(bridge.call("search_law", {"query": "법인세법"}))
        self.assertFalse(status["auto_install"])
        self.assertFalse(status["executable_available"])
        self.assertEqual(caught.exception.code, ErrorCode.POLICY_DISABLED)
        self.assertIn("사전 설치", str(caught.exception))

    def test_missing_operator_credential_is_mcp_error_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = LegalKnowledgeService(root, data_dir=root / "state", provider=LawGoProvider(credential=""))
            response = service.search_legal_sources(query="민법", target="law", upstream=True)
            self.assertEqual(response.status, ResponseStatus.BLOCKED)
            self.assertEqual(response.error.code, ErrorCode.AUTH_REQUIRED)
            self.assertEqual(response.coverage.failed_providers, ["law.go.kr"])

    def test_pii_like_query_is_blocked_before_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            service, transport = self.make_service(Path(directory))
            response = service.search_legal_sources(query="주민등록번호: 900101-1234567", target="law", upstream=True)
            self.assertEqual(response.status, ResponseStatus.BLOCKED)
            self.assertEqual(response.error.code, ErrorCode.ACCESS_DENIED)
            self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
