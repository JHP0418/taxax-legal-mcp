from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from taxax.legal.evidence import rank_document
from taxax.legal.models import (
    CitationCheck,
    CitationStatus,
    ContentCompleteness,
    ErrorCode,
    EvidenceScore,
    LegalDocument,
    ResearchReport,
    ResponseStatus,
    RetrievalStatus,
    TextSection,
)
from taxax.legal.providers.law_go import LawGoProvider
from taxax.legal.router import route_tax_issue
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
        return HttpResponse(
            url=url,
            status=200,
            headers={"content-type": "application/json"},
            body=(FIXTURES / fixture).read_bytes(),
        )


class LegalResearchTests(unittest.TestCase):
    def make_service(self, root: Path):
        transport = RoutedTransport()
        provider = LawGoProvider(credential="operator-secret", transport=transport)
        return LegalKnowledgeService(root, data_dir=root / "state", provider=provider), transport

    def seed_statute(self, service: LegalKnowledgeService) -> str:
        response = service.get_legal_document(
            target="law",
            source_document_id="000123",
            identifier_kind="ID",
            refresh=True,
        )
        self.assertEqual(response.status, ResponseStatus.OK)
        return response.data["document_id"]

    def test_offline_research_routes_ranks_and_persists_scoped_report(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            document_id = self.seed_statute(service)
            response = service.research_tax_issue(
                issue="법인세 대손금 손금산입",
                tax_type="법인세",
                transaction_date="2026-01-02",
                research_as_of="2026-09-14",
                knowledge_cutoff="2025-12-31",
                budget={
                    "max_remote_requests": 100,
                    "max_detail_documents": 100,
                    "max_seconds": 1000,
                },
                upstream=False,
                principal_id="employee-1",
                org_id="office-1",
            )
            self.assertEqual(response.status, ResponseStatus.PARTIAL)
            self.assertEqual(response.data["route"]["tax_scope"], "national")
            self.assertEqual(response.data["route"]["providers"], ["law.go.kr", "taxlaw.nts.go.kr"])
            self.assertIn(document_id, response.data["candidate_documents"])
            ranking = next(item for item in response.data["evidence_ranking"] if item["document_id"] == document_id)
            self.assertIn("post_knowledge_cutoff", ranking["flags"])
            self.assertTrue(ranking["selected_for_detail"])
            self.assertFalse(response.data["legal_conclusion_confirmed"])
            self.assertEqual(response.data["budget"]["max_remote_requests"], 20)
            self.assertEqual(response.data["budget"]["max_detail_documents"], 8)
            self.assertEqual(response.data["budget"]["max_seconds"], 60.0)
            self.assertEqual(response.data["budget"]["consumed_remote_requests"], 0)

            report_id = response.data["report_id"]
            same_scope = service.get_research_report(
                report_id=report_id,
                principal_id="employee-1",
                org_id="office-1",
            )
            self.assertEqual(same_scope.data["report_id"], report_id)
            for principal_id, org_id in (("employee-2", "office-1"), ("employee-1", "office-2")):
                hidden = service.get_research_report(
                    report_id=report_id,
                    principal_id=principal_id,
                    org_id=org_id,
                )
                self.assertEqual(hidden.status, ResponseStatus.ERROR)
                self.assertEqual(hidden.error.code, ErrorCode.NOT_FOUND)

    def test_zero_remote_budget_stops_before_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            service, transport = self.make_service(Path(directory))
            response = service.research_tax_issue(
                issue="법인세 대손금 손금산입",
                tax_type="법인세",
                transaction_date="2026-01-02",
                budget=0,
                upstream=True,
            )
            self.assertEqual(response.status, ResponseStatus.PARTIAL)
            self.assertTrue(response.data["budget"]["exhausted"])
            self.assertEqual(response.data["budget"]["consumed_remote_requests"], 0)
            self.assertTrue(response.data["unresearched_scope"])
            self.assertEqual(transport.calls, [])

    def test_long_case_narrative_is_accepted_and_truncated_before_upstream_search(self):
        """issue는 검색 키워드가 아니라 사실관계 서술이라 500자를 훌쩍 넘을 수 있다.

        실제 상담 사례를 붙여넣은 것과 비슷한 700자 서술을 넣어도 거부되지
        않아야 하고, law.go.kr에 실제로 나가는 query 파라미터는 provider가
        받아들이는 500자 이내로 줄어 있어야 한다(법제처 검색은 query 500자
        제한이 있다).
        """
        with tempfile.TemporaryDirectory() as directory:
            service, transport = self.make_service(Path(directory))
            long_issue = "비상장법인 A는 특수관계에 있는 개인 B에게 발행주식총수의 30%에 해당하는 지분을 " * 12
            self.assertGreater(len(long_issue), 500)
            self.assertLessEqual(len(long_issue), 4000)
            response = service.research_tax_issue(
                issue=long_issue,
                tax_type="상속세 및 증여세",
                transaction_date="2026-01-02",
                budget={"max_remote_requests": 3, "max_detail_documents": 0, "max_seconds": 10},
            )
            self.assertIn(response.status, (ResponseStatus.OK, ResponseStatus.PARTIAL))
            self.assertNotEqual(response.status, ResponseStatus.ERROR)
            queries = [call["query"] for call in transport.calls if "query" in call]
            self.assertTrue(queries, "law.go.kr에 검색 요청이 전혀 나가지 않았습니다")
            for query in queries:
                self.assertLessEqual(len(query), 500)

    def test_over_length_issue_is_rejected_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            response = service.research_tax_issue(issue="가" * 4001)
            self.assertEqual(response.status, ResponseStatus.ERROR)
            self.assertEqual(response.error.code, ErrorCode.INVALID_REQUEST)
            self.assertIn("4000", response.error.message)

    def test_future_research_as_of_is_rejected_but_past_and_timezone_slack_pass(self):
        """research_as_of는 "이 시점 기준으로 조사했다"는 출처 표기다.

        미래 날짜를 받아주면 아직 오지 않은 시점의 법 상태를 조사한 것처럼
        보고서에 남는다. 다만 UTC와 KST가 하루 차이 날 수 있어 하루치는 허용한다.
        """
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            far_future = service.research_tax_issue(issue="법인세 대손금", research_as_of="2099-01-01", upstream=False)
            self.assertEqual(far_future.status, ResponseStatus.ERROR)
            self.assertEqual(far_future.error.code, ErrorCode.INVALID_REQUEST)
            self.assertIn("미래", far_future.error.message)

            past = service.research_tax_issue(issue="법인세 대손금", research_as_of="2020-01-01", upstream=False)
            self.assertNotEqual(past.status, ResponseStatus.ERROR)

    def test_report_pagination_and_scope_change_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _ = self.make_service(Path(directory))
            report = ResearchReport(
                report_id="research-" + "a" * 32,
                issue="합성 보고서",
                created_at="2026-09-14T00:00:00Z",
                updated_at="2026-09-14T00:00:00Z",
                evidence_ranking=[
                    EvidenceScore(document_id=f"fixture:{index}", score=50 - index)
                    for index in range(3)
                ],
            )
            service.report_repository.save(report, principal_id="employee-1", org_id="office-1")
            first = service.get_research_report(
                report_id=report.report_id,
                limit=2,
                principal_id="employee-1",
                org_id="office-1",
            )
            self.assertEqual(first.pagination.total, 3)
            self.assertEqual(first.pagination.next_cursor, "2")
            self.assertEqual(len(first.data["evidence_ranking"]), 2)
            second = service.get_research_report(
                report_id=report.report_id,
                cursor="2",
                limit=2,
                principal_id="employee-1",
                org_id="office-1",
            )
            self.assertIsNone(second.pagination.next_cursor)
            self.assertEqual(len(second.data["evidence_ranking"]), 1)
            with self.assertRaises(PermissionError):
                service.report_repository.save(report, principal_id="employee-2", org_id="office-1")

    def test_router_and_evidence_keep_later_case_as_flagged_material(self):
        route, missing, _ = route_tax_issue(
            "법인세 대손금 손금산입",
            tax_type="법인세",
            jurisdiction="KR",
            has_temporal_input=True,
        )
        self.assertEqual(missing, [])
        # lsHistory는 법제처가 HTML로만 제공해 구조화 수집이 불가능하므로 조사
        # 대상에 넣지 않는다(넣으면 매 조사마다 실패가 하나씩 확정으로 붙는다).
        self.assertNotIn("lsHistory", route.official_targets)
        self.assertIn("law", route.official_targets)
        document = LegalDocument(
            provider="law.go.kr",
            document_type="precedent",
            source_document_id="case-1",
            document_id="law-go:prec:case-1",
            title="대손금 손금산입 판결",
            court="대법원",
            case_no="2026두100",
            decided_on="2026-06-01",
            retrieved_at="2026-09-14T00:00:00Z",
            raw_sha256="1" * 64,
            snapshot_ref="v1/raw/fixture.json",
            parser_version="fixture-v1",
            collection_run_id="run-fixture",
            retrieval_status=RetrievalStatus.SUCCESS,
            content_completeness=ContentCompleteness.COMPLETE,
            sections=[
                TextSection(
                    section_id="reasoning",
                    kind="reasoning",
                    locator="판결이유",
                    text="법인세법상 대손금 손금산입 요건을 판단한다.",
                )
            ],
        )
        check = CitationCheck(
            document_id=document.document_id,
            status=CitationStatus.VERIFIED,
            document_exists=True,
            metadata_matches=True,
            quote_matches=True,
            locator_matches=True,
        )
        ranked = rank_document(
            document,
            route,
            citation_check=check,
            transaction_date="2025-12-31",
            knowledge_cutoff="2025-12-31",
        )
        self.assertIn("post_knowledge_cutoff", ranked.flags)
        self.assertIn("post_transaction_material", ranked.flags)
        self.assertEqual(ranked.dimensions["citation_verification"], 5)
        self.assertGreater(ranked.score, 0)


if __name__ == "__main__":
    unittest.main()
