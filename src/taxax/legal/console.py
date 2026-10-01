"""표준 출력 스트림을 주변 코드페이지와 무관하게 만든다.

이 프로그램의 사용자 대면 문자열은 대부분 한글이다. Windows에서 stdout의
인코딩은 실행 환경의 코드페이지를 따르는데, 한국어 Windows가 아니면 그 값이
cp1252처럼 한글을 담을 수 없는 것이 된다. 그 상태로 한 줄만 출력해도
UnicodeEncodeError가 나고, 그것이 처리되지 않은 예외로 올라간다.

콘솔 프로그램이면 traceback을 남기고 죽는 것으로 끝나지만, PyInstaller
--windowed로 만든 GUI 실행파일은 처리되지 않은 예외를 메시지 상자로 띄우고
사용자가 누를 때까지 기다린다. 누를 사람이 없는 환경에서는 프로세스가 그대로
멈춘다. 실제로 GitHub Actions의 windows runner에서 `taxax-legal-setup --help`
한 줄이 이 경로로 영구 정지해 job을 잡아먹었다. PYTHONUTF8=1을 환경변수로
줘도 frozen 실행파일에는 반영되지 않았다.

출력이 깨지는 것과 프로그램이 멈추는 것 중에는 전자가 낫다.
"""

from __future__ import annotations

import os
import sys


def prepare_console_streams() -> None:
    """stdout/stderr를 UTF-8로 맞추고, 없으면 버리는 스트림을 붙인다."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            # windowed 실행파일은 콘솔이 없어 아예 None일 수 있다.
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # errors="replace"까지 주는 이유: UTF-8로 바꾸지 못하는 스트림이
            # 남아 있더라도 인코딩 실패로 죽지는 않게 한다.
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
