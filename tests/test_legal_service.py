from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taxax.legal.models import ErrorCode, ResponseStatus
from taxax.legal.providers.base import ProviderError
from taxax.legal.providers.korean_law_bridge import KoreanLawBridge
from taxax.legal.providers.law_go import LawGoProvider
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
