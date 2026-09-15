from __future__ import annotations

import json
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

from taxax.legal.budget import RemoteRequestBudget, activate_remote_budget
from taxax.legal.models import ErrorCode
from taxax.legal.providers.base import LegalTarget, ProviderError
from taxax.legal.providers.law_go import LawGoProvider, extract_items, parse_response
from taxax.legal.transport import HttpResponse, HttpTransport, TransportError, redact_text, redact_url

FIXTURES = Path(__file__).parent / "legal_fixtures"


class FixtureTransport:
    def __init__(self, body: bytes, content_type: str = "application/json"):
        self.body = body
        self.content_type = content_type
        self.calls: list[tuple[str, dict[str, object], tuple[str, ...]]] = []

    def request(self, url, *, params=None, secrets=()):
        self.calls.append((url, dict(params or {}), secrets))
        return HttpResponse(url=url, status=200, headers={"content-type": self.content_type}, body=self.body)


class FakeHttpResponse:
    def __init__(self, body: bytes = b"{}", status: int = 200):
        self._body = body
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body

    def geturl(self):
        return "https://www.law.go.kr/DRF/lawSearch.do"


class SequenceOpener:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0
        self.timeouts: list[float] = []

    def open(self, request, timeout):
        self.calls += 1
        self.timeouts.append(timeout)
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class LawGoParserTests(unittest.TestCase):
    def test_json_and_xml_list_parse(self):
        for name, media, response_type in (
            ("law_search.json", "application/json", "JSON"),
            ("law_search.xml", "application/xml", "XML"),
        ):
            response = HttpResponse("https://www.law.go.kr/DRF/lawSearch.do", 200, {"content-type": media}, (FIXTURES / name).read_bytes())
            payload = parse_response(response, response_type)
            items, total, page = extract_items(payload, LegalTarget.LAW)
            self.assertEqual(total, 1)
            self.assertEqual(page, 1)
            self.assertEqual(items[0]["source_document_id"], "12345")
            self.assertEqual(items[0]["metadata"]["upstream_identifiers"]["ID"], ["000123"])
            self.assertEqual(items[0]["metadata"]["upstream_identifiers"]["MST"], ["12345"])

    def test_nested_detail_combines_metadata_and_sections(self):
        response = HttpResponse("https://www.law.go.kr/DRF/lawService.do", 200, {"content-type": "application/json"}, (FIXTURES / "law_detail.json").read_bytes())
        items, _, _ = extract_items(parse_response(response, "JSON"), LegalTarget.LAW)
        self.assertEqual(len(items), 1)
        self.assertEqual({section["kind"] for section in items[0]["sections"]}, {"articles", "addenda"})

    def test_zero_results_is_distinct_from_parse_failure(self):
        response = HttpResponse("https://www.law.go.kr/DRF/lawSearch.do", 200, {"content-type": "application/json"}, b'{"LawSearch":{"totalCnt":0,"page":1,"law":[]}}')
        items, total, page = extract_items(parse_response(response, "JSON"), LegalTarget.LAW)
        self.assertEqual((items, total, page), ([], 0, 1))

    def test_html_empty_and_entity_payloads_are_rejected(self):
        cases = (
            ((FIXTURES / "error_login.html").read_bytes(), "JSON", ErrorCode.AUTH_FAILED),
            (b" ", "JSON", ErrorCode.INCOMPLETE_RESULT),
            (b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]><x>&y;</x>', "XML", ErrorCode.PARSE_ERROR),
        )
        for body, response_type, code in cases:
            with self.subTest(code=code), self.assertRaises(ProviderError) as caught:
                parse_response(HttpResponse("https://www.law.go.kr/DRF/lawService.do", 200, {"content-type": "text/plain"}, body), response_type)
            self.assertEqual(caught.exception.code, code)


class LawGoProviderTests(unittest.TestCase):
    def provider(self, fixture="law_search.json", content_type="application/json"):
        transport = FixtureTransport((FIXTURES / fixture).read_bytes(), content_type)
        return LawGoProvider(credential="operator-secret", transport=transport), transport

    def test_search_contract_hides_credential(self):
        provider, transport = self.provider()
        result = provider.search(LegalTarget.LAW, query="법인세법", filters={"efYd": "20260101"})
        self.assertEqual(result.total, 1)
        self.assertNotIn("OC", result.request_parameters)
        self.assertEqual(transport.calls[0][1]["OC"], "operator-secret")
        self.assertNotIn("operator-secret", LawGoProvider.detail_url(LegalTarget.LAW, "000123"))

    def test_unsupported_filter_and_itmno_query_are_rejected(self):
        provider, _ = self.provider()
        with self.assertRaises(ProviderError) as unsupported:
            provider.search(LegalTarget.LAW, query="법인세법", filters={"unknown": "x"})
        self.assertEqual(unsupported.exception.code, ErrorCode.INVALID_REQUEST)
        with self.assertRaises(ProviderError):
            provider.search(LegalTarget.NTS_INTERPRETATION, query="대손", filters={"itmno": "1"})

    def test_effective_law_mst_requires_date_and_id_omits_date(self):
        provider, transport = self.provider("law_detail.json")
        with self.assertRaises(ProviderError) as missing:
            provider.detail(LegalTarget.EFFECTIVE_LAW, identifier="12345", identifier_kind="MST")
        self.assertEqual(missing.exception.code, ErrorCode.TEMPORAL_UNRESOLVED)
        mst = provider.detail(LegalTarget.EFFECTIVE_LAW, identifier="12345", identifier_kind="MST", effective_on="2026-01-02")
        self.assertEqual(transport.calls[-1][1]["efYd"], "20260102")
        by_id = provider.detail(LegalTarget.EFFECTIVE_LAW, identifier="000123", identifier_kind="ID", effective_on="2026-01-02")
        self.assertNotIn("efYd", transport.calls[-1][1])
        self.assertTrue(any("무시" in warning for warning in by_id.warnings))
        self.assertEqual(mst.items[0]["version_id"], "12345")

    def test_identifier_kind_must_match_response_field(self):
        provider, _ = self.provider("law_detail.json")
        with self.assertRaises(ProviderError) as caught:
            provider.detail(LegalTarget.LAW, identifier="12345", identifier_kind="ID")
        self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)
        self.assertNotIn("12345", str(caught.exception.details))

    def test_missing_credential_fails_closed(self):
        provider = LawGoProvider(credential="", transport=FixtureTransport(b"{}"))
        with self.assertRaises(ProviderError) as caught:
            provider.search(LegalTarget.LAW, query="민법")
        self.assertEqual(caught.exception.code, ErrorCode.AUTH_REQUIRED)


class TransportTests(unittest.TestCase):
    def test_retry_then_success(self):
        opener = SequenceOpener([URLError("temporary"), FakeHttpResponse()])
        sleeps: list[float] = []
        transport = HttpTransport(min_interval_seconds=0, max_attempts=2, sleeper=sleeps.append, opener=opener)
        response = transport.request("https://www.law.go.kr/DRF/lawSearch.do")
        self.assertEqual(response.attempts, 2)
        self.assertEqual(opener.calls, 2)
        self.assertEqual(sleeps, [1])

    def test_auth_failure_is_not_retried(self):
        headers = Message()
        error = HTTPError("https://www.law.go.kr/DRF/lawSearch.do", 403, "denied", headers, None)
        opener = SequenceOpener([error])
        transport = HttpTransport(min_interval_seconds=0, max_attempts=3, opener=opener)
        with self.assertRaises(TransportError) as caught:
            transport.request("https://www.law.go.kr/DRF/lawSearch.do")
        self.assertEqual(caught.exception.code, ErrorCode.AUTH_FAILED)
        self.assertEqual(opener.calls, 1)

    def test_socket_timeout_is_bounded_by_remaining_research_budget(self):
        """max_seconds가 짧아도 첫 HTTP 시도가 기본 30초까지 그대로 기다리던 문제.

        consume_remote_request()의 check_time()은 시도를 "시작하기 전"에만
        검사하므로, 그 검사를 통과하고 나면 남은 예산이 1초든 30초든 기본
        socket timeout(여기선 30초)을 그대로 쓰고 있었다. 활성 예산이 있으면
        그 남은 시간이 실제 timeout 값에 반영돼야 한다.
        """
        opener = SequenceOpener([FakeHttpResponse()])
        transport = HttpTransport(min_interval_seconds=0, timeout_seconds=30.0, opener=opener)
        budget = RemoteRequestBudget(max_requests=10, max_seconds=5.0)
        with activate_remote_budget(budget):
            transport.request("https://www.law.go.kr/DRF/lawSearch.do")
        self.assertEqual(opener.timeouts, [5.0])

    def test_socket_timeout_is_not_shortened_without_an_active_budget(self):
        opener = SequenceOpener([FakeHttpResponse()])
        transport = HttpTransport(min_interval_seconds=0, timeout_seconds=30.0, opener=opener)
        transport.request("https://www.law.go.kr/DRF/lawSearch.do")
        self.assertEqual(opener.timeouts, [30.0])

    def test_off_domain_and_secret_redaction(self):
        transport = HttpTransport(min_interval_seconds=0)
        with self.assertRaises(TransportError) as caught:
            transport.request("http://example.com/law")
        self.assertEqual(caught.exception.code, ErrorCode.ACCESS_DENIED)
        self.assertNotIn("secret", redact_url("https://www.law.go.kr/x?OC=secret&query=law"))
        self.assertNotIn("secret", redact_text("Authorization=secret", ("secret",)))


if __name__ == "__main__":
    unittest.main()
