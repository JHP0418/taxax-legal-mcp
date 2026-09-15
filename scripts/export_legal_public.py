from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_EXACT_FILES = (
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
)
_PRIVATE_KEY = re.compile(br"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_TOKEN = re.compile(br"(?:gh[opusr]_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _selected_files() -> list[Path]:
    files = [_ROOT / relative for relative in _EXACT_FILES]
    files.extend((_ROOT / "src" / "taxax" / "legal").rglob("*.py"))
    files.extend((_ROOT / "src" / "taxax" / "mcp").rglob("*.py"))
    files.extend((_ROOT / "tests").glob("test_legal_*.py"))
    files.extend(path for path in (_ROOT / "tests" / "legal_fixtures").rglob("*") if path.is_file())
    return sorted(set(files), key=lambda path: path.relative_to(_ROOT).as_posix())


def export_public(destination: Path) -> dict[str, object]:
    target_root = destination.resolve()
    if target_root.exists():
        raise ValueError("공개 export destination은 존재하지 않는 새 경로여야 합니다.")
    if target_root == _ROOT or _ROOT in target_root.parents:
        raise ValueError("공개 export destination은 source checkout 밖에 있어야 합니다.")
    if not target_root.parent.is_dir():
        raise ValueError("공개 export destination의 parent directory가 없습니다.")

    selected = _selected_files()
    missing = [path.relative_to(_ROOT).as_posix() for path in selected if not path.is_file()]
    if missing:
        raise ValueError("공개 allowlist 필수 파일이 없습니다: " + ", ".join(missing))

    casefolded: set[str] = set()
    for source in selected:
        relative = source.relative_to(_ROOT)
        name = relative.as_posix()
        folded = name.casefold()
        if folded in casefolded:
            raise ValueError(f"대소문자 비구분 경로가 충돌합니다: {name}")
        casefolded.add(folded)
        if source.is_symlink():
            raise ValueError(f"symbolic link는 공개 bundle에 포함할 수 없습니다: {name}")
        payload = source.read_bytes()
        if _PRIVATE_KEY.search(payload) or _TOKEN.search(payload):
            raise ValueError(f"비밀값으로 보이는 내용이 있어 공개를 중단합니다: {name}")

    with tempfile.TemporaryDirectory(prefix=".taxax-legal-public-", dir=target_root.parent) as directory:
        staged_root = Path(directory) / "bundle"
        staged_root.mkdir()
        manifest_files: dict[str, dict[str, object]] = {}
        for source in selected:
            relative = source.relative_to(_ROOT)
            target = staged_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            manifest_files[relative.as_posix()] = {
                "sha256": _sha256(target),
                "bytes": target.stat().st_size,
            }

        manifest = {
            "format": "taxax-legal-public-allowlist-v1",
            "source_distribution": "taxax-legal-mcp",
            "files": manifest_files,
            "excluded_by_construction": [
                "knowledge/**",
                "data/**",
                "var/**",
                "*.sqlite*",
                ".env",
                "src/taxax/engine.py",
                "src/taxax/storage.py",
                "src/taxax/orchestration.py",
                "src/taxax/canonical.py",
                "src/taxax/ingest.py",
                "src/taxax/export.py",
                "src/taxax/adapters/**",
            ],
        }
        manifest_path = staged_root / "PUBLIC_FILE_MANIFEST.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_sha256 = _sha256(manifest_path)
        try:
            staged_root.rename(target_root)
        except FileExistsError as exc:
            raise ValueError("공개 export destination이 동시에 생성돼 덮어쓰지 않습니다.") from exc

    return {
        "status": "ok",
        "destination": target_root.name,
        "files": len(manifest_files),
        "manifest_sha256": manifest_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="새 빈 경로에 taxax-legal 공개 allowlist만 복사합니다.")
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    try:
        result = export_public(arguments.destination)
    except (OSError, ValueError) as exc:
        result = {"status": "error", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
