"""按时间步进的推演引擎（用于动画播放）。

每个 tick 依次处理：入口到达 → 退避到期的新尝试 → 处理完成 → 调用超时
→ 预算中止 → 空闲 worker 准入。所有时长按 ``dt`` 量化成整数 tick。

### 建模的关键选择

* **invocation 从开始处理到结束一直占着 worker**（阻塞式客户端）。
  一次调用要等它的所有下游返回才算完，等待期间线程不释放。
  这是"线程池被打满"的真实机制。
* **超时后调用方放弃，但被调用方继续干活**（孤儿请求），除非该边配了
  ``propagate_deadline``——那才会把剩余预算传下去并按时中止。
  孤儿请求 + 重试正是放大倍数的来源。
* 服务队列有上限（worker 的 8 倍），排队溢出即快速拒绝，模拟连接池打满。
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
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


class _Inv:
    __slots__ = (
        "id", "svc", "seq", "k", "q_tick", "start_tick", "compute_done",
        "budget", "abort_tick", "held", "pending", "total", "child_failed",
        "error_injected", "done", "errored", "rejected", "is_root", "spawned",
        "attempt_closed",
    )

    def __init__(self, inv_id, svc, seq, k, is_root=False):
        self.id = inv_id
        self.svc = svc
        self.seq = seq
        self.k = k
        self.q_tick = 0
        self.start_tick = -1
        self.compute_done = -1
        self.budget = INF
        self.abort_tick = INF
        self.held = False
        self.pending = 0
        self.total = 0
        self.child_failed = False
        self.error_injected = False
        self.done = False
        self.errored = False
        self.rejected = False
        self.is_root = is_root
        self.spawned = False
        self.attempt_closed = False


class _Seq:
    """一次调用（可能包含多次重试尝试）。"""

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


class SteppedSim:
    kind = "stepped"

    def __init__(self, topo: Topology, resolved: Dict[str, ResolvedEdge],
                 scenario: Scenario):
        self.topo = topo
        self.resolved = resolved
        self.sc = scenario
        self.dt = max(1.0, scenario.dt_ms)
        fg = build_focus(topo, scenario)
        self.nodes = set(fg.nodes)
        self.edge_ids = [eid for eid in fg.edges]
        self.edge_set = set(self.edge_ids)
        self.out_cache: Dict[str, List[str]] = {
            sid: [e.id for e in topo.out_edges(sid) if e.id in self.edge_set]
            for sid in self.nodes
        }
        self.in_cache: Dict[str, List[str]] = {
            sid: [e.id for e in topo.in_edges(sid) if e.id in self.edge_set]
            for sid in self.nodes
        }
        self.root = scenario.root
        self.n_ticks = int(math.ceil(scenario.duration_ms / self.dt))
        self.frame_every = max(1, int(math.ceil(250.0 / self.dt)))
        self.arrivals = generate_arrivals(scenario)
        self.arrival_buckets: Dict[int, List[float]] = defaultdict(list)
        for at in self.arrivals:
            tk = min(self.n_ticks - 1, int(at / self.dt))
            self.arrival_buckets[tk].append(at)

        self.queues: Dict[str, deque] = {sid: deque() for sid in self.nodes}
        self.busy: Dict[str, int] = {sid: 0 for sid in self.nodes}
        self.workers: Dict[str, int] = {
            sid: effective_workers(topo.services[sid]) for sid in self.nodes
        }
        self.qlimit: Dict[str, int] = {sid: queue_limit(topo, sid) for sid in self.nodes}

        self.ec: Dict[str, EdgeCounter] = {eid: EdgeCounter() for eid in self.edge_ids}
        self.buckets = {
            eid: TokenBucket(self.resolved[eid].retry_budget) for eid in self.edge_ids
        }
        self.sc_stats: Dict[str, ServiceCounter] = {
            sid: ServiceCounter(self.workers[sid]) for sid in self.nodes
        }

        self.retry_buckets: Dict[int, List[Tuple[_Seq, int, _Inv]]] = defaultdict(list)
        self.compute_buckets: Dict[int, List[_Inv]] = defaultdict(list)
        self.deadline_buckets: Dict[int, List[Tuple[_Seq, int, _Inv]]] = defaultdict(list)
        self.abort_buckets: Dict[int, List[_Inv]] = defaultdict(list)

        self._stack: List[Tuple] = []
        self._inv_seq = 0
        self._seq_seq = 0
        self.n_jobs = 0
        self.truncated = False
        self.root_requests = 0
        self.root_errors = 0
        self.root_inflight = 0
        self.frames: List[dict] = []
        self.t = 0
        self._frame_win: Dict[str, int] = defaultdict(int)
        self._frame_timeouts: Dict[str, int] = defaultdict(int)
        self._frame_arrivals = 0

    # ------------------------------------------------------------------
    # tick 工具
    # ------------------------------------------------------------------
    def _ticks(self, ms: float) -> int:
        return max(1, int(math.ceil(ms / self.dt)))

    def _dur_ticks(self, sid: str, T: int) -> int:
        svc = self.topo.services[sid]
        factor = fault_slow(self.sc, sid, T * self.dt)
        if self.sc.deterministic_durations:
            d = svc.service_ms * factor
        else:
            d = service_duration_ms(svc, self.rng, factor)
        return max(1, self._ticks(d))

    # ------------------------------------------------------------------
    # 发起调用 / 入队 / 准入
    # ------------------------------------------------------------------
    def _new_inv(self, svc, seq, k, is_root=False) -> _Inv:
        self._inv_seq += 1
        self.n_jobs += 1
        return _Inv(self._inv_seq, svc, seq, k, is_root)

    def _enqueue(self, inv: _Inv, T: int) -> bool:
        """入队；队列满则快速拒绝并解析失败。返回是否成功入队。"""
        st = self.sc_stats[inv.svc]
        st.arrivals += 1
        self._frame_arrivals += 1
        if len(self.queues[inv.svc]) >= self.qlimit[inv.svc]:
            inv.rejected = True
            st.rejected += 1
            self._push_resolution(inv, False, "rejected", T)
            return False
        inv.q_tick = T
        self.queues[inv.svc].append(inv)
        return True

    def _start_attempt(self, seq: _Seq, k: int, T: int) -> None:
        if seq.resolved:
            return
        edge = self.resolved[seq.edge_id]
        parent = seq.parent
        remaining = (parent.budget - T) * self.dt if parent.budget < INF else INF
        timeout_ms = attempt_timeout_ms(edge, remaining)
        ticks = self._ticks(timeout_ms)
        deadline = T + ticks

        inv = self._new_inv(edge.callee, seq, k)
        inv.budget = deadline if edge.propagate_deadline else INF
        inv.abort_tick = inv.budget
        seq.cur_inv = inv
        seq.k = k

        ec = self.ec[seq.edge_id]
        ec.attempts += 1
        if k == 0:
            ec.initial += 1
            self.buckets[seq.edge_id].on_initial()
        else:
            ec.retries += 1
        self._frame_win[seq.edge_id] += 1

        self.deadline_buckets[deadline].append((seq, k, inv))
        if inv.abort_tick < INF:
            self.abort_buckets[inv.abort_tick].append(inv)

        if self.n_jobs > MAX_JOBS:
            self.truncated = True
            self._push_resolution(inv, False, "timeout", T)
            return
        self._enqueue(inv, T)

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def _push_resolution(self, inv: _Inv, success: bool, kind: str, T: int) -> None:
        if inv.seq is not None:
            self._stack.append(("attempt", inv.seq, inv.k, inv, success, kind, T))
        else:
            self._stack.append(("root", inv, success, T))

    def _drain(self, T: int) -> None:
        while self._stack:
            item = self._stack.pop()
            if item[0] == "attempt":
                _, seq, k, inv, success, kind, tt = item
                self._resolve_attempt(seq, k, inv, success, kind, tt)
            else:
                _, inv, success, tt = item
                self._resolve_root(inv, success)

    def _resolve_attempt(self, seq, k, inv, success, kind, T) -> None:
        if inv.attempt_closed:
            return
        if seq.resolved or seq.cur_inv is not inv or seq.k != k:
            return
        inv.attempt_closed = True
        ec = self.ec[seq.edge_id]
        if kind == "timeout":
            ec.timeouts += 1
            self._frame_timeouts[seq.edge_id] += 1
            # 被调用方还在跑 -> 孤儿
            if not inv.done:
                ec.orphaned += 1
        elif kind == "rejected":
            ec.rejected += 1
        if success:
            ec.successes += 1
            self._resolve_seq(seq, True, T)
            return
        ec.failures += 1
        if k + 1 < seq.n:
            if not self.buckets[seq.edge_id].take():
                ec.retry_denied += 1
                self._resolve_seq(seq, False, T)
                return
            edge = self.resolved[seq.edge_id]
            delay = backoff_delay_ms(edge, k)
            nk = k + 1
            fire = T + self._ticks(delay) if delay > 0 else T + 1
            self.retry_buckets[fire].append((seq, nk, seq.parent))
        else:
            self._resolve_seq(seq, False, T)

    def _resolve_seq(self, seq: _Seq, success: bool, T: int) -> None:
        if seq.resolved:
            return
        seq.resolved = True
        seq.success = success
        parent = seq.parent
        if parent is None:
            return
        if success:
            parent.pending -= 1
        else:
            parent.pending -= 1
            parent.child_failed = True
        if parent.pending == 0 and parent.spawned and not parent.done:
            self._finish_inv(parent, parent.child_failed or parent.error_injected, T)

    def _resolve_root(self, inv: _Inv, success: bool) -> None:
        self.root_inflight -= 1
        if not success:
            self.root_errors += 1

    def _finish_inv(self, inv: _Inv, errored: bool, T: int) -> None:
        if inv.done:
            return
        inv.done = True
        inv.errored = errored
        if inv.held:
            self.busy[inv.svc] -= 1
            inv.held = False
        st = self.sc_stats[inv.svc]
        st.completions += 1
        if errored:
            st.errors += 1
        if inv.is_root:
            self._push_resolution(inv, not errored, "done", T)
        elif inv.seq is not None:
            self._push_resolution(inv, not errored, "done", T)

    def _abort_inv(self, inv: _Inv, T: int) -> None:
        if inv.done:
            return
        # 预算耗尽：放弃自身，孩子的尝试继续（它们有自己的截止）
        self._finish_inv(inv, True, T)

    # ------------------------------------------------------------------
    # 计算完成 -> 派生下游
    # ------------------------------------------------------------------
    def _on_compute_done(self, inv: _Inv, T: int) -> None:
        if inv.done:
            return
        if inv.error_injected:
            self._finish_inv(inv, True, T)
            return
        outs = self.out_cache.get(inv.svc, [])
        if not outs:
            inv.spawned = True
            self._finish_inv(inv, False, T)
            return
        # 按 call_prob 决定每条边是否真的调用；至少保证概率性
        chosen = []
        for eid in outs:
            edge = self.topo.edges[eid]
            p = edge.call_prob
            if p >= 1.0 or self.rng.random() < p:
                chosen.append(eid)
        if not chosen:
            inv.spawned = True
            self._finish_inv(inv, False, T)
            return
        inv.total = len(chosen)
        inv.pending = len(chosen)
        inv.spawned = True
        for eid in chosen:
            re = self.resolved[eid]
            seq = _Seq(self._next_seq_id(), eid, inv, re.attempts)
            self._start_attempt(seq, 0, T)

    def _next_seq_id(self) -> int:
        self._seq_seq += 1
        return self._seq_seq

    # ------------------------------------------------------------------
    # 准入
    # ------------------------------------------------------------------
    def _admit_all(self, T: int) -> None:
        progressed = True
        guard = 0
        while progressed and guard < 4:
            progressed = False
            guard += 1
            for sid in self.nodes:
                q = self.queues[sid]
                while q and self.busy[sid] < self.workers[sid]:
                    inv = q.popleft()
                    if inv.done:
                        continue
                    inv.start_tick = T
                    inv.held = True
                    self.busy[sid] += 1
                    st = self.sc_stats[sid]
                    st.starts += 1
                    if len(q) > st.max_queue:
                        st.max_queue = len(q)
                    # 错误注入
                    if fault_error(self.sc, sid, T * self.dt) > 0:
                        if self.rng.random() < fault_error(self.sc, sid, T * self.dt):
                            inv.error_injected = True
                    dur = self._dur_ticks(sid, T)
                    inv.compute_done = T + dur
                    self.compute_buckets[inv.compute_done].append(inv)
                    progressed = True

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self) -> "SteppedSim":
        import random

        self.rng = random.Random(self.sc.seed * 104729 + 7)
        for T in range(self.n_ticks):
            self.t = T
            # 1. 入口到达
            for at in self.arrival_buckets.get(T, []):
                if self.n_jobs > MAX_JOBS:
                    self.truncated = True
                    break
                inv = self._new_inv(self.root, None, 0, is_root=True)
                self.root_requests += 1
                self.root_inflight += 1
                self._enqueue(inv, T)

            # 2. 退避到期
            for seq, k, parent in self.retry_buckets.pop(T, []):
                self._start_attempt(seq, k, T)

            self._drain(T)

            # 3. 处理完成
            for inv in self.compute_buckets.pop(T, []):
                if not inv.done:
                    self._on_compute_done(inv, T)
            self._drain(T)

            # 4. 调用超时
            for seq, k, inv in self.deadline_buckets.pop(T, []):
                if inv.done or seq.resolved or seq.cur_inv is not inv:
                    continue
                self._push_resolution(inv, False, "timeout", T)
            self._drain(T)

            # 5. 预算中止
            for inv in self.abort_buckets.pop(T, []):
                if not inv.done:
                    self._abort_inv(inv, T)
            self._drain(T)

            # 6. 准入
            self._admit_all(T)

            # 指标
            for sid in self.nodes:
                self.sc_stats[sid].tick(self.busy[sid], len(self.queues[sid]))

            if T % self.frame_every == 0:
                self._snapshot(T)

        self._snapshot(self.n_ticks - 1)
        return self

    # ------------------------------------------------------------------
    def _snapshot(self, T: int) -> None:
        services = {}
        for sid in self.nodes:
            st = self.sc_stats[sid]
            services[sid] = {
                "busy": self.busy[sid],
                "queue": len(self.queues[sid]),
                "workers": self.workers[sid],
                "rejected": st.rejected,
            }
        edges = {}
        for eid in self.edge_ids:
            ec = self.ec[eid]
            edges[eid] = {
                "attempts": ec.attempts,
                "window": self._frame_win.get(eid, 0),
                "timeouts": ec.timeouts,
                "timeout_window": self._frame_timeouts.get(eid, 0),
                "orphaned": ec.orphaned,
            }
        self.frames.append(
            {"t": round(T * self.dt, 1), "services": services, "edges": edges,
             "root_arrivals": self._frame_arrivals}
        )
        self._frame_win = defaultdict(int)
        self._frame_timeouts = defaultdict(int)
        self._frame_arrivals = 0

    def result(self):
        from .scenario import SimResult

        res = SimResult(
            kind="stepped",
            root=self.root,
            duration_ms=self.sc.duration_ms,
            dt_ms=self.dt,
            entry_rps=self.sc.entry_rps,
            root_requests=self.root_requests,
            root_errors=self.root_errors,
            sim_time_ms=self.n_ticks * self.dt,
        )
        for eid, ec in self.ec.items():
            res.edge_stats[eid] = {
                **ec.to_dict(self.root_requests),
                "caller": self.topo.edges[eid].caller,
                "callee": self.topo.edges[eid].callee,
            }
        for sid, st in self.sc_stats.items():
            d = st.to_dict()
            d["capacity_rps"] = (
                self.workers[sid] * 1000.0 / self.topo.services[sid].service_ms
                if self.topo.services[sid].service_ms > 0 else 1e9
            )
            # 饱和：请求真的排起了队（或溢出被拒），而不只是"很忙"。
            # 一个被注入故障而变慢的服务占用率会很高，但只要队列没积压、
            # 没有拒绝，它就没被打垮 —— 这正是"改前被打死 / 改后只是慢"的区别。
            d["node_saturated"] = (
                d["rejected"] > 0
                or d["max_queue"] >= max(8, self.qlimit[sid] * 0.25)
                or d["saturated_steps"] >= max(10, st.steps * 0.25)
            )
            res.service_stats[sid] = d
        res.frames = self.frames
        if self.truncated:
            res.notes.append(f"任务数超过上限 {MAX_JOBS}，推演提前截止")
        return res


def run_stepped(topo, resolved, scenario) -> "SteppedSim":
    sim = SteppedSim(topo, resolved, scenario)
    sim.run()
    return sim
