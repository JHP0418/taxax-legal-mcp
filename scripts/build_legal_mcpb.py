"""Claude Desktop 확장(.mcpb)을 만든다.

MCPB `uv` 서버 형식(manifest 0.4)이라 사용자는 Python을 설치하지 않아도 된다.
Claude Desktop이 uv로 의존성을 받아 `src/server.py`를 실행한다. 번들에는 공개
export와 같은 규칙으로 고른 `taxax.legal`·`taxax.mcp` 소스만 넣는다.

    python scripts/build_legal_mcpb.py <출력 폴더>
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from scripts.export_legal_public import _selected_files  # noqa: E402

REPOSITORY = "https://github.com/JHP0418/taxax-legal-mcp"
_SERVER = '''"""Claude Desktop(MCPB uv 형식)이 실행하는 진입점."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxax.mcp.server import main  # noqa: E402

sys.exit(main(["--transport", "stdio"]))
'''


def bundle_name(version: str) -> str:
    return f"taxax-legal-mcp-{version}.mcpb"


def manifest(project: dict) -> dict:
    version = project["version"]
    return {
        "manifest_version": "0.4",
        "name": "taxax-legal",
        "display_name": "TAXax 세무 법률 근거",
        "version": version,
        "description": "법제처·국세청·지방세 공식 원문으로 세무 근거를 찾고 인용 문구를 원문과 대조합니다.",
        "long_description": (
            "세법 조문(시점별 시행본 포함), 판례·조세심판·국세청 해석 원문을 조회하고, "
            "답변에 쓴 인용 문구가 원문의 해당 위치에 실제로 있는지 검증합니다. 법적·세무 결론을 자동 확정하지 않습니다. "
            "법제처 OC 인증키는 open.law.go.kr에서 무료로 발급받습니다. 비워 두면 국세청·지방세 출처만 조회합니다."
        ),
        "author": {"name": "Park JH", "url": "https://github.com/JHP0418"},
        "repository": {"type": "git", "url": REPOSITORY},
        "homepage": REPOSITORY,
        "support": f"{REPOSITORY}/issues",
        "license": "MIT",
        "keywords": ["tax", "korea", "law", "세무", "법령", "판례", "국세청"],
        "server": {
            "type": "uv",
            "entry_point": "src/server.py",
            "mcp_config": {
                "command": "uv",
                "args": ["run", "--directory", "${__dirname}", "src/server.py"],
                "env": {"TAXAX_LAW_GO_OC": "${user_config.law_go_oc}"},
            },
        },
        "tools": [
            {"name": "search_legal_sources", "description": "법령·판례·조세심판·국세청 해석·지방세 결정례 검색"},
            {"name": "get_legal_document", "description": "공식 원문 조회(조문·기준일 시행본·판결문)"},
            {"name": "get_applicable_law", "description": "기준일에 시행 중이던 법령 버전 확인"},
            {"name": "verify_legal_citations", "description": "인용 문구·위치·날짜를 원문과 대조"},
            {"name": "get_source_status", "description": "출처 연결과 저장소 상태"},
            {"name": "search_knowledge", "description": "선택 설치한 내부 지식 검색"},
        ],
        "user_config": {
            "law_go_oc": {
                "type": "string",
                "title": "법제처 OC 인증키",
                "description": "open.law.go.kr에서 무료로 발급받은 키. 비워 두면 국세청·지방세 출처만 조회합니다.",
                "sensitive": True,
                "required": False,
            }
        },
        "compatibility": {
            "platforms": ["win32", "darwin", "linux"],
            "runtimes": {"python": project["requires-python"]},
        },
    }


def pyproject(project: dict) -> str:
    # 빌드 백엔드 없는 가상 프로젝트로 둔다. uv는 의존성만 설치하고 소스는 src/에서 바로 읽는다.
    dependencies = "".join(f'    "{item}",\n' for item in project["dependencies"])
    return (
        "[project]\n"
        'name = "taxax-legal-mcpb"\n'
        f'version = "{project["version"]}"\n'
        'description = "TAXax legal MCP server bundle for Claude Desktop"\n'
        f'requires-python = "{project["requires-python"]}"\n'
        f"dependencies = [\n{dependencies}]\n"
    )


def stage(destination: Path, *, lock: bool = True) -> Path:
    """번들에 들어갈 파일을 destination 아래에 펼친다."""
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    destination.mkdir(parents=True)
    for source in _selected_files():
        relative = source.relative_to(_ROOT)
        if relative.parts[:2] == ("src", "taxax") and relative.parts[2] in {"legal", "mcp"}:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (destination / "src" / "server.py").write_text(_SERVER, encoding="utf-8")
    (destination / "pyproject.toml").write_text(pyproject(project), encoding="utf-8")
    (destination / "manifest.json").write_text(json.dumps(manifest(project), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / ".mcpbignore").write_text(".venv/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    shutil.copy2(_ROOT / "LICENSE", destination / "LICENSE")
    if lock and shutil.which("uv"):
        # 잠금 파일이 있으면 사용자 PC에서도 같은 의존성 버전으로 설치된다.
        subprocess.run(["uv", "lock", "--directory", str(destination), "--quiet"], check=True)
    return destination


def pack(staged: Path, output: Path) -> Path:
    files = sorted(path for path in staged.rglob("*") if path.is_file() and "__pycache__" not in path.parts and ".venv" not in path.parts)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(staged).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output_dir", type=Path)
    arguments = parser.parse_args(argv)
    output_dir = arguments.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    version = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    staged = stage(output_dir / "mcpb-staging")
    bundle = pack(staged, output_dir / bundle_name(version))
    print(json.dumps({"bundle": str(bundle), "bytes": bundle.stat().st_size, "files": len(list(staged.rglob("*.py")))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
