from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

_ROOT = Path(__file__).resolve().parents[1]
_REQUIRED_SDIST_FILES = {
    ".env.example",
    ".gitignore",
    ".github/workflows/ci.yml",
    "CHANGELOG.md",
    "Dockerfile",
    "LICENSE",
    "MANIFEST.in",
    "PUBLIC_ALLOWLIST.md",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "compose.yaml",
    "docs/legal-mcp-deployment.md",
    "examples/claude-code-stdio.json",
    "examples/hosted-mcp.json",
    "pyproject.toml",
    "requirements.lock",
    "scripts/build_windows_installer.py",
    "scripts/export_legal_public.py",
    "scripts/smoke_legal_install.py",
    "scripts/smoke_windows_executables.py",
    "scripts/validate_legal_distribution.py",
}
_FORBIDDEN_PARTS = {"knowledge", "data", "var", "adapters", "__pycache__"}
_FORBIDDEN_MODULES = {
    "src/taxax/__init__.py",
    "src/taxax/engine.py",
    "src/taxax/storage.py",
    "src/taxax/orchestration.py",
    "src/taxax/canonical.py",
    "src/taxax/ingest.py",
    "src/taxax/export.py",
}
_PRIVATE_KEY = re.compile(br"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_TOKEN = re.compile(br"(?:gh[opusr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_modules() -> set[str]:
    paths = list((_ROOT / "src" / "taxax" / "legal").rglob("*.py"))
    paths.extend((_ROOT / "src" / "taxax" / "mcp").rglob("*.py"))
    return {path.relative_to(_ROOT / "src").as_posix() for path in paths}


def _source_tests() -> set[str]:
    paths = [path for path in (_ROOT / "tests").glob("test_legal_*.py") if path.name != "test_legal_claude_e2e.py"]
    paths.extend(path for path in (_ROOT / "tests" / "legal_fixtures").rglob("*") if path.is_file())
    return {path.relative_to(_ROOT).as_posix() for path in paths}


def _safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name and name == path.as_posix() and not path.is_absolute() and ".." not in path.parts and "\\" not in name)


def _scan_payload(name: str, payload: bytes) -> None:
    if _PRIVATE_KEY.search(payload) or _TOKEN.search(payload):
        raise ValueError(f"비밀값으로 보이는 내용이 포함됐습니다: {name}")


def _check_forbidden(name: str) -> None:
    path = PurePosixPath(name)
    lowered = {part.casefold() for part in path.parts}
    if lowered & _FORBIDDEN_PARTS:
        raise ValueError(f"금지된 경로가 distribution에 포함됐습니다: {name}")
    if name in _FORBIDDEN_MODULES or name.endswith((".sqlite", ".sqlite3", ".db", ".env")):
        raise ValueError(f"금지된 파일이 distribution에 포함됐습니다: {name}")


def _validate_wheel(path: Path) -> dict[str, object]:
    expected_modules = _source_modules()
    found_modules: set[str] = set()
    with zipfile.ZipFile(path) as bundle:
        for member in bundle.infolist():
            name = member.filename
            if name.endswith("/"):
                continue
            if not _safe_path(name) or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(f"wheel entry가 안전하지 않습니다: {name}")
            _check_forbidden(name)
            if name.startswith(("taxax/legal/", "taxax/mcp/")) and name.endswith(".py"):
                found_modules.add(name)
            elif ".dist-info/" not in name:
                raise ValueError(f"wheel allowlist 밖의 entry입니다: {name}")
            _scan_payload(name, bundle.read(member))
    if found_modules != expected_modules:
        missing = sorted(expected_modules - found_modules)
        extra = sorted(found_modules - expected_modules)
        raise ValueError(f"wheel Python module 목록이 다릅니다. missing={missing}, extra={extra}")
    return {"file": path.name, "sha256": _sha256(path), "entries": len(found_modules), "modules": sorted(found_modules)}


def _strip_sdist_root(names: list[str]) -> tuple[str, dict[str, str]]:
    roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    if len(roots) != 1:
        raise ValueError("sdist root directory가 하나가 아닙니다.")
    root = next(iter(roots))
    prefix = root + "/"
    relative = {name: name[len(prefix):] for name in names if name.startswith(prefix) and name != prefix}
    return root, relative


def _allowed_sdist_path(name: str) -> bool:
    if name in _REQUIRED_SDIST_FILES | {"PKG-INFO", "setup.cfg"}:
        return True
    if name.startswith("src/taxax_legal_mcp.egg-info/"):
        return True
    if name.startswith(("src/taxax/legal/", "src/taxax/mcp/")) and name.endswith(".py"):
        return True
    if name.startswith("tests/legal_fixtures/"):
        return True
    return name.startswith("tests/test_legal_") and name.endswith(".py") and "/" not in name[len("tests/"):]


def _validate_sdist(path: Path) -> dict[str, object]:
    with tarfile.open(path, "r:gz") as bundle:
        regular = [member for member in bundle.getmembers() if member.isfile()]
        if any(member.issym() or member.islnk() for member in bundle.getmembers()):
            raise ValueError("sdist에 symbolic link 또는 hard link가 포함됐습니다.")
        names = [member.name for member in regular]
        _, relative_names = _strip_sdist_root(names)
        found = set(relative_names.values())
        for member in regular:
            relative = relative_names[member.name]
            if not _safe_path(relative):
                raise ValueError(f"sdist entry가 안전하지 않습니다: {relative}")
            _check_forbidden(relative)
            if not _allowed_sdist_path(relative):
                raise ValueError(f"sdist allowlist 밖의 entry입니다: {relative}")
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise ValueError(f"sdist entry를 읽지 못했습니다: {relative}")
            _scan_payload(relative, extracted.read())

    required = _REQUIRED_SDIST_FILES | {"PKG-INFO"}
    missing_required = sorted(required - found)
    expected_sources = {"src/" + name for name in _source_modules()}
    expected_tests = _source_tests()
    missing_sources = sorted(expected_sources - found)
    missing_tests = sorted(expected_tests - found)
    if missing_required or missing_sources or missing_tests:
        raise ValueError(
            "sdist 필수 항목이 없습니다. "
            f"files={missing_required}, modules={missing_sources}, tests={missing_tests}"
        )
    return {"file": path.name, "sha256": _sha256(path), "entries": len(found)}


def validate_distribution(directory: Path) -> dict[str, object]:
    wheels = sorted(directory.glob("taxax_legal_mcp-*.whl"))
    sdists = sorted(directory.glob("taxax_legal_mcp-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("검증 경로에는 taxax-legal-mcp wheel과 sdist가 각각 정확히 하나 있어야 합니다.")
    return {
        "status": "ok",
        "wheel": _validate_wheel(wheels[0]),
        "sdist": _validate_sdist(sdists[0]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="taxax-legal wheel/sdist의 공개 allowlist를 검증합니다.")
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    try:
        result = validate_distribution(arguments.directory.resolve())
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        result = {"status": "error", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
