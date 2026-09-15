from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from email.message import Message
from http.cookiejar import CookieJar
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from .budget import RemoteBudgetExceeded, consume_remote_request, remaining_seconds
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
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status


class _RestrictedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req: Request, fp, code: int, msg: str, headers: Message, newurl: str):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in self.allowed_hosts:
            raise TransportError(ErrorCode.ACCESS_DENIED, "허용되지 않은 redirect 대상입니다.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SharedRateLimiter:
    _lock = threading.Lock()
    _next_allowed: dict[str, float] = {}

    @classmethod
    def wait(cls, key: str, interval_seconds: float, sleeper: Callable[[float], None]) -> None:
        if interval_seconds <= 0:
            return
        with cls._lock:
            now = time.monotonic()
            wait = max(0.0, cls._next_allowed.get(key, now) - now)
            cls._next_allowed[key] = max(now, cls._next_allowed.get(key, now)) + interval_seconds
        if wait:
            sleeper(wait)


class HttpTransport:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...] = ("www.law.go.kr", "open.law.go.kr"),
        timeout_seconds: float = 30.0,
        min_interval_seconds: float = 1.0,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        opener=None,
    ):
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self.max_attempts = max(1, max_attempts)
        self.sleeper = sleeper
        self.opener = opener or build_opener(_RestrictedRedirectHandler(self.allowed_hosts))

    def request(self, url: str, *, params: Mapping[str, str | int] | None = None, secrets: tuple[str, ...] = ()) -> HttpResponse:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in self.allowed_hosts or parsed.username or parsed.password:
            raise TransportError(ErrorCode.ACCESS_DENIED, "공식 HTTPS allowlist 밖의 URL은 요청할 수 없습니다.")
        existing = list(parse_qsl(parsed.query, keep_blank_values=True))
        encoded_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(existing + [(key, str(value)) for key, value in (params or {}).items()]), parsed.fragment))
        request = Request(encoded_url, headers={"Accept": "application/json, application/xml;q=0.9", "User-Agent": "TAXax-legal-source/1.0"}, method="GET")
        for attempt in range(1, self.max_attempts + 1):
            try:
                consume_remote_request()
            except RemoteBudgetExceeded as exc:
                raise TransportError(ErrorCode.BUDGET_EXHAUSTED, str(exc)) from None
            SharedRateLimiter.wait(host, self.min_interval_seconds, self.sleeper)
            try:
                with self.opener.open(request, timeout=_bounded_timeout(self.timeout_seconds)) as response:
                    body = response.read()
                    result = HttpResponse(
                        url=redact_url(response.geturl()),
                        status=response.status,
                        headers={key.lower(): value for key, value in response.headers.items()},
                        body=body,
                        attempts=attempt,
                    )
                if result.status in {429} or result.status >= 500:
                    if attempt < self.max_attempts:
                        self.sleeper(self._retry_delay(result.headers, attempt))
                        continue
                    raise self._status_error(result.status, secrets)
                if result.status >= 400:
                    raise self._status_error(result.status, secrets)
                return result
            except HTTPError as exc:
                status = exc.code
                headers = {key.lower(): value for key, value in exc.headers.items()}
                if (status == 429 or status >= 500) and attempt < self.max_attempts:
                    self.sleeper(self._retry_delay(headers, attempt))
                    continue
                raise self._status_error(status, secrets) from None
            except (TimeoutError, URLError, OSError) as exc:
                if attempt < self.max_attempts:
                    self.sleeper(min(2 ** (attempt - 1), 8))
                    continue
                message = redact_text(f"공식 출처 연결 실패: {type(exc).__name__}", secrets)
                raise TransportError(ErrorCode.UPSTREAM_UNAVAILABLE, message, retryable=True) from None
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_delay(headers: Mapping[str, str], attempt: int) -> float:
        retry_after = headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 60.0)
        return min(float(2 ** (attempt - 1)), 8.0)

    @staticmethod
    def _status_error(status: int, secrets: tuple[str, ...]) -> TransportError:
        if status in {401, 403}:
            return TransportError(ErrorCode.AUTH_FAILED, redact_text("공식 API 인증 또는 접근이 거부되었습니다.", secrets), status=status)
        if status == 404:
            return TransportError(ErrorCode.NOT_FOUND, "공식 출처 문서를 찾지 못했습니다.", status=status)
        if status == 429:
            return TransportError(ErrorCode.RATE_LIMITED, "공식 API 요청 한도에 도달했습니다.", retryable=True, status=status)
        return TransportError(ErrorCode.UPSTREAM_UNAVAILABLE, f"공식 API가 HTTP {status}를 반환했습니다.", retryable=status >= 500, status=status)


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
        try:
            consume_remote_request()
        except RemoteBudgetExceeded as exc:
            raise TransportError(ErrorCode.BUDGET_EXHAUSTED, str(exc)) from None
        SharedRateLimiter.wait(self.host, self.min_interval_seconds, self.sleeper)
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
                return HttpResponse(
                    url=redact_url(final_url),
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=payload,
                )
        except HTTPError as exc:
            raise HttpTransport._status_error(exc.code, ()) from None
        except TransportError:
            raise
        except (TimeoutError, URLError, OSError) as exc:
            raise TransportError(ErrorCode.UPSTREAM_UNAVAILABLE, f"공개 출처 연결 실패: {type(exc).__name__}", retryable=True) from None


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
