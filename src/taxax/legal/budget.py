from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, Iterator


class RemoteBudgetExceeded(RuntimeError):
    pass


@dataclass
class RemoteRequestBudget:
    max_requests: int
    max_seconds: float
    clock: Callable[[], float] = time.monotonic
    consumed_requests: int = 0
    started_at: float = field(init=False)
    exhausted_reason: str | None = None

    def __post_init__(self) -> None:
        if self.max_requests < 0 or self.max_seconds < 0:
            raise ValueError("조사 예산은 0 이상이어야 합니다.")
        self.started_at = self.clock()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self.started_at)

    @property
    def exhausted(self) -> bool:
        return self.exhausted_reason is not None

    def check_time(self) -> None:
        if self.elapsed_seconds >= self.max_seconds:
            self.exhausted_reason = "time_limit"
            raise RemoteBudgetExceeded("조사 시간 예산에 도달했습니다.")

    def consume(self) -> None:
        self.check_time()
        if self.consumed_requests >= self.max_requests:
            self.exhausted_reason = "remote_request_limit"
            raise RemoteBudgetExceeded("원격 요청 예산에 도달했습니다.")
        self.consumed_requests += 1


_ACTIVE_BUDGET: ContextVar[RemoteRequestBudget | None] = ContextVar("taxax_legal_remote_budget", default=None)


@contextmanager
def activate_remote_budget(budget: RemoteRequestBudget) -> Iterator[RemoteRequestBudget]:
    token = _ACTIVE_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE_BUDGET.reset(token)


def consume_remote_request() -> None:
    budget = _ACTIVE_BUDGET.get()
    if budget is not None:
        budget.consume()


def check_remote_time() -> None:
    budget = _ACTIVE_BUDGET.get()
    if budget is not None:
        budget.check_time()


def remaining_seconds() -> float | None:
    """활성 예산에 남은 시간(초). 예산이 없으면 None(제한 없음).

    max_seconds는 요청 사이(attempt 경계)에서만 검사되므로, 이 값을 실제 소켓
    timeout에 반영하지 않으면 예산이 1초여도 이미 시작한 HTTP 시도 하나가
    기본 timeout(law.go.kr 30초, NTS/OLTA 20초)까지 그대로 걸릴 수 있다.
    """
    budget = _ACTIVE_BUDGET.get()
    if budget is None:
        return None
    return max(0.0, budget.max_seconds - budget.elapsed_seconds)
