from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import ResearchReport

SCHEMA_VERSION = 1


def _scope_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 512 or "\x00" in normalized:
        raise ValueError("report scope 식별자가 올바르지 않습니다.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ResearchReportRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS research_reports (
                    report_id TEXT PRIMARY KEY,
                    principal_key TEXT NOT NULL,
                    org_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    issue_hash TEXT NOT NULL,
                    report_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS research_report_scope_idx
                    ON research_reports(principal_key, org_key, updated_at);
                """
            )
            connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            connection.commit()

    def save(self, report: ResearchReport, *, principal_id: str, org_id: str) -> None:
        principal_key = _scope_key(principal_id)
        org_key = _scope_key(org_id)
        issue_hash = hashlib.sha256(report.issue.encode("utf-8")).hexdigest()
        payload = report.model_dump_json()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT principal_key, org_key FROM research_reports WHERE report_id=?",
                (report.report_id,),
            ).fetchone()
            if existing and (existing["principal_key"] != principal_key or existing["org_key"] != org_key):
                raise PermissionError("report scope를 변경할 수 없습니다.")
            connection.execute(
                """INSERT INTO research_reports(
                       report_id, principal_key, org_key, created_at, updated_at, issue_hash, report_json
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(report_id) DO UPDATE SET
                       updated_at=excluded.updated_at,
                       issue_hash=excluded.issue_hash,
                       report_json=excluded.report_json""",
                (report.report_id, principal_key, org_key, report.created_at, report.updated_at, issue_hash, payload),
            )
            connection.commit()

    def get(self, report_id: str, *, principal_id: str, org_id: str) -> ResearchReport | None:
        principal_key = _scope_key(principal_id)
        org_key = _scope_key(org_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT report_json FROM research_reports WHERE report_id=? AND principal_key=? AND org_key=?",
                (report_id, principal_key, org_key),
            ).fetchone()
        return ResearchReport.model_validate_json(row["report_json"]) if row else None

    def status(self) -> dict[str, int]:
        with self.connection() as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM research_reports").fetchone()["count"]
        return {"reports": int(count), "database_schema_version": SCHEMA_VERSION}
