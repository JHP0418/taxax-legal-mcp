from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import sqlite3
import ssl
import time
import uuid
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from email.message import Message
from http.cookiejar import CookieJar
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from .budget import RemoteBudgetExceeded, consume_remote_request, remaining_seconds
from .campaign import (
    CampaignEnvironment,
    CampaignError,
    campaign_environment,
    contains_access_challenge,
    safe_public_headers,
)
from .models import ErrorCode

_SECRET_KEYS = {"oc", "authorization", "cookie", "api_key", "apikey", "token"}


def redact_url(url: str) -> str:
    parsed = urlparse(url)
    query = [(key, "[REDACTED]" if key.lower() in _SECRET_KEYS else value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))


def strip_secret_query_params(url: str) -> str:
    """official_url처럼 사용자에게 그대로 노출되는 링크에서 인증값 파라미터를
    아예 제거한다. law.go.kr 검색 응답은 다음 조회용 상세링크에 요청에 쓰인 OC를
    그대로 실어 돌려주므로, redact_url처럼 값만 치환하면 여전히 `oc=` 형태가 남아
    출처 URL 검증(allowlist)에 걸린다.
    """
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in _SECRET_KEYS]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))


def redact_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(r"(?i)(OC|Authorization|Cookie|api[_-]?key|token)(\s*[=:]\s*)[^\s&]+", r"\1\2[REDACTED]", redacted)
    return redacted


def redact_body(body: bytes, secrets: tuple[str, ...]) -> tuple[bytes, bool]:
    """정상 응답도 우리가 보낸 요청값(예: 다음 조회용 링크에 담긴 OC)을 그대로
    되돌려주는 경우가 있어, credential을 저장 전 치환한다. law.go.kr 검색 응답의
    `법령상세링크` 필드가 대표적인 예로, 매 성공 응답마다 등장한다.
    """
    redacted = body
    changed = False
    for secret in secrets:
        if not secret:
            continue
        token = secret.encode("utf-8")
        if token in redacted:
            redacted = redacted.replace(token, b"[REDACTED]")
            changed = True
    return redacted, changed


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    attempts: int = 1


class TransportError(RuntimeError):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        status: int | None = None,
        details: Mapping[str, object] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status
        self.details = dict(details or {})


def _request_signature(
    method: str,
    url: str,
    body: bytes | None,
    secrets: tuple[str, ...],
) -> str:
    safe_url = redact_text(redact_url(url), secrets)
    safe_body = body or b""
    for secret in secrets:
        if secret:
            safe_body = safe_body.replace(secret.encode("utf-8"), b"[REDACTED]")
    payload = json.dumps(
        {
            "method": method.upper(),
            "url": safe_url,
            "body_sha256": hashlib.sha256(safe_body).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ReplayStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "responses": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise TransportError(
                ErrorCode.INVALID_REQUEST,
                "replay response store를 읽을 수 없습니다.",
                details={"stage": "replay", "replay_store_invalid": True},
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or not isinstance(value.get("responses"), dict)
        ):
            raise TransportError(
                ErrorCode.INVALID_REQUEST,
                "replay response store 형식이 올바르지 않습니다.",
                details={"stage": "replay", "replay_store_invalid": True},
            )
        return value

    def put(
        self,
        *,
        method: str,
        url: str,
        body: bytes | None,
        response: HttpResponse,
        secrets: tuple[str, ...] = (),
    ) -> str:
        signature = _request_signature(method, url, body, secrets)
        payload = self._read()
        responses = payload["responses"]
        assert isinstance(responses, dict)
        response_body, _ = redact_body(response.body, secrets)
        responses[signature] = {
            "url": redact_text(redact_url(response.url), secrets),
            "status": int(response.status),
            "headers": safe_public_headers(response.headers),
            "body_base64": base64.b64encode(response_body).decode("ascii"),
            "attempts": int(response.attempts),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        return signature

    def lookup(
        self,
        *,
        method: str,
        url: str,
        body: bytes | None,
        secrets: tuple[str, ...] = (),
    ) -> tuple[str, HttpResponse]:
        signature = _request_signature(method, url, body, secrets)
        payload = self._read()
        responses = payload["responses"]
        assert isinstance(responses, dict)
        item = responses.get(signature)
        if not isinstance(item, dict):
            raise TransportError(
                ErrorCode.NOT_FOUND,
                "등록된 exact-match replay 응답이 없습니다.",
                details={
                    "stage": "replay",
                    "replay_miss": True,
                    "request_signature": signature,
                },
            )
        try:
            response = HttpResponse(
                url=str(item["url"]),
                status=int(item["status"]),
                headers={
                    str(key).lower(): str(value)
                    for key, value in dict(item["headers"]).items()
                },
                body=base64.b64decode(str(item["body_base64"]), validate=True),
                attempts=int(item.get("attempts", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TransportError(
                ErrorCode.INVALID_REQUEST,
                "replay response 항목 형식이 올바르지 않습니다.",
                details={"stage": "replay", "replay_store_invalid": True},
            ) from exc
        return signature, response


class _CampaignHttpChain:
    def __init__(
        self,
        context: CampaignEnvironment,
        source: str,
        sleeper: Callable[[float], None],
    ):
        self.context = context
        self.source = source
        self.sleeper = sleeper
        self.tokens: list[str] = []

    def begin_attempt(
        self,
        source: str | None = None,
        *,
        previous_status: int | None = None,
    ) -> None:
        target = (source or self.source).lower()
        if target != self.source:
            raise TransportError(
                ErrorCode.ACCESS_DENIED,
                "campaign redirect가 다른 출처로 이동할 수 없습니다.",
                details={"stage": "campaign", "reason": "cross_source_redirect"},
            )
        if self.tokens:
            if previous_status is None:
                raise TransportError(
                    ErrorCode.RATE_LIMITED,
                    "같은 출처의 이전 HTTP attempt가 끝나지 않았습니다.",
                    details={"stage": "campaign", "reason": "source_in_flight"},
                )
            self.finish(status=previous_status)
        try:
            reservation = self.context.ledger.reserve_http_attempt(
                target,
                self.context.case_id,
                self.context.session_id,
                recovery_probe=self.context.recovery_probe,
            )
            if reservation.wait_seconds:
                self.sleeper(reservation.wait_seconds)
            self.context.ledger.start_http_attempt(reservation.token)
        except CampaignError as exc:
            raise _campaign_transport_error(exc) from None
        self.tokens.append(reservation.token)

    def finish(
        self,
        *,
        status: int | None = None,
        error_stage: str | None = None,
        response_body: bytes = b"",
        retry_after: str | int | float | None = None,
    ) -> None:
        for token in tuple(self.tokens):
            self.context.ledger.finish_http_attempt(
                token,
                status=status,
                error_stage=error_stage,
                response_body=response_body,
                retry_after=retry_after,
            )
        self.tokens.clear()

    def abandon(self) -> None:
        self.context.ledger.release_http_owner(self.context.session_id)
        self.tokens.clear()


_ACTIVE_CAMPAIGN_CHAIN: ContextVar[_CampaignHttpChain | None] = ContextVar(
    "taxax_active_campaign_http_chain",
    default=None,
)


def _campaign_transport_error(exc: CampaignError) -> TransportError:
    access_reasons = {
        "source_permanently_blocked",
        "cross_source_redirect",
    }
    rate_reasons = {
        "source_in_flight",
        "source_circuit_open",
        "recovery_probe_required",
        "recovery_probe_limit",
        "source_spacing_not_elapsed",
    }
    if exc.reason in access_reasons:
        code = ErrorCode.ACCESS_DENIED
    elif exc.reason in rate_reasons:
        code = ErrorCode.RATE_LIMITED
    else:
        code = ErrorCode.BUDGET_EXHAUSTED
    return TransportError(
        code,
        str(exc),
        retryable=False,
        details={"stage": "campaign", "reason": exc.reason, **exc.details},
    )


def _campaign_context() -> CampaignEnvironment | None:
    try:
        return campaign_environment()
    except CampaignError as exc:
        raise _campaign_transport_error(exc) from None


def _replay_response(
    context: CampaignEnvironment | None,
    *,
    method: str,
    url: str,
    body: bytes | None,
    secrets: tuple[str, ...],
) -> HttpResponse | None:
    if context is None or context.mode != "replay":
        return None
    assert context.replay_store is not None
    store = ReplayStore(context.replay_store)
    try:
        signature, response = store.lookup(
            method=method,
            url=url,
            body=body,
            secrets=secrets,
        )
    except TransportError as exc:
        signature = str(exc.details.get("request_signature") or _request_signature(method, url, body, secrets))
        context.ledger.record_replay_request(context.case_id, signature, found=False)
        raise
    context.ledger.record_replay_request(context.case_id, signature, found=True)
    return response


def _campaign_source(host: str) -> str:
    normalized = host.lower()
    if normalized in {"www.law.go.kr", "open.law.go.kr"}:
        return "law.go.kr"
    return normalized


def _live_chain(
    context: CampaignEnvironment | None,
    *,
    source: str,
    sleeper: Callable[[float], None],
) -> _CampaignHttpChain | None:
    if context is None:
        return None
    if context.mode != "live":
        raise TransportError(
            ErrorCode.INVALID_REQUEST,
            "replay mode에서 live HTTP 경로를 사용할 수 없습니다.",
            details={"stage": "campaign", "reason": "replay_network_forbidden"},
        )
    chain = _CampaignHttpChain(context, source, sleeper)
    chain.begin_attempt()
    return chain


def _finish_campaign_chain(
    chain: _CampaignHttpChain | None,
    **result,
) -> None:
    if chain is not None:
        chain.finish(**result)


class _RestrictedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req: Request, fp, code: int, msg: str, headers: Message, newurl: str):
        parsed = urlparse(newurl)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in self.allowed_hosts:
            raise TransportError(ErrorCode.ACCESS_DENIED, "허용되지 않은 redirect 대상입니다.")
        chain = _ACTIVE_CAMPAIGN_CHAIN.get()
        if chain is not None:
            chain.begin_attempt(_campaign_source(host), previous_status=code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 공유 저장소를 못 쓰는 것과 남은 시간 안에 차례가 오지 않는 것은 다른 일이다.
# 둘 다 None으로 돌려주면 시간이 없는데도 프로세스 안 잠금으로 넘어가 그냥
# 통과해 버린다. 간격 제어가 필요한 바로 그 상황에서 무력해진다.
_NO_TIME = object()


class SharedRateLimiter:
    """host별 요청 간격을 이 기계 전체에서 지킨다.

    예전에는 클래스 변수 dict 하나였다. 이름은 Shared인데 실제로는 프로세스
    하나 안에서만 공유돼서, MCP client가 여러 개 떠 있으면 각자 따로 "1초에
    한 건"을 지켰다. 이 기계에서 Claude Desktop·Codex·Claude Code의 서버와
    CLI가 동시에 돌던 두 시간 동안 법제처로 나간 요청은 분당 최대 17건(재시도와
    상세조회를 포함하면 그 몇 배)이었고, 그 몇 시간 뒤 공인 IP가 차단됐다.
    상대 서버가 보는 것은 프로세스가 아니라 IP 하나다.

    프로세스 사이에서 "마지막으로 언제 보냈는지"를 나누려면 공유 저장소와
    원자적 갱신이 필요하다. SQLite를 쓰는 이유는 파일 잠금을 OS 수준에서
    올바르게 처리하기 때문이다. 직접 만든 잠금 파일은 죽은 프로세스가 남긴
    잠금을 푸는 문제를 따로 풀어야 한다.

    공유 저장소를 못 쓰는 상황(폴더 권한 없음, 디스크 오류)에는 프로세스마다
    독립 전송하는 위험한 fallback 대신 요청을 차단한다.
    """

    @classmethod
    def wait(cls, key: str, interval_seconds: float, sleeper: Callable[[float], None]) -> None:
        if interval_seconds <= 0:
            return
        # 줄을 서는 시간도 조사 예산에서 나간다. 같은 조사를 둘이 동시에 돌리면
        # 한쪽이 순서를 기다리다 60초 예산을 다 쓰고 세 건밖에 못 보내는 일이
        # 실측됐다(다른 쪽은 16건). 기다려도 남은 시간 안에 못 보낼 상황이면
        # 자리를 잡지 않고 예산 초과로 알린다. 아무 소득 없이 시간을 태우고
        # 자리까지 맡아 두면 다른 요청도 함께 늦어진다.
        budget_left = remaining_seconds()
        claimed = cls._claim_shared(key, interval_seconds, max_wait=budget_left)
        if claimed is None:
            raise RemoteBudgetExceeded("공유 요청 간격 저장소에 접근하지 못해 상류 조회를 중단합니다.")
        if claimed is _NO_TIME:
            raise RemoteBudgetExceeded("요청 간격을 지키며 보낼 시간이 남지 않았습니다.")
        if claimed:
            sleeper(float(claimed))

    @classmethod
    def _claim_shared(cls, key: str, interval_seconds: float, *, max_wait: float | None = None) -> "float | object | None":
        """공유 파일에서 다음 허용 시각을 당겨온다.

        공유 저장소를 못 쓰면 None을 돌려 프로세스 안 잠금으로 넘긴다.
        남은 시간 안에 차례가 오지 않으면 자리를 잡지 않고 None을 돌린다.
        """
        from .paths import shared_pace_database

        try:
            path = shared_pace_database()
            path.parent.mkdir(parents=True, exist_ok=True)
            # 벽시계(time.time)를 쓴다. monotonic은 프로세스마다 기준이 달라
            # 공유가 안 된다. 시스템 시각이 뒤로 조정되면 그 순간 한 번 더
            # 보내게 되는데, 간격이 어긋나는 쪽이 조회가 멈추는 쪽보다 낫다.
            now = time.time()
            with closing(sqlite3.connect(path, timeout=5.0, isolation_level=None)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS upstream_pace(host TEXT PRIMARY KEY, next_allowed REAL NOT NULL)"
                )
                row = connection.execute(
                    "SELECT next_allowed FROM upstream_pace WHERE host = ?", (key,)
                ).fetchone()
                previous = float(row[0]) if row else now
                # 오래 쉰 뒤라면 밀린 시각을 그대로 쓰지 않는다.
                start = max(now, previous)
                if max_wait is not None and (start - now) > max_wait:
                    connection.execute("COMMIT")
                    return _NO_TIME
                connection.execute(
                    "INSERT INTO upstream_pace(host, next_allowed) VALUES(?, ?)"
                    " ON CONFLICT(host) DO UPDATE SET next_allowed = excluded.next_allowed",
                    (key, start + interval_seconds),
                )
                connection.execute("COMMIT")
            return max(0.0, start - now)
        except (sqlite3.Error, OSError, ValueError):
            return None


def _network_failure(
    exc: BaseException,
    *,
    attempts: int,
    secrets: tuple[str, ...],
    retry_stopped: str | None = None,
) -> TransportError:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, TimeoutError):
        stage = "timeout"
    elif isinstance(reason, ssl.SSLError):
        stage = "tls"
    elif isinstance(reason, socket.gaierror):
        stage = "dns"
    elif isinstance(reason, OSError):
        stage = "tcp"
    else:
        stage = "network"
    details: dict[str, object] = {
        "stage": stage,
        "exception_type": type(reason).__name__,
        "attempts": attempts,
    }
    if retry_stopped is not None:
        details["retry_stopped"] = retry_stopped
    message = redact_text(
        f"공식 출처 연결 실패({stage}): {type(reason).__name__}",
        secrets,
    )
    return TransportError(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        message,
        retryable=True,
        details=details,
    )


class HttpTransport:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...] = ("www.law.go.kr", "open.law.go.kr"),
        timeout_seconds: float = 20.0,
        min_interval_seconds: float = 1.0,
        # 시간초과·연결 실패·5xx만 1회 더 묻는다. 429·Retry-After·캠페인 회로는
        # 아래에서 그대로 즉시 멈춘다. 한 번의 일시 장애로 조사가 끊기지 않게 하되
        # 요청 증폭은 최대 2배로 묶는다.
        max_attempts: int = 2,
        max_response_bytes: int = 16 * 1024 * 1024,
        sleeper: Callable[[float], None] = time.sleep,
        opener=None,
    ):
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self.max_attempts = max(1, max_attempts)
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes는 1 이상이어야 합니다.")
        self.max_response_bytes = max_response_bytes
        self.sleeper = sleeper
        self.opener = opener or build_opener(_RestrictedRedirectHandler(self.allowed_hosts))

    def request(self, url: str, *, params: Mapping[str, str | int] | None = None, secrets: tuple[str, ...] = ()) -> HttpResponse:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in self.allowed_hosts or parsed.username or parsed.password:
            raise TransportError(ErrorCode.ACCESS_DENIED, "공식 HTTPS allowlist 밖의 URL은 요청할 수 없습니다.")
        # law.go.kr의 DRF 엔드포인트(lawSearch.do/lawService.do)는 문서가 실제로
        # 없을 때뿐 아니라 일시적인 상류 오류에도 404를 반환하는 사례가 관찰됐다
        # (chrisryugj/korean-law-mcp b9e0aad). 그 자리에서만 404를 "문서 없음"이
        # 아니라 "확인 안 됨"으로 다루고, DRF가 아닌 경로의 404는 그대로 둔다.
        is_drf_endpoint = host == "www.law.go.kr" and parsed.path in {"/DRF/lawSearch.do", "/DRF/lawService.do"}
        existing = list(parse_qsl(parsed.query, keep_blank_values=True))
        encoded_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(existing + [(key, str(value)) for key, value in (params or {}).items()]), parsed.fragment))
        # 법제처 DRF는 Referer가 없으면 유효한 OC에도 "사용자 정보 검증에 실패"를
        # 돌려주는 사례가 보고됐다(chrisryugj/korean-law-mcp v4.0.9, PR #45).
        request = Request(encoded_url, headers={"Accept": "application/json, application/xml;q=0.9", "User-Agent": "TAXax-legal-source/1.0", "Referer": "https://www.law.go.kr/"}, method="GET")
        context = _campaign_context()
        replay = _replay_response(
            context,
            method="GET",
            url=encoded_url,
            body=None,
            secrets=secrets,
        )
        if replay is not None:
            return replay
        last_retry_error: TransportError | None = None
        for attempt in range(1, self.max_attempts + 1):
            chain: _CampaignHttpChain | None = None
            reset_token = None
            try:
                consume_remote_request()
                SharedRateLimiter.wait(
                    host,
                    self.min_interval_seconds,
                    self.sleeper,
                )
                chain = _live_chain(
                    context,
                    source=_campaign_source(host),
                    sleeper=self.sleeper,
                )
                if chain is not None:
                    reset_token = _ACTIVE_CAMPAIGN_CHAIN.set(chain)
            except RemoteBudgetExceeded as exc:
                if last_retry_error is not None:
                    details = {
                        **last_retry_error.details,
                        "retry_stopped": "budget_exhausted",
                    }
                    raise TransportError(
                        last_retry_error.code,
                        str(last_retry_error),
                        retryable=last_retry_error.retryable,
                        status=last_retry_error.status,
                        details=details,
                    ) from None
                raise TransportError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    str(exc),
                    details={"stage": "budget", "attempts": attempt - 1},
                ) from None
            try:
                with self.opener.open(request, timeout=_bounded_timeout(self.timeout_seconds)) as response:
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    declared = headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > self.max_response_bytes:
                        _finish_campaign_chain(chain, status=response.status, error_stage="response_size")
                        chain = None
                        raise TransportError(ErrorCode.INCOMPLETE_RESULT, "공식 출처 원문이 허용 크기를 초과해 저장하지 않았습니다.", status=response.status, details={"stage": "response_size", "attempts": attempt})
                    body = response.read(self.max_response_bytes + 1)
                    if len(body) > self.max_response_bytes:
                        _finish_campaign_chain(chain, status=response.status, error_stage="response_size")
                        chain = None
                        raise TransportError(ErrorCode.INCOMPLETE_RESULT, "공식 출처 원문이 허용 크기를 초과해 저장하지 않았습니다.", status=response.status, details={"stage": "response_size", "attempts": attempt})
                    result = HttpResponse(
                        url=redact_url(response.geturl()),
                        status=response.status,
                        headers=headers,
                        body=body,
                        attempts=attempt,
                    )
                _finish_campaign_chain(
                    chain,
                    status=result.status,
                    response_body=result.body,
                    retry_after=result.headers.get("retry-after"),
                )
                chain = None
                if context is not None and contains_access_challenge(result.body):
                    raise TransportError(
                        ErrorCode.ACCESS_DENIED,
                        "공식 출처가 CAPTCHA 또는 인증 안내를 반환했습니다.",
                        status=result.status,
                        details={
                            "stage": "http",
                            "attempts": attempt,
                            "access_challenge": True,
                        },
                    )
                if result.status in {429} or result.status >= 500 or (is_drf_endpoint and result.status == 404):
                    error = self._drf_not_found(attempt) if is_drf_endpoint and result.status == 404 else self._status_error(result.status, secrets, attempts=attempt)
                    retry_after = self._retry_after_seconds(result.headers)
                    if retry_after is not None:
                        error.details["retry_after_seconds"] = retry_after
                    if context is not None and result.status != 404:
                        error.details["retry_stopped"] = "source_circuit_open"
                        raise error
                    if attempt < self.max_attempts and result.status != 429 and "retry-after" not in result.headers:
                        last_retry_error = error
                        self.sleeper(self._retry_delay(attempt))
                        continue
                    raise error
                if result.status >= 400:
                    raise self._status_error(result.status, secrets, attempts=attempt)
                return result
            except HTTPError as exc:
                status = exc.code
                headers = {key.lower(): value for key, value in exc.headers.items()}
                try:
                    error_body = exc.read(256 * 1024)
                except (AttributeError, OSError):
                    error_body = b""
                _finish_campaign_chain(
                    chain,
                    status=status,
                    response_body=error_body,
                    retry_after=headers.get("retry-after"),
                )
                chain = None
                error = self._drf_not_found(attempt) if is_drf_endpoint and status == 404 else self._status_error(status, secrets, attempts=attempt)
                retry_after = self._retry_after_seconds(headers)
                if retry_after is not None:
                    error.details["retry_after_seconds"] = retry_after
                if context is not None and contains_access_challenge(error_body):
                    error = TransportError(
                        ErrorCode.ACCESS_DENIED,
                        "공식 출처가 CAPTCHA 또는 인증 안내를 반환했습니다.",
                        status=status,
                        details={
                            "stage": "http",
                            "attempts": attempt,
                            "access_challenge": True,
                        },
                    )
                    raise error from None
                if context is not None and (status == 429 or status >= 500):
                    error.details["retry_stopped"] = "source_circuit_open"
                    raise error from None
                if (status >= 500 or (is_drf_endpoint and status == 404)) and attempt < self.max_attempts and "retry-after" not in headers:
                    last_retry_error = error
                    self.sleeper(self._retry_delay(attempt))
                    continue
                raise error from None
            except (TimeoutError, URLError, OSError) as exc:
                error = _network_failure(
                    exc,
                    attempts=attempt,
                    secrets=secrets,
                )
                _finish_campaign_chain(
                    chain,
                    error_stage=str(error.details.get("stage") or "network"),
                )
                chain = None
                if context is not None:
                    error.details["retry_stopped"] = "source_circuit_open"
                    raise error from None
                if attempt < self.max_attempts:
                    last_retry_error = error
                    self.sleeper(min(2 ** (attempt - 1), 8))
                    continue
                raise error from None
            except TransportError:
                if chain is not None:
                    chain.abandon()
                    chain = None
                raise
            finally:
                if reset_token is not None:
                    _ACTIVE_CAMPAIGN_CHAIN.reset(reset_token)
        raise AssertionError("unreachable")

    @staticmethod
    def _drf_not_found(attempts: int) -> TransportError:
        return TransportError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "법제처 DRF의 HTTP 404는 일시 오류와 문서 부재를 구별할 수 없어 확인이 필요합니다.",
            retryable=True,
            status=404,
            details={"stage": "http", "http_status": 404, "attempts": attempts},
        )

    @staticmethod
    def _retry_after_seconds(headers: Mapping[str, str]) -> int | None:
        value = headers.get("retry-after", "").strip()
        if value.isascii() and value.isdecimal():
            try:
                return int(value)
            except ValueError:
                return None
        if value:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0, int((date - datetime.now(timezone.utc)).total_seconds()) + 1)
            except (TypeError, ValueError, OverflowError):
                pass
        return None

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        return min(float(2 ** (attempt - 1)), 4.0)

    @staticmethod
    def _status_error(
        status: int,
        secrets: tuple[str, ...],
        *,
        attempts: int = 1,
    ) -> TransportError:
        details = {
            "stage": "http",
            "http_status": status,
            "attempts": attempts,
        }
        if status in {401, 403}:
            return TransportError(
                ErrorCode.AUTH_FAILED,
                redact_text(
                    "공식 API 인증 또는 접근이 거부되었습니다.",
                    secrets,
                ),
                status=status,
                details=details,
            )
        if status == 404:
            return TransportError(
                ErrorCode.NOT_FOUND,
                "공식 출처 문서를 찾지 못했습니다.",
                status=status,
                details=details,
            )
        if status == 429:
            return TransportError(
                ErrorCode.RATE_LIMITED,
                "공식 API 요청 한도에 도달했습니다.",
                retryable=True,
                status=status,
                details=details,
            )
        return TransportError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            f"공식 API가 HTTP {status}를 반환했습니다.",
            retryable=status >= 500,
            status=status,
            details=details,
        )


class SessionHttpTransport:
    def __init__(
        self,
        host: str,
        *,
        timeout_seconds: float = 20.0,
        min_interval_seconds: float = 1.0,
        max_response_bytes: int = 10 * 1024 * 1024,
        sleeper: Callable[[float], None] = time.sleep,
        opener=None,
    ):
        self.host = host.lower()
        self.allowed_hosts = frozenset({self.host})
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self.max_response_bytes = max_response_bytes
        self.sleeper = sleeper
        self._injected_opener = opener
        self.opener = opener or self._new_opener()

    def _new_opener(self):
        return build_opener(HTTPCookieProcessor(CookieJar()), _RestrictedRedirectHandler(self.allowed_hosts))

    def reset(self) -> None:
        if self._injected_opener is None:
            self.opener = self._new_opener()

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        # 국세청·OLTA도 시간초과·연결 실패·5xx는 1회만 다시 묻는다(검색·상세 모두 읽기 전용).
        # 캠페인 회로가 연 오류(retry_stopped)와 429·접근 차단은 그대로 올린다.
        try:
            return self._request_once(url, method=method, form=form, headers=headers, max_bytes=max_bytes)
        except TransportError as exc:
            transient = exc.details.get("stage") in {"timeout", "tcp", "network"} or (exc.status or 0) >= 500
            if not transient or "retry_stopped" in exc.details or exc.code == ErrorCode.BUDGET_EXHAUSTED:
                raise
            self.sleeper(1.0)
            return self._request_once(url, method=method, form=form, headers=headers, max_bytes=max_bytes)

    def _request_once(
        self,
        url: str,
        *,
        method: str = "GET",
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != self.host or parsed.username or parsed.password:
            raise TransportError(ErrorCode.ACCESS_DENIED, "공개 출처 HTTPS allowlist 밖의 URL은 요청할 수 없습니다.")
        upper_method = method.upper()
        if upper_method not in {"GET", "POST"}:
            raise TransportError(ErrorCode.INVALID_REQUEST, "공개 출처 transport는 GET과 POST만 허용합니다.")
        body = urlencode(form).encode("utf-8") if form is not None else None
        request_headers = {
            "Accept": "application/json, text/html;q=0.9, application/pdf;q=0.8",
            "User-Agent": "TAXax-public-legal-source/1.0",
            **dict(headers or {}),
        }
        if form is not None:
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        request = Request(url, data=body, headers=request_headers, method=upper_method)
        context = _campaign_context()
        replay = _replay_response(
            context,
            method=upper_method,
            url=url,
            body=body,
            secrets=(),
        )
        if replay is not None:
            return replay
        chain: _CampaignHttpChain | None = None
        reset_token = None
        try:
            consume_remote_request()
            SharedRateLimiter.wait(
                self.host,
                self.min_interval_seconds,
                self.sleeper,
            )
            chain = _live_chain(
                context,
                source=_campaign_source(self.host),
                sleeper=self.sleeper,
            )
            if chain is not None:
                reset_token = _ACTIVE_CAMPAIGN_CHAIN.set(chain)
        except RemoteBudgetExceeded as exc:
            raise TransportError(
                ErrorCode.BUDGET_EXHAUSTED,
                str(exc),
                details={"stage": "budget", "attempts": 0},
            ) from None
        limit = max_bytes if max_bytes is not None else self.max_response_bytes
        try:
            with self.opener.open(request, timeout=_bounded_timeout(self.timeout_seconds)) as response:
                final_url = response.geturl()
                final = urlparse(final_url)
                if final.scheme != "https" or (final.hostname or "").lower() != self.host or final.username or final.password:
                    raise TransportError(ErrorCode.ACCESS_DENIED, "허용되지 않은 redirect 대상입니다.")
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise TransportError(ErrorCode.ACCESS_DENIED, "공개 출처 응답이 허용 크기를 초과합니다.")
                payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise TransportError(ErrorCode.ACCESS_DENIED, "공개 출처 응답이 허용 크기를 초과합니다.")
                result = HttpResponse(
                    url=redact_url(final_url),
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=payload,
                )
            _finish_campaign_chain(
                chain,
                status=result.status,
                response_body=result.body,
                retry_after=result.headers.get("retry-after"),
            )
            chain = None
            if context is not None and contains_access_challenge(result.body):
                raise TransportError(
                    ErrorCode.ACCESS_DENIED,
                    "공개 출처가 CAPTCHA 또는 인증 안내를 반환했습니다.",
                    status=result.status,
                    details={"stage": "http", "attempts": 1, "access_challenge": True},
                )
            if result.status >= 400:
                error = HttpTransport._status_error(result.status, (), attempts=1)
                if context is not None and (result.status == 429 or result.status >= 500):
                    error.details["retry_stopped"] = "source_circuit_open"
                raise error
            return result
        except HTTPError as exc:
            headers_value = {key.lower(): value for key, value in exc.headers.items()}
            try:
                error_body = exc.read(256 * 1024)
            except (AttributeError, OSError):
                error_body = b""
            _finish_campaign_chain(
                chain,
                status=exc.code,
                response_body=error_body,
                retry_after=headers_value.get("retry-after"),
            )
            chain = None
            if context is not None and contains_access_challenge(error_body):
                raise TransportError(
                    ErrorCode.ACCESS_DENIED,
                    "공개 출처가 CAPTCHA 또는 인증 안내를 반환했습니다.",
                    status=exc.code,
                    details={"stage": "http", "attempts": 1, "access_challenge": True},
                ) from None
            error = HttpTransport._status_error(exc.code, (), attempts=1)
            if context is not None and (exc.code == 429 or exc.code >= 500):
                error.details["retry_stopped"] = "source_circuit_open"
            raise error from None
        except TransportError:
            if chain is not None:
                chain.abandon()
                chain = None
            raise
        except (TimeoutError, URLError, OSError) as exc:
            error = _network_failure(
                exc,
                attempts=1,
                secrets=(),
            )
            _finish_campaign_chain(
                chain,
                error_stage=str(error.details.get("stage") or "network"),
            )
            chain = None
            if context is not None:
                error.details["retry_stopped"] = "source_circuit_open"
            raise error from None
        finally:
            if reset_token is not None:
                _ACTIVE_CAMPAIGN_CHAIN.reset(reset_token)


def _bounded_timeout(default: float) -> float:
    """이번 attempt에 쓸 소켓 timeout. 활성 예산이 더 짧으면 그쪽을 따른다.

    consume_remote_request()의 check_time()은 attempt를 "시작하기 전"에만
    검사한다. 그 검사를 통과해 소켓 read가 실제로 시작되면, 남은 예산이
    1초든 5초든 상관없이 기본 timeout(law.go.kr 30초, NTS/OLTA 20초) 전체를
    기다릴 수 있다 — 그래서 짧게 잡은 max_seconds가 실제로는 지켜지지 않는다.
    """
    remaining = remaining_seconds()
    if remaining is None:
        return default
    # 예산이 이미 바닥났다면(<=0) check_time()이 이 attempt를 시작하기 전에
    # 막았어야 한다. 그래도 0/음수 timeout으로 소켓을 열지 않도록 바닥을 둔다.
    return max(0.5, min(default, remaining))


def response_media_type(response: HttpResponse) -> str:
    return response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()


def body_contains_secret(body: bytes, secrets: tuple[str, ...]) -> bool:
    return any(secret and secret.encode("utf-8") in body for secret in secrets)


def decode_body(response: HttpResponse) -> str:
    content_type = response.headers.get("content-type", "")
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8", "euc-kr"])
    for encoding in encodings:
        try:
            return response.body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return response.body.decode("utf-8", errors="replace")


def normalized_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
