"""배포 버전의 단일 출처.

버전 문자열을 파일마다 상수로 박아두면 릴리스할 때 전부 같이 고쳐야 하고,
하나라도 빠뜨리면 조용히 어긋난다. 실제로 Dockerfile은 0.2.0을 고정해 둔 탓에
0.2.8 컨테이너 빌드가 깨졌고, MCP 서버는 클라이언트에 0.2.0을 계속 광고하고
있었다. pyproject.toml의 version이 유일한 출처이며, 설치된 배포본에서는 그
값이 패키지 메타데이터로 들어오므로 여기서 한 번만 읽는다.
"""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

DISTRIBUTION_NAME = "taxax-legal-mcp"
_UNKNOWN = "0+unknown"
_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def package_version() -> str:
    """지금 실행 중인 배포본의 버전.

    실행 시점의 질문("이 서버는 몇 버전인가")에는 설치 메타데이터가 답이다.
    설치 없이 저장소에서 바로 실행하는 경우에만 pyproject.toml로 물러선다.
    """
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return source_version()


def source_version() -> str:
    """이 소스 트리가 만들어낼 배포본의 버전.

    빌드·패키징 스크립트는 이쪽을 써야 한다. 구버전이 설치된 체크아웃에서
    빌드하면 설치 메타데이터는 옛 번호를 돌려주므로, 방금 만든 산출물에 남의
    버전표를 붙이게 된다.
    """
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return _UNKNOWN
    match = _PYPROJECT_VERSION.search(text)
    return match.group(1) if match else _UNKNOWN
