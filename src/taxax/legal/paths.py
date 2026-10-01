"""사용자별 기본 경로. 다른 taxax 모듈을 import하지 않는다.

transport는 요청 간격을 이 기계 전체에서 지키기 위해 공유 상태 파일이 필요한데,
그 위치를 알려면 데이터 폴더 기본값을 알아야 한다. 그 함수가 service에 있으면
service가 transport를 import하고 있으므로 순환이 된다. 양쪽이 함께 의존할 수
있도록 말단 모듈로 떼어 둔다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_legal_data_dir() -> Path:
    if os.name == "nt":
        configured = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(configured).expanduser() if configured else Path.home() / "AppData" / "Local"
        return base / "TAXax" / "legal"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TAXax" / "legal"
    configured = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(configured).expanduser() if configured and Path(configured).expanduser().is_absolute() else Path.home() / ".local" / "share"
    return base / "taxax" / "legal"


def shared_pace_database() -> Path:
    """upstream 요청 간격을 기록하는 파일의 위치.

    데이터 폴더 안에 둔다. MCP 등록과 CLI가 모두 같은 데이터 폴더를 쓰므로,
    이 기계에서 도는 모든 프로세스가 같은 파일을 보게 된다. 상대 서버가 보는
    것은 프로세스가 아니라 IP 하나이므로 간격도 그 단위로 지켜야 한다.
    """
    configured = os.environ.get("TAXAX_LEGAL_DATA_DIR", "").strip()
    base = Path(configured).expanduser() if configured else default_legal_data_dir()
    return base / "v1" / "upstream-pace.sqlite3"
