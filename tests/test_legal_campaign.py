from __future__ import annotations

import io
import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from taxax.legal.campaign import (
    CampaignError,
    CampaignLedger,
    CampaignLimits,
)
from taxax.legal import transport as transport_module
from taxax.legal.models import ErrorCode
from taxax.legal.transport import (
    HttpResponse,
    HttpTransport,
    ReplayStore,
    SessionHttpTransport,
    TransportError,
)


class FakeClock:
    def __init__(self, value: float = 1_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FailingOpener:
    def __init__(self):
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        raise AssertionError("replay에서 opener를 호출하면 안 됩니다")


class RawResponse:
    def __init__(
        self,
        body: bytes = b"ok",
        *,
        url: str = "https://www.law.go.kr/result",
        status: int = 200,
        headers: Message | None = None,
    ):
        self._body = body
        self._url = url
        self.status = status
        self.headers = headers or Message()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, amount: int | None = None) -> bytes:
        return self._body if amount is None else self._body[:amount]

    def geturl(self) -> str:
        return self._url


class SequenceOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RedirectingOpener:
    def __init__(self, handler, target="https://www.law.go.kr/redirected"):
        self.handler = handler
        self.target = target
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        redirected = self.handler.redirect_request(
            request,
            None,
            302,
            "Found",
            Message(),
            self.target,
        )
        return RawResponse(url=redirected.full_url)


def _reserve_session_worker(path: str, start, output, index: int) -> None:
    limits = CampaignLimits(
        max_sessions=4,
        max_live_sessions=4,
        max_concurrent_live_sessions=4,
    )
    ledger = CampaignLedger(Path(path), "campaign-mp-session", limits=limits)
    start.wait()
    try:
        ledger.reserve_session(
            f"session-{index}",
            mode="live",
            case_id=f"case-{index}",
            expected_max_cost_usd=1.0,
        )
    except CampaignError as exc:
        output.put((False, exc.reason))
    else:
        output.put((True, None))


def _reserve_http_worker(path: str, start, output, index: int) -> None:
    limits = CampaignLimits(max_live_http=4, min_source_interval_seconds=0)
    ledger = CampaignLedger(Path(path), "campaign-mp-http", limits=limits)
    start.wait()
    try:
        ledger.reserve_http_attempt(
            source=f"source-{index}.example",
            case_id=f"case-{index}",
            owner_id=f"owner-{index}",
        )
    except CampaignError as exc:
        output.put((False, exc.reason))
    else:
        output.put((True, None))


class CampaignLedgerTests(unittest.TestCase):
    def test_session_reservation_survives_reopen_and_settlement_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.sqlite3"
            first = CampaignLedger(path, "campaign-durable")
            first.reserve_session(
                "session-1",
                mode="live",
                case_id="E01",
                expected_max_cost_usd=2.5,
            )

            reopened = CampaignLedger(path, "campaign-durable")
            before = reopened.snapshot()
            self.assertEqual(before["sessions"]["total"], 1)
            self.assertEqual(before["cost_usd"]["reserved"], 2.5)
            reopened.settle_session("session-1", actual_cost_usd=1.25)
            reopened.settle_session("session-1", actual_cost_usd=1.25)
            after = reopened.snapshot()
            self.assertEqual(after["cost_usd"]["reserved"], 0.0)
            self.assertEqual(after["cost_usd"]["settled"], 1.25)

    def test_concurrent_session_reservations_cannot_exceed_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "campaign.sqlite3")
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            output = context.Queue()
            processes = [
                context.Process(
                    target=_reserve_session_worker,
                    args=(path, start, output, index),
                )
                for index in range(10)
            ]
            for process in processes:
                process.start()
            start.set()
            results = [output.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sum(1 for accepted, _ in results if accepted), 4)
            self.assertEqual(
                CampaignLedger(
                    Path(path),
                    "campaign-mp-session",
                    limits=CampaignLimits(
                        max_sessions=4,
                        max_live_sessions=4,
                        max_concurrent_live_sessions=4,
                    ),
                ).snapshot()["sessions"]["total"],
                4,
            )

    def test_concurrent_http_reservations_cannot_exceed_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "campaign.sqlite3")
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            output = context.Queue()
            processes = [
                context.Process(
                    target=_reserve_http_worker,
                    args=(path, start, output, index),
                )
                for index in range(10)
            ]
            for process in processes:
                process.start()
            start.set()
            results = [output.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(sum(1 for accepted, _ in results if accepted), 4)

    def test_unknown_or_over_reservation_cost_halts_new_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = CampaignLedger(Path(directory) / "campaign.sqlite3", "campaign-cost")
            ledger.reserve_session(
                "unknown",
                mode="live",
                case_id="E01",
                expected_max_cost_usd=2.0,
            )
            ledger.settle_session("unknown", actual_cost_usd=None)
            self.assertEqual(ledger.snapshot()["halt_reason"], "usage_or_cost_unknown")
            with self.assertRaises(CampaignError) as blocked:
                ledger.reserve_session(
                    "after-unknown",
                    mode="replay",
                    case_id="E01",
                    expected_max_cost_usd=1.0,
                )
            self.assertEqual(blocked.exception.reason, "campaign_halted")

            second = CampaignLedger(
                Path(directory) / "second.sqlite3",
                "campaign-overrun",
            )
            second.reserve_session(
                "overrun",
                mode="live",
                case_id="E01",
                expected_max_cost_usd=1.0,
            )
            second.settle_session("overrun", actual_cost_usd=1.01)
            self.assertEqual(second.snapshot()["halt_reason"], "reservation_exceeded")

    def test_new_session_stop_reserves_expected_max_and_hard_ceiling_is_retained(self):
        limits = CampaignLimits(
            model_cost_ceiling_usd=60.0,
            new_session_stop_usd=48.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = CampaignLedger(
                Path(directory) / "campaign.sqlite3",
                "campaign-threshold",
                limits=limits,
            )
            ledger.reserve_session(
                "first",
                mode="live",
                case_id="E01",
                expected_max_cost_usd=47.5,
            )
            with self.assertRaises(CampaignError) as stopped:
                ledger.reserve_session(
                    "second",
                    mode="replay",
                    case_id="E01",
                    expected_max_cost_usd=0.6,
                )
            self.assertEqual(stopped.exception.reason, "new_session_cost_stop")
            self.assertEqual(ledger.snapshot()["limits"]["model_cost_ceiling_usd"], 60.0)

    def test_only_one_live_model_session_can_be_in_flight(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = CampaignLedger(
                Path(directory) / "campaign.sqlite3",
                "campaign-live-in-flight",
            )
            ledger.reserve_session(
                "live-1",
                mode="live",
                case_id="E01",
                expected_max_cost_usd=1,
            )
            with self.assertRaises(CampaignError) as blocked:
                ledger.reserve_session(
                    "live-2",
                    mode="live",
                    case_id="E02",
                    expected_max_cost_usd=1,
                )
            self.assertEqual(blocked.exception.reason, "live_session_in_flight")
            ledger.settle_session("live-1", actual_cost_usd=0.5)
            ledger.reserve_session(
                "live-2",
                mode="live",
                case_id="E02",
                expected_max_cost_usd=1,
            )

    def test_live_replay_and_correction_cycle_limits_are_independent(self):
        limits = CampaignLimits(
            max_sessions=3,
            max_live_sessions=1,
            max_replay_sessions=2,
            max_correction_cycles=3,
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = CampaignLedger(
                Path(directory) / "campaign.sqlite3",
                "campaign-modes",
                limits=limits,
            )
            ledger.reserve_session("live-1", mode="live", case_id="E01", expected_max_cost_usd=1)
            with self.assertRaises(CampaignError) as live_limit:
                ledger.reserve_session("live-2", mode="live", case_id="E02", expected_max_cost_usd=1)
            self.assertEqual(live_limit.exception.reason, "live_session_limit")
            ledger.reserve_session("replay-1", mode="replay", case_id="E01", expected_max_cost_usd=1)
            ledger.reserve_session("replay-2", mode="replay", case_id="E02", expected_max_cost_usd=1)
            for cycle in range(1, 4):
                ledger.record_correction_cycle(f"cycle-{cycle}")
            with self.assertRaises(CampaignError) as cycles:
                ledger.record_correction_cycle("cycle-4")
            self.assertEqual(cycles.exception.reason, "correction_cycle_limit")

    def test_source_inflight_and_three_second_spacing(self):
        clock = FakeClock()
        limits = CampaignLimits(min_source_interval_seconds=3)
        with tempfile.TemporaryDirectory() as directory:
            ledger = CampaignLedger(
                Path(directory) / "campaign.sqlite3",
                "campaign-source",
                limits=limits,
                clock=clock,
            )
            first = ledger.reserve_http_attempt(
                source="taxlaw.nts.go.kr",
                case_id="E03",
                owner_id="owner-1",
            )
            self.assertEqual(first.wait_seconds, 0)
            ledger.start_http_attempt(first.token)
            with self.assertRaises(CampaignError) as busy:
                ledger.reserve_http_attempt(
                    source="taxlaw.nts.go.kr",
                    case_id="E04",
                    owner_id="owner-2",
                )
            self.assertEqual(busy.exception.reason, "source_in_flight")
            with self.assertRaises(CampaignError) as same_owner_busy:
                ledger.reserve_http_attempt(
                    source="taxlaw.nts.go.kr",
                    case_id="E03",
                    owner_id="owner-1",
                )
            self.assertEqual(same_owner_busy.exception.reason, "source_in_flight")
            ledger.finish_http_attempt(first.token, status=200)
            second = ledger.reserve_http_attempt(
                source="taxlaw.nts.go.kr",
                case_id="E03",
                owner_id="owner-2",
            )
            self.assertEqual(second.wait_seconds, 3)
            clock.sleep(second.wait_seconds)
            ledger.start_http_attempt(second.token)
            ledger.finish_http_attempt(second.token, status=200)
            self.assertEqual(
                ledger.snapshot()["sources"]["taxlaw.nts.go.kr"]["last_started_at"],
                clock(),
            )

    def test_oversize_response_is_not_recorded_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.sqlite3"
            ledger = CampaignLedger(path, "oversize-case")
            attempt = ledger.reserve_http_attempt("www.law.go.kr", "E01", "owner")
            ledger.start_http_attempt(attempt.token)
            ledger.finish_http_attempt(attempt.token, status=200, error_stage="response_size")
            with sqlite3.connect(path) as connection:
                outcome, stage = connection.execute(
                    "SELECT outcome, error_stage FROM http_attempts WHERE token=?", (attempt.token,)
                ).fetchone()
            self.assertEqual((outcome, stage), ("incomplete_response", "response_size"))

    def test_not_found_http_attempt_is_not_recorded_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign.sqlite3"
            ledger = CampaignLedger(path, "drf-404")
            attempt = ledger.reserve_http_attempt("www.law.go.kr", "E01", "owner")
            ledger.start_http_attempt(attempt.token)
            ledger.finish_http_attempt(attempt.token, status=404)
            with sqlite3.connect(path) as connection:
                outcome = connection.execute(
                    "SELECT outcome FROM http_attempts WHERE token=?", (attempt.token,)
                ).fetchone()[0]
            self.assertEqual(outcome, "http_error")

    def test_drf_404_challenge_does_not_retry_under_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "TAXAX_E2E_MODE": "live",
                "TAXAX_E2E_CAMPAIGN_LEDGER": str(Path(directory) / "campaign.sqlite3"),
                "TAXAX_E2E_CAMPAIGN_ID": "campaign-challenge",
                "TAXAX_E2E_RUN_ID": "run-challenge",
                "TAXAX_E2E_CASE_ID": "S09",
                "TAXAX_E2E_SESSION_ID": "session-challenge",
            }
            url = "https://www.law.go.kr/DRF/lawSearch.do"
            opener = SequenceOpener([HTTPError(url, 404, "challenge", Message(), io.BytesIO(b"<html>CAPTCHA</html>")), RawResponse()])
            with patch.dict(os.environ, environment, clear=False):
                with self.assertRaises(TransportError) as caught:
                    HttpTransport(min_interval_seconds=0, max_attempts=3, opener=opener).request(url)
            self.assertEqual(caught.exception.code, ErrorCode.ACCESS_DENIED)
            self.assertTrue(caught.exception.details["access_challenge"])
            self.assertEqual(opener.calls, 1)

    def test_auth_captcha_rate_limit_timeout_and_server_error_open_source_circuit(self):
        scenarios = (
            ({"status": 403}, "permanent"),
            ({"status": 200, "response_body": b"<html>CAPTCHA</html>"}, "permanent"),
            ({"status": 429, "retry_after": "60"}, "open"),
            ({"error_stage": "timeout"}, "open"),
            ({"status": 503}, "open"),
        )
        for index, (result, expected_state) in enumerate(scenarios):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as directory:
                clock = FakeClock()
                ledger = CampaignLedger(
                    Path(directory) / "campaign.sqlite3",
                    f"campaign-circuit-{index}",
                    clock=clock,
                )
                attempt = ledger.reserve_http_attempt(
                    source="olta.re.kr",
                    case_id="S09",
                    owner_id="owner",
                )
                ledger.start_http_attempt(attempt.token)
                ledger.finish_http_attempt(attempt.token, **result)
                state = ledger.snapshot()["sources"]["olta.re.kr"]
                self.assertEqual(state["circuit_state"], expected_state)
                if expected_state == "open":
                    minimum = 60 if result.get("status") == 429 else 1800
                    self.assertGreaterEqual(state["blocked_until"], clock() + minimum)
                with self.assertRaises(CampaignError):
                    ledger.reserve_http_attempt(
                        source="olta.re.kr",
                        case_id="S09",
                        owner_id="next",
                    )

    def test_recovery_probe_is_required_and_limited_to_three_per_day(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            ledger = CampaignLedger(
                Path(directory) / "campaign.sqlite3",
                "campaign-recovery",
                clock=clock,
            )
            failed = ledger.reserve_http_attempt("www.law.go.kr", "E01", "owner-0")
            ledger.start_http_attempt(failed.token)
            ledger.finish_http_attempt(failed.token, error_stage="timeout")
            clock.advance(1800)
            with self.assertRaises(CampaignError) as normal:
                ledger.reserve_http_attempt("www.law.go.kr", "E01", "normal")
            self.assertEqual(normal.exception.reason, "recovery_probe_required")
            for index in range(3):
                probe = ledger.reserve_http_attempt(
                    "www.law.go.kr",
                    "E01",
                    f"probe-{index}",
                    recovery_probe=True,
                )
                ledger.start_http_attempt(probe.token)
                ledger.finish_http_attempt(probe.token, error_stage="timeout")
                clock.advance(1800)
            with self.assertRaises(CampaignError) as exhausted:
                ledger.reserve_http_attempt(
                    "www.law.go.kr",
                    "E01",
                    "probe-4",
                    recovery_probe=True,
                )
            self.assertEqual(exhausted.exception.reason, "recovery_probe_limit")

    def test_replay_exact_match_never_falls_back_to_network_and_has_separate_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "campaign.sqlite3"
            store_path = root / "replay.json"
            url = "https://taxlaw.nts.go.kr/entry.do"
            ReplayStore(store_path).put(
                method="GET",
                url=url,
                body=None,
                response=HttpResponse(
                    url=url,
                    status=200,
                    headers={"content-type": "text/html"},
                    body=b"public fixture",
                ),
            )
            opener = FailingOpener()
            environment = {
                "TAXAX_E2E_MODE": "replay",
                "TAXAX_E2E_CAMPAIGN_LEDGER": str(ledger_path),
                "TAXAX_E2E_CAMPAIGN_ID": "campaign-replay",
                "TAXAX_E2E_RUN_ID": "run-replay",
                "TAXAX_E2E_CASE_ID": "E03",
                "TAXAX_E2E_SESSION_ID": "session-replay",
                "TAXAX_E2E_REPLAY_STORE": str(store_path),
            }
            with patch.dict(os.environ, environment, clear=False):
                transport = SessionHttpTransport(
                    "taxlaw.nts.go.kr",
                    min_interval_seconds=0,
                    opener=opener,
                )
                response = transport.request(url)
                self.assertEqual(response.body, b"public fixture")
                with self.assertRaises(TransportError) as missing:
                    transport.request(url + "?missing=1")
            self.assertEqual(missing.exception.code, ErrorCode.NOT_FOUND)
            self.assertTrue(missing.exception.details["replay_miss"])
            self.assertEqual(opener.calls, 0)
            snapshot = CampaignLedger(
                ledger_path,
                "campaign-replay",
            ).snapshot()
            self.assertEqual(snapshot["http"]["live"], 0)
            self.assertEqual(snapshot["http"]["replay_hits"], 1)
            self.assertEqual(snapshot["http"]["replay_misses"], 1)

    def test_replay_store_redacts_secrets_from_persisted_request_and_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.json"
            secret = "credential-canary"
            ReplayStore(path).put(
                method="GET",
                url=f"https://www.law.go.kr/DRF/lawSearch.do?OC={secret}",
                body=None,
                response=HttpResponse(
                    url=f"https://www.law.go.kr/result?OC={secret}",
                    status=200,
                    headers={"set-cookie": secret, "content-type": "application/json"},
                    body=json.dumps({"official_url": f"https://www.law.go.kr/?OC={secret}"}).encode(),
                ),
                secrets=(secret,),
            )
            persisted = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, persisted)
            self.assertNotIn("set-cookie", persisted.lower())


    def test_live_transport_guards_bootstrap_and_stops_retry_after_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "campaign.sqlite3"
            environment = {
                "TAXAX_E2E_MODE": "live",
                "TAXAX_E2E_CAMPAIGN_LEDGER": str(ledger_path),
                "TAXAX_E2E_CAMPAIGN_ID": "campaign-live",
                "TAXAX_E2E_RUN_ID": "run-live",
                "TAXAX_E2E_CASE_ID": "S07",
                "TAXAX_E2E_SESSION_ID": "session-live",
            }
            opener = SequenceOpener([URLError(TimeoutError("timed out")), RawResponse()])
            with patch.dict(os.environ, environment, clear=False):
                transport = HttpTransport(
                    min_interval_seconds=0,
                    max_attempts=3,
                    opener=opener,
                )
                with self.assertRaises(TransportError) as failed:
                    transport.request("https://www.law.go.kr/DRF/lawSearch.do")
                with self.assertRaises(TransportError) as queued:
                    transport.request("https://www.law.go.kr/DRF/lawSearch.do")
            self.assertEqual(opener.calls, 1)
            self.assertEqual(failed.exception.details["stage"], "timeout")
            self.assertEqual(failed.exception.details["retry_stopped"], "source_circuit_open")
            self.assertEqual(queued.exception.details["reason"], "source_circuit_open")
            snapshot = CampaignLedger(ledger_path, "campaign-live").snapshot()
            self.assertEqual(snapshot["http"]["live"], 1)
            self.assertEqual(snapshot["sources"]["law.go.kr"]["circuit_state"], "open")

    def test_redirect_creates_a_separate_guarded_http_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "campaign.sqlite3"
            environment = {
                "TAXAX_E2E_MODE": "live",
                "TAXAX_E2E_CAMPAIGN_LEDGER": str(ledger_path),
                "TAXAX_E2E_CAMPAIGN_ID": "campaign-redirect",
                "TAXAX_E2E_RUN_ID": "run-redirect",
                "TAXAX_E2E_CASE_ID": "E01",
                "TAXAX_E2E_SESSION_ID": "session-redirect",
            }
            handler = transport_module._RestrictedRedirectHandler(
                frozenset({"www.law.go.kr", "open.law.go.kr"})
            )
            opener = RedirectingOpener(
                handler,
                target="https://open.law.go.kr/redirected",
            )
            with patch.dict(os.environ, environment, clear=False):
                response = HttpTransport(
                    min_interval_seconds=0,
                    max_attempts=1,
                    opener=opener,
                ).request("https://www.law.go.kr/start")
            self.assertEqual(response.status, 200)
            snapshot = CampaignLedger(ledger_path, "campaign-redirect").snapshot()
            self.assertEqual(snapshot["http"]["live"], 2)
            self.assertIsNone(snapshot["sources"]["law.go.kr"]["in_flight_owner"])


if __name__ == "__main__":
    unittest.main()
