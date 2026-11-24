"""核心数据模型：服务、调用边、三来源配置。

## 关键的语义约定（分析器和模拟器共用）

* 一条调用边的 ``timeout_ms`` 是**单次尝试**的超时，由调用方计时。
  应用层和网格层各有一个计时器，最终生效的是一次尝试里较小的那个。
* 重试分两层：应用层 ``retries`` 与网格层 ``retries``。
  一次上游调用最多打到下游 ``attempts = (1+r_app) × (1+r_mesh)`` 次。
* 两次尝试之间的退避（backoff）只消耗时间、不占用下游 worker 之外的资源；
  最坏情况总耗时 = ``attempts × timeout + backoff_total``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 来源元数据
# ---------------------------------------------------------------------------

SOURCE_PRECEDENCE: Dict[str, int] = {
    "code": 10,
    "config_center": 20,
    "mesh": 30,
    "proposed": 100,
}

SOURCE_LAYER: Dict[str, str] = {
    "code": "app",
    "config_center": "app",
    "mesh": "infra",
    "default": "default",
    "proposed": "proposed",
}

SOURCE_LABEL: Dict[str, str] = {
    "code": "代码注解",
    "config_center": "配置中心",
    "mesh": "服务网格",
    "default": "框架默认",
    "composite": "两层叠加",
    "proposed": "建议方案",
}

LAYER_LABEL: Dict[str, str] = {
    "app": "应用层",
    "infra": "基础设施层",
    "default": "默认值",
    "proposed": "建议",
}

CONFIG_FIELDS = (
    "timeout_ms",
    "retries",
    "backoff_base_ms",
    "backoff_mult",
    "backoff_max_ms",
    "jitter",
    "retry_budget",
    "propagate_deadline",
)

FIELD_LABEL: Dict[str, str] = {
    "timeout_ms": "超时(ms)",
    "retries": "重试次数",
    "backoff_base_ms": "退避基数(ms)",
    "backoff_mult": "退避倍数",
    "backoff_max_ms": "退避上限(ms)",
    "jitter": "抖动",
    "retry_budget": "重试预算",
    "propagate_deadline": "超时传递",
}


@dataclass
class EdgeConfig:
    """单个来源对一条调用边的配置。未设置的字段为 None。"""

    source: str
    #: 同一来源内部的细分优先级（精确匹配 > 服务通配 > 全局），越大越优先
    priority: int = 0
    #: 展示用标签，如 "配置中心/精确匹配"
    label: str = ""
    timeout_ms: Optional[float] = None
    retries: Optional[int] = None
    backoff_base_ms: Optional[float] = None
    backoff_mult: Optional[float] = None
    backoff_max_ms: Optional[float] = None
    jitter: Optional[float] = None
    retry_budget: Optional[float] = None
    propagate_deadline: Optional[bool] = None

    def provided(self) -> Dict[str, object]:
        return {f: getattr(self, f) for f in CONFIG_FIELDS if getattr(self, f) is not None}

    def describe(self) -> str:
        p = self.provided()
        if not p:
            return "（空配置）"
        return "，".join(f"{FIELD_LABEL[f]}={p[f]}" for f in CONFIG_FIELDS if f in p)


@dataclass
class Service:
    id: str
    name: str
    tier: int
    domain: str
    #: 并发处理槽位（连接池 / 线程池大小）；请求排队超过它就排队
    workers: int
    #: 自身处理耗时均值（ms），不含下游
    service_ms: float
    #: 与本次链路无关的背景流量（rps）
    background_rps: float = 0.0
    #: 入口服务对客户端承诺的总预算（ms）；非入口为 None
    slo_ms: Optional[float] = None
    #: 服务处理耗时的离散度（对数正态 sigma），供离散事件模拟使用
    latency_sigma: float = 0.25
    #: 是否并发调用下游（聚合型服务常见）；False 表示串行
    parallel_outgoing: bool = True

    @property
    def capacity_rps(self) -> float:
        return self.workers * 1000.0 / self.service_ms if self.service_ms > 0 else 1e9


@dataclass
class Edge:
    id: str
    caller: str
    callee: str
    #: 同一条边可挂多条规则（配置中心"全局默认" + "精确匹配"等）
    configs: List[EdgeConfig] = field(default_factory=list)
    #: 业务调用比例：每次上游请求有 p 的概率走到这条边（0~1）
    call_prob: float = 1.0

    def add_config(self, cfg: EdgeConfig) -> None:
        self.configs.append(cfg)

    @property
    def key(self) -> Tuple[str, str]:
        return (self.caller, self.callee)


@dataclass
class Topology:
    services: Dict[str, Service]
    edges: Dict[str, Edge]
    seed: int = 0
    #: 入口服务 id 列表
    roots: List[str] = field(default_factory=list)
    #: 场景元数据（故障链路、命名空间、公司名等）
    meta: dict = field(default_factory=dict)

    def out_edges(self, node: str) -> List[Edge]:
        return [e for e in self.edges.values() if e.caller == node]

    def in_edges(self, node: str) -> List[Edge]:
        return [e for e in self.edges.values() if e.callee == node]

    def add_service(self, svc: Service) -> None:
        self.services[svc.id] = svc

    def add_edge(self, edge: Edge) -> None:
        self.edges[edge.id] = edge

    def clone(self) -> "Topology":
        import copy

        return copy.deepcopy(self)


def default_config() -> EdgeConfig:
    """框架内置默认值（没有任何来源配置时生效）。"""
    return EdgeConfig(
        source="default",
        label="框架默认",
        timeout_ms=1000.0,
        retries=0,
        backoff_base_ms=100.0,
        backoff_mult=2.0,
        backoff_max_ms=2000.0,
        jitter=0.2,
        retry_budget=None,
        propagate_deadline=False,
    )
