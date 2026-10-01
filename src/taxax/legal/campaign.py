from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Callable, Iterator, Mapping


@dataclass(frozen=True)
class CampaignLimits:
    model_cost_ceiling_usd: float = 60.0
    new_session_stop_usd: float = 48.0
    max_sessions: int = 180
    max_live_sessions: int = 60
    max_replay_sessions: int = 120
    max_concurrent_live_sessions: int = 1
    max_live_http: int = 360
    max_source_http_24h: int = 120
    max_case_live_http: int = 12
    min_source_interval_seconds: float = 3.0
    max_recovery_probes_24h: int = 3
    circuit_cooldown_seconds: float = 30 * 60
    max_correction_cycles: int = 3

    def validate(self) -> None:
        if not 0 < self.new_session_stop_usd <= self.model_cost_ceiling_usd:
            raise ValueError("campaign 비용 상한이 올바르지 않습니다.")
        integer_limits = (
            self.max_sessions,
            self.max_live_sessions,
            self.max_replay_sessions,
            self.max_concurrent_live_sessions,
            self.max_live_http,
            self.max_source_http_24h,
            self.max_case_live_http,
            self.max_recovery_probes_24h,
            self.max_correction_cycles,
        )
        if any(value <= 0 for value in integer_limits):
            raise ValueError("campaign 횟수 상한은 양수여야 합니다.")
        if self.max_live_sessions + self.max_replay_sessions < self.max_sessions:
            raise ValueError("mode별 session 상한 합이 전체 상한보다 작습니다.")
        if self.min_source_interval_seconds < 0 or self.circuit_cooldown_seconds <= 0:
            raise ValueError("campaign source 시간 제한이 올바르지 않습니다.")


class CampaignError(RuntimeError):
    def __init__(
        self,
        reason: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.details = dict(details or {})


@dataclass(frozen=True)
class HttpAttemptReservation:
    token: str
    source: str
    case_id: str
    owner_id: str
    wait_seconds: float
    recovery_probe: bool


@dataclass(frozen=True)
class CampaignEnvironment:
    ledger: "CampaignLedger"
    mode: str
    campaign_id: str
    run_id: str
    session_id: str
    case_id: str
    replay_store: Path | None
    recovery_probe: bool


_COST_SCALE = Decimal("1000000")
_SECRET_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-api-key",
    }
)


def _cost_microusd(value: float | int | str, *, positive: bool) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CampaignError("invalid_cost", "모델 비용 값이 올바르지 않습니다.") from exc
    if not amount.is_finite() or amount < 0 or (positive and amount <= 0):
        raise CampaignError("invalid_cost", "모델 비용 값이 올바르지 않습니다.")
    return int((amount * _COST_SCALE).to_integral_value(rounding=ROUND_CEILING))


def _usd(value: int) -> float:
    return float(Decimal(value) / _COST_SCALE)


def _limits_payload(limits: CampaignLimits) -> tuple[str, str]:
    payload = json.dumps(asdict(limits), sort_keys=True, separators=(",", ":"))
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _retry_after_seconds(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def contains_access_challenge(body: bytes) -> bool:
    sample = body[:256 * 1024].lower()
    markers = (
        b"captcha",
        b"recaptcha",
        b"access denied",
        "접근이 거부".encode("utf-8"),
        "로그인이 필요".encode("utf-8"),
        "인증이 필요".encode("utf-8"),
    )
    return any(marker in sample for marker in markers)


class CampaignLedger:
    def __init__(
        self,
        path: Path,
        campaign_id: str,
        *,
        limits: CampaignLimits | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path)
        self.campaign_id = campaign_id.strip()
        self.limits = limits or CampaignLimits()
        self.clock = clock
        if not self.campaign_id or len(self.campaign_id) > 200:
            raise ValueError("campaign_id가 올바르지 않습니다.")
        self.limits.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        payload, digest = _limits_payload(self.limits)
        with closing(self._connect()) as connection:
            # 여러 Codex 세션이 새 campaign DB를 동시에 열면 SQLite가 WAL 전환에
            # busy_timeout을 적용하지 않고 즉시 'database is locked'를 반환할 수 있다.
            for attempt in range(10):
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 9:
                        raise
                    time.sleep(0.01 * (attempt + 1))
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS campaigns(
                    campaign_id TEXT PRIMARY KEY,
                    limits_json TEXT NOT NULL,
                    limits_sha256 TEXT NOT NULL,
                    halt_reason TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_reservations(
                    campaign_id TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    reserved_microusd INTEGER NOT NULL,
                    actual_microusd INTEGER,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    settled_at REAL,
                    PRIMARY KEY(campaign_id, reservation_id)
                );
                CREATE TABLE IF NOT EXISTS correction_cycles(
                    campaign_id TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(campaign_id, cycle_id)
                );
                CREATE TABLE IF NOT EXISTS http_attempts(
                    campaign_id TEXT NOT NULL,
                    token TEXT NOT NULL,
                    source TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    recovery_probe INTEGER NOT NULL,
                    reserved_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    status_code INTEGER,
                    error_stage TEXT,
                    outcome TEXT,
                    PRIMARY KEY(campaign_id, token)
                );
                CREATE INDEX IF NOT EXISTS http_attempts_campaign_time
                    ON http_attempts(campaign_id, reserved_at);
                CREATE INDEX IF NOT EXISTS http_attempts_source_time
                    ON http_attempts(campaign_id, source, reserved_at);
                CREATE TABLE IF NOT EXISTS source_state(
                    campaign_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    circuit_state TEXT NOT NULL DEFAULT 'closed',
                    blocked_until REAL,
                    circuit_reason TEXT,
                    in_flight_owner TEXT,
                    last_started_at REAL,
                    PRIMARY KEY(campaign_id, source)
                );
                CREATE TABLE IF NOT EXISTS replay_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    found INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO campaigns("
                    "campaign_id, limits_json, limits_sha256, halt_reason, created_at"
                    ") VALUES(?,?,?,?,?)",
                    (self.campaign_id, payload, digest, None, self.clock()),
                )
                row = connection.execute(
                    "SELECT limits_sha256 FROM campaigns WHERE campaign_id=?",
                    (self.campaign_id,),
                ).fetchone()
                if row is None or row["limits_sha256"] != digest:
                    raise CampaignError(
                        "limits_mismatch",
                        "같은 campaign_id에 서로 다른 제한값을 사용할 수 없습니다.",
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            finally:
                connection.close()
            raise
        else:
            connection.close()

    def _campaign_halt(self, connection: sqlite3.Connection) -> str | None:
        row = connection.execute(
            "SELECT halt_reason FROM campaigns WHERE campaign_id=?",
            (self.campaign_id,),
        ).fetchone()
        return row["halt_reason"] if row else "campaign_missing"

    def _set_halt(self, connection: sqlite3.Connection, reason: str) -> None:
        connection.execute(
            "UPDATE campaigns SET halt_reason=COALESCE(halt_reason, ?) WHERE campaign_id=?",
            (reason, self.campaign_id),
        )

    def reserve_session(
        self,
        reservation_id: str,
        *,
        mode: str,
        case_id: str,
        expected_max_cost_usd: float,
    ) -> dict[str, object]:
        if mode not in {"live", "replay"}:
            raise CampaignError("invalid_mode", "campaign mode는 live 또는 replay여야 합니다.")
        if not reservation_id or not case_id:
            raise CampaignError("invalid_reservation", "session reservation 식별자가 필요합니다.")
        reserved = _cost_microusd(expected_max_cost_usd, positive=True)
        now = self.clock()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM session_reservations "
                "WHERE campaign_id=? AND reservation_id=?",
                (self.campaign_id, reservation_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["mode"] != mode
                    or existing["case_id"] != case_id
                    or existing["reserved_microusd"] != reserved
                ):
                    raise CampaignError(
                        "reservation_conflict",
                        "같은 reservation_id의 내용이 기존 장부와 다릅니다.",
                    )
                return {
                    "reservation_id": reservation_id,
                    "mode": mode,
                    "case_id": case_id,
                    "reserved_cost_usd": _usd(reserved),
                    "status": existing["status"],
                }
            halt_reason = self._campaign_halt(connection)
            if halt_reason:
                raise CampaignError(
                    "campaign_halted",
                    "campaign이 중단 상태라 새 session을 시작할 수 없습니다.",
                    details={"halt_reason": halt_reason},
                )
            counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN mode='live' THEN 1 ELSE 0 END) AS live, "
                "SUM(CASE WHEN mode='replay' THEN 1 ELSE 0 END) AS replay "
                "FROM session_reservations WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            total = int(counts["total"] or 0)
            live = int(counts["live"] or 0)
            replay = int(counts["replay"] or 0)
            if total >= self.limits.max_sessions:
                raise CampaignError("session_limit", "campaign model session 상한에 도달했습니다.")
            if mode == "live" and live >= self.limits.max_live_sessions:
                raise CampaignError("live_session_limit", "campaign live session 상한에 도달했습니다.")
            if mode == "replay" and replay >= self.limits.max_replay_sessions:
                raise CampaignError("replay_session_limit", "campaign replay session 상한에 도달했습니다.")
            if mode == "live":
                active_live = connection.execute(
                    "SELECT COUNT(*) AS count FROM session_reservations "
                    "WHERE campaign_id=? AND mode='live' AND status='reserved'",
                    (self.campaign_id,),
                ).fetchone()
                if int(active_live["count"] or 0) >= self.limits.max_concurrent_live_sessions:
                    raise CampaignError(
                        "live_session_in_flight",
                        "동시에 실행할 수 있는 live model session 상한에 도달했습니다.",
                    )
            cost = connection.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN actual_microusd IS NOT NULL THEN actual_microusd ELSE 0 END),0) AS settled, "
                "COALESCE(SUM(CASE WHEN status IN ('reserved','unknown') THEN reserved_microusd ELSE 0 END),0) AS reserved "
                "FROM session_reservations WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            committed = int(cost["settled"] or 0) + int(cost["reserved"] or 0)
            stop = _cost_microusd(self.limits.new_session_stop_usd, positive=True)
            if committed >= stop or committed + reserved > stop:
                raise CampaignError(
                    "new_session_cost_stop",
                    "48달러 신규 실행 중지선 때문에 session을 예약할 수 없습니다.",
                    details={
                        "committed_cost_usd": _usd(committed),
                        "requested_reserve_usd": _usd(reserved),
                    },
                )
            connection.execute(
                "INSERT INTO session_reservations("
                "campaign_id, reservation_id, mode, case_id, reserved_microusd, "
                "actual_microusd, status, created_at, settled_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    self.campaign_id,
                    reservation_id,
                    mode,
                    case_id,
                    reserved,
                    None,
                    "reserved",
                    now,
                    None,
                ),
            )
        return {
            "reservation_id": reservation_id,
            "mode": mode,
            "case_id": case_id,
            "reserved_cost_usd": _usd(reserved),
            "status": "reserved",
        }

    def settle_session(
        self,
        reservation_id: str,
        *,
        actual_cost_usd: float | None,
    ) -> dict[str, object]:
        now = self.clock()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM session_reservations "
                "WHERE campaign_id=? AND reservation_id=?",
                (self.campaign_id, reservation_id),
            ).fetchone()
            if row is None:
                raise CampaignError("reservation_missing", "settlement 대상 reservation이 없습니다.")
            if actual_cost_usd is None:
                if row["status"] == "settled":
                    raise CampaignError(
                        "settlement_conflict",
                        "이미 비용이 확정된 session을 미확정으로 바꿀 수 없습니다.",
                    )
                connection.execute(
                    "UPDATE session_reservations SET status='unknown', settled_at=? "
                    "WHERE campaign_id=? AND reservation_id=?",
                    (now, self.campaign_id, reservation_id),
                )
                self._set_halt(connection, "usage_or_cost_unknown")
                return {
                    "reservation_id": reservation_id,
                    "status": "unknown",
                    "actual_cost_usd": None,
                }
            actual = _cost_microusd(actual_cost_usd, positive=False)
            if row["status"] == "settled":
                if row["actual_microusd"] != actual:
                    raise CampaignError(
                        "settlement_conflict",
                        "같은 reservation의 settlement 금액이 다릅니다.",
                    )
                return {
                    "reservation_id": reservation_id,
                    "status": "settled",
                    "actual_cost_usd": _usd(actual),
                }
            connection.execute(
                "UPDATE session_reservations SET actual_microusd=?, status='settled', settled_at=? "
                "WHERE campaign_id=? AND reservation_id=?",
                (actual, now, self.campaign_id, reservation_id),
            )
            if actual > int(row["reserved_microusd"]):
                self._set_halt(connection, "reservation_exceeded")
            total = connection.execute(
                "SELECT COALESCE(SUM(actual_microusd),0) AS total "
                "FROM session_reservations "
                "WHERE campaign_id=? AND actual_microusd IS NOT NULL",
                (self.campaign_id,),
            ).fetchone()
            ceiling = _cost_microusd(self.limits.model_cost_ceiling_usd, positive=True)
            if int(total["total"] or 0) > ceiling:
                self._set_halt(connection, "model_cost_ceiling_exceeded")
        return {
            "reservation_id": reservation_id,
            "status": "settled",
            "actual_cost_usd": _usd(actual),
        }

    def record_correction_cycle(self, cycle_id: str) -> int:
        if not cycle_id:
            raise CampaignError("invalid_cycle", "correction cycle 식별자가 필요합니다.")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM correction_cycles WHERE campaign_id=? AND cycle_id=?",
                (self.campaign_id, cycle_id),
            ).fetchone()
            if existing is not None:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM correction_cycles WHERE campaign_id=?",
                    (self.campaign_id,),
                ).fetchone()
                return int(row["count"])
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM correction_cycles WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            count = int(row["count"])
            if count >= self.limits.max_correction_cycles:
                raise CampaignError(
                    "correction_cycle_limit",
                    "campaign 수정 cycle 상한에 도달했습니다.",
                )
            connection.execute(
                "INSERT INTO correction_cycles(campaign_id, cycle_id, created_at) VALUES(?,?,?)",
                (self.campaign_id, cycle_id, self.clock()),
            )
            return count + 1

    def reserve_http_attempt(
        self,
        source: str,
        case_id: str,
        owner_id: str,
        *,
        recovery_probe: bool = False,
    ) -> HttpAttemptReservation:
        source = source.strip().lower()
        if not source or not case_id or not owner_id:
            raise CampaignError("invalid_http_reservation", "HTTP reservation 식별자가 필요합니다.")
        now = self.clock()
        cutoff = now - 24 * 60 * 60
        with self._transaction() as connection:
            if self._campaign_halt(connection):
                raise CampaignError("campaign_halted", "campaign이 중단 상태입니다.")
            connection.execute(
                "INSERT OR IGNORE INTO source_state("
                "campaign_id, source, circuit_state, blocked_until, circuit_reason, "
                "in_flight_owner, last_started_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (self.campaign_id, source, "closed", None, None, None, None),
            )
            state = connection.execute(
                "SELECT * FROM source_state WHERE campaign_id=? AND source=?",
                (self.campaign_id, source),
            ).fetchone()
            circuit_state = state["circuit_state"]
            blocked_until = state["blocked_until"]
            if circuit_state == "permanent":
                raise CampaignError(
                    "source_permanently_blocked",
                    "출처가 403/CAPTCHA/인증 회로로 중단됐습니다.",
                    details={"source": source, "reason": state["circuit_reason"]},
                )
            if circuit_state == "open":
                if blocked_until is not None and float(blocked_until) > now:
                    raise CampaignError(
                        "source_circuit_open",
                        "출처 회로의 대기 시간이 끝나지 않았습니다.",
                        details={"source": source, "blocked_until": float(blocked_until)},
                    )
                if not recovery_probe:
                    raise CampaignError(
                        "recovery_probe_required",
                        "열린 출처 회로는 명시적 recovery probe만 허용합니다.",
                        details={"source": source},
                    )
            elif recovery_probe:
                raise CampaignError(
                    "recovery_probe_not_required",
                    "닫힌 출처 회로에는 recovery probe를 사용할 수 없습니다.",
                )
            if recovery_probe:
                probe_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM http_attempts "
                    "WHERE campaign_id=? AND source=? AND recovery_probe=1 AND reserved_at>=?",
                    (self.campaign_id, source, cutoff),
                ).fetchone()
                if int(probe_count["count"]) >= self.limits.max_recovery_probes_24h:
                    raise CampaignError(
                        "recovery_probe_limit",
                        "출처별 24시간 recovery probe 상한에 도달했습니다.",
                    )
            in_flight_owner = state["in_flight_owner"]
            if in_flight_owner:
                raise CampaignError(
                    "source_in_flight",
                    "같은 출처에는 동시에 하나의 HTTP 요청만 허용됩니다.",
                    details={"source": source},
                )
            total_count = connection.execute(
                "SELECT COUNT(*) AS count FROM http_attempts WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            if int(total_count["count"]) >= self.limits.max_live_http:
                raise CampaignError("live_http_limit", "campaign live HTTP 상한에 도달했습니다.")
            source_count = connection.execute(
                "SELECT COUNT(*) AS count FROM http_attempts "
                "WHERE campaign_id=? AND source=? AND reserved_at>=?",
                (self.campaign_id, source, cutoff),
            ).fetchone()
            if int(source_count["count"]) >= self.limits.max_source_http_24h:
                raise CampaignError(
                    "source_http_24h_limit",
                    "출처별 24시간 HTTP 상한에 도달했습니다.",
                )
            case_count = connection.execute(
                "SELECT COUNT(*) AS count FROM http_attempts "
                "WHERE campaign_id=? AND case_id=?",
                (self.campaign_id, case_id),
            ).fetchone()
            if int(case_count["count"]) >= self.limits.max_case_live_http:
                raise CampaignError(
                    "case_http_limit",
                    "사례별 live HTTP 상한에 도달했습니다.",
                )
            last_started = state["last_started_at"]
            wait_seconds = 0.0
            if last_started is not None:
                wait_seconds = max(
                    0.0,
                    float(last_started)
                    + self.limits.min_source_interval_seconds
                    - now,
                )
            token = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO http_attempts("
                "campaign_id, token, source, case_id, owner_id, recovery_probe, "
                "reserved_at, started_at, finished_at, status_code, error_stage, outcome"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.campaign_id,
                    token,
                    source,
                    case_id,
                    owner_id,
                    int(recovery_probe),
                    now,
                    None,
                    None,
                    None,
                    None,
                    "reserved",
                ),
            )
            connection.execute(
                "UPDATE source_state SET in_flight_owner=? "
                "WHERE campaign_id=? AND source=?",
                (owner_id, self.campaign_id, source),
            )
        return HttpAttemptReservation(
            token=token,
            source=source,
            case_id=case_id,
            owner_id=owner_id,
            wait_seconds=wait_seconds,
            recovery_probe=recovery_probe,
        )

    def start_http_attempt(self, token: str) -> None:
        now = self.clock()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM http_attempts WHERE campaign_id=? AND token=?",
                (self.campaign_id, token),
            ).fetchone()
            if row is None:
                raise CampaignError("http_reservation_missing", "HTTP reservation이 없습니다.")
            if row["started_at"] is not None:
                return
            if row["finished_at"] is not None:
                raise CampaignError("http_reservation_finished", "종료된 HTTP reservation입니다.")
            state = connection.execute(
                "SELECT * FROM source_state WHERE campaign_id=? AND source=?",
                (self.campaign_id, row["source"]),
            ).fetchone()
            if state is None or state["in_flight_owner"] != row["owner_id"]:
                raise CampaignError("http_owner_mismatch", "HTTP source owner가 일치하지 않습니다.")
            last_started = state["last_started_at"]
            if (
                last_started is not None
                and now
                < float(last_started) + self.limits.min_source_interval_seconds - 1e-6
            ):
                raise CampaignError(
                    "source_spacing_not_elapsed",
                    "출처별 최소 요청 간격이 지나지 않았습니다.",
                    details={
                        "wait_seconds": float(last_started)
                        + self.limits.min_source_interval_seconds
                        - now
                    },
                )
            connection.execute(
                "UPDATE http_attempts SET started_at=?, outcome='started' "
                "WHERE campaign_id=? AND token=?",
                (now, self.campaign_id, token),
            )
            connection.execute(
                "UPDATE source_state SET last_started_at=? "
                "WHERE campaign_id=? AND source=?",
                (now, self.campaign_id, row["source"]),
            )

    def finish_http_attempt(
        self,
        token: str,
        *,
        status: int | None = None,
        error_stage: str | None = None,
        response_body: bytes = b"",
        retry_after: str | int | float | None = None,
    ) -> None:
        now = self.clock()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM http_attempts WHERE campaign_id=? AND token=?",
                (self.campaign_id, token),
            ).fetchone()
            if row is None:
                raise CampaignError("http_reservation_missing", "HTTP reservation이 없습니다.")
            if row["finished_at"] is not None:
                return
            challenge = contains_access_challenge(response_body)
            outcome = "success"
            circuit_state: str | None = None
            blocked_until: float | None = None
            circuit_reason: str | None = None
            if status in {401, 403} or challenge:
                outcome = "access_blocked"
                circuit_state = "permanent"
                circuit_reason = "captcha_or_auth" if challenge else f"http_{status}"
            elif status == 429:
                outcome = "rate_limited"
                circuit_state = "open"
                delay = max(
                    self.limits.circuit_cooldown_seconds,
                    _retry_after_seconds(retry_after) or 0.0,
                )
                blocked_until = now + delay
                circuit_reason = "http_429"
            elif error_stage == "response_size":
                outcome = "incomplete_response"
            elif error_stage in {"timeout", "dns", "tcp", "tls", "network"}:
                outcome = "network_failure"
                circuit_state = "open"
                blocked_until = now + self.limits.circuit_cooldown_seconds
                circuit_reason = error_stage
            elif status is not None and status >= 500:
                outcome = "server_failure"
                circuit_state = "open"
                blocked_until = now + self.limits.circuit_cooldown_seconds
                circuit_reason = f"http_{status}"
            elif status is not None and status >= 400:
                outcome = "http_error"
            connection.execute(
                "UPDATE http_attempts SET finished_at=?, status_code=?, error_stage=?, outcome=? "
                "WHERE campaign_id=? AND token=?",
                (
                    now,
                    status,
                    error_stage,
                    outcome,
                    self.campaign_id,
                    token,
                ),
            )
            if circuit_state is not None:
                connection.execute(
                    "UPDATE source_state SET circuit_state=?, blocked_until=?, circuit_reason=? "
                    "WHERE campaign_id=? AND source=?",
                    (
                        circuit_state,
                        blocked_until,
                        circuit_reason,
                        self.campaign_id,
                        row["source"],
                    ),
                )
            elif bool(row["recovery_probe"]):
                connection.execute(
                    "UPDATE source_state SET circuit_state='closed', blocked_until=NULL, "
                    "circuit_reason=NULL WHERE campaign_id=? AND source=?",
                    (self.campaign_id, row["source"]),
                )
            remaining = connection.execute(
                "SELECT COUNT(*) AS count FROM http_attempts "
                "WHERE campaign_id=? AND source=? AND owner_id=? AND finished_at IS NULL",
                (self.campaign_id, row["source"], row["owner_id"]),
            ).fetchone()
            if int(remaining["count"]) == 0:
                connection.execute(
                    "UPDATE source_state SET in_flight_owner=NULL "
                    "WHERE campaign_id=? AND source=? AND in_flight_owner=?",
                    (self.campaign_id, row["source"], row["owner_id"]),
                )

    def release_http_owner(self, owner_id: str) -> int:
        now = self.clock()
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT token, source FROM http_attempts "
                "WHERE campaign_id=? AND owner_id=? AND finished_at IS NULL",
                (self.campaign_id, owner_id),
            ).fetchall()
            connection.execute(
                "UPDATE http_attempts SET finished_at=?, outcome='abandoned' "
                "WHERE campaign_id=? AND owner_id=? AND finished_at IS NULL",
                (now, self.campaign_id, owner_id),
            )
            for source in {row["source"] for row in rows}:
                connection.execute(
                    "UPDATE source_state SET in_flight_owner=NULL "
                    "WHERE campaign_id=? AND source=? AND in_flight_owner=?",
                    (self.campaign_id, source, owner_id),
                )
            return len(rows)

    def record_replay_request(self, case_id: str, signature: str, *, found: bool) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO replay_events(campaign_id, case_id, signature, found, created_at) "
                "VALUES(?,?,?,?,?)",
                (self.campaign_id, case_id, signature, int(found), self.clock()),
            )

    def snapshot(self) -> dict[str, object]:
        with closing(self._connect()) as connection:
            campaign = connection.execute(
                "SELECT halt_reason FROM campaigns WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            sessions = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN mode='live' THEN 1 ELSE 0 END) AS live, "
                "SUM(CASE WHEN mode='replay' THEN 1 ELSE 0 END) AS replay "
                "FROM session_reservations WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            costs = connection.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN actual_microusd IS NOT NULL THEN actual_microusd ELSE 0 END),0) AS settled, "
                "COALESCE(SUM(CASE WHEN status IN ('reserved','unknown') THEN reserved_microusd ELSE 0 END),0) AS reserved "
                "FROM session_reservations WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            live_http = connection.execute(
                "SELECT COUNT(*) AS count FROM http_attempts WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            replay = connection.execute(
                "SELECT "
                "SUM(CASE WHEN found=1 THEN 1 ELSE 0 END) AS hits, "
                "SUM(CASE WHEN found=0 THEN 1 ELSE 0 END) AS misses "
                "FROM replay_events WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            cycles = connection.execute(
                "SELECT COUNT(*) AS count FROM correction_cycles WHERE campaign_id=?",
                (self.campaign_id,),
            ).fetchone()
            source_rows = connection.execute(
                "SELECT * FROM source_state WHERE campaign_id=? ORDER BY source",
                (self.campaign_id,),
            ).fetchall()
        sources = {
            row["source"]: {
                "circuit_state": row["circuit_state"],
                "blocked_until": row["blocked_until"],
                "circuit_reason": row["circuit_reason"],
                "in_flight_owner": row["in_flight_owner"],
                "last_started_at": row["last_started_at"],
            }
            for row in source_rows
        }
        return {
            "campaign_id": self.campaign_id,
            "halt_reason": campaign["halt_reason"] if campaign else "campaign_missing",
            "limits": asdict(self.limits),
            "sessions": {
                "total": int(sessions["total"] or 0),
                "live": int(sessions["live"] or 0),
                "replay": int(sessions["replay"] or 0),
            },
            "cost_usd": {
                "settled": _usd(int(costs["settled"] or 0)),
                "reserved": _usd(int(costs["reserved"] or 0)),
            },
            "http": {
                "live": int(live_http["count"] or 0),
                "replay_hits": int(replay["hits"] or 0),
                "replay_misses": int(replay["misses"] or 0),
            },
            "correction_cycles": int(cycles["count"] or 0),
            "sources": sources,
        }


def campaign_environment(
    environment: Mapping[str, str] | None = None,
) -> CampaignEnvironment | None:
    values = environment or os.environ
    mode = values.get("TAXAX_E2E_MODE", "").strip().lower()
    if not mode:
        return None
    if mode not in {"live", "replay"}:
        raise CampaignError("invalid_mode", "TAXAX_E2E_MODE는 live 또는 replay여야 합니다.")
    required = {
        name: values.get(name, "").strip()
        for name in (
            "TAXAX_E2E_CAMPAIGN_LEDGER",
            "TAXAX_E2E_CAMPAIGN_ID",
            "TAXAX_E2E_RUN_ID",
            "TAXAX_E2E_SESSION_ID",
            "TAXAX_E2E_CASE_ID",
        )
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise CampaignError(
            "campaign_environment_incomplete",
            "campaign 실행 환경이 완전하지 않습니다.",
            details={"missing": sorted(missing)},
        )
    replay_value = values.get("TAXAX_E2E_REPLAY_STORE", "").strip()
    if mode == "replay" and not replay_value:
        raise CampaignError(
            "replay_store_missing",
            "replay mode에는 TAXAX_E2E_REPLAY_STORE가 필요합니다.",
        )
    return CampaignEnvironment(
        ledger=CampaignLedger(
            Path(required["TAXAX_E2E_CAMPAIGN_LEDGER"]),
            required["TAXAX_E2E_CAMPAIGN_ID"],
        ),
        mode=mode,
        campaign_id=required["TAXAX_E2E_CAMPAIGN_ID"],
        run_id=required["TAXAX_E2E_RUN_ID"],
        session_id=required["TAXAX_E2E_SESSION_ID"],
        case_id=required["TAXAX_E2E_CASE_ID"],
        replay_store=Path(replay_value) if replay_value else None,
        recovery_probe=values.get("TAXAX_E2E_RECOVERY_PROBE", "").strip() == "1",
    )


def safe_public_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() not in _SECRET_HEADERS
    }
