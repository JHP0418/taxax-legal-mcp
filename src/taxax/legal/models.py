from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "taxax.legal.v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResponseStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    ERROR = "error"


class ErrorCode(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    PARSE_ERROR = "PARSE_ERROR"
    NOT_FOUND = "NOT_FOUND"
    INCOMPLETE_RESULT = "INCOMPLETE_RESULT"
    TEMPORAL_UNRESOLVED = "TEMPORAL_UNRESOLVED"
    ACCESS_DENIED = "ACCESS_DENIED"
    POLICY_DISABLED = "POLICY_DISABLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INVALID_REQUEST = "INVALID_REQUEST"


class RetrievalStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"


class ContentCompleteness(str, Enum):
    COMPLETE = "complete"
    METADATA_ONLY = "metadata_only"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNKNOWN = "unknown"


class TemporalStatus(str, Enum):
    CURRENT = "current"
    HISTORICAL = "historical"
    CANDIDATE = "candidate"
    UNRESOLVED = "unresolved"


class CitationStatus(str, Enum):
    VERIFIED = "verified"
    DOCUMENT_ONLY = "document_only"
    MISMATCH = "mismatch"
    UNVERIFIED = "unverified"


class ReviewState(str, Enum):
    UNREVIEWED = "unreviewed"
    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"


class TextSection(StrictModel):
    section_id: str
    kind: str
    heading: str | None = None
    locator: str | None = None
    text: str
    derived: bool = False
    source_field: str | None = None


class SourceSnapshot(StrictModel):
    schema_version: str = SCHEMA_VERSION
    snapshot_id: str
    provider: str
    source_document_id: str
    raw_sha256: str
    normalized_sha256: str | None = None
    snapshot_ref: str
    parser_version: str
    retrieved_at: str
    retrieval_status: RetrievalStatus
    content_completeness: ContentCompleteness
    run_id: str
    media_type: str | None = None
    byte_length: int
    redacted_secrets: bool = False


class LegalDocument(StrictModel):
    schema_version: str = SCHEMA_VERSION
    provider: str
    document_type: str
    source_document_id: str
    document_id: str
    version_id: str | None = None
    title: str
    issuer: str | None = None
    court: str | None = None
    case_no: str | None = None
    document_number: str | None = None
    tax_type: str | None = None
    jurisdiction: str = "KR"
    promulgated_on: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    decided_on: str | None = None
    interpreted_on: str | None = None
    registered_at: str | None = None
    retrieved_at: str
    official_url: str | None = None
    version_url: str | None = None
    raw_sha256: str
    normalized_sha256: str | None = None
    snapshot_ref: str
    parser_version: str
    collection_run_id: str
    retrieval_status: RetrievalStatus = RetrievalStatus.SUCCESS
    content_completeness: ContentCompleteness = ContentCompleteness.UNKNOWN
    temporal_status: TemporalStatus = TemporalStatus.UNRESOLVED
    citation_status: CitationStatus = CitationStatus.UNVERIFIED
    review_state: ReviewState = ReviewState.UNREVIEWED
    sections: list[TextSection] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CitationInput(StrictModel):
    document_id: str
    version_id: str | None = None
    quote: str | None = None
    locator: str | None = None
    expected_provider: str | None = None
    expected_date: str | None = None


class CitationCheck(StrictModel):
    document_id: str
    version_id: str | None = None
    status: CitationStatus
    document_exists: bool
    metadata_matches: bool | None = None
    quote_matches: bool | None = None
    locator_matches: bool | None = None
    matched_section_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ResearchRoute(StrictModel):
    tax_type: str | None = None
    jurisdiction: str = "KR"
    tax_scope: Literal["national", "local", "mixed", "unknown"] = "unknown"
    matched_issue: str | None = None
    issue_terms: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    official_targets: list[str] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)


class EvidenceScore(StrictModel):
    document_id: str
    score: int = Field(ge=0, le=100)
    dimensions: dict[str, int] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    selected_for_detail: bool = False


class ResearchBudget(StrictModel):
    max_remote_requests: int = Field(ge=0)
    consumed_remote_requests: int = Field(ge=0)
    max_detail_documents: int = Field(ge=0)
    consumed_detail_documents: int = Field(ge=0)
    max_seconds: float = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    exhausted: bool = False


class ResearchReport(StrictModel):
    schema_version: str = SCHEMA_VERSION
    report_id: str
    status: Literal["draft", "partial", "needs_review"] = "needs_review"
    issue: str
    created_at: str
    updated_at: str
    transaction_date: str | None = None
    tax_period: str | None = None
    research_as_of: str | None = None
    knowledge_cutoff: str | None = None
    route: ResearchRoute | None = None
    research_plan: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    candidate_documents: list[str] = Field(default_factory=list)
    evidence_ranking: list[EvidenceScore] = Field(default_factory=list)
    citation_checks: list[CitationCheck] = Field(default_factory=list)
    temporal_review: dict[str, Any] = Field(default_factory=dict)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    provider_errors: list[dict[str, Any]] = Field(default_factory=list)
    unresearched_scope: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    budget: ResearchBudget | None = None
    legal_conclusion_confirmed: Literal[False] = False


class SourceReference(StrictModel):
    provider: str
    document_id: str | None = None
    version_id: str | None = None
    title: str | None = None
    document_number: str | None = None
    official_url: str | None = None
    version_url: str | None = None
    retrieved_at: str | None = None
    raw_sha256: str | None = None
    snapshot_raw_sha256: str | None = None
    snapshot_ref: str | None = None
    section_locator: str | None = None


class Coverage(StrictModel):
    queried_providers: list[str] = Field(default_factory=list)
    succeeded_providers: list[str] = Field(default_factory=list)
    failed_providers: list[str] = Field(default_factory=list)
    disabled_providers: list[str] = Field(default_factory=list)
    upstream_total: int | None = None
    fetched: int = 0
    filtered: int = 0
    date_filter_location: Literal["upstream", "client", "none"] = "none"
    truncated: bool = False
    next_cursor: str | None = None


class Pagination(StrictModel):
    page: int = 1
    page_size: int = 0
    total: int | None = None
    next_cursor: str | None = None
    truncated: bool = False


class ToolError(StrictModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class LegalToolResponse(StrictModel):
    schema_version: str = SCHEMA_VERSION
    request_id: str
    status: ResponseStatus
    data: Any = None
    sources: list[SourceReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    pagination: Pagination | None = None
    coverage: Coverage = Field(default_factory=Coverage)
    freshness: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
