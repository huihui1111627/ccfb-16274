"""逐个请求的离散事件模拟（DES），用于核对步进推演的放大倍数。

与 :mod:`cascade.sim_stepped` 的区别：

* 时间是**连续精确**的（event heap），没有 tick 量化；
* 请求对象显式排队、占用/释放 worker；
* 调用超时、退避重试、预算中止都是独立事件，按时间先后处理；
* 共享同一份到达序列（:func:`cascade.sim_common.generate_arrivals`）与
  同一套策略函数（:mod:`cascade.policy`），因此两者的差异只来自
  调度与量化方式，而不是对配置的理解不同。

对齐验证时使用 ``deterministic_durations=True`` 并保持低负载，使两边都
落在"无队列饱和"区间，此时每个入口请求的放大倍数应稳定等于
``Π attempts``，可与解析结果三方对照。
"""
from __future__ import annotations

import heapq
import random
from collections import deque
from typing import Dict, List, Optional, Tuple

from .model import Topology
from .policy import attempt_timeout_ms, backoff_delay_ms, effective_workers, service_duration_ms
from .resolve import ResolvedEdge
from .scenario import Scenario, build_focus
from .sim_common import (
    MAX_JOBS,
    EdgeCounter,
    ServiceCounter,
    TokenBucket,
    fault_error,
    fault_slow,
    generate_arrivals,
    queue_limit,
)

INF = float("inf")

# 事件类型（数字越小越先处理，同一时刻的稳定排序）
EV_ARRIVE = 0
EV_START = 1
EV_FINISH = 2
EV_TIMEOUT = 3
EV_RETRY = 4
EV_ABORT = 5


class _Inv:
    __slots__ = (
        "id", "svc", "seq", "k", "arrive", "start", "finish", "budget",
        "abort_at", "held", "pending", "children_done", "child_failed",
        "error_injected", "resolved", "errored", "rejected", "is_root",
        "spawned", "dead", "attempt_closed",
    )

    def __init__(self, inv_id, svc, seq, k, is_root=False):
        self.id = inv_id
        self.svc = svc
        self.seq = seq
        self.k = k
        self.arrive = 0.0
        self.start = -1.0
        self.finish = INF
        self.budget = INF
        self.abort_at = INF
        self.held = False
        self.pending = 0
        self.children_done = 0
        self.child_failed = False
        self.error_injected = False
        self.resolved = False
        self.errored = False
        self.rejected = False
        self.is_root = is_root
        self.spawned = False
        self.dead = False
        self.attempt_closed = False


class _Seq:
    __slots__ = ("id", "edge_id", "parent", "n", "k", "cur_inv", "resolved", "success")

    def __init__(self, seq_id, edge_id, parent, n):
        self.id = seq_id
        self.edge_id = edge_id
        self.parent = parent
        self.n = n
        self.k = 0
        self.cur_inv: Optional[_Inv] = None
        self.resolved = False
        self.success = False


class DesSim:
    kind = "des"

    def __init__(self, topo: Topology, resolved: Dict[str, ResolvedEdge],
                 scenario: Scenario):
        self.topo = topo
        self.resolved = resolved
        self.sc = scenario
        fg = build_focus(topo, scenario)
        self.nodes = set(fg.nodes)
        self.edge_ids = list(fg.edges)
        self.edge_set = set(self.edge_ids)
        self.out_cache = {
            sid: [e.id for e in topo.out_edges(sid) if e.id in self.edge_set]
            for sid in self.nodes
        }
        self.root = scenario.root
        self.workers = {
            sid: effective_workers(topo.services[sid]) for sid in self.nodes
        }
        self.qlimit = {sid: queue_limit(topo, sid) for sid in self.nodes}
        self.busy = {sid: 0 for sid in self.nodes}

        self.ec = {eid: EdgeCounter() for eid in self.edge_ids}
        self.buckets = {
            eid: TokenBucket(self.resolved[eid].retry_budget) for eid in self.edge_ids
        }
        self.sc_stats = {sid: ServiceCounter(self.workers[sid]) for sid in self.nodes}

        self.heap: List[Tuple] = []
        self._ev = 0
        self._inv_seq = 0
        self._seq_seq = 0
        self.n_jobs = 0
        self.truncated = False
        self.root_requests = 0
        self.root_errors = 0
        self.now = 0.0
        self.done_time = 0.0
        self._arrivals = generate_arrivals(scenario)
        self._arr_idx = 0
        self._util_samples: Dict[str, float] = {sid: 0.0 for sid in self.nodes}
        self._last_sample = 0.0
        self._sample_period = 25.0
        self._next_sample = 0.0
        self._inflight_seqs = 0

    # ------------------------------------------------------------------
    def _push(self, t: float, kind: int, payload) -> None:
        self._ev += 1
        heapq.heappush(self.heap, (t, kind, self._ev, payload))

    def _next_seq_id(self) -> int:
        self._seq_seq += 1
        return self._seq_seq

    def _new_inv(self, svc, seq, k, is_root=False) -> _Inv:
        self._inv_seq += 1
        self.n_jobs += 1
        return _Inv(self._inv_seq, svc, seq, k, is_root)

    def _rng(self, inv_id: int, salt: int) -> random.Random:
        return random.Random(self.sc.seed * 1000003 + inv_id * 97 + salt)

    # ------------------------------------------------------------------
    def _schedule_arrivals(self) -> None:
        for at in self._arrivals:
            self._push(at, EV_ARRIVE, None)

    def _on_arrive(self, t: float) -> None:
        if self.n_jobs > MAX_JOBS:
            self.truncated = True
            return
        inv = self._new_inv(self.root, None, 0, is_root=True)
        self.root_requests += 1
        inv.arrive = t
        self._offer(inv, t)

    # ------------------------------------------------------------------
    def _offer(self, inv: _Inv, t: float) -> None:
        """入队或立即开始。"""
        st = self.sc_stats[inv.svc]
        st.arrivals += 1
        if self.busy[inv.svc] < self.workers[inv.svc]:
            self._start(inv, t)
        else:
            if len(self._qlens[inv.svc]) >= self.qlimit[inv.svc]:
                st.rejected += 1
                inv.rejected = True
                self._push_resolution(inv, False, "rejected", t)
                return
            self._qlens[inv.svc].append(inv)
            if len(self._qlens[inv.svc]) > st.max_queue:
                st.max_queue = len(self._qlens[inv.svc])

    def _start(self, inv: _Inv, t: float) -> None:
        inv.start = t
        inv.held = True
        self.busy[inv.svc] += 1
        st = self.sc_stats[inv.svc]
        st.starts += 1
        if self.busy[inv.svc] > st.max_busy:
            st.max_busy = self.busy[inv.svc]
        # 错误注入按精确时刻
        p = fault_error(self.sc, inv.svc, t)
        if p > 0:
            rng = self._rng(inv.id, 1)
            if rng.random() < p:
                inv.error_injected = True
        svc = self.topo.services[inv.svc]
        factor = fault_slow(self.sc, inv.svc, t)
        if self.sc.deterministic_durations:
            dur = svc.service_ms * factor
        else:
            rng = self._rng(inv.id, 2)
            dur = service_duration_ms(svc, rng, factor)
        inv.finish = t + max(0.01, dur)
        self._push(inv.finish, EV_FINISH, inv)

    def _try_admit(self, sid: str, t: float) -> None:
        q = self._qlens[sid]
        while q and self.busy[sid] < self.workers[sid]:
            inv = q.popleft()
            if inv.dead or inv.resolved:
                continue
            self._start(inv, t)

    # ------------------------------------------------------------------
    def _push_resolution(self, inv: _Inv, success: bool, kind: str, t: float) -> None:
        if inv.seq is not None:
            self._push(t, EV_ABORT + 1, ("attempt", inv.seq, inv.k, inv, success, kind))
        else:
            self._push(t, EV_ABORT + 1, ("root", inv, success))

    def _resolve_attempt(self, seq: _Seq, k: int, inv: _Inv, success: bool,
                         kind: str, t: float) -> None:
        # 每次尝试只能结算一次：既可能先到时超时结算，也可能处理完成结算。
        # 没有这个闸门，同一次尝试会被结算两次（超时 + 完成），各推一次重试，
        # 放大倍数就会突破 attempts 上限。
        if inv.attempt_closed:
            return
        if seq.resolved or seq.cur_inv is not inv or seq.k != k:
            return
        inv.attempt_closed = True
        ec = self.ec[seq.edge_id]
        if kind == "timeout":
            ec.timeouts += 1
            if not (inv.resolved and inv.finish <= t):
                ec.orphaned += 1
        elif kind == "rejected":
            ec.rejected += 1
        if success:
            ec.successes += 1
            self._resolve_seq(seq, True, t)
            return
        ec.failures += 1
        if k + 1 < seq.n:
            bucket = self.buckets[seq.edge_id]
            if not bucket.take():
                ec.retry_denied += 1
                self._resolve_seq(seq, False, t)
                return
            edge = self.resolved[seq.edge_id]
            rng = self._rng(seq.id * 31 + k, 3)
            delay = backoff_delay_ms(edge, k, rng)
            self._push(t + delay, EV_RETRY, (seq, k + 1))
        else:
            self._resolve_seq(seq, False, t)

    def _resolve_seq(self, seq: _Seq, success: bool, t: float) -> None:
        if seq.resolved:
            return
        seq.resolved = True
        seq.success = success
        self._inflight_seqs -= 1
        parent = seq.parent
        if parent is None:
            return
        parent.pending -= 1
        if not success:
            parent.child_failed = True
        if parent.pending == 0 and parent.spawned and not parent.resolved and not parent.dead:
            self._finish(parent, parent.child_failed or parent.error_injected, t)

    def _resolve_root(self, inv: _Inv, success: bool) -> None:
        if not success:
            self.root_errors += 1

    def _finish(self, inv: _Inv, errored: bool, t: float) -> None:
        if inv.resolved:
            return
        inv.resolved = True
        inv.errored = errored
        inv.dead = True
        if inv.held:
            self.busy[inv.svc] -= 1
            inv.held = False
            self._try_admit(inv.svc, t)
        st = self.sc_stats[inv.svc]
        st.completions += 1
        if errored:
            st.errors += 1
        if inv.is_root:
            self._push_resolution(inv, not errored, "done", t)
        elif inv.seq is not None:
            self._push_resolution(inv, not errored, "done", t)

    def _abort(self, inv: _Inv, t: float) -> None:
        if inv.resolved:
            return
        self._finish(inv, True, t)

    # ------------------------------------------------------------------
    def _on_finish(self, inv: _Inv, t: float) -> None:
        if inv.resolved:
            return
        # 计算完成：释放 worker（无论下游是否还在跑）
        if inv.error_injected:
            self._finish(inv, True, t)
            return
        outs = self.out_cache.get(inv.svc, [])
        chosen = []
        for eid in outs:
            p = self.topo.edges[eid].call_prob
            if p >= 1.0:
                chosen.append(eid)
            else:
                rng = self._rng(inv.id, 10 + len(chosen))
                if rng.random() < p:
                    chosen.append(eid)
        if not chosen:
            inv.spawned = True
            self._finish(inv, False, t)
            return
        inv.spawned = True
        inv.pending = len(chosen)
        for i, eid in enumerate(chosen):
            seq = _Seq(self._next_seq_id(), eid, inv, self.resolved[eid].attempts)
            self._inflight_seqs += 1
            self._start_attempt(seq, 0, t, i)

    def _start_attempt(self, seq: _Seq, k: int, t: float, salt: int = 0) -> None:
        if seq.resolved:
            return
        edge = self.resolved[seq.edge_id]
        parent = seq.parent
        remaining = INF
        if parent is not None and parent.budget < INF:
            remaining = parent.budget - t
        timeout_ms = attempt_timeout_ms(edge, remaining)
        deadline = t + timeout_ms

        inv = self._new_inv(edge.callee, seq, k)
        inv.arrive = t
        inv.budget = deadline if edge.propagate_deadline else INF
        inv.abort_at = inv.budget
        seq.cur_inv = inv
        seq.k = k

        ec = self.ec[seq.edge_id]
        ec.attempts += 1
        if k == 0:
            ec.initial += 1
            self.buckets[seq.edge_id].on_initial()
        else:
            ec.retries += 1

        self._push(deadline, EV_TIMEOUT, (seq, k, inv))
        if inv.abort_at < INF:
            self._push(inv.abort_at, EV_ABORT, inv)
        if self.n_jobs > MAX_JOBS:
            self.truncated = True
            self._push_resolution(inv, False, "timeout", t)
            return
        self._offer(inv, t)

    # ------------------------------------------------------------------
    def _sample(self, t: float) -> None:
        dt = t - self._last_sample
        if dt <= 0:
            return
        for sid in self.nodes:
            util = self.busy[sid] / max(1, self.workers[sid])
            self._util_samples[sid] += util * dt
            self.sc_stats[sid].util_sum += util * dt
        self._last_sample = t
        self._total_dt = getattr(self, "_total_dt", 0.0) + dt

    def run(self) -> "DesSim":
        self._qlens = {sid: deque() for sid in self.nodes}
        self._schedule_arrivals()
        self._last_sample = 0.0
        self._total_dt = 0.0
        horizon = self.sc.duration_ms
        drain = self.sc.drain
        tick = 0
        while self.heap:
            t = self.heap[0][0]
            if t > horizon and not (drain and self._in_flight()):
                break
            _, kind, _, payload = heapq.heappop(self.heap)
            if t > self._last_sample + self._sample_period:
                self._sample(t)
            self.now = t
            self.done_time = max(self.done_time, t)
            self._handle(kind, payload, t)
            tick += 1
            if tick % 20000 == 0:
                self._drain_dead()
        self._sample(min(horizon, max(self._last_sample, self.now)))
        return self

    def _drain_dead(self) -> None:
        # 清理内存中已解析的对象引用（队列里跳过即可）
        pass

    def _in_flight(self) -> bool:
        """是否还有未结算的调用序列（用于排空到静止）。"""
        if any(self.busy.values()) or any(self._qlens.values()):
            return True
        if self._inflight_seqs:
            return True
        return False

    def _handle(self, kind: int, payload, t: float) -> None:
        if kind == EV_ARRIVE:
            self._on_arrive(t)
        elif kind == EV_FINISH:
            self._on_finish(payload, t)
        elif kind == EV_TIMEOUT:
            seq, k, inv = payload
            if inv.resolved or seq.resolved or seq.cur_inv is not inv:
                return
            self._push_resolution(inv, False, "timeout", t)
        elif kind == EV_RETRY:
            seq, k = payload
            self._start_attempt(seq, k, t)
        elif kind == EV_ABORT:
            self._abort(payload, t)
        elif kind == EV_ABORT + 1:
            if payload[0] == "attempt":
                _, seq, k, inv, success, sub = payload
                self._resolve_attempt(seq, k, inv, success, sub, t)
            else:
                _, inv, success = payload
                self._resolve_root(inv, success)

    # ------------------------------------------------------------------
    def result(self):
        from .scenario import SimResult

        total = max(1e-9, getattr(self, "_total_dt", 0.0))
        res = SimResult(
            kind="des",
            root=self.root,
            duration_ms=self.sc.duration_ms,
            dt_ms=0.0,
            entry_rps=self.sc.entry_rps,
            root_requests=self.root_requests,
            root_errors=self.root_errors,
            sim_time_ms=min(self.sc.duration_ms, self.done_time),
        )
        for eid, ec in self.ec.items():
            res.edge_stats[eid] = {
                **ec.to_dict(self.root_requests),
                "caller": self.topo.edges[eid].caller,
                "callee": self.topo.edges[eid].callee,
            }
        for sid, st in self.sc_stats.items():
            d = st.to_dict()
            d["avg_util"] = st.util_sum / total
            d["capacity_rps"] = (
                self.workers[sid] * 1000.0 / self.topo.services[sid].service_ms
                if self.topo.services[sid].service_ms > 0 else 1e9
            )
            d["node_saturated"] = d["rejected"] > 0 or d["peak_util"] >= 0.95
            res.service_stats[sid] = d
        if self.truncated:
            res.notes.append(f"任务数超过上限 {MAX_JOBS}，推演提前截止")
        return res


def run_des(topo, resolved, scenario) -> "DesSim":
    sim = DesSim(topo, resolved, scenario)
    sim.run()
    return sim
