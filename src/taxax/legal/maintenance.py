from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

_BACKUP_FORMAT = "taxax-legal-backup-v1"
_DATABASES = (Path("v1/legal.sqlite3"),)
_LEGACY_REPORT_DATABASE = Path("private/v1/reports.sqlite3")
_DATA_DIRECTORIES = (Path("v1/raw"), Path("v1/extracted"), Path("v1/runs"))
_MAX_RESTORE_BYTES = 8 * 1024 * 1024 * 1024
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class MaintenanceError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _native_path(path: Path) -> str:
    value = str(path.resolve())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _remove_tree(path: Path) -> None:
    shutil.rmtree(_native_path(path))


def _logical_path(value: str) -> Path:
    if os.name != "nt":
        return Path(value)
    if value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\"):
        return Path(value[4:])
    return Path(value)


@contextmanager
def _temporary_directory(*, prefix: str, parent: Path):
    directory = _logical_path(
        tempfile.mkdtemp(
            prefix=prefix,
            dir=_native_path(parent),
        )
    )
    try:
        yield directory
    finally:
        _remove_tree(directory)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(_native_path(path), "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only_database_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _validate_database(path: Path) -> None:
    try:
        with closing(sqlite3.connect(_read_only_database_uri(path), uri=True)) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise MaintenanceError("SQLite integrity 검증에 실패했습니다.")
            schema = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if schema is None:
                raise MaintenanceError("SQLite schema version을 확인하지 못했습니다.")
    except sqlite3.Error as exc:
        raise MaintenanceError("SQLite backup 파일을 열거나 검증하지 못했습니다.") from exc


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(_read_only_database_uri(source), uri=True)) as source_connection:
            with closing(sqlite3.connect(destination)) as destination_connection:
                source_connection.backup(destination_connection)
                destination_connection.execute("PRAGMA journal_mode = DELETE")
    except sqlite3.Error as exc:
        raise MaintenanceError("SQLite online backup에 실패했습니다.") from exc
    _validate_database(destination)


def _copy_data_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    source_root = source.resolve()
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise MaintenanceError("backup 대상에 symbolic link가 포함될 수 없습니다.")
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(source_root):
            raise MaintenanceError("backup 대상 경로가 data directory 밖을 가리킵니다.")
        relative = path.relative_to(source)
        target = destination / relative
        os.makedirs(_native_path(target.parent), exist_ok=True)
        shutil.copy2(_native_path(path), _native_path(target))


def _publish_new_file(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise MaintenanceError("기존 backup archive를 덮어쓰지 않습니다.") from exc
    except OSError as exc:
        error_code = exc.winerror if getattr(exc, "winerror", None) is not None else exc.errno
        raise MaintenanceError(
            f"backup archive를 원자적으로 생성하지 못했습니다. OS error={error_code}"
        ) from exc


def create_backup(data_dir: Path, archive_path: Path) -> dict[str, Any]:
    source_root = data_dir.resolve()
    archive = archive_path.resolve()
    if not source_root.is_dir():
        raise MaintenanceError("법률 data directory를 찾지 못했습니다.")
    if archive.exists():
        raise MaintenanceError("기존 backup archive를 덮어쓰지 않습니다.")
    archive.parent.mkdir(parents=True, exist_ok=True)
    missing = [relative.as_posix() for relative in _DATABASES if not (source_root / relative).is_file()]
    if missing:
        raise MaintenanceError("필수 SQLite database가 없습니다: " + ", ".join(missing))

    with _temporary_directory(
        prefix=".taxax-legal-backup-",
        parent=archive.parent,
    ) as temporary_root:
        staged = temporary_root / "data"
        staged.mkdir()
        for relative in _DATABASES:
            _backup_database(source_root / relative, staged / relative)
        if (source_root / _LEGACY_REPORT_DATABASE).is_file():
            _backup_database(source_root / _LEGACY_REPORT_DATABASE, staged / _LEGACY_REPORT_DATABASE)
        for relative in _DATA_DIRECTORIES:
            _copy_data_tree(source_root / relative, staged / relative)

        files = {
            path.relative_to(staged).as_posix(): {
                "sha256": _sha256(path),
                "bytes": os.stat(_native_path(path)).st_size,
            }
            for path in sorted(staged.rglob("*"))
            if os.path.isfile(_native_path(path))
        }
        manifest = {
            "format": _BACKUP_FORMAT,
            "created_at": _utc_now(),
            "files": files,
        }
        (staged / "backup-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_archive = temporary_root / "backup.zip"
        with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staged.rglob("*")):
                if os.path.isfile(_native_path(path)):
                    bundle.write(_native_path(path), path.relative_to(staged).as_posix())
        _publish_new_file(temporary_archive, archive)

    return {
        "status": "ok",
        "format": _BACKUP_FORMAT,
        "archive": archive.name,
        "archive_sha256": _sha256(archive),
        "files": len(files),
        "created_at": manifest["created_at"],
    }


def _is_portable_archive_path(name: str) -> bool:
    path = PurePosixPath(name)
    if not name or name != path.as_posix() or path.is_absolute() or not path.parts:
        return False
    for part in path.parts:
        if (
            part in {".", ".."}
            or part.endswith((".", " "))
            or any(character in _WINDOWS_INVALID_CHARACTERS or ord(character) < 32 for character in part)
            or part.split(".", 1)[0].rstrip(" .").upper() in _WINDOWS_RESERVED_NAMES
        ):
            return False
    return True


def _validated_members(bundle: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    portable_names: set[str] = set()
    total = 0
    for member in bundle.infolist():
        name = member.filename
        mode = member.external_attr >> 16
        portable_name = name.casefold()
        if (
            name.endswith("/")
            or not _is_portable_archive_path(name)
            or "\\" in name
            or portable_name in portable_names
            or member.flag_bits & 0x1
            or stat.S_ISLNK(mode)
        ):
            raise MaintenanceError("backup archive에 허용되지 않은 entry가 있습니다.")
        total += member.file_size
        if total > _MAX_RESTORE_BYTES:
            raise MaintenanceError("backup archive 압축 해제 크기 한도를 초과했습니다.")
        members[name] = member
        portable_names.add(portable_name)
    if "backup-manifest.json" not in members:
        raise MaintenanceError("backup manifest가 없습니다.")
    return members


def _extract_validated(bundle: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], destination: Path) -> None:
    for name, member in members.items():
        target = destination.joinpath(*PurePosixPath(name).parts)
        os.makedirs(_native_path(target.parent), exist_ok=True)
        with bundle.open(member, "r") as source, open(_native_path(target), "xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


def _validate_manifest(staged: Path, members: dict[str, zipfile.ZipInfo]) -> dict[str, Any]:
    try:
        manifest = json.loads((staged / "backup-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError("backup manifest를 읽지 못했습니다.") from exc
    if manifest.get("format") != _BACKUP_FORMAT or not isinstance(manifest.get("files"), dict):
        raise MaintenanceError("지원하지 않는 backup manifest입니다.")
    expected = set(manifest["files"])
    actual = set(members) - {"backup-manifest.json"}
    if expected != actual:
        raise MaintenanceError("backup manifest와 archive 파일 목록이 일치하지 않습니다.")
    for name, metadata in manifest["files"].items():
        if not isinstance(metadata, dict):
            raise MaintenanceError("backup manifest 파일 정보가 올바르지 않습니다.")
        path = staged.joinpath(*PurePosixPath(name).parts)
        if metadata.get("sha256") != _sha256(path) or metadata.get("bytes") != os.stat(_native_path(path)).st_size:
            raise MaintenanceError("backup 파일 hash 또는 크기가 manifest와 일치하지 않습니다.")
    for relative in _DATABASES:
        if relative.as_posix() not in expected:
            raise MaintenanceError("backup에 필수 SQLite database가 없습니다.")
        _validate_database(staged / relative)
    if _LEGACY_REPORT_DATABASE.as_posix() in expected:
        _validate_database(staged / _LEGACY_REPORT_DATABASE)
    return manifest


def restore_backup(archive_path: Path, destination_dir: Path) -> dict[str, Any]:
    archive = archive_path.resolve()
    destination = destination_dir.resolve()
    if not archive.is_file():
        raise MaintenanceError("backup archive를 찾지 못했습니다.")
    if destination.exists():
        raise MaintenanceError("기존 data directory를 덮어쓰지 않습니다.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with _temporary_directory(
        prefix=".taxax-legal-restore-",
        parent=destination.parent,
    ) as temporary_root:
        staged = temporary_root / "data"
        staged.mkdir()
        try:
            with zipfile.ZipFile(archive, "r") as bundle:
                members = _validated_members(bundle)
                _extract_validated(bundle, members, staged)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            error_code = exc.winerror if getattr(exc, "winerror", None) is not None else getattr(exc, "errno", None)
            raise MaintenanceError(
                "backup archive를 안전하게 해제하지 못했습니다. "
                f"cause={type(exc).__name__}, OS error={error_code}"
            ) from exc
        manifest = _validate_manifest(staged, members)
        staged.rename(destination)

    return {
        "status": "ok",
        "format": _BACKUP_FORMAT,
        "archive": archive.name,
        "archive_sha256": _sha256(archive),
        "files": len(manifest["files"]),
        "created_at": manifest.get("created_at"),
        "restored_to": destination.name,
    }


# 저장된 문서는 수집 당시의 파서가 만든 결과다. 파서를 고쳐도 이미 들어온
# 문서는 그대로 남는다. 실제로 NTS 검색 응답의 <!HS>/<!HE> 하이라이트 태그를
# 벗기는 수정을 한 뒤에도, 그 전에 수집된 56건은 제목과 본문에 태그를 단 채
# 색인에 남아 사용자에게 제공됐다. 원본 응답은 스냅샷으로 보존돼 있고 거기에는
# 그 태그가 실제로 들어 있으므로, 출처 기록은 손대지 않고 파생된 문서만 현재
# 파서 기준으로 다시 정규화한다.
_TEXT_NORMALIZERS: dict[str, Any] = {}


def _normalizer_for(provider: str):
    if not _TEXT_NORMALIZERS:
        from .providers.nts import strip_highlight_markup

        _TEXT_NORMALIZERS["taxlaw.nts.go.kr"] = strip_highlight_markup
    return _TEXT_NORMALIZERS.get(provider)


def renormalize_documents(database_path: Path, *, apply: bool = False) -> dict[str, Any]:
    """저장된 문서를 현재 파서 기준으로 다시 정규화한다.

    기본은 검사만 하고 바꾸지 않는다(apply=False). 실제로 고칠 때만 apply를
    준다. 원본 스냅샷과 수집 이력은 건드리지 않는다.
    """
    changed: list[str] = []
    scanned = 0
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT record_id, provider, title, searchable_text, document_json FROM legal_documents"
        ).fetchall()
        for row in rows:
            normalize = _normalizer_for(row["provider"])
            if normalize is None:
                continue
            scanned += 1
            document = json.loads(row["document_json"])
            before = json.dumps(document, ensure_ascii=False, sort_keys=True)
            document["title"] = normalize(document.get("title") or "")
            for section in document.get("sections") or []:
                if isinstance(section.get("text"), str):
                    section["text"] = normalize(section["text"])
                if isinstance(section.get("heading"), str):
                    section["heading"] = normalize(section["heading"])
            after = json.dumps(document, ensure_ascii=False, sort_keys=True)
            if before == after:
                continue
            changed.append(document.get("document_id") or str(row["record_id"]))
            if apply:
                connection.execute(
                    "UPDATE legal_documents SET title = ?, searchable_text = ?, document_json = ? WHERE record_id = ?",
                    (
                        document["title"],
                        normalize(row["searchable_text"] or ""),
                        after,
                        row["record_id"],
                    ),
                )
        if apply and changed:
            connection.commit()
    finally:
        connection.close()
    return {
        "status": "ok",
        "scanned": scanned,
        "changed": len(changed),
        "applied": bool(apply and changed),
        "document_ids": changed[:20],
    }
