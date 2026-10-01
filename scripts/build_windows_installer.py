from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
# 설치 없이 체크아웃에서 바로 실행해도 버전 출처 한 곳을 그대로 쓰기 위해.
sys.path.insert(0, str(_ROOT / "src"))
from taxax.legal.version import source_version  # noqa: E402

# 빌드 산출물에는 이 체크아웃이 만들 버전을 붙인다(설치된 구버전이 아니라).
_VERSION = source_version()
_MCP_FROZEN_ENTRY = _ROOT / "src" / "taxax" / "mcp" / "frozen_server.py"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_pyinstaller(entry: Path, *, name: str, dist: Path, work: Path, spec: Path, windowed: bool = False, binaries: tuple[Path, ...] = ()) -> None:
    staged_entry = spec / entry.name
    shutil.copy2(entry, staged_entry)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noupx",
        "--windowed" if windowed else "--console",
        "--name",
        name,
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(spec),
        "--paths",
        str(_ROOT / "src"),
        "--copy-metadata",
        "taxax-legal-mcp",
        "--collect-all",
        "mcp",
        "--collect-all",
        "pydantic",
        "--collect-all",
        "jwt",
        "--collect-all",
        "cryptography",
    ]
    for binary in binaries:
        command.extend(["--add-binary", f"{binary}{os.pathsep}payload"])
    command.append(str(staged_entry))
    completed = subprocess.run(command, cwd=_ROOT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"PyInstaller build가 실패했습니다: {name}")


def build_windows_installer(output: Path) -> dict[str, object]:
    if os.name != "nt":
        raise ValueError("Windows executable은 Windows에서 빌드해야 합니다.")
    if importlib.util.find_spec("PyInstaller") is None:
        raise ValueError("PyInstaller가 없습니다. `python -m pip install -e .[windows]` 후 실행하십시오.")
    target = output.resolve()
    if target.exists():
        raise ValueError("installer output은 존재하지 않는 새 directory여야 합니다.")
    if not target.parent.is_dir():
        raise ValueError("installer output parent directory가 없습니다.")

    with tempfile.TemporaryDirectory(prefix="taxax-legal-pyinstaller-", dir=target.parent) as directory:
        staging = Path(directory)
        payload = staging / "windows-payload"
        setup_dist = staging / "setup-dist"
        spec = staging / "spec"
        payload.mkdir()
        setup_dist.mkdir()
        spec.mkdir()

        _run_pyinstaller(
            _ROOT / "src" / "taxax" / "legal" / "frozen_cli.py",
            name="taxax-legal",
            dist=payload,
            work=staging / "work-cli",
            spec=spec,
        )
        _run_pyinstaller(
            _MCP_FROZEN_ENTRY,
            name="taxax-legal-mcp",
            dist=payload,
            work=staging / "work-mcp",
            spec=spec,
        )
        _run_pyinstaller(
            _ROOT / "src" / "taxax" / "legal" / "frozen_setup.py",
            name="taxax-legal-setup",
            dist=setup_dist,
            work=staging / "work-setup",
            spec=spec,
            windowed=True,
            binaries=(payload / "taxax-legal.exe", payload / "taxax-legal-mcp.exe"),
        )

        bundle = staging / "bundle"
        bundle.mkdir()
        architecture = platform.machine().lower() or "unknown"
        setup_name = f"taxax-legal-setup-{_VERSION}-windows-{architecture}.exe"
        shutil.copy2(setup_dist / "taxax-legal-setup.exe", bundle / setup_name)
        shutil.copytree(payload, bundle / "windows-payload")
        files = {
            path.relative_to(bundle).as_posix(): {
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(bundle.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "format": "taxax-legal-windows-installer-v1",
            "version": _VERSION,
            "architecture": architecture,
            "code_signed": False,
            "smartscreen_reputation": "not_established",
            "files": files,
        }
        (bundle / "SHA256SUMS.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            bundle.rename(target)
        except FileExistsError as exc:
            raise ValueError("installer output이 동시에 생성돼 덮어쓰지 않습니다.") from exc

    return {
        "status": "ok",
        "output": target.name,
        "setup": setup_name,
        "payload_files": sorted(files),
        "code_signed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows PyInstaller local setup executable을 빌드합니다.")
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    try:
        result = build_windows_installer(arguments.output)
    except (OSError, RuntimeError, ValueError) as exc:
        result = {"status": "error", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
