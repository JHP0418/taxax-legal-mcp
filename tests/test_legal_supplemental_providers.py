from __future__ import annotations

import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs

from taxax.legal.models import ErrorCode, ResponseStatus
from taxax.legal.providers.base import ProviderError, SupplementalResult
from taxax.legal.providers.nts import (
    ACTION_URL,
    DETAIL_ACTION,
    FILE_META_ACTION,
    SEARCH_ACTION,
    NtsProvider,
)
from taxax.legal.providers.olta import OltaProvider, parse_search_html
from taxax.legal.service import LegalKnowledgeService
from taxax.legal.transport import HttpResponse, SessionHttpTransport, TransportError

FIXTURES = Path(__file__).parent / "legal_fixtures"


def fixture_response(name: str, content_type: str) -> HttpResponse:
    return HttpResponse(
        url="https://fixture.invalid/source",
        status=200,
        headers={"content-type": content_type},
        body=(FIXTURES / name).read_bytes(),
    )


ENTRY = fixture_response("public_entry.html", "text/html; charset=utf-8")
NTS_SEARCH = fixture_response("nts_search.json", "application/json; charset=utf-8")
NTS_DETAIL = fixture_response("nts_detail.json", "application/json; charset=utf-8")
NTS_ATTACHMENT = fixture_response("nts_attachment.json", "application/json; charset=utf-8")
OLTA_SEARCH = fixture_response("olta_search.html", "text/html; charset=utf-8")
OLTA_DETAIL = fixture_response("olta_detail.html", "text/html; charset=utf-8")


class QueueTransport:
    def __init__(self, values):
        self.values = list(values)
        self.calls: list[dict[str, object]] = []
        self.resets = 0

    def request(self, url, *, method="GET", form=None, headers=None, max_bytes=None):
        self.calls.append({"url": url, "method": method, "form": dict(form or {}), "headers": dict(headers or {}), "max_bytes": max_bytes})
        if not self.values:
            raise AssertionError("unexpected transport call")
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def reset(self):
        self.resets += 1


class RawResponse:
    def __init__(self, body=b"{}", *, url="https://taxlaw.nts.go.kr/action.do", content_type="application/json", content_length=None):
        self.body = body
        self.status = 200
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def geturl(self):
        return self.url


class CapturingOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return self.responses.pop(0)


class SessionTransportTests(unittest.TestCase):
    def test_rejects_off_domain_input_and_final_redirect(self):
        opener = CapturingOpener([RawResponse(url="https://example.com/redirect")])
        transport = SessionHttpTransport("taxlaw.nts.go.kr", min_interval_seconds=0, opener=opener)
        with self.assertRaises(TransportError) as direct:
            transport.request("https://example.com/source")
        self.assertEqual(direct.exception.code, ErrorCode.ACCESS_DENIED)
        with self.assertRaises(TransportError) as redirected:
            transport.request(ACTION_URL)
        self.assertEqual(redirected.exception.code, ErrorCode.ACCESS_DENIED)

    def test_posts_form_and_enforces_declared_and_actual_size(self):
        opener = CapturingOpener([
            RawResponse(b"ok"),
            RawResponse(b"large", content_length=100),
            RawResponse(b"123456"),
        ])
        transport = SessionHttpTransport("taxlaw.nts.go.kr", min_interval_seconds=0, opener=opener)
        response = transport.request(ACTION_URL, method="POST", form={"actionId": SEARCH_ACTION})
        self.assertEqual(response.body, b"ok")
        self.assertEqual(parse_qs(opener.requests[0].data.decode()), {"actionId": [SEARCH_ACTION]})
        with self.assertRaises(TransportError):
            transport.request(ACTION_URL, max_bytes=10)
        with self.assertRaises(TransportError):
            transport.request(ACTION_URL, max_bytes=5)


class NtsProviderTests(unittest.TestCase):
    def provider(self, values, **kwargs):
        transport = QueueTransport(values)
        return NtsProvider(enabled=True, terms_confirmed=True, transport=transport, **kwargs), transport

    def test_policy_gate_requires_explicit_terms_confirmation(self):
        provider = NtsProvider(enabled=True, terms_confirmed=False, transport=QueueTransport([]))
        with self.assertRaises(ProviderError) as caught:
            provider.search("대손")
        self.assertEqual(caught.exception.code, ErrorCode.POLICY_DISABLED)

    def test_search_normalizes_results_and_cache_key_includes_filters(self):
        provider, transport = self.provider([ENTRY, NTS_SEARCH, NTS_SEARCH])
        first = provider.search("대손", collections=("ruling",), display=20)
        cached = provider.search("대손", collections=("ruling",), display=20)
        changed = provider.search("대손", collections=("ruling",), display=20, sort="date_desc")
        self.assertEqual(first.total, 2)
        self.assertEqual(first.items[0]["source_document_id"], "NTS001")
        self.assertEqual(first.items[0]["document_type"], "nts_interpretation")
        self.assertEqual(first.date_filter_location, "none")
        self.assertTrue(any("cache" in warning for warning in cached.warnings))
        self.assertEqual(len(transport.calls), 3)
        submitted = json.loads(transport.calls[-1]["form"]["paramData"])
        self.assertEqual(submitted["sortField"], "DCM_RGT_DTM/DESC")
        self.assertEqual(changed.request_parameters["parser_version"], "nts-taxlaw-v1")

    def test_search_result_highlight_markup_is_stripped_from_title_and_gist(self):
        """NTS는 검색어와 일치한 부분을 <!HS>...<!HE>로 감싸 돌려준다(HTML 태그가
        아니라 JSON 문자열 값 안에 그대로 들어있는 문자열이다). 사람이 읽을
        제목·요지에 그 마커가 그대로 남으면 안 된다."""
        rows = [
            {
                "DOC_ID": "NTS900",
                "TTL": "<!HS>부당행위계산부인<!HE> 해당여부",
                "DCM_RGT_DTM_S": "2026-01-01",
                "GIST_CNTN": "이 건은 <!HS>부당행위계산<!HE>부인 대상이 아니다.",
                "NTST_DCM_CL_NM": "서면질의",
            }
        ]
        payload = {"data": {SEARCH_ACTION: {"searchResultVO": {"collectionList": [{"nameEn": "question", "totalCount": "1", "resultList": rows}]}}}}
        page = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode())
        provider, _ = self.provider([ENTRY, page])
        result = provider.search("부당행위계산부인", collections=("ruling",))
        item = result.items[0]
        self.assertEqual(item["title"], "부당행위계산부인 해당여부")
        self.assertNotIn("<!HS>", item["title"])
        self.assertNotIn("<!HE>", item["title"])
        summary = next((section["text"] for section in item["sections"] if section["kind"] == "summary"), None)
        self.assertEqual(summary, "이 건은 부당행위계산부인 대상이 아니다.")

    def test_client_date_filter_marks_unscanned_range_partial(self):
        rows = [
            {"DOC_ID": f"OLD{index:03d}", "TTL": f"과거 문서 {index}", "DCM_RGT_DTM_S": "20200101", "NTST_DCM_CL_NM": "서면질의"}
            for index in range(50)
        ]
        payload = {"data": {SEARCH_ACTION: {"searchResultVO": {"collectionList": [{"nameEn": "question", "totalCount": "100", "resultList": rows}]}}}}
        page = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode())
        provider, _ = self.provider([ENTRY, page])
        result = provider.search("대손", collections=("ruling",), display=2, date_from="2026-01-01", max_pages=1)
        self.assertEqual(result.items, [])
        self.assertEqual(result.total, 100)
        self.assertTrue(result.partial)
        self.assertEqual(result.date_filter_location, "client")
        self.assertTrue(any("예산" in warning for warning in result.warnings))

    def test_schema_failure_resets_session_at_most_once(self):
        invalid = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, b"{}")
        provider, transport = self.provider([ENTRY, invalid, ENTRY, NTS_SEARCH])
        result = provider.search("대손", collections=("ruling",))
        self.assertEqual(result.total, 2)
        self.assertEqual(transport.resets, 1)

        failing, failing_transport = self.provider([invalid, invalid])
        with self.assertRaises(ProviderError):
            failing.search("대손", collections=("ruling",))
        self.assertEqual(failing_transport.resets, 1)

    def test_detail_validates_identifier_and_extracts_sections(self):
        provider, _ = self.provider([ENTRY, NTS_DETAIL])
        result = provider.detail("NTS001")
        self.assertEqual(result.items[0]["source_document_id"], "NTS001")
        self.assertEqual(result.items[0]["interpreted_on"], "2026-03-10")
        self.assertEqual({section["kind"] for section in result.items[0]["sections"]}, {"gist", "answer", "references"})
        self.assertEqual(len(result.raw_responses), 2)

    def test_detail_identifier_mismatch_is_not_accepted_after_reset(self):
        payload = json.loads((FIXTURES / "nts_detail.json").read_text(encoding="utf-8"))
        payload["data"][DETAIL_ACTION]["dcmDVO"]["ntstDcmId"] = "OTHER"
        mismatch = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, json.dumps(payload).encode())
        provider, transport = self.provider([ENTRY, mismatch, ENTRY, mismatch])
        with self.assertRaises(ProviderError) as caught:
            provider.detail("NTS001")
        self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)
        self.assertEqual(transport.resets, 1)

    def test_attachment_download_and_html_rejection(self):
        pdf = HttpResponse("https://taxlaw.nts.go.kr/file/download.do", 200, {"content-type": "application/pdf"}, b"%PDF-fixture")
        provider, transport = self.provider([ENTRY, NTS_ATTACHMENT, pdf])
        result = provider.attachment("FILE001")
        self.assertEqual(result.items[0]["document_type"], "nts_attachment")
        self.assertEqual(transport.calls[-1]["max_bytes"], 10 * 1024 * 1024)

        html = HttpResponse("https://taxlaw.nts.go.kr/file/download.do", 200, {"content-type": "text/html"}, b"<html>login</html>")
        blocked, blocked_transport = self.provider([ENTRY, NTS_ATTACHMENT, html, ENTRY, NTS_ATTACHMENT, html])
        with self.assertRaises(ProviderError) as caught:
            blocked.attachment("FILE001")
        self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)
        self.assertEqual(blocked_transport.resets, 1)


class OltaProviderTests(unittest.TestCase):
    def provider(self, values, **kwargs):
        transport = QueueTransport(values)
        return OltaProvider(enabled=True, terms_confirmed=True, transport=transport, **kwargs), transport

    def test_parser_preserves_category_provenance_and_preview_boundary(self):
        items, totals = parse_search_html(OLTA_SEARCH)
        self.assertEqual(totals, {"tax_tribunal": 2, "constitutional": 1, "local_gov_ruling": 4})
        self.assertEqual(items[0]["source_document_id"], "12345678")
        self.assertEqual(items[0]["metadata"]["category_code"], "40000")
        local = next(item for item in items if item["metadata"]["category"] == "local_gov_ruling")
        self.assertTrue(local["metadata"]["preview_only"])
        self.assertTrue(local["source_document_id"].startswith("preview-"))

    def test_integrated_search_is_partial_and_cache_is_filter_sensitive(self):
        provider, transport = self.provider([ENTRY, OLTA_SEARCH, OLTA_SEARCH])
        integrated = provider.search("취득세")
        cached = provider.search("취득세")
        category = provider.search("취득세", category="tax_tribunal")
        self.assertTrue(integrated.partial)
        self.assertEqual(integrated.total, 7)
        self.assertTrue(any("preview" in warning for warning in integrated.warnings))
        self.assertTrue(any("cache" in warning for warning in cached.warnings))
        self.assertEqual(category.total, 2)
        self.assertEqual([item["metadata"]["category"] for item in category.items], ["tax_tribunal"])
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(category.request_parameters["parser_version"], "olta-v1")

    def test_terms_gate_and_unverified_detail_type_fail_closed(self):
        gated = OltaProvider(enabled=True, terms_confirmed=False, transport=QueueTransport([]))
        with self.assertRaises(ProviderError) as terms:
            gated.search("취득세")
        self.assertEqual(terms.exception.code, ErrorCode.POLICY_DISABLED)
        provider, transport = self.provider([])
        with self.assertRaises(ProviderError) as detail:
            provider.detail("court", "12345678")
        self.assertEqual(detail.exception.code, ErrorCode.POLICY_DISABLED)
        self.assertEqual(transport.calls, [])
        with self.assertRaises(ProviderError):
            provider.attachment("x")

    def test_verified_detail_endpoint_and_schema_reset(self):
        provider, _ = self.provider([OLTA_DETAIL])
        detail = provider.detail("tax_tribunal", "12345678")
        self.assertEqual(detail.items[0]["source_document_id"], "12345678")
        self.assertGreater(len(detail.items[0]["sections"][0]["text"]), 100)

        invalid = HttpResponse("https://olta.re.kr/search", 200, {"content-type": "text/html"}, b"<html><body>changed</body></html>")
        recovered, transport = self.provider([ENTRY, invalid, ENTRY, OLTA_SEARCH])
        result = recovered.search("취득세")
        self.assertEqual(result.total, 7)
        self.assertEqual(transport.resets, 1)


class MaliciousSupplementalProvider:
    name = "taxlaw.nts.go.kr"
    enabled = True

    def capabilities(self):
        return {"parser_version": "malicious-fixture-v1"}

    def search(self, query, **kwargs):
        response = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, b"{}")
        item = {
            "source_document_id": "BAD001",
            "title": "외부 URL 문서",
            "document_type": "nts_interpretation",
            "official_url": "https://example.com/stolen",
            "sections": [],
            "metadata": {"preview_only": True, "raw_response_index": 0, "parser_version": "malicious-fixture-v1"},
        }
        return SupplementalResult(self.name, "nts_search_result", {"query": query, "parser_version": "malicious-fixture-v1"}, response, {}, [item], 1, 1, 10, raw_responses=(response,))


class SupplementalServiceTests(unittest.TestCase):
    def make_service(self, root: Path, *, nts=None, olta=None):
        return LegalKnowledgeService(
            root,
            data_dir=root / "state",
            nts_provider=nts or NtsProvider(enabled=False, terms_confirmed=False, transport=QueueTransport([])),
            olta_provider=olta or OltaProvider(enabled=False, terms_confirmed=False, transport=QueueTransport([])),
        )

    def test_nts_search_persists_and_reuses_exact_completed_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = QueueTransport([ENTRY, NTS_SEARCH])
            nts = NtsProvider(enabled=True, terms_confirmed=True, transport=transport)
            service = self.make_service(root, nts=nts)
            first = service.search_legal_sources(query="대손", provider="nts", upstream=True)
            second = service.search_legal_sources(query="대손", provider="taxlaw.nts.go.kr", upstream=True)
            self.assertEqual(first.status, ResponseStatus.OK)
            self.assertEqual(first.pagination.total, 2)
            self.assertEqual(len(first.data["items"]), 2)
            self.assertEqual(len(transport.calls), 2)
            self.assertTrue(any("재사용" in warning for warning in second.warnings))
            self.assertEqual(first.sources[0].provider, "taxlaw.nts.go.kr")
            raw_files = list((root / "state" / "v1" / "raw" / "taxlaw.nts.go.kr").rglob("*.*"))
            self.assertEqual(len(raw_files), 1)
            local = service.search_legal_sources(query="대손", provider="nts", jurisdiction="KR")
            self.assertEqual(local.pagination.total, 1)

    def test_nts_detail_is_stored_and_read_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transport = QueueTransport([ENTRY, NTS_DETAIL])
            nts = NtsProvider(enabled=True, terms_confirmed=True, transport=transport)
            service = self.make_service(root, nts=nts)
            remote = service.get_legal_document(provider="nts", source_document_id="NTS001", refresh=True)
            self.assertEqual(remote.status, ResponseStatus.OK)
            self.assertEqual(remote.data["content_completeness"], "complete")
            self.assertEqual(len(remote.data["sections"]), 3)
            local = service.get_legal_document(document_id=remote.data["document_id"])
            self.assertEqual(local.data["raw_sha256"], remote.data["raw_sha256"])
            self.assertEqual(len(transport.calls), 2)

    def test_olta_preview_propagates_partial_and_local_tax_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            olta = OltaProvider(enabled=True, terms_confirmed=True, transport=QueueTransport([ENTRY, OLTA_SEARCH]))
            service = self.make_service(root, olta=olta)
            result = service.search_legal_sources(query="취득세", provider="olta", upstream=True)
            self.assertEqual(result.status, ResponseStatus.PARTIAL)
            self.assertTrue(result.coverage.truncated)
            self.assertEqual(result.data["items"][0]["provider"], "olta.re.kr")
            stored = service.repository.get_document(result.data["items"][0]["document_id"])
            self.assertEqual(stored.metadata["tax_scope"], "local")
            self.assertTrue(stored.metadata["preview_only"])

    def test_all_disabled_returns_blocked_with_provider_status(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            result = service.search_legal_sources(query="취득세", provider="all", upstream=True)
            self.assertEqual(result.status, ResponseStatus.BLOCKED)
            self.assertEqual(result.error.code, ErrorCode.POLICY_DISABLED)
            self.assertEqual(set(result.coverage.disabled_providers), {"taxlaw.nts.go.kr", "olta.re.kr"})

    def test_off_domain_source_url_is_rejected_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root, nts=MaliciousSupplementalProvider())
            result = service.search_legal_sources(query="대손", provider="nts", upstream=True)
            self.assertEqual(result.status, ResponseStatus.BLOCKED)
            self.assertEqual(result.error.code, ErrorCode.ACCESS_DENIED)
            self.assertEqual(service.repository.source_status()["documents"], 0)


if __name__ == "__main__":
    unittest.main()
