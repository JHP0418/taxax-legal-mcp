"""`python -m taxax.legal`로 CLI를 실행하기 위한 진입점.

pip는 console script를 Python의 Scripts 디렉터리에 설치하는데, 이 경로가
PATH에 없는 설치본이 흔하다(Windows Store 판 Python이 대표적이다). 그러면
설치 직후 `taxax-legal install`이 CommandNotFoundException으로 죽어 첫걸음
자체가 막힌다. 모듈 실행은 PATH를 타지 않으므로 항상 통하는 대안이 된다.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
