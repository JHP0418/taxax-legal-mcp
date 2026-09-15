from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .models import ContentCompleteness, RetrievalStatus, SourceSnapshot
from .transport import HttpResponse, body_contains_secret, normalized_json_bytes, redact_body, response_media_type

SNAPSHOT_SCHEMA = "taxax.legal-source-snapshot.v1"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if not slug or slug in {".", ".."}:
        raise ValueError("안전하지 않은 snapshot 식별자입니다.")
    return slug[:160]


def _extension(media_type: str) -> str:
    if "json" in media_type:
        return ".json"
    if "xml" in media_type:
        return ".xml"
    if "html" in media_type:
        return ".html"
    if "pdf" in media_type:
        return ".pdf"
    return ".bin"


def _native_path(path: Path) -> str:
    value = str(path.resolve())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def write_once(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    native_temporary = _native_path(temporary)
    native_path = _native_path(path)
    descriptor = os.open(native_temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(native_temporary, native_path)
            return True
        except FileExistsError:
            with open(native_path, "rb") as existing:
                if existing.read() != data:
                    raise RuntimeError(f"불변 경로 충돌: {path.name}")
            return False
    finally:
        try:
            os.unlink(native_temporary)
        except FileNotFoundError:
            pass


class SnapshotStore:
    def __init__(self, data_dir: Path, *, parser_version: str):
        self.data_dir = data_dir.resolve()
        self.raw_root = self.data_dir / "v1" / "raw"
        self.extracted_root = self.data_dir / "v1" / "extracted"
        self.run_root = self.data_dir / "v1" / "runs"
        self.parser_version = safe_slug(parser_version)

    def save(
        self,
        *,
        provider: str,
        document_type: str,
        source_document_id: str,
        response: HttpResponse,
        parsed: Any,
        retrieved_at: str,
        run_id: str,
        completeness: ContentCompleteness,
        secrets: tuple[str, ...] = (),
        parser_version: str | None = None,
    ) -> SourceSnapshot:
        # 정상 응답도 우리가 보낸 요청값(예: law.go.kr 검색 결과의 법령상세링크에
        # 담긴 OC)을 그대로 되돌려주는 경우가 있어, 무조건 차단하면 해당 provider의
        # 모든 요청이 막힌다. 저장 전 credential 문자열을 치환해 원문에서 지운다.
        body, redacted = redact_body(response.body, secrets)
        if body_contains_secret(body, secrets):
            raise ValueError("인증값이 반사된 응답은 치환 후에도 원문 snapshot으로 저장할 수 없습니다.")
        raw_hash = sha256_bytes(body)
        normalized, normalized_redacted = redact_body(normalized_json_bytes(parsed), secrets)
        redacted = redacted or normalized_redacted
        normalized_hash = sha256_bytes(normalized)
        provider_slug = safe_slug(provider)
        type_slug = safe_slug(document_type)
        id_slug = safe_slug(source_document_id)
        version_slug = safe_slug(parser_version) if parser_version else self.parser_version
        media_type = response_media_type(response)
        relative = Path("v1/raw") / provider_slug / type_slug / id_slug / f"{raw_hash}{_extension(media_type)}"
        raw_path = self.data_dir / relative
        write_once(raw_path, body)
        extraction_relative = Path("v1/extracted") / provider_slug / type_slug / id_slug / version_slug / f"{raw_hash}.json"
        write_once(self.data_dir / extraction_relative, normalized)
        snapshot_id = f"snap-{raw_hash[:24]}"
        return SourceSnapshot(
            snapshot_id=snapshot_id,
            provider=provider,
            source_document_id=source_document_id,
            raw_sha256=raw_hash,
            normalized_sha256=normalized_hash,
            snapshot_ref=relative.as_posix(),
            parser_version=version_slug,
            retrieved_at=retrieved_at,
            retrieval_status=RetrievalStatus.SUCCESS,
            content_completeness=completeness,
            run_id=run_id,
            media_type=media_type,
            byte_length=len(body),
            redacted_secrets=redacted,
        )

    def save_run_manifest(self, run_id: str, payload: dict[str, Any]) -> str:
        safe_run_id = safe_slug(run_id)
        relative = Path("v1/runs") / f"{safe_run_id}.json"
        encoded = (json.dumps({"schema": SNAPSHOT_SCHEMA, **payload}, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        write_once(self.data_dir / relative, encoded)
        return relative.as_posix()

    def read_raw(self, snapshot_ref: str) -> bytes:
        relative = Path(snapshot_ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("snapshot_ref 경로가 허용 범위를 벗어났습니다.")
        path = (self.data_dir / relative).resolve()
        if not path.is_relative_to(self.data_dir):
            raise ValueError("snapshot_ref 경로가 허용 범위를 벗어났습니다.")
        return path.read_bytes()

    def reextract(self, snapshot_ref: str, *, parsed: Any, parser_version: str) -> str:
        raw = self.read_raw(snapshot_ref)
        raw_hash = sha256_bytes(raw)
        source = Path(snapshot_ref)
        if len(source.parts) < 6:
            raise ValueError("지원하지 않는 snapshot_ref입니다.")
        provider_slug, type_slug, id_slug = source.parts[2:5]
        version_slug = safe_slug(parser_version)
        relative = Path("v1/extracted") / provider_slug / type_slug / id_slug / version_slug / f"{raw_hash}.json"
        write_once(self.data_dir / relative, normalized_json_bytes(parsed))
        return relative.as_posix()

    @staticmethod
    def read_legacy_manifest(project_root: Path) -> list[dict[str, Any]]:
        path = project_root / "knowledge" / "raw" / "official_ar_ap_law" / "manifest.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def new_run_id() -> str:
    return "legal-" + uuid.uuid4().hex
