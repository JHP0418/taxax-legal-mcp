from __future__ import annotations

import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from taxax.legal.models import ErrorCode, ResponseStatus
from taxax.legal.providers.base import ProviderError, SupplementalResult
from taxax.legal.providers.law_go import LawGoProvider
from taxax.legal.providers.nts import (
    ACTION_URL,
    DETAIL_ACTION,
    PARSER_VERSION as NTS_PARSER_VERSION,
    SEARCH_ACTION,
    SEARCH_ENTRY_URL,
    NtsProvider,
)
from taxax.legal.providers.olta import ENTRY_URL as OLTA_ENTRY_URL, SEARCH_URL, DETAIL_PATHS, OltaProvider, parse_search_html
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
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


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


    def test_retries_one_transient_failure_but_not_access_denials(self):
        sleeps: list[float] = []
        opener = CapturingOpener([TimeoutError("timed out"), RawResponse(b"ok")])
        transport = SessionHttpTransport("taxlaw.nts.go.kr", min_interval_seconds=0, opener=opener, sleeper=sleeps.append)
        self.assertEqual(transport.request(ACTION_URL).body, b"ok")
        self.assertEqual((len(opener.requests), sleeps), (2, [1.0]))
        opener = CapturingOpener([TimeoutError("timed out"), TimeoutError("timed out"), RawResponse(b"ok")])
        transport = SessionHttpTransport("taxlaw.nts.go.kr", min_interval_seconds=0, opener=opener, sleeper=lambda _: None)
        with self.assertRaises(TransportError):
            transport.request(ACTION_URL)
        self.assertEqual(len(opener.requests), 2)
        opener = CapturingOpener([RawResponse(b"<html>blocked</html>", content_type="text/html", url="https://example.com/x"), RawResponse(b"ok")])
        with self.assertRaises(TransportError):
            SessionHttpTransport("taxlaw.nts.go.kr", min_interval_seconds=0, opener=opener, sleeper=lambda _: None).request(ACTION_URL)
        self.assertEqual(len(opener.requests), 1)


class NtsProviderTests(unittest.TestCase):
    def provider(self, values, **kwargs):
        transport = QueueTransport(values)
        return NtsProvider(enabled=True, terms_confirmed=True, transport=transport, **kwargs), transport

    def test_defaults_to_enabled_and_legacy_terms_values_do_not_block(self):
        with patch.dict(os.environ, {}, clear=True):
            default = NtsProvider(transport=QueueTransport([]))
        self.assertTrue(default.enabled)
        self.assertNotIn("terms_confirmed", default.capabilities())

        with patch.dict(
            os.environ,
            {"TAXAX_NTS_TERMS_CONFIRMED": "0"},
            clear=True,
        ):
            provider = NtsProvider(
                terms_confirmed=False,
                transport=QueueTransport([ENTRY, NTS_SEARCH]),
            )
            result = provider.search("대손", collections=("ruling",))
        self.assertEqual(result.total, 2)

    def test_explicit_disable_remains_an_opt_out(self):
        with patch.dict(
            os.environ,
            {"TAXAX_NTS_ENABLED": "0"},
            clear=True,
        ):
            env_disabled = NtsProvider(transport=QueueTransport([]))
        argument_disabled = NtsProvider(
            enabled=False,
            terms_confirmed=True,
            transport=QueueTransport([]),
        )
        for provider in (env_disabled, argument_disabled):
            with self.subTest(provider=provider):
                with self.assertRaises(ProviderError) as caught:
                    provider.search("대손")
                self.assertEqual(caught.exception.code, ErrorCode.POLICY_DISABLED)
                self.assertNotIn("약관", str(caught.exception))

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
        self.assertEqual(changed.request_parameters["parser_version"], NTS_PARSER_VERSION)

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

    def test_bootstrap_failure_does_not_reset_or_replay(self):
        invalid = HttpResponse(SEARCH_ENTRY_URL, 200, {"content-type": "text/html"}, b"<html>changed</html>")
        for operation in ("search", "attachment"):
            with self.subTest(operation=operation):
                provider, transport = self.provider([invalid])
                with self.assertRaises(ProviderError) as caught:
                    getattr(provider, operation)("FILE001" if operation == "attachment" else "대손")
                self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)
                self.assertEqual([call["url"] for call in transport.calls], [SEARCH_ENTRY_URL])
                self.assertEqual(transport.resets, 0)
                self.assertFalse(provider._bootstrapped)

    def test_search_parse_failure_does_not_reset_or_replay(self):
        for body, code in ((b"{}", ErrorCode.INCOMPLETE_RESULT), (b"{broken", ErrorCode.PARSE_ERROR)):
            with self.subTest(body=body):
                invalid = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, body)
                provider, transport = self.provider([ENTRY, invalid])
                with self.assertRaises(ProviderError) as caught:
                    provider.search("대손", collections=("ruling",))
                self.assertEqual(caught.exception.code, code)
                self.assertEqual([call["url"] for call in transport.calls], [SEARCH_ENTRY_URL, ACTION_URL])
                self.assertEqual(transport.resets, 0)
                self.assertTrue(provider._bootstrapped)
                self.assertEqual(provider._cache, {})

    def test_transport_errors_propagate_without_reset_or_replay(self):
        stages = (
            ("search", []),
            ("search", [ENTRY]),
            ("detail", []),
            ("detail", [ENTRY]),
            ("attachment", []),
            ("attachment", [ENTRY]),
            ("attachment", [ENTRY, NTS_ATTACHMENT]),
        )
        for code, status in ((ErrorCode.UPSTREAM_UNAVAILABLE, None), (ErrorCode.ACCESS_DENIED, 403), (ErrorCode.RATE_LIMITED, 429)):
            for operation, prefix in stages:
                with self.subTest(code=code, operation=operation, stage=len(prefix)):
                    error = TransportError(code, "original failure", retryable=status != 403, status=status, details={"stage": "fixture"})
                    provider, transport = self.provider([*prefix, error])
                    argument = {"search": "대손", "detail": "NTS001", "attachment": "FILE001"}[operation]
                    with self.assertRaises(TransportError) as caught:
                        getattr(provider, operation)(argument)
                    self.assertIs(caught.exception, error)
                    self.assertEqual(len(transport.calls), len(prefix) + 1)
                    self.assertEqual(transport.resets, 0)

    def test_detail_validates_identifier_and_extracts_sections(self):
        provider, _ = self.provider([ENTRY, NTS_DETAIL])
        result = provider.detail("NTS001")
        self.assertEqual(result.items[0]["source_document_id"], "NTS001")
        self.assertEqual(result.items[0]["interpreted_on"], "2026-03-10")
        self.assertEqual({section["kind"] for section in result.items[0]["sections"]}, {"gist", "answer", "references"})
        self.assertEqual(len(result.raw_responses), 2)

    def test_precedent_html_editor_is_judgment_not_attachment_notice(self):
        payload = json.loads((FIXTURES / "nts_detail.json").read_text(encoding="utf-8"))
        record = payload["data"][DETAIL_ACTION]
        detail = record["dcmDVO"]
        detail.update(ntstDcmClCd="09", ntstDcmDscmCntn="수원고등법원-2025-누-688",
                      ntstDcmCntn="판결 내용은 상세내용과 같습니다.", ntstDcmGistCntn="검색 요지")
        record["dcmHwpEditorDVOList"] = [
            {"dcmFleTy": "hwp", "dcmFleByte": ""},
            {"dcmFleTy": "html", "dcmFleByte": "<html><body><table><tr><td>사 건</td><td>2025누688 증여세부과처분취소</td></tr><tr><td>판 결 선 고</td><td>2026. 1. 14.</td></tr></table><p>주 문</p><p>원고들의 항소를 모두 기각한다.</p></body></html>"},
        ]
        response = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode())
        provider, _ = self.provider([ENTRY, response])
        item = provider.detail("NTS001").items[0]
        self.assertEqual(item["document_type"], "court_precedent")
        self.assertEqual(item["case_no"], "2025누688")
        self.assertEqual(item["decided_on"], "2026-01-14")
        self.assertIn("원고들의 항소를 모두 기각한다.", item["sections"][-1]["text"])
        self.assertNotIn("판결 내용은 상세내용과 같습니다.", [s["text"] for s in item["sections"]])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = LegalKnowledgeService(root, data_dir=root / "data", nts_provider=self.provider([ENTRY, response])[0])
            result = service.get_legal_document(provider="taxlaw.nts.go.kr", source_document_id="NTS001", refresh=True)
            self.assertEqual(result.data["content_completeness"], "complete")
            checked = service.verify_legal_citations(citations=[
                {"document_id": result.data["document_id"], "locator": item["sections"][-1]["locator"], "quote": "원고들의 항소를 모두 기각한다."},
                {"document_id": result.data["document_id"], "locator": item["sections"][-1]["locator"], "quote": "판결 내용은 상세내용과 같습니다."},
            ])
            self.assertEqual([c["status"] for c in checked.data["checks"]], ["verified", "mismatch"])

    def test_law_go_precedent_without_body_is_served_from_matching_nts_judgment(self):
        """법제처에 본문이 없는 국세청 출처 판례는 같은 사건번호의 국세청 판결문으로 받는다."""
        def response(url, body):
            return HttpResponse(url, 200, {"content-type": "application/json"}, json.dumps(body, ensure_ascii=False).encode())

        class LawQueue:
            def __init__(self, values):
                self.values, self.calls = list(values), []

            def request(self, url, *, params=None, secrets=()):
                self.calls.append(dict(params or {}))
                return self.values.pop(0)

        law_search = response("https://www.law.go.kr/DRF/lawSearch.do", {"PrecSearch": {"page": "1", "totalCnt": "1", "prec": [
            {"id": "1", "판례일련번호": "621227", "사건명": "비상장주식 저가 거래", "사건번호": "수원고등법원-2025-누-688",
             "데이터출처명": "국세법령정보시스템", "선고일자": "2026.01.14"}]}})
        no_match = response("https://www.law.go.kr/DRF/lawService.do", {"Law": "일치하는 판례가 없습니다.  판례명을 확인하여 주십시오."})
        nts_search = response(ACTION_URL, {"data": {"ASEISA001MR01": {"searchResultVO": {"collectionList": [{"nameEn": "precedent", "totalCount": "2", "resultList": [
            {"DOC_ID": "NTS009", "TTL": "다른 사건", "NTST_DCM_CL_NM": "판례", "NTST_DCM_DSCM_CNTN": "수원고등법원-2025-누-6880"},
            {"DOC_ID": "NTS001", "TTL": "비상장주식 저가 거래", "NTST_DCM_CL_NM": "판례", "NTST_DCM_DSCM_CNTN": "수원고등법원-2025-누-688"},
        ]}]}}}})
        payload = json.loads((FIXTURES / "nts_detail.json").read_text(encoding="utf-8"))
        record = payload["data"][DETAIL_ACTION]
        record["dcmDVO"].update(ntstDcmClCd="09", ntstDcmDscmCntn="수원고등법원-2025-누-688", ntstDcmCntn="판결 내용은 상세내용과 같습니다.")
        record["dcmHwpEditorDVOList"] = [{"dcmFleTy": "html", "dcmFleByte": "<html><body><p>사 건 2025누688 증여세부과처분취소</p><p>판 결 선 고 2026. 1. 14.</p><p>주 문</p><p>원고들의 항소를 모두 기각한다.</p></body></html>"}]
        law = LawQueue([law_search, no_match])
        nts, nts_transport = self.provider([ENTRY, nts_search, ENTRY, response(ACTION_URL, payload)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = LegalKnowledgeService(root, data_dir=root / "data", provider=LawGoProvider(credential="operator-secret", transport=law), nts_provider=nts)
            found = service.search_legal_sources(query="2025누688", provider="law.go.kr", target="prec", upstream=True)
            result = service.get_legal_document(document_id=found.data["items"][0]["document_id"], refresh=True)
            self.assertEqual(result.status, ResponseStatus.OK, result.error)
            self.assertEqual(result.data["provider"], "taxlaw.nts.go.kr")
            self.assertEqual(result.data["source_document_id"], "NTS001")
            self.assertEqual((result.data["court"], result.data["case_no"], result.data["decided_on"]), ("수원고등법원", "2025누688", "2026-01-14"))
            self.assertEqual(result.data["content_completeness"], "complete")
            self.assertTrue(any("국세청 원문" in warning for warning in result.warnings))
            # 검색 결과가 국세청 출처라고 알려 주므로 법제처 상세는 부르지 않는다.
            self.assertEqual([call.get("query") for call in law.calls], ["2025누688"])
            checked = service.verify_legal_citations(citations=[
                {"document_id": result.data["document_id"], "locator": "판결문", "quote": "원고들의 항소를 모두 기각한다.", "expected_date": "2026-01-14"},
            ])
            self.assertEqual(checked.data["checks"][0]["status"], "verified")

    def test_precedent_without_substantive_html_stays_partial(self):
        for html in (None, "<html><body><p>사 건 2025누688</p><p>주 문</p>"):
            with self.subTest(html=html), tempfile.TemporaryDirectory() as directory:
                payload = json.loads((FIXTURES / "nts_detail.json").read_text(encoding="utf-8"))
                record = payload["data"][DETAIL_ACTION]
                record["dcmDVO"].update(ntstDcmClCd="09", ntstDcmCntn="판결 내용은 상세내용과 같습니다.")
                if html is not None:
                    record["dcmHwpEditorDVOList"] = [{"dcmFleTy": "html", "dcmFleByte": html}]
                response = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode())
                root = Path(directory)
                service = LegalKnowledgeService(root, data_dir=root / "data", nts_provider=self.provider([ENTRY, response])[0])
                result = service.get_legal_document(provider="taxlaw.nts.go.kr", source_document_id="NTS001", refresh=True)
                self.assertEqual(result.data["document_type"], "court_precedent")
                self.assertEqual(result.data["content_completeness"], "partial")
                self.assertTrue(result.data["metadata"]["preview_only"])
                checked = service.verify_legal_citations(citations=[
                    {"document_id": result.data["document_id"], "quote": "판결 내용은 상세내용과 같습니다."},
                    {"document_id": result.data["document_id"], "locator": "판결문", "quote": "원고들의 항소를 모두 기각한다."},
                ])
                self.assertEqual([c["status"] for c in checked.data["checks"]], ["unverified", "unverified"])

    def test_detail_retains_observed_official_registration_date_and_document_number(self):
        payload = json.loads((FIXTURES / "nts_detail.json").read_text(encoding="utf-8"))
        detail = payload["data"][DETAIL_ACTION]["dcmDVO"]
        detail.pop("dcmRgtDtm")
        detail.pop("explYd")
        detail["ntstDcmRgtDt"] = "2023-02-07"
        detail["ntstDcmDscmCntn"] = "서면-2021-법규법인-7996"
        response = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode())
        provider, _ = self.provider([ENTRY, response])
        item = provider.detail("NTS001").items[0]
        self.assertEqual(item["registered_at"], "2023-02-07")
        self.assertIsNone(item["interpreted_on"])
        self.assertEqual(item["metadata"]["document_number"], "서면-2021-법규법인-7996")

    def test_detail_identifier_mismatch_does_not_reset_or_replay(self):
        payload = json.loads((FIXTURES / "nts_detail.json").read_text(encoding="utf-8"))
        payload["data"][DETAIL_ACTION]["dcmDVO"]["ntstDcmId"] = "OTHER"
        mismatch = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, json.dumps(payload).encode())
        provider, transport = self.provider([ENTRY, mismatch])
        with self.assertRaises(ProviderError) as caught:
            provider.detail("NTS001")
        self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[-1]["form"]["actionId"], DETAIL_ACTION)
        self.assertEqual(transport.resets, 0)

    def test_attachment_download_and_html_rejection(self):
        pdf = HttpResponse("https://taxlaw.nts.go.kr/file/download.do", 200, {"content-type": "application/pdf"}, b"%PDF-fixture")
        provider, transport = self.provider([ENTRY, NTS_ATTACHMENT, pdf])
        result = provider.attachment("FILE001")
        self.assertEqual(result.items[0]["document_type"], "nts_attachment")
        self.assertEqual(transport.calls[-1]["max_bytes"], 10 * 1024 * 1024)

        html = HttpResponse("https://taxlaw.nts.go.kr/file/download.do", 200, {"content-type": "text/html"}, b"<html>login</html>")
        blocked, blocked_transport = self.provider([ENTRY, NTS_ATTACHMENT, html])
        with self.assertRaises(ProviderError) as caught:
            blocked.attachment("FILE001")
        self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)
        self.assertEqual(len(blocked_transport.calls), 3)
        self.assertEqual(blocked_transport.calls[-1]["url"], html.url + "?fleId=FILE001")
        self.assertEqual(blocked_transport.resets, 0)


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

    def test_search_results_carry_the_original_url_when_the_endpoint_is_known(self):
        """검색 결과만 받아 본 쪽도 출처를 가리킬 수 있어야 한다.

        지금까지는 상세조회를 해야만 official_url이 생겼다. 그러면 검색
        단계에서 인용하려는 쪽은 근거는 있는데 어디서 왔는지 쓸 수 없다.
        상세 endpoint가 확인된 유형은 검색 단계에서도 같은 규칙으로 주소를
        만들 수 있다. 확인되지 않은 유형은 비워 두는 것이 맞다.
        """
        items, _ = parse_search_html(OLTA_SEARCH)
        for item in items:
            category = item["metadata"]["category"]
            with self.subTest(category=category):
                if category in DETAIL_PATHS and not item["source_document_id"].startswith("preview-"):
                    self.assertIsNotNone(item.get("official_url"), "상세 endpoint가 있는 유형인데 주소가 없습니다.")
                    self.assertIn(item["source_document_id"], item["official_url"])
                    self.assertTrue(item["official_url"].startswith("https://olta.re.kr/"))
                else:
                    # 상세 endpoint를 모르는 유형에 주소를 지어내면 안 된다.
                    self.assertIsNone(item.get("official_url"))

    def test_a_search_with_no_hits_is_zero_results_not_a_broken_parser(self):
        """결과 0건을 "사이트 구조가 변경됐다"고 보고하던 문제.

        OLTA는 지방세 심판례 사이트라 국세 주제를 물으면 결과가 없다. 그때
        category heading도 없는데, 예전에는 heading이 없으면 무조건 구조 변경
        오류를 냈다. "가업상속공제 사후관리 위반"은 85KB짜리 멀쩡한 페이지를
        받고도 이 오류가 났다. 없는 것과 고장난 것은 다르다.
        """
        empty = HttpResponse(
            url=SEARCH_URL,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            # 결과는 없지만 category 안내 문구는 남아 있는 실제 페이지 모양.
            body=(
                "<html><body><div class='search_wrap'>"
                "<span>법원판례</span><span>행정안전부 유권해석</span><span>법제처 유권해석</span>"
                "<span>조세심판원 결정례</span><span>감사원 심사결정례</span>"
                "<span>헌법재판소 결정례</span><span>자치단체 질의회신</span>"
                "</div></body></html>"
            ).encode("utf-8"),
        )
        items, totals = parse_search_html(empty)
        self.assertEqual(items, [])
        self.assertEqual(totals, {})

    def test_an_unknown_page_still_fails_loudly(self):
        """검색 화면이 아닌 것을 0건으로 삼키면 진짜 구조 변경을 놓친다."""
        stranger = HttpResponse(
            url=SEARCH_URL,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"<html><body><div>system maintenance</div></body></html>",
        )
        with self.assertRaises(ProviderError) as caught:
            parse_search_html(stranger)
        self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)

    def test_a_national_tax_case_is_not_labelled_local_just_because_olta_indexed_it(self):
        """출처만 보고 세목을 단정하던 문제.

        OLTA는 지방세 연구 포털이지만 법원판례·헌재결정례 섹션에는 국세 사건도
        색인한다. 그런데 tax_scope를 "local"로 박아 두어서 상속세 대법원
        판결(2007두16493 등)이 지방세 자료로 표시됐다. OLTA는 그런 행의 세목을
        "기타"라고만 적어 주므로 그 표기를 그대로 믿을 수도 없다.
        """
        from taxax.legal.router import infer_tax_type

        self.assertEqual(infer_tax_type("상속세부과처분취소")[1], "national")
        self.assertEqual(infer_tax_type("취득세 중과세 대상")[1], "local")
        # 읽히지 않으면 지방세로 채우지 않고 모른다고 남긴다.
        self.assertEqual(infer_tax_type("제2차 납세의무자 지정처분")[1], "unknown")

        items, _ = parse_search_html(OLTA_SEARCH)
        for item in items:
            scope = item["metadata"]["tax_scope"]
            with self.subTest(title=item["title"][:20]):
                self.assertIn(scope, {"national", "local", "mixed", "unknown"})
                # 출처가 OLTA라는 사실은 따로 남긴다. 세목과 섞지 않는다.
                self.assertEqual(item["metadata"]["source_portal_scope"], "local")

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

    def test_defaults_to_enabled_and_legacy_terms_values_do_not_block(self):
        with patch.dict(os.environ, {}, clear=True):
            default = OltaProvider(transport=QueueTransport([]))
        self.assertTrue(default.enabled)
        self.assertNotIn("terms_confirmed", default.capabilities())

        with patch.dict(
            os.environ,
            {"TAXAX_OLTA_TERMS_CONFIRMED": "0"},
            clear=True,
        ):
            provider = OltaProvider(
                terms_confirmed=False,
                transport=QueueTransport([ENTRY, OLTA_SEARCH]),
            )
            result = provider.search("취득세")
        self.assertEqual(result.total, 7)

    def test_explicit_disable_remains_an_opt_out(self):
        with patch.dict(
            os.environ,
            {"TAXAX_OLTA_ENABLED": "0"},
            clear=True,
        ):
            env_disabled = OltaProvider(transport=QueueTransport([]))
        argument_disabled = OltaProvider(
            enabled=False,
            terms_confirmed=True,
            transport=QueueTransport([]),
        )
        for provider in (env_disabled, argument_disabled):
            with self.subTest(provider=provider):
                with self.assertRaises(ProviderError) as caught:
                    provider.search("취득세")
                self.assertEqual(caught.exception.code, ErrorCode.POLICY_DISABLED)
                self.assertNotIn("약관", str(caught.exception))

    def test_unverified_detail_type_and_attachment_fail_closed(self):
        provider, transport = self.provider([])
        with self.assertRaises(ProviderError) as detail:
            provider.detail("court", "12345678")
        self.assertEqual(detail.exception.code, ErrorCode.POLICY_DISABLED)
        self.assertEqual(transport.calls, [])
        with self.assertRaises(ProviderError):
            provider.attachment("x")

    def test_verified_detail_endpoint(self):
        provider, _ = self.provider([OLTA_DETAIL])
        detail = provider.detail("tax_tribunal", "12345678")
        self.assertEqual(detail.items[0]["source_document_id"], "12345678")
        self.assertGreater(len(detail.items[0]["sections"][0]["text"]), 100)

    def test_bootstrap_failure_does_not_reset_or_replay(self):
        invalid = HttpResponse(OLTA_ENTRY_URL, 200, {"content-type": "text/html"}, b"<html>changed</html>")
        provider, transport = self.provider([invalid])
        with self.assertRaises(ProviderError) as caught:
            provider.search("취득세")
        self.assertEqual(caught.exception.code, ErrorCode.INCOMPLETE_RESULT)
        self.assertEqual([call["url"] for call in transport.calls], [OLTA_ENTRY_URL])
        self.assertEqual(transport.resets, 0)
        self.assertFalse(provider._bootstrapped)

    def test_blocked_and_unknown_search_results_do_not_reset_or_replay(self):
        for body, code in (
            (b"<html><body>access denied</body></html>", ErrorCode.ACCESS_DENIED),
            (b"<html><body>changed</body></html>", ErrorCode.INCOMPLETE_RESULT),
            (b"not html", ErrorCode.INCOMPLETE_RESULT),
            (b'<html><p class="se_title">Unknown category (1)</p></html>', ErrorCode.INCOMPLETE_RESULT),
        ):
            with self.subTest(body=body):
                invalid = HttpResponse(SEARCH_URL, 200, {"content-type": "text/html"}, body)
                provider, transport = self.provider([ENTRY, invalid])
                with self.assertRaises(ProviderError) as caught:
                    provider.search("취득세")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual([call["url"] for call in transport.calls], [OLTA_ENTRY_URL, SEARCH_URL])
                self.assertEqual(transport.resets, 0)
                self.assertTrue(provider._bootstrapped)
                self.assertEqual(provider._cache, {})

    def test_blocked_and_short_detail_results_do_not_reset_or_replay(self):
        for body, code in (
            (b"<html><body>captcha</body></html>", ErrorCode.ACCESS_DENIED),
            (b'<html><div id="print_contents">short</div></html>', ErrorCode.NOT_FOUND),
        ):
            with self.subTest(body=body):
                invalid = HttpResponse("https://olta.re.kr/detail", 200, {"content-type": "text/html"}, body)
                provider, transport = self.provider([invalid])
                with self.assertRaises(ProviderError) as caught:
                    provider.detail("tax_tribunal", "12345678")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(transport.calls), 1)
                self.assertIn("num=12345678", transport.calls[0]["url"])
                self.assertEqual(transport.resets, 0)

    def test_transport_errors_propagate_without_reset_or_replay(self):
        for code, status in ((ErrorCode.UPSTREAM_UNAVAILABLE, None), (ErrorCode.ACCESS_DENIED, 403), (ErrorCode.RATE_LIMITED, 429)):
            for operation, prefix in (("search", []), ("search", [ENTRY]), ("detail", [])):
                with self.subTest(code=code, operation=operation, stage=len(prefix)):
                    error = TransportError(code, "original failure", retryable=status != 403, status=status, details={"stage": "fixture"})
                    provider, transport = self.provider([*prefix, error])
                    with self.assertRaises(TransportError) as caught:
                        if operation == "search":
                            provider.search("취득세")
                        else:
                            provider.detail("tax_tribunal", "12345678")
                    self.assertIs(caught.exception, error)
                    self.assertEqual(len(transport.calls), len(prefix) + 1)
                    self.assertEqual(transport.resets, 0)


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

    def test_nts_detail_official_number_and_date_survive_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = json.loads((FIXTURES / "nts_detail.json").read_text(encoding="utf-8"))
            detail = payload["data"][DETAIL_ACTION]["dcmDVO"]
            detail["ntstDcmRgtDt"] = "20230207"
            detail["ntstDcmDscmCntn"] = "서면-2021-법규법인-7996"
            response = HttpResponse(ACTION_URL, 200, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode())
            nts = NtsProvider(enabled=True, transport=QueueTransport([ENTRY, response]))
            service = self.make_service(root, nts=nts)
            remote = service.get_legal_document(provider="nts", source_document_id="NTS001", refresh=True)
            self.assertEqual(remote.data["document_number"], "서면-2021-법규법인-7996")
            self.assertEqual(remote.data["registered_at"], "2023-02-07")
            local = service.get_legal_document(document_id=remote.data["document_id"])
            self.assertEqual(local.data["document_number"], remote.data["document_number"])

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

    def test_missing_law_go_credential_does_not_block_nts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nts = NtsProvider(
                transport=QueueTransport([ENTRY, NTS_SEARCH]),
            )
            service = LegalKnowledgeService(
                root,
                data_dir=root / "state",
                provider=LawGoProvider(
                    credential="",
                    transport=QueueTransport([]),
                ),
                nts_provider=nts,
                olta_provider=OltaProvider(
                    enabled=False,
                    transport=QueueTransport([]),
                ),
            )
            result = service.search_legal_sources(
                query="대손",
                provider="nts",
                upstream=True,
            )
        self.assertEqual(result.status, ResponseStatus.OK)
        self.assertEqual(result.pagination.total, 2)

    def test_status_contract_has_no_terms_confirmation_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            doctor = service.doctor()
            source_status = service.get_source_status()
        checks = doctor.data["checks"]
        self.assertIn("nts_enabled", checks)
        self.assertIn("olta_enabled", checks)
        self.assertNotIn("nts_enabled_and_terms_confirmed", checks)
        self.assertNotIn("olta_enabled_and_terms_confirmed", checks)
        serialized = json.dumps(
            source_status.model_dump(mode="json"),
            ensure_ascii=False,
        ).lower()
        self.assertNotIn("terms", serialized)
        self.assertNotIn("약관", serialized)

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
