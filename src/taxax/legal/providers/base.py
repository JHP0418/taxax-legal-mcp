from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol

from ..models import ErrorCode
from ..transport import HttpResponse


class LegalTarget(str, Enum):
    LAW = "law"
    LAW_HISTORY = "lsHistory"
    EFFECTIVE_LAW = "eflaw"
    PRECEDENT = "prec"
    TAX_TRIBUNAL = "ttSpecialDecc"
    NTS_INTERPRETATION = "ntsCgmExpc"
    ADMIN_RULE = "admrul"
    LAW_ATTACHMENT = "licbyl"


@dataclass(frozen=True)
class TargetContract:
    target: LegalTarget
    document_type: str
    search_supported: bool
    detail_supported: bool
    allowed_search_filters: frozenset[str] = frozenset()
    date_filter: str | None = None
    detail_identifiers: frozenset[str] = frozenset({"ID"})
    notes: tuple[str, ...] = ()


class ProviderError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})


@dataclass(frozen=True)
class ProviderResult:
    target: LegalTarget
    request_parameters: dict[str, Any]
    response: HttpResponse
    parsed: Any
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    page: int = 1
    page_size: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SupplementalResult:
    provider: str
    document_type: str
    request_parameters: dict[str, Any]
    response: HttpResponse
    parsed: Any
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    page: int = 1
    page_size: int = 0
    warnings: tuple[str, ...] = ()
    partial: bool = False
    date_filter_location: str = "none"
    raw_responses: tuple[HttpResponse, ...] = ()


class LegalSourceProvider(Protocol):
    name: str

    def search(
        self,
        target: LegalTarget,
        *,
        query: str | None = None,
        page: int = 1,
        display: int = 10,
        filters: Mapping[str, str] | None = None,
        response_type: str = "JSON",
    ) -> ProviderResult: ...

    def detail(
        self,
        target: LegalTarget,
        *,
        identifier: str,
        identifier_kind: str = "ID",
        effective_on: str | None = None,
        article: str | None = None,
        response_type: str = "JSON",
    ) -> ProviderResult: ...
