"""推演场景定义：故障注入、聚焦子图、结果结构。

推演只关注"从某个入口出发、经过故障服务"的那部分拓扑，避免把 300 个服务
全部塞进模拟。聚焦子图包含：

* 入口到故障服务的所有可达路径（上行链路）；
* 故障服务下游的所有服务（用来观察故障如何继续向下传导）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from .model import Topology



@dataclass
class FaultSpec:
    """对一个服务的故障注入。"""

    service: str
    #: 自身处理耗时放大倍数（1.0 = 正常）
    slow_factor: float = 1.0
    #: 每次处理返回错误的概率 0~1
    error_rate: float = 0.0
    start_ms: float = 0.0
    end_ms: Optional[float] = None

    def active_at(self, t_ms: float) -> bool:
        if t_ms < self.start_ms:
            return False
        if self.end_ms is not None and t_ms >= self.end_ms:
            return False
        return True


@dataclass
class Scenario:
    root: str
    faults: List[FaultSpec]
    duration_ms: float = 60000.0
    dt_ms: float = 25.0
    entry_rps: float = 30.0
    seed: int = 7
    #: 用固定处理时长（取均值），便于与解析结果精确对齐
    deterministic_durations: bool = False
    #: 只发这么多入口请求，之后把在途请求排空到静止（用于核对倍数）
    max_root_requests: Optional[int] = None
    #: 到达发完后继续推进直到没有在途请求（配合 max_root_requests 使用）
    drain: bool = False
    #: 只保留这些边（None 表示按 focus 自动推导）
    edges: Optional[List[str]] = None
    services: Optional[List[str]] = None

    def fault_for(self, service: str) -> Optional[FaultSpec]:
        for f in self.faults:
            if f.service == service:
                return f
        return None


# ---------------------------------------------------------------------------
# 聚焦子图
# ---------------------------------------------------------------------------

def _forward(topo: Topology, start: Sequence[str]) -> Set[str]:
    seen: Set[str] = set()
    stack = [s for s in start if s in topo.services]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        for e in topo.out_edges(n):
            stack.append(e.callee)
    return seen


def _backward(topo: Topology, start: Sequence[str]) -> Set[str]:
    seen: Set[str] = set()
    stack = [s for s in start if s in topo.services]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        for e in topo.in_edges(n):
            stack.append(e.caller)
    return seen


def focus_nodes(topo: Topology, root: str, faults: Sequence[FaultSpec]) -> Set[str]:
    fault_svcs = [f.service for f in faults if f.service in topo.services]
    upstream = _forward(topo, [root]) & _backward(topo, fault_svcs)
    downstream = _forward(topo, fault_svcs)
    keep = upstream | downstream | {root}
    return keep


def pick_root_for(topo: Topology, service: str) -> str:
    """给一个服务挑一个真正能"打进来"的入口。

    优先选**最短路径最长**的那个入口（即最外层的真实入口），这样聚焦子图
    能覆盖从边缘网关一路到故障服务的完整链路——只选"最近的入口"会把中间层
    当成入口，丢掉上游的放大。排除服务自己。
    """
    ancestors = _backward(topo, [service])
    candidates = [r for r in topo.roots if r in ancestors and r != service]
    if not candidates:
        ups = _backward(topo, [service])
        candidates = [r for r in topo.roots if r in ups and r != service]
    if candidates:
        best = None
        best_len = 0
        for r in candidates:
            d = shortest_hops(topo, r, service)
            if d is not None and d > best_len:
                best, best_len = r, d
        if best:
            return best
        return candidates[0]
    if service in topo.roots:
        return service
    return topo.roots[0] if topo.roots else service


def shortest_hops(topo: Topology, src: str, dst: str) -> Optional[int]:
    from collections import deque

    q = deque([(src, 0)])
    seen = {src}
    while q:
        n, d = q.popleft()
        if n == dst:
            return d
        for e in topo.out_edges(n):
            if e.callee not in seen:
                seen.add(e.callee)
                q.append((e.callee, d + 1))
    return None


@dataclass
class FocusGraph:
    nodes: Set[str]
    edges: List[str]
    root: str

    def to_dict(self) -> dict:
        return {"nodes": sorted(self.nodes), "edges": self.edges, "root": self.root}


def build_focus(topo: Topology, scenario: Scenario) -> FocusGraph:
    if scenario.services is not None:
        nodes = set(scenario.services)
        nodes.add(scenario.root)
        edges = [
            eid
            for eid, e in topo.edges.items()
            if e.caller in nodes and e.callee in nodes
        ]
        if scenario.edges is not None:
            edges = [e for e in edges if e in scenario.edges]
        return FocusGraph(nodes=nodes, edges=edges, root=scenario.root)

    nodes = focus_nodes(topo, scenario.root, scenario.faults)
    edges = [
        eid for eid, e in topo.edges.items() if e.caller in nodes and e.callee in nodes
    ]
    if scenario.edges is not None:
        edges = [e for e in edges if e in scenario.edges]
    return FocusGraph(nodes=nodes, edges=edges, root=scenario.root)


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------

@dataclass
class SimResult:
    kind: str
    root: str
    duration_ms: float
    dt_ms: float
    entry_rps: float
    root_requests: int = 0
    root_errors: int = 0
    #: edge_id -> 计数
    edge_stats: Dict[str, dict] = field(default_factory=dict)
    #: service_id -> 计数/峰值
    service_stats: Dict[str, dict] = field(default_factory=dict)
    #: 逐帧时间序列（播放用）
    frames: List[dict] = field(default_factory=list)
    #: 实际模拟到的最大时间
    sim_time_ms: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def amplification(self) -> Dict[str, float]:
        denom = max(1, self.root_requests)
        return {
            eid: st["attempts"] / denom for eid, st in self.edge_stats.items()
        }

    def to_dict(self, include_frames: bool = True) -> dict:
        d = {
            "kind": self.kind,
            "root": self.root,
            "duration_ms": self.duration_ms,
            "dt_ms": self.dt_ms,
            "entry_rps": self.entry_rps,
            "root_requests": self.root_requests,
            "root_errors": self.root_errors,
            "root_error_rate": (
                self.root_errors / self.root_requests if self.root_requests else 0.0
            ),
            "edge_stats": self.edge_stats,
            "service_stats": self.service_stats,
            "sim_time_ms": self.sim_time_ms,
            "notes": self.notes,
        }
        if include_frames:
            d["frames"] = self.frames
        return d


def new_edge_stat() -> dict:
    return {
        "attempts": 0,
        "initial": 0,
        "retries": 0,
        "timeouts": 0,
        "successes": 0,
        "failures": 0,
        "orphaned": 0,
        "amplification": 0.0,
    }


def new_service_stat(workers: int) -> dict:
    return {
        "workers": workers,
        "arrivals": 0,
        "starts": 0,
        "completions": 0,
        "errors": 0,
        "max_queue": 0,
        "max_busy": 0,
        "saturated_steps": 0,
        "peak_util": 0.0,
        "avg_util": 0.0,
        "_util_sum": 0.0,
        "_steps": 0,
    }


def finalize_stats(result: SimResult, topo: Topology) -> None:
    denom = max(1, result.root_requests)
    for eid, st in result.edge_stats.items():
        st["amplification"] = st["attempts"] / denom
    for sid, st in result.service_stats.items():
        steps = max(1, st["_steps"])
        st["avg_util"] = st["_util_sum"] / steps
        st["peak_util"] = st["max_busy"] / max(1, st["workers"])
        st.pop("_util_sum", None)
        st.pop("_steps", None)


def scenario_from_incident(topo: Topology, **overrides) -> Scenario:
    meta = topo.meta.get("incident", {})
    fault = meta.get("slow_service")
    root = meta.get("root") or (topo.roots[0] if topo.roots else None)
    chain = meta.get("chain") or []
    # inventory 基线 1.2s、容量 16rps；变慢 2.2 倍（2.6s）就超过 order 给的 2s 超时，
    # 每次调用超时 -> order 重试 2 次（3 次尝试）-> bff 再叠 8 次尝试。
    # 4rps 的入口被放大到 70+ rps，远超 inventory 容量，从"有点慢"被打死。
    faults = (
        [FaultSpec(service=fault, slow_factor=2.2, start_ms=5000.0)]
        if fault
        else []
    )
    # 复现只看这条事故链本身，避免全公司其它随机链路串扰
    edges = [f"{a}->{b}" for a, b in zip(chain, chain[1:])]
    kw = dict(
        root=root,
        faults=faults,
        duration_ms=60000.0,
        dt_ms=25.0,
        entry_rps=4.0,
        seed=7,
        deterministic_durations=False,
        services=list(chain) if chain else None,
        edges=edges if edges else None,
    )
    kw.update(overrides)
    return Scenario(**kw)
