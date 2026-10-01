from __future__ import annotations

import json
import os
import socket
import ssl
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError, URLError

from taxax.legal.budget import (
    RemoteBudgetExceeded,
    RemoteRequestBudget,
    activate_remote_budget,
    consume_remote_request,
)
from taxax.legal.models import ErrorCode
from taxax.legal.providers.base import LegalTarget, ProviderError
from taxax.legal.providers.law_go import CONTRACTS, _FIELD_ALIASES, LawGoProvider, extract_items, parse_response
from taxax.legal.transport import (
    HttpResponse,
    HttpTransport,
    SessionHttpTransport,
    SharedRateLimiter,
    TransportError,
    redact_text,
    redact_url,
)

_ROOT = Path(__file__).resolve().parents[1]
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
    def __init__(self, body: bytes = b"{}", status: int = 200, *, content_length: str | None = None):
        self._body = body
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.read_sizes: list[int | None] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=None):
        self.read_sizes.append(size)
        return self._body if size is None else self._body[:size]

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

    def test_law_detail_keeps_nested_paragraphs_and_subparagraphs(self):
        payload = {"법령": {
            "기본정보": {"법령ID": "001563", "법령명_한글": "법인세법", "시행일자": "20260101"},
            "조문": {"조문단위": {
                "조문번호": "19", "조문가지번호": "2", "조문제목": "대손금의 손금불산입",
                "조문내용": "제19조의2(대손금의 손금불산입)",
                "항": [
                    {"항번호": "①", "항내용": "① 회수할 수 없는 채권을 손금에 산입한다."},
                    {"항번호": "②", "항내용": "② 다음 채권에는 적용하지 아니한다.",
                     "호": [{"호번호": "1.", "호내용": "1. 구상채권"},
                            {"호번호": "2.", "호내용": "2. 가지급금", "목": {"목내용": "가. 특수관계인"}}]},
                ],
            }},
        }}
        items, _, _ = extract_items(payload, LegalTarget.LAW)
        self.assertEqual(len(items), 1)
        article = next(section for section in items[0]["sections"] if section["kind"] == "articles")
        self.assertEqual(article["locator"], "제19조의2")
        for text in ("제19조의2(대손금의 손금불산입)", "① 회수할 수 없는", "② 다음 채권", "1. 구상채권", "2. 가지급금", "가. 특수관계인"):
            self.assertIn(text, article["text"])

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

    def test_effective_law_mst_requires_date_and_dated_id_fails_closed(self):
        provider, transport = self.provider("law_detail.json")
        with self.assertRaises(ProviderError) as missing:
            provider.detail(LegalTarget.EFFECTIVE_LAW, identifier="12345", identifier_kind="MST")
        self.assertEqual(missing.exception.code, ErrorCode.TEMPORAL_UNRESOLVED)
        mst = provider.detail(LegalTarget.EFFECTIVE_LAW, identifier="12345", identifier_kind="MST", effective_on="2026-01-02")
        self.assertEqual(transport.calls[-1][1]["efYd"], "20260102")
        with self.assertRaises(ProviderError) as dated_id:
            provider.detail(LegalTarget.EFFECTIVE_LAW, identifier="000123", identifier_kind="ID", effective_on="2026-01-02")
        self.assertEqual(dated_id.exception.code, ErrorCode.TEMPORAL_UNRESOLVED)
        self.assertEqual(len(transport.calls), 1, "무시되는 기준일을 상류에 전송해서는 안 됩니다")
        self.assertEqual(mst.items[0]["version_id"], "12345")

    def test_mst_request_keeps_version_when_detail_omits_serial(self):
        payload = {"법령": {"기본정보": {"법령ID": "003608", "법령명_한글": "법인세법 시행령", "시행일자": "20260227"},
                            "조문": {"조문단위": {"조문번호": "19", "조문내용": "제19조(손금)\n① 조건이 있다."}}}}
        transport = FixtureTransport(json.dumps(payload, ensure_ascii=False).encode())
        provider = LawGoProvider(credential="operator-secret", transport=transport)
        for mst in ("283635", "290843"):
            result = provider.detail(LegalTarget.LAW, identifier=mst, identifier_kind="MST")
            self.assertEqual(result.items[0]["version_id"], mst)
            self.assertEqual(result.items[0]["metadata"]["version_identity_source"], "request_mst")
            self.assertEqual(result.items[0]["metadata"]["requested_mst"], mst)

    def test_identifier_kind_must_match_response_field(self):
        provider, _ = self.provider("law_detail.json")
        with self.assertRaises(ProviderError) as caught:
            provider.detail(LegalTarget.LAW, identifier="12345", identifier_kind="ID")
        self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)
        self.assertNotIn("12345", str(caught.exception.details))

    def test_nts_interpretation_is_index_only_and_keeps_the_body_link(self):
        """법제처는 국세청 법령해석을 색인만 제공하고 본문은 갖고 있지 않다.

        검색 응답의 각 항목은 법령해석상세링크로 taxlaw.nts.go.kr을 가리킨다.
        그런데도 detail을 지원한다고 선언하면 lawService.do에 본문을 요청하게
        되고, 법제처는 제공하지 않는 요청에 "미신청된 목록/본문에 대한
        접근입니다" 안내를 돌려준다. 우리는 그것을 AUTH_FAILED로 읽어, 신청이
        모두 끝난 계정에서도 조사마다 인증 실패가 쌓였다.
        """
        contract = CONTRACTS[LegalTarget.NTS_INTERPRETATION]
        self.assertTrue(contract.search_supported, "목록 조회는 법제처가 제공합니다.")
        self.assertFalse(
            contract.detail_supported,
            "법제처에 없는 본문을 요청하면 인증 실패로 오인됩니다.",
        )
        self.assertIn("법령해석상세링크", _FIELD_ALIASES["official_url"])

    def test_a_law_name_in_an_id_field_is_refused_here_not_upstream(self):
        """law_id에 "소득세법"을 넣으면 upstream 실패로 이어지던 문제.

        식별자 검증 정규식이 한글을 허용하고 있었다. admrul의 LM이 법령명을
        받는 자리라 넓게 잡아 둔 것인데, 숫자만 받는 ID/MST에도 같은 규칙이
        적용됐다. 그래서 사람이 자연스럽게 넣는 법령명이 그대로 ID=소득세법으로
        법제처에 전송됐고, 거기서 실패한 결과가 돌아와 "이 기능은 통째로
        죽었다"는 진단이 나왔다. 여기서 막으면 무엇을 해야 하는지 알 수 있다.
        """
        provider, transport = self.provider("law_detail.json")
        with self.assertRaises(ProviderError) as caught:
            provider.detail(LegalTarget.LAW, identifier="소득세법", identifier_kind="ID")
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertIn("search_legal_sources", str(caught.exception))
        self.assertEqual(transport.calls, [], "잘못된 입력인데 upstream을 호출했습니다.")

        # 숫자 식별자는 그대로 통과해야 한다.
        provider.detail(LegalTarget.LAW, identifier="000123", identifier_kind="ID")
        self.assertEqual(len(transport.calls), 1)

    def test_missing_credential_fails_closed(self):
        provider = LawGoProvider(credential="", transport=FixtureTransport(b"{}"))
        with self.assertRaises(ProviderError) as caught:
            provider.search(LegalTarget.LAW, query="민법")
        self.assertEqual(caught.exception.code, ErrorCode.AUTH_REQUIRED)


class TransportTests(unittest.TestCase):
    def test_drf_404_is_indeterminate_after_bounded_retries(self):
        url = "https://www.law.go.kr/DRF/lawSearch.do"
        def missing():
            return HTTPError(url, 404, "missing", Message(), None)
        recovered = SequenceOpener([missing(), FakeHttpResponse()])
        transport = HttpTransport(min_interval_seconds=0, max_attempts=2, sleeper=lambda _: None, opener=recovered)
        self.assertEqual(transport.request(url).attempts, 2)
        self.assertEqual(recovered.calls, 2)
        failed = SequenceOpener([missing(), missing()])
        transport = HttpTransport(min_interval_seconds=0, max_attempts=2, sleeper=lambda _: None, opener=failed)
        with self.assertRaises(TransportError) as caught:
            transport.request(url)
        self.assertEqual(caught.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)
        self.assertEqual(caught.exception.details["http_status"], 404)
        self.assertEqual(caught.exception.details["attempts"], 2)
        response_404 = SequenceOpener([FakeHttpResponse(status=404), FakeHttpResponse()])
        self.assertEqual(
            HttpTransport(min_interval_seconds=0, max_attempts=2, sleeper=lambda _: None, opener=response_404).request(url).attempts,
            2,
        )

    def test_non_drf_404_remains_not_found_without_retry(self):
        url = "https://www.law.go.kr/other"
        opener = SequenceOpener([HTTPError(url, 404, "missing", Message(), None)])
        with self.assertRaises(TransportError) as caught:
            HttpTransport(min_interval_seconds=0, max_attempts=3, opener=opener).request(url)
        self.assertEqual(caught.exception.code, ErrorCode.NOT_FOUND)
        self.assertEqual(opener.calls, 1)

    def test_bounded_success_body_refuses_oversize_without_truncation(self):
        url = "https://www.law.go.kr/DRF/lawService.do"
        exact = FakeHttpResponse(b"1234")
        self.assertEqual(HttpTransport(min_interval_seconds=0, max_response_bytes=4, opener=SequenceOpener([exact])).request(url).body, b"1234")
        self.assertEqual(exact.read_sizes, [5])
        for content_length in (None, "5"):
            with self.subTest(content_length=content_length):
                oversized = FakeHttpResponse(b"12345", content_length=content_length)
                with self.assertRaises(TransportError) as caught:
                    HttpTransport(min_interval_seconds=0, max_response_bytes=4, opener=SequenceOpener([oversized])).request(url)
                self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)
                self.assertLessEqual(max(oversized.read_sizes or [0]), 5)

    def test_retry_then_success(self):
        opener = SequenceOpener([URLError("temporary"), FakeHttpResponse()])
        sleeps: list[float] = []
        transport = HttpTransport(min_interval_seconds=0, max_attempts=2, sleeper=sleeps.append, opener=opener)
        response = transport.request("https://www.law.go.kr/DRF/lawSearch.do")
        self.assertEqual(response.attempts, 2)
        self.assertEqual(opener.calls, 2)
        self.assertEqual(sleeps, [1])

    def test_default_does_not_amplify_official_failures(self):
        url = "https://www.law.go.kr/DRF/lawSearch.do"
        cases = (
            (TimeoutError("timed out"), ErrorCode.UPSTREAM_UNAVAILABLE),
            (HTTPError(url, 429, "limited", Message(), None), ErrorCode.RATE_LIMITED),
            (HTTPError(url, 503, "unavailable", Message(), None), ErrorCode.UPSTREAM_UNAVAILABLE),
            (HTTPError(url, 404, "indeterminate", Message(), None), ErrorCode.UPSTREAM_UNAVAILABLE),
            (FakeHttpResponse(status=429), ErrorCode.RATE_LIMITED),
        )
        for failed_response, code in cases:
            with self.subTest(error=type(failed_response).__name__, code=code):
                opener = SequenceOpener([failed_response, FakeHttpResponse()])
                sleeps: list[float] = []
                transport = HttpTransport(min_interval_seconds=0, sleeper=sleeps.append, opener=opener)
                with self.assertRaises(TransportError) as caught:
                    transport.request(url)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.details["attempts"], 1)
                self.assertEqual(opener.calls, 1)
                self.assertEqual(sleeps, [])

    def test_explicit_retries_do_not_override_rate_limit_hold(self):
        url = "https://www.law.go.kr/DRF/lawSearch.do"
        for limited in (
            HTTPError(url, 429, "limited", Message(), None),
            FakeHttpResponse(status=429),
        ):
            with self.subTest(response=type(limited).__name__):
                opener = SequenceOpener([limited, FakeHttpResponse()])
                sleeps: list[float] = []
                with self.assertRaises(TransportError) as caught:
                    HttpTransport(min_interval_seconds=0, max_attempts=3, sleeper=sleeps.append, opener=opener).request(url)
                self.assertEqual(caught.exception.code, ErrorCode.RATE_LIMITED)
                self.assertEqual(opener.calls, 1)
                self.assertEqual(sleeps, [])

        headers = Message()
        headers["Retry-After"] = "3600"
        opener = SequenceOpener([HTTPError(url, 429, "limited", headers, None), FakeHttpResponse()])
        sleeps: list[float] = []
        transport = HttpTransport(min_interval_seconds=0, max_attempts=3, sleeper=sleeps.append, opener=opener)
        with self.assertRaises(TransportError) as caught:
            transport.request(url)
        self.assertEqual(caught.exception.code, ErrorCode.RATE_LIMITED)
        self.assertEqual(caught.exception.details["retry_after_seconds"], 3600)
        self.assertEqual(opener.calls, 1)
        self.assertEqual(sleeps, [])

        date_headers = Message()
        date_headers["Retry-After"] = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=3), usegmt=True)
        opener = SequenceOpener([HTTPError(url, 503, "unavailable", date_headers, None), FakeHttpResponse()])
        with self.assertRaises(TransportError) as dated:
            HttpTransport(min_interval_seconds=0, max_attempts=3, sleeper=sleeps.append, opener=opener).request(url)
        self.assertEqual(dated.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)
        self.assertGreaterEqual(dated.exception.details["retry_after_seconds"], 175)
        self.assertLessEqual(dated.exception.details["retry_after_seconds"], 181)
        self.assertEqual(opener.calls, 1)
        self.assertEqual(sleeps, [])

        future = datetime.now(timezone.utc) + timedelta(minutes=3)
        asctime_date = f"{future:%a %b} {future.day:2d} {future:%H:%M:%S %Y}"
        for header_value in ("²", "9" * 5000, asctime_date):
            for response_style in ("http_error", "returned_status"):
                with self.subTest(header=header_value[:20], response_style=response_style):
                    metadata = Message()
                    metadata["Retry-After"] = header_value
                    if response_style == "http_error":
                        failure = HTTPError(url, 503, "unavailable", metadata, None)
                    else:
                        failure = FakeHttpResponse(status=503)
                        failure.headers["Retry-After"] = header_value
                    opener = SequenceOpener([failure, FakeHttpResponse()])
                    with self.assertRaises(TransportError) as failed:
                        HttpTransport(min_interval_seconds=0, max_attempts=3, sleeper=sleeps.append, opener=opener).request(url)
                    self.assertEqual(failed.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)
                    self.assertEqual(failed.exception.details["http_status"], 503)
                    self.assertEqual(opener.calls, 1)
                    self.assertEqual(sleeps, [])
                    if header_value == asctime_date:
                        self.assertGreaterEqual(failed.exception.details["retry_after_seconds"], 175)
                    else:
                        self.assertNotIn("retry_after_seconds", failed.exception.details)

        healthy = SequenceOpener([FakeHttpResponse()])
        self.assertEqual(HttpTransport(min_interval_seconds=0, sleeper=sleeps.append, opener=healthy).request(url).attempts, 1)
        self.assertEqual(healthy.calls, 1)
        self.assertEqual(sleeps, [])

    def test_auth_failure_is_not_retried(self):
        headers = Message()
        error = HTTPError("https://www.law.go.kr/DRF/lawSearch.do", 403, "denied", headers, None)
        opener = SequenceOpener([error])
        transport = HttpTransport(min_interval_seconds=0, max_attempts=3, opener=opener)
        with self.assertRaises(TransportError) as caught:
            transport.request("https://www.law.go.kr/DRF/lawSearch.do")
        self.assertEqual(caught.exception.code, ErrorCode.AUTH_FAILED)
        self.assertEqual(caught.exception.details["stage"], "http")
        self.assertEqual(caught.exception.details["http_status"], 403)
        self.assertEqual(opener.calls, 1)

    def test_network_failures_report_the_origin_stage(self):
        cases = (
            (URLError(socket.gaierror(11001, "host not found")), "dns"),
            (URLError(ConnectionRefusedError(10061, "refused")), "tcp"),
            (URLError(ssl.SSLError("handshake failed")), "tls"),
            (TimeoutError("timed out"), "timeout"),
        )
        for source_error, stage in cases:
            with self.subTest(stage=stage):
                transport = HttpTransport(
                    min_interval_seconds=0,
                    max_attempts=1,
                    opener=SequenceOpener([source_error]),
                )
                with self.assertRaises(TransportError) as caught:
                    transport.request("https://www.law.go.kr/DRF/lawSearch.do")
                self.assertEqual(caught.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)
                self.assertEqual(caught.exception.details["stage"], stage)
                self.assertEqual(caught.exception.details["attempts"], 1)

    def test_retry_budget_exhaustion_preserves_the_first_network_failure(self):
        opener = SequenceOpener(
            [URLError(socket.gaierror(11001, "host not found"))]
        )
        transport = HttpTransport(
            min_interval_seconds=0,
            max_attempts=2,
            sleeper=lambda _: None,
            opener=opener,
        )
        budget = RemoteRequestBudget(max_requests=1, max_seconds=30.0)
        with activate_remote_budget(budget):
            with self.assertRaises(TransportError) as caught:
                transport.request("https://www.law.go.kr/DRF/lawSearch.do")
        self.assertEqual(caught.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)
        self.assertEqual(caught.exception.details["stage"], "dns")
        self.assertEqual(caught.exception.details["attempts"], 1)
        self.assertEqual(
            caught.exception.details["retry_stopped"],
            "budget_exhausted",
        )
        self.assertEqual(opener.calls, 1)
        self.assertEqual(budget.exhausted_reason, "remote_request_limit")

    def test_e2e_process_request_cap_is_enforced_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "upstream.json"
            with patch.dict(
                os.environ,
                {
                    "TAXAX_E2E_MAX_REMOTE_REQUESTS": "2",
                    "TAXAX_E2E_REMOTE_AUDIT_FILE": str(audit_path),
                },
            ):
                consume_remote_request()
                consume_remote_request()
                with self.assertRaises(RemoteBudgetExceeded):
                    consume_remote_request()
                payload = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["limit"], 2)
        self.assertEqual(payload["attempted"], 2)
        self.assertTrue(payload["exhausted"])

    def test_session_rate_limiter_budget_failure_is_structured(self):
        transport = SessionHttpTransport(
            "taxlaw.nts.go.kr",
            min_interval_seconds=1.0,
            opener=SequenceOpener([FakeHttpResponse()]),
        )
        with patch.object(
            SharedRateLimiter,
            "wait",
            side_effect=RemoteBudgetExceeded("요청 간격 예산 초과"),
        ):
            with self.assertRaises(TransportError) as caught:
                transport.request("https://taxlaw.nts.go.kr/index.do")
        self.assertEqual(caught.exception.code, ErrorCode.BUDGET_EXHAUSTED)
        self.assertEqual(caught.exception.details["stage"], "budget")
        self.assertEqual(caught.exception.details["attempts"], 0)

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
        # 남은 예산은 요청을 시작하기까지 흐른 시간만큼 5.0보다 조금 작다.
        # 정확히 5.0을 기대하면 시계 정밀도가 높은 환경에서 불안정해진다.
        self.assertEqual(len(opener.timeouts), 1)
        self.assertLessEqual(opener.timeouts[0], 5.0)
        self.assertGreater(opener.timeouts[0], 4.0)

    def test_socket_timeout_is_not_shortened_without_an_active_budget(self):
        opener = SequenceOpener([FakeHttpResponse()])
        transport = HttpTransport(min_interval_seconds=0, timeout_seconds=30.0, opener=opener)
        transport.request("https://www.law.go.kr/DRF/lawSearch.do")
        self.assertEqual(opener.timeouts, [30.0])

    def test_request_pacing_is_shared_between_processes(self):
        """이름은 Shared인데 프로세스 하나 안에서만 공유되던 문제.

        MCP client가 여러 개 떠 있으면 각자 따로 "1초에 한 건"을 지켰다. 이
        기계에서 Claude Desktop·Codex·Claude Code의 서버와 CLI가 동시에 돌던
        두 시간 동안 법제처로 나간 요청이 쌓여 공인 IP가 차단됐다. 상대가 보는
        것은 프로세스가 아니라 IP 하나다.

        같은 프로세스 안에서는 클래스 변수만으로도 통과하므로, 별도 프로세스를
        띄워 실제로 간격이 지켜지는지 본다.
        """
        import subprocess
        import sys as _sys

        worker = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(_ROOT / 'src')!r})\n"
            "from taxax.legal.transport import SharedRateLimiter\n"
            "SharedRateLimiter.wait('pace.test', 1.0, time.sleep)\n"
            "print(time.time())\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "TAXAX_LEGAL_DATA_DIR": directory}
            processes = [
                subprocess.Popen(
                    [_sys.executable, "-c", worker],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                for _ in range(3)
            ]
            stamps = []
            for process in processes:
                out, err = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, err)
                stamps.append(float(out.strip()))
        stamps.sort()
        gaps = [second - first for first, second in zip(stamps, stamps[1:])]
        self.assertTrue(gaps, "프로세스가 하나뿐이라 간격을 잴 수 없습니다.")
        self.assertGreater(
            min(gaps),
            0.8,
            f"별도 프로세스들이 간격을 지키지 않았습니다: {gaps}",
        )

    def test_shared_pacing_database_failure_blocks_instead_of_using_local_fallback(self):
        with patch.object(SharedRateLimiter, "_claim_shared", return_value=None):
            with self.assertRaises(RemoteBudgetExceeded):
                SharedRateLimiter.wait("pace.database.unavailable", 1.0, lambda _: None)

    def test_queueing_does_not_burn_a_budget_that_cannot_afford_the_wait(self):
        """줄을 서다 예산을 다 쓰고 아무것도 못 보내던 문제.

        같은 조사를 둘이 동시에 돌리면 한쪽이 순서를 기다리다 60초 예산을 다
        쓰고 세 건밖에 못 보냈다(다른 쪽은 16건). 기다려도 남은 시간 안에 못
        보낼 상황이면 자리를 잡지 않고 예산 초과로 알려야 한다. 아무 소득 없이
        시간을 태우고 자리까지 맡아 두면 다른 요청도 함께 늦어진다.
        """
        from taxax.legal.budget import RemoteBudgetExceeded, RemoteRequestBudget, activate_remote_budget

        key = f"pace-budget-{time.time()}"
        # 슬롯을 하나 잡아 다음 차례를 멀리 밀어 둔다.
        SharedRateLimiter.wait(key, 10.0, lambda seconds: None)

        slept: list[float] = []
        budget = RemoteRequestBudget(max_requests=10, max_seconds=1.0)
        with activate_remote_budget(budget):
            with self.assertRaises(RemoteBudgetExceeded):
                SharedRateLimiter.wait(key, 10.0, slept.append)
        self.assertEqual(slept, [], "예산이 없는데 기다렸습니다.")

        # 예산이 없으면 제한 없이 기다린다.
        SharedRateLimiter.wait(key, 10.0, slept.append)
        self.assertTrue(slept and slept[0] > 0, "예산이 없을 때는 차례를 기다려야 합니다.")

    def test_off_domain_and_secret_redaction(self):
        transport = HttpTransport(min_interval_seconds=0)
        with self.assertRaises(TransportError) as caught:
            transport.request("http://example.com/law")
        self.assertEqual(caught.exception.code, ErrorCode.ACCESS_DENIED)
        self.assertNotIn("secret", redact_url("https://www.law.go.kr/x?OC=secret&query=law"))
        self.assertNotIn("secret", redact_text("Authorization=secret", ("secret",)))


if __name__ == "__main__":
    unittest.main()
