"""推演共用的部件：到达过程、故障参数、统计结构、排空判定。

两个引擎共用同一份**到达序列**与**策略函数**（:mod:`cascade.policy`），
但调度方式完全独立：

* :mod:`cascade.sim_stepped` —— 固定时间步长，逐 tick 推进，worker 的
  工作量按 tick 累加，超时/准入都在 tick 边界判定。用于播放动画。
* :mod:`cascade.sim_des` —— 逐个请求的离散事件模拟（事件堆 + 精确时间），
  不设 tick。用于核对倍数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import Topology
from .policy import effective_workers
from .resolve import ResolvedEdge
from .scenario import Scenario, build_focus

MAX_JOBS = 400_000
#: 每个服务的等待队列上限（超出即快速拒绝，模拟线程池/连接池打满）
QUEUE_FACTOR = 8


def poisson(rng, lam: float) -> int:
    """Knuth 法；lam 较小，足够。"""
    import math

    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def generate_arrivals(scenario: Scenario) -> List[float]:
    """生成入口请求的到达时刻（ms）。两次推演用同一份，保证输入一致。"""
    import random

    rng = random.Random(scenario.seed * 7919 + 13)
    arrivals: List[float] = []
    dt = scenario.dt_ms
    lam = scenario.entry_rps * dt / 1000.0
    t = 0.0
    while t < scenario.duration_ms:
        n = poisson(rng, lam)
        for _ in range(n):
            arrivals.append(t + rng.random() * dt)
        t += dt
    arrivals.sort()
    if scenario.max_root_requests is not None:
        arrivals = arrivals[: scenario.max_root_requests]
    return arrivals


def fault_slow(scenario: Scenario, service: str, t_ms: float) -> float:
    f = scenario.fault_for(service)
    if f is not None and f.active_at(t_ms):
        return max(0.0, f.slow_factor)
    return 1.0


def fault_error(scenario: Scenario, service: str, t_ms: float) -> float:
    f = scenario.fault_for(service)
    if f is not None and f.active_at(t_ms):
        return max(0.0, min(1.0, f.error_rate))
    return 0.0


def queue_limit(topo: Topology, sid: str) -> int:
    return max(4, effective_workers(topo.services[sid]) * QUEUE_FACTOR)


@dataclass
class SimBase:
    topo: Topology
    resolved: Dict[str, ResolvedEdge]
    scenario: Scenario
    focus_edges: List[str] = field(default_factory=list)
    focus_nodes: List[str] = field(default_factory=list)

    @classmethod
    def prepare(cls, topo: Topology, resolved: Dict[str, ResolvedEdge],
                scenario: Scenario):
        fg = build_focus(topo, scenario)
        return cls(
            topo=topo,
            resolved=resolved,
            scenario=scenario,
            focus_edges=sorted(fg.edges),
            focus_nodes=sorted(fg.nodes),
        )

    def out_edges(self, sid: str) -> List[ResolvedEdge]:
        return [
            self.resolved[e.id]
            for e in self.topo.out_edges(sid)
            if e.id in self.focus_edge_set
        ]

    @property
    def focus_edge_set(self) -> set:
        return set(self.focus_edges)


class EdgeCounter:
    __slots__ = ("attempts", "initial", "retries", "timeouts", "successes",
                 "failures", "orphaned", "rejected", "retry_denied")

    def __init__(self):
        self.attempts = 0
        self.initial = 0
        self.retries = 0
        self.timeouts = 0
        self.successes = 0
        self.failures = 0
        self.orphaned = 0
        self.rejected = 0
        self.retry_denied = 0

    def to_dict(self, denom: int) -> dict:
        return {
            "attempts": self.attempts,
            "initial": self.initial,
            "retries": self.retries,
            "timeouts": self.timeouts,
            "successes": self.successes,
            "failures": self.failures,
            "orphaned": self.orphaned,
            "rejected": self.rejected,
            "retry_denied": self.retry_denied,
            "amplification": self.attempts / max(1, denom),
        }


class TokenBucket:
    """重试预算：每发一次首次尝试就补充 ``budget`` 个令牌，
    每发一次重试消耗 1 个。稳态下重试比例被限制在 ``budget`` 以内，
    于是单边放大倍数 ≈ ``1 + budget``。桶有上限，防止长期空闲后攒出一次性突发。
    """

    __slots__ = ("budget", "tokens", "cap")

    def __init__(self, budget: Optional[float]):
        self.budget = budget
        self.tokens = 0.0
        self.cap = 2.0 * budget if budget else 0.0

    @property
    def enabled(self) -> bool:
        return self.budget is not None

    def on_initial(self) -> None:
        if self.budget is None:
            return
        self.tokens = min(self.cap, self.tokens + self.budget)

    def take(self) -> bool:
        """尝试取一个重试令牌。未启用预算时永远允许。"""
        if self.budget is None:
            return True
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class ServiceCounter:
    __slots__ = ("workers", "arrivals", "starts", "completions", "errors",
                 "max_queue", "max_busy", "saturated_steps", "util_sum", "steps",
                 "rejected")

    def __init__(self, workers: int):
        self.workers = workers
        self.arrivals = 0
        self.starts = 0
        self.completions = 0
        self.errors = 0
        self.max_queue = 0
        self.max_busy = 0
        self.saturated_steps = 0
        self.util_sum = 0.0
        self.steps = 0
        self.rejected = 0

    def tick(self, busy: int, queue: int) -> None:
        self.steps += 1
        self.util_sum += busy
        if queue > self.max_queue:
            self.max_queue = queue
        if busy > self.max_busy:
            self.max_busy = busy
        if queue > 0:
            self.saturated_steps += 1

    def to_dict(self) -> dict:
        steps = max(1, self.steps)
        return {
            "workers": self.workers,
            "arrivals": self.arrivals,
            "starts": self.starts,
            "completions": self.completions,
            "errors": self.errors,
            "max_queue": self.max_queue,
            "max_busy": self.max_busy,
            "saturated_steps": self.saturated_steps,
            "avg_util": self.util_sum / steps,
            "peak_util": self.max_busy / max(1, self.workers),
            "rejected": self.rejected,
        }
