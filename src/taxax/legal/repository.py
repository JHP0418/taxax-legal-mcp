from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .models import LegalDocument, SourceSnapshot
from .snapshots import new_run_id

SCHEMA_VERSION = 2

# start_run()이 같은 요청(provider+action+fingerprint)을 다시 시작하지 못하게
# 막는 이유는 진짜 동시 실행 중인 작업과의 중복을 막기 위해서다. 그런데 그
# 작업을 하던 프로세스가 응답 없이 죽거나(강제종료, 크래시, 아직 못 잡은 예외
# 타입) MCP client가 먼저 포기해 버리면, 그 소유자는 영영 finish_run을 부르지
# 못해 행은 'running'에 그대로 멈춘다. 그러면 같은 요청은 재시도해도 매번
# UPSTREAM_UNAVAILABLE("같은 요청이 이미 실행 중")만 돌려받고 영원히 못 벗어난다.
# HTTP 계층 자체가 재시도까지 포함해 최대 ~90초 안에는 반드시 끝나므로, 그보다
# 훨씬 긴 시간(10분) 동안 'running'인 행은 죽은 소유자의 것으로 보고 새로
# 재시도할 수 있게 한다. 이 임계값은 진짜 동시 실행 중인 다른 프로세스의 작업을
# 가로챌 만큼 짧지는 않다(우리는 Claude Desktop·Code·Codex가 같은 DB를 공유하는
# 여러 프로세스로 동시에 뜰 수 있는 구조를 실제로 쓰고 있다).
_STALE_RUN_SECONDS = 600.0


def _seconds_since(started_at: str, reference: str) -> float:
    try:
        started = datetime.fromisoformat(started_at)
        now = datetime.fromisoformat(reference)
    except ValueError:
        return 0.0
    return (now - started).total_seconds()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class LegalRepository:
    def __init__(self, database_path: Path, *, clock: Callable[[], str] = _utc_now):
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_runs (
                    run_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    action TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','partial','failed')),
                    resume_cursor TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    UNIQUE(provider, action, request_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS source_snapshots (
                    snapshot_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_document_id TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    normalized_sha256 TEXT,
                    snapshot_ref TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    retrieval_status TEXT NOT NULL,
                    content_completeness TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
                    media_type TEXT,
                    byte_length INTEGER NOT NULL,
                    PRIMARY KEY(provider, source_document_id, raw_sha256)
                );
                CREATE TABLE IF NOT EXISTS legal_documents (
                    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    document_type TEXT NOT NULL,
                    source_document_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    version_id TEXT NOT NULL DEFAULT '',
                    raw_sha256 TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    searchable_text TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    UNIQUE(document_id, version_id, raw_sha256, parser_version)
                );
                CREATE INDEX IF NOT EXISTS legal_documents_search_idx
                    ON legal_documents(provider, document_type, source_document_id, retrieved_at);
                CREATE TABLE IF NOT EXISTS run_documents (
                    run_id TEXT NOT NULL REFERENCES collection_runs(run_id),
                    document_id TEXT NOT NULL,
                    version_id TEXT NOT NULL DEFAULT '',
                    raw_sha256 TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    PRIMARY KEY(run_id, document_id, version_id, raw_sha256, parser_version)
                );
                CREATE TABLE IF NOT EXISTS collection_results (
                    run_id TEXT PRIMARY KEY REFERENCES collection_runs(run_id),
                    result_json TEXT NOT NULL
                );
                """
            )
            connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            connection.commit()
        self._recover_stale_running_runs()

    def _recover_stale_running_runs(self) -> None:
        """프로세스가 새로 뜰 때마다, 이 프로세스가 시작하기 전부터 이미
        'running'이던 행은 이 프로세스가 만든 것이 아니므로 주인이 없다. 오래
        방치된 행을 그대로 두면 get_source_status가 실제로는 아무 일도 하지
        않는 유령 작업을 계속 "실행 중"이라고 보여줘 혼란을 준다(운영자가
        전체 조사 기능이 멈췄다고 오인하게 만든 실제 사례가 있었다). 판단
        기준은 start_run의 재시도 허용 임계값과 반드시 같아야 앞뒤가 맞는다 -
        같은 동시성 프로세스가 아직 일하고 있을 만한 시간 안에서는 건드리지
        않는다.
        """
        now = self._clock()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale = [
                row["run_id"]
                for row in connection.execute("SELECT run_id, started_at FROM collection_runs WHERE status='running'")
                if _seconds_since(row["started_at"], now) > _STALE_RUN_SECONDS
            ]
            for run_id in stale:
                connection.execute(
                    "UPDATE collection_runs SET status='failed', finished_at=?, error_code='INTERRUPTED', error_message=? WHERE run_id=?",
                    (now, "이전 프로세스가 정상 종료되지 못해 중단된 것으로 보이는 수집 작업입니다.", run_id),
                )
            connection.commit()

    @staticmethod
    def fingerprint(request: dict[str, Any]) -> str:
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def start_run(
        self,
        *,
        provider: str,
        action: str,
        request: dict[str, Any],
        started_at: str,
        run_id: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        fingerprint = self.fingerprint(request)
        candidate = run_id or new_run_id()
        request_json = json.dumps(request, ensure_ascii=False, sort_keys=True)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO collection_runs(run_id, provider, action, request_fingerprint, request_json, status, started_at) VALUES(?,?,?,?,?,'running',?)",
                (candidate, provider, action, fingerprint, request_json, started_at),
            )
            inserted = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE provider=? AND action=? AND request_fingerprint=?",
                (provider, action, fingerprint),
            ).fetchone()
            stale_running = (
                row is not None
                and row["status"] == "running"
                and not inserted
                and _seconds_since(row["started_at"], started_at) > _STALE_RUN_SECONDS
            )
            resumable = row is not None and (row["status"] in {"failed", "partial"} or stale_running)
            explicit_running_resume = row is not None and run_id is not None and row["run_id"] == run_id and row["status"] == "running"
            if row is not None and not inserted and resume and (resumable or explicit_running_resume):
                connection.execute(
                    "UPDATE collection_runs SET status='running', finished_at=NULL, error_code=NULL, error_message=NULL WHERE run_id=?",
                    (row["run_id"],),
                )
                row = connection.execute("SELECT * FROM collection_runs WHERE run_id=?", (row["run_id"],)).fetchone()
                inserted = True
            connection.commit()
        if row is None:
            raise RuntimeError("수집 run을 시작하지 못했습니다.")
        result = dict(row)
        result["claimed"] = inserted
        return result

    def update_cursor(self, run_id: str, cursor: str | None) -> None:
        with self.connection() as connection:
            connection.execute("UPDATE collection_runs SET resume_cursor=? WHERE run_id=? AND status='running'", (cursor, run_id))
            connection.commit()

    def finish_run(self, run_id: str, *, status: str, finished_at: str, error_code: str | None = None, error_message: str | None = None) -> None:
        if status not in {"completed", "partial", "failed"}:
            raise ValueError("지원하지 않는 run 완료 상태입니다.")
        with self.connection() as connection:
            connection.execute(
                "UPDATE collection_runs SET status=?, finished_at=?, error_code=?, error_message=? WHERE run_id=?",
                (status, finished_at, error_code, error_message, run_id),
            )
            connection.commit()

    def save_result(self, snapshot: SourceSnapshot, documents: Sequence[LegalDocument]) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO source_snapshots(
                    snapshot_id, provider, source_document_id, raw_sha256, normalized_sha256,
                    snapshot_ref, parser_version, retrieved_at, retrieval_status,
                    content_completeness, run_id, media_type, byte_length
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot.snapshot_id, snapshot.provider, snapshot.source_document_id,
                    snapshot.raw_sha256, snapshot.normalized_sha256, snapshot.snapshot_ref,
                    snapshot.parser_version, snapshot.retrieved_at, snapshot.retrieval_status.value,
                    snapshot.content_completeness.value, snapshot.run_id, snapshot.media_type,
                    snapshot.byte_length,
                ),
            )
            for document in documents:
                payload = document.model_dump(mode="json")
                searchable = "\n".join([document.title, document.case_no or "", *(section.text for section in document.sections)])
                connection.execute(
                    """INSERT OR IGNORE INTO legal_documents(
                        provider, document_type, source_document_id, document_id, version_id,
                        raw_sha256, parser_version, retrieved_at, title, searchable_text, document_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        document.provider, document.document_type, document.source_document_id,
                        document.document_id, document.version_id or "", document.raw_sha256,
                        document.parser_version, document.retrieved_at, document.title,
                        searchable, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO run_documents(
                        run_id, document_id, version_id, raw_sha256, parser_version
                    ) VALUES(?,?,?,?,?)""",
                    (
                        snapshot.run_id, document.document_id, document.version_id or "",
                        document.raw_sha256, document.parser_version,
                    ),
                )
            connection.commit()

    def get_document(self, document_id: str, *, version_id: str | None = None) -> LegalDocument | None:
        query = "SELECT document_json FROM legal_documents WHERE document_id=?"
        params: list[Any] = [document_id]
        if version_id is not None:
            query += " AND version_id=?"
            params.append(version_id)
        query += " ORDER BY retrieved_at DESC, record_id DESC LIMIT 1"
        with self.connection() as connection:
            row = connection.execute(query, params).fetchone()
        return LegalDocument.model_validate_json(row["document_json"]) if row else None

    def search_documents(
        self,
        query: str,
        *,
        provider: str | None = None,
        document_type: str | None = None,
        jurisdiction: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[LegalDocument], int]:
        if not 1 <= limit <= 50 or offset < 0:
            raise ValueError("limit는 1~50, offset은 0 이상이어야 합니다.")
        clauses = ["(title LIKE ? ESCAPE '\\' OR searchable_text LIKE ? ESCAPE '\\')"]
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: list[Any] = [f"%{escaped}%", f"%{escaped}%"]
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if document_type:
            clauses.append("document_type=?")
            params.append(document_type)
        if jurisdiction:
            clauses.append("json_extract(document_json, '$.jurisdiction')=?")
            params.append(jurisdiction)
        where = " AND ".join(clauses)
        with self.connection() as connection:
            total = connection.execute(f"SELECT COUNT(*) AS count FROM legal_documents WHERE {where}", params).fetchone()["count"]
            rows = connection.execute(
                f"SELECT document_json FROM legal_documents WHERE {where} ORDER BY retrieved_at DESC, record_id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [LegalDocument.model_validate_json(row["document_json"]) for row in rows], int(total)

    def save_collection_result(self, run_id: str, result: dict[str, Any]) -> None:
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO collection_results(run_id, result_json) VALUES(?,?)",
                (run_id, payload),
            )
            connection.commit()

    def collection_result(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT result_json FROM collection_results WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def documents_for_run(self, run_id: str) -> list[LegalDocument]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT d.document_json
                   FROM run_documents r
                   JOIN legal_documents d
                     ON d.document_id=r.document_id
                    AND d.version_id=r.version_id
                    AND d.raw_sha256=r.raw_sha256
                    AND d.parser_version=r.parser_version
                   WHERE r.run_id=?
                   ORDER BY d.record_id""",
                (run_id,),
            ).fetchall()
        return [LegalDocument.model_validate_json(row["document_json"]) for row in rows]

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM collection_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def source_status(self) -> dict[str, Any]:
        with self.connection() as connection:
            runs = [dict(row) for row in connection.execute("SELECT provider, status, COUNT(*) AS count FROM collection_runs GROUP BY provider, status")]
            snapshots = connection.execute("SELECT COUNT(*) AS count FROM source_snapshots").fetchone()["count"]
            documents = connection.execute("SELECT COUNT(*) AS count FROM legal_documents").fetchone()["count"]
        return {"runs": runs, "snapshots": int(snapshots), "documents": int(documents), "database_schema_version": SCHEMA_VERSION}
