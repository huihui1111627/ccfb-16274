"""三来源配置合并。

## 覆盖顺序（为什么不是简单的"后者覆盖前者"）

真实系统里这三份配置分属两个**运行时层**：

* **应用层 (app)** —— ``code``（客户端注解/硬编码兜底）与 ``config_center``
  （配置中心动态下发）。两者都在客户端进程里被解释，是同一个客户端对象的
  不同输入。语义：**逐字段覆盖**，``config_center`` 优先于 ``code`` ——
  配置中心是运维可以随时调整的那份，代码里的只是兜底。

* **基础设施层 (infra)** —— ``mesh``（Sidecar 的 route / destination 规则）。
  它在客户端进程**之外**执行：请求发出后，由本地 sidecar 决定何时放弃、
  要不要再试一次。所以它不是"覆盖"应用层，而是**叠加**在应用层之上。

由此得到两条合并规则：

1. **层内覆盖**：同一层若有精确匹配、服务通配、全局默认多条规则，精确者胜；
   ``app`` 层内 ``config_center`` 胜过 ``code``（同为精确度时）。
2. **跨层叠加**：
   * 超时取 **min** —— 两层是串联的两个计时器，谁先放弃谁生效。
   * 重试 **相乘** —— 应用层重试 M 次、网格层重试 N 次时，下游一次业务调用
     最多被打 ``(1+M) × (1+N)`` 次。这正是事故里放大十几倍的来源。

最后，任何一层都没设置的字段由 :func:`cascade.model.default_config` 补齐。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .model import (
    CONFIG_FIELDS,
    SOURCE_LABEL,
    SOURCE_PRECEDENCE,
    Edge,
    EdgeConfig,
    Topology,
    default_config,
)


@dataclass
class FieldOrigin:
    value: object
    source: str
    label: str = ""

    @property
    def display(self) -> str:
        return self.label or SOURCE_LABEL.get(self.source, self.source)


@dataclass
class ResolvedEdge:
    """一条调用边最终生效的配置，以及每个字段的来源。"""

    edge_id: str
    caller: str
    callee: str
    values: Dict[str, object]
    origins: Dict[str, FieldOrigin]
    #: 应用层单独解析结果 field -> (value, source, label)
    app: Dict[str, tuple]
    #: 基础设施层单独解析结果
    infra: Dict[str, tuple]

    @property
    def timeout_ms(self) -> float:
        return float(self.values["timeout_ms"])  # type: ignore[arg-type]

    @property
    def app_retries(self) -> int:
        v = self.app.get("retries")
        return 0 if v is None else int(v[0])

    @property
    def mesh_retries(self) -> int:
        v = self.infra.get("retries")
        return 0 if v is None else int(v[0])

    @property
    def attempts(self) -> int:
        """一次上游调用最多打到下游的次数。"""
        o = self.origins.get("retries")
        if o is not None and o.source == "proposed":
            return int(self.values["retries"]) + 1  # type: ignore[arg-type]
        return (1 + self.app_retries) * (1 + self.mesh_retries)

    @property
    def effective_retries(self) -> int:
        return self.attempts - 1

    @property
    def retry_budget(self) -> Optional[float]:
        return self.values.get("retry_budget")  # type: ignore[return-value]

    @property
    def jitter(self) -> float:
        return float(self.values.get("jitter") or 0.0)  # type: ignore[arg-type]

    @property
    def propagate_deadline(self) -> bool:
        return bool(self.values.get("propagate_deadline"))

    def num(self, f: str) -> float:
        return float(self.values.get(f) or 0.0)  # type: ignore[arg-type]

    def explain(self, f: str) -> str:
        o = self.origins.get(f)
        return "未设置" if o is None else f"{o.value} ← {o.display}"

    def backoff_ms(self, attempt_index: int) -> float:
        """第 attempt_index 次失败之后、下一次尝试前的退避时长（不含抖动）。

        attempt_index 从 0 开始：第 0 次尝试失败后 -> base。
        """
        base = self.num("backoff_base_ms") or 0.0
        mult = self.num("backoff_mult") or 1.0
        cap = self.num("backoff_max_ms")
        d = base * (mult ** attempt_index)
        if cap > 0:
            d = min(d, cap)
        return d

    def backoff_total_ms(self) -> float:
        """如果所有 attempts 次尝试都失败，累计退避时长（不含抖动）。"""
        return sum(self.backoff_ms(i) for i in range(self.attempts - 1))

    def worst_case_ms(self) -> float:
        """最坏情况：每次尝试都吃满超时 + 全部退避。"""
        return self.attempts * self.timeout_ms + self.backoff_total_ms()


# ---------------------------------------------------------------------------
# 单层解析
# ---------------------------------------------------------------------------

def _resolve_layer(cfgs: List[EdgeConfig], sources: List[str]) -> Dict[str, tuple]:
    """在给定来源集合内逐字段覆盖：按 (来源优先级, 规则优先级) 从高到低。"""
    ordered = [c for c in cfgs if c.source in sources]
    ordered.sort(
        key=lambda c: (SOURCE_PRECEDENCE.get(c.source, 0), c.priority),
        reverse=True,
    )
    out: Dict[str, tuple] = {}
    for c in ordered:
        for f, v in c.provided().items():
            if f not in out:  # 先到的是更高优先级，不被覆盖
                out[f] = (v, c.source, c.label)
    return out


def resolve_edge(edge: Edge) -> ResolvedEdge:
    cfgs = list(edge.configs)
    # "建议方案" 是人工覆盖层：一旦存在，它对该边所有显式字段拥有最高决定权。
    # 它本身已经是按最终语义写好的（retries 即等价重试、timeout 即最终超时），
    # 因此不再与应用/网格层做 min/相乘，而是直接采用。
    proposed = _resolve_layer(cfgs, ["proposed"])
    app = _resolve_layer(cfgs, ["code", "config_center"])
    infra = _resolve_layer(cfgs, ["mesh"])
    dflt = default_config()

    values: Dict[str, object] = {}
    origins: Dict[str, FieldOrigin] = {}

    # ---- timeout_ms: proposed 直接覆盖；否则层内覆盖、跨层取 min ----
    if "timeout_ms" in proposed:
        v = float(proposed["timeout_ms"][0])
        values["timeout_ms"] = v
        origins["timeout_ms"] = FieldOrigin(v, "proposed", "建议方案")
    else:
        candidates: List[Tuple[float, str]] = []
        if "timeout_ms" in app:
            candidates.append((float(app["timeout_ms"][0]), app["timeout_ms"][1]))
        if "timeout_ms" in infra:
            candidates.append((float(infra["timeout_ms"][0]), infra["timeout_ms"][1]))
        if candidates:
            val, src = min(candidates, key=lambda x: x[0])
            values["timeout_ms"] = val
            origins["timeout_ms"] = FieldOrigin(val, src)
            if len(candidates) > 1:
                origins["timeout_ms"].label = "min(" + " , ".join(
                    f"{SOURCE_LABEL.get(s, s)}={v:g}" for v, s in candidates
                ) + ")"
            else:
                origins["timeout_ms"].label = SOURCE_LABEL.get(src, src)
        else:
            values["timeout_ms"] = dflt.timeout_ms
            origins["timeout_ms"] = FieldOrigin(dflt.timeout_ms, "default")  # type: ignore[arg-type]

    # ---- retries: proposed 直接覆盖；否则两层相乘 ----
    if "retries" in proposed:
        eff = int(proposed["retries"][0])
        values["retries"] = eff
        origins["retries"] = FieldOrigin(eff, "proposed", "建议方案（等价重试次数）")
    else:
        a_r = app.get("retries")
        i_r = infra.get("retries")
        if a_r is None and i_r is None:
            values["retries"] = int(dflt.retries or 0)
            origins["retries"] = FieldOrigin(values["retries"], "default")
            origins["retries"].label = "三层都没配 → 框架默认 0 次"
        else:
            ar = 0 if a_r is None else int(a_r[0])
            ir = 0 if i_r is None else int(i_r[0])
            eff = (1 + ar) * (1 + ir) - 1
            values["retries"] = eff
            origins["retries"] = FieldOrigin(eff, "composite")
            app_desc = f"应用层 {ar} 次" + (
                f"（{SOURCE_LABEL.get(a_r[1], a_r[1])}）" if a_r else "（未配）"
            )
            mesh_desc = f"网格层 {ir} 次" + (
                f"（{SOURCE_LABEL.get(i_r[1], i_r[1])}）" if i_r else "（未配）"
            )
            origins["retries"].label = (
                f"({ar + 1}) × ({ir + 1}) = {1 + ar}×{1 + ir} 次尝试 "
                f"→ 等价重试 {eff} 次（{app_desc}；{mesh_desc}）"
            )

    # ---- 其余字段：proposed > app > infra > 默认 ----
    for f in CONFIG_FIELDS:
        if f in ("timeout_ms", "retries"):
            continue
        if f in proposed:
            v = proposed[f][0]
            values[f] = v
            origins[f] = FieldOrigin(v, "proposed", "建议方案")
        elif f in app:
            v, s, _l = app[f]
            values[f] = v
            origins[f] = FieldOrigin(v, s, label=_src_label(s, cfgs, f))
        elif f in infra:
            v, s, _l = infra[f]
            values[f] = v
            origins[f] = FieldOrigin(v, s, label=_src_label(s, cfgs, f))
        else:
            dv = getattr(dflt, f)
            values[f] = dv
            origins[f] = FieldOrigin(dv, "default")

    return ResolvedEdge(
        edge_id=edge.id,
        caller=edge.caller,
        callee=edge.callee,
        values=values,
        origins=origins,
        app=app,
        infra=infra,
    )


def _src_label(source: str, cfgs: List[EdgeConfig], field_name: str) -> str:
    """找到提供该字段的规则，用它的 label 做展示；没有则用来源名。"""
    for c in cfgs:
        if c.source == source and getattr(c, field_name) is not None and c.label:
            return c.label
    return SOURCE_LABEL.get(source, source)


def resolve_all(topo: Topology) -> Dict[str, ResolvedEdge]:
    return {eid: resolve_edge(e) for eid, e in topo.edges.items()}
