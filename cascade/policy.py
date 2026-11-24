"""调用策略的纯函数：超时、退避、处理时长。

分析器、时间步进推演、逐请求离散事件模拟**共用**这里的定义，
保证三者对"配置如何变成行为"的理解一致。调度与排队各自独立实现。
"""
from __future__ import annotations

import math


from .model import Service
from .resolve import ResolvedEdge

INF = float("inf")


def attempt_timeout_ms(edge: ResolvedEdge, remaining_budget_ms: float) -> float:
    """一次尝试的调用方计时器。

    ``propagate_deadline`` 打开时，调用方会把自己剩余的预算传给下层：
    本次尝试最多只等 ``min(本边超时, 剩余预算)``。关闭时下层看不见上游预算，
    各层各算各的 —— 正是"网关已经放弃、底下还在重试"的配置根源。
    """
    t = edge.timeout_ms
    if edge.propagate_deadline and remaining_budget_ms < INF:
        t = min(t, max(0.0, remaining_budget_ms))
    return t


def backoff_delay_ms(edge: ResolvedEdge, attempt_index: int, rng=None) -> float:
    """第 attempt_index 次尝试失败后，到下一次尝试之间的等待。"""
    d = edge.backoff_ms(attempt_index)
    j = edge.jitter
    if j > 0 and rng is not None:
        d *= 1.0 + rng.uniform(-j, j)
    return max(0.0, d)


def service_duration_ms(svc: Service, rng, slow_factor: float = 1.0) -> float:
    """一次请求占用服务 worker 的自身处理时长（对数正态）。"""
    base = svc.service_ms * slow_factor
    sigma = max(0.0, svc.latency_sigma)
    if sigma > 0 and rng is not None:
        base *= math.exp(rng.gauss(0.0, sigma) - 0.5 * sigma * sigma)
    return max(0.01, base)


def effective_workers(svc: Service) -> int:
    """扣掉背景流量占用的槽位后，留给本链路的并发数。"""
    if svc.service_ms <= 0:
        return max(1, svc.workers)
    bg_util = svc.background_rps * svc.service_ms / (svc.workers * 1000.0)
    bg_util = min(0.95, max(0.0, bg_util))
    slots = int(round(svc.workers * bg_util))
    return max(1, svc.workers - slots)


def effective_capacity_rps(svc: Service) -> float:
    return effective_workers(svc) * 1000.0 / svc.service_ms
