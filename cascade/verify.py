"""推演结果核对：解析公式 vs 步进推演 vs 逐请求离散事件模拟。

核对分两种工况：

* **收敛工况**（``drain``）：固定请求数、发完排空到静止、无队列饱和、
  确定性时长。此时三者的放大倍数应**精确相等**，容差 1e-6。
* **饱和工况**（正常推演）：有排队与拒绝，三者只要求相对偏差在阈值内
  （默认 15%），因为量化、调度细节会造成差异。

任何一项超出容差即 ``ok=False``，并在报告里给出具体偏差。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .analysis import AnalysisResult, analyze
from .model import Topology
from .resolve import ResolvedEdge, resolve_all
from .scenario import Scenario
from .sim_des import run_des
from .sim_stepped import run_stepped


@dataclass
class DiffRow:
    edge_id: str
    analytical: Optional[float]
    stepped: Optional[float]
    des: Optional[float]
    rel_err_stepped: Optional[float] = None
    rel_err_des: Optional[float] = None
    ok: bool = True

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "analytical": self.analytical,
            "stepped": self.stepped,
            "des": self.des,
            "rel_err_stepped": self.rel_err_stepped,
            "rel_err_des": self.rel_err_des,
            "ok": self.ok,
        }


@dataclass
class Verdict:
    mode: str
    ok: bool
    rows: List[DiffRow]
    max_rel_err: float
    n_root_requests: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "ok": self.ok,
            "max_rel_err": self.max_rel_err,
            "n_root_requests": self.n_root_requests,
            "rows": [r.to_dict() for r in self.rows],
            "notes": self.notes,
        }


def _rel(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom


def compare_amplification(
    topo: Topology,
    resolved: Dict[str, ResolvedEdge],
    analysis: AnalysisResult,
    scenario: Scenario,
    mode: str = "saturated",
    tol: float = 0.15,
    max_edge_per_root: Optional[Dict[str, int]] = None,
) -> Verdict:
    """跑两台模拟器，和解析倍数逐边比对。

    ``max_edge_per_root``：一条边上限倍数（解析值），若为 None 则从 analysis 推。
    """
    stepped = run_stepped(topo, resolved, scenario).result()
    des = run_des(topo, resolved, scenario).result()

    rows: List[DiffRow] = []
    notes: List[str] = []
    for eid in sorted(set(stepped.edge_stats) | set(des.edge_stats)):
        st = stepped.edge_stats.get(eid, {}).get("amplification")
        de = des.edge_stats.get(eid, {}).get("amplification")
        an = None
        if max_edge_per_root is not None:
            an = float(max_edge_per_root.get(eid, 0))
        rows.append(DiffRow(edge_id=eid, analytical=an, stepped=st, des=de))

    max_err = 0.0
    any_bad = False
    for r in rows:
        if r.analytical is None:
            continue
        r.rel_err_stepped = _rel(r.analytical, r.stepped)
        r.rel_err_des = _rel(r.analytical, r.des)
        e = max(
            x for x in (r.rel_err_stepped, r.rel_err_des) if x is not None
        ) if (r.rel_err_stepped is not None or r.rel_err_des is not None) else 0.0
        max_err = max(max_err, e)
        r.ok = e <= tol
        any_bad = any_bad or not r.ok

    if mode == "converged":
        # 收敛工况额外要求两台模拟器彼此一致
        for r in rows:
            if r.stepped is not None and r.des is not None:
                d = _rel(r.stepped, r.des)
                if d is not None and d > 1e-6:
                    r.ok = False
                    any_bad = True
                    max_err = max(max_err, d)

    if stepped.root_requests != des.root_requests:
        notes.append(
            f"两台模拟器入口请求数不同：stepped={stepped.root_requests} "
            f"des={des.root_requests}"
        )
        any_bad = True

    return Verdict(
        mode=mode,
        ok=not any_bad,
        rows=rows,
        max_rel_err=max_err,
        n_root_requests=stepped.root_requests,
        notes=notes,
    )


def amplification_bound(topo: Topology, resolved: Dict[str, ResolvedEdge],
                        chain: List[str]) -> Dict[str, int]:
    """一条链上每条边的理论最坏放大（相对于入口请求）:
    第 i 跳 = Π_{j<=i} attempts_j。
    """
    out: Dict[str, int] = {}
    prod = 1
    for a, b in zip(chain, chain[1:]):
        eid = f"{a}->{b}"
        re = resolved.get(eid)
        if re is None:
            continue
        prod *= re.attempts
        out[eid] = prod
    return out


def converged_check(topo: Topology, chain: List[str], scenario: Scenario,
                    n_requests: int = 40) -> Verdict:
    """收敛工况核对：固定请求 + 排空 + 确定性时长，要求精确相等。

    故障必须从 t=0 起就生效，否则早期请求会正常成功、达不到理论放大。
    """
    from .scenario import FaultSpec

    resolved = resolve_all(topo)
    edges = [f"{a}->{b}" for a, b in zip(chain, chain[1:])]
    faults = [
        FaultSpec(
            service=f.service,
            slow_factor=f.slow_factor,
            error_rate=f.error_rate,
            start_ms=0.0,
            end_ms=None,
        )
        for f in scenario.faults
    ]
    sc = Scenario(
        root=chain[0],
        faults=faults,
        duration_ms=max(scenario.duration_ms, 900000.0),
        entry_rps=0.2,
        seed=scenario.seed,
        deterministic_durations=True,
        services=list(chain),
        edges=edges,
        max_root_requests=n_requests,
        drain=True,
    )
    bound = amplification_bound(topo, resolved, chain)
    return compare_amplification(
        topo, resolved, analyze(topo, resolved),
        sc, mode="converged", tol=1e-6, max_edge_per_root=bound,
    )
