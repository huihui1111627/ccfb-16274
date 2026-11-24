"""模拟一家"乱"的公司：300+ 服务、多层调用、三来源配置互相打架。

生成是**确定性**的（固定 seed），所以每次启动看到的是同一家公司。
配置的"乱"体现在：

* 配置中心有全局默认规则（低优先级）、服务通配规则（中）、精确匹配（高）；
* 代码注解与配置中心对同一条边给出不同值；
* 网格层再单独设 route timeout 和 retries，与应用层叠加；
* 大量边什么都不配，落到框架默认；
* 有些边的 propagate_deadline 开着（超时逐层扣减），有些关着（各层各自计时）。

同时按用户描述硬编码一条"上周故障"的链：
``gateway-01 -> bff-orchestrator -> order-service -> inventory-service``，
网关 3s、中间层 5s 重试 3 次、下一层重试 2 次，外加网格层再放大一次。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Optional

from .model import Edge, EdgeConfig, Service, Topology

# ---------------------------------------------------------------------------
# 命名素材
# ---------------------------------------------------------------------------

DOMAINS = [
    ("edge", "接入"),
    ("user", "用户"),
    ("order", "交易"),
    ("pay", "支付"),
    ("inventory", "库存"),
    ("search", "搜索"),
    ("feed", "内容"),
    ("notify", "消息"),
    ("risk", "风控"),
    ("data", "数据"),
    ("ops", "运维"),
    ("ads", "广告"),
]

SERVICE_WORDS = {
    "edge": ["gateway", "bff", "router", "cdn-edge"],
    "user": ["account", "profile", "session", "auth", "sso"],
    "order": ["order", "cart", "checkout", "orchestrator", "fulfillment"],
    "pay": ["payment", "wallet", "ledger", "refund", "risk-check"],
    "inventory": ["inventory", "stock", "warehouse", "reservation"],
    "search": ["search", "indexer", "suggest", "ranking"],
    "feed": ["feed", "timeline", "comment", "like"],
    "notify": ["notify", "sms", "email", "push", "im"],
    "risk": ["risk", "fraud", "rule-engine", "blacklist"],
    "data": ["report", "etl", "aggregator", "stats", "olap"],
    "ops": ["config", "registry", "scheduler", "audit"],
    "ads": ["ads", "bid", "targeting", "budget"],
}


@dataclass
class GeneratorSettings:
    n_services: int = 320
    seed: int = 20241118
    #: 背景流量总 rps（分摊到各入口）
    entry_rps: float = 400.0


# ---------------------------------------------------------------------------
# 故障链（硬编码，必须能稳定复现）
# ---------------------------------------------------------------------------

INCIDENT_NODES = [
    # (id, name, tier, domain, workers, service_ms, slo_ms)
    # inventory 基线 1.2s、20 worker -> 16rps 容量。它是条老链路上的小服务。
    # 变慢 2.2 倍（2.6s）就超过 order 给它的 2s 超时，触发 order 的 2 次重试，
    # 再叠上 bff 的 8 次尝试——重试把 4rps 的入口放大到 70+ rps，
    # 远超它的 16rps 容量，从"有点慢"直接被打死。
    ("edge-gateway-01", "gateway-01", 0, "edge", 800, 2.0, 3000.0),
    ("order-bff-01", "bff-orchestrator", 1, "order", 120, 8.0, None),
    ("order-svc-01", "order-service", 2, "order", 80, 12.0, None),
    ("inventory-svc-01", "inventory-service", 3, "inventory", 20, 1200.0, None),
]

# (caller, callee, 应用层 timeout, 应用层 retries, 网格 timeout, 网格 retries)
INCIDENT_EDGES = [
    ("edge-gateway-01", "order-bff-01", 3000.0, 0, None, 0),
    ("order-bff-01", "order-svc-01", 5000.0, 3, 5000.0, 1),
    ("order-svc-01", "inventory-svc-01", 2000.0, 2, None, 0),
]


def build_incident(topo: Topology) -> None:
    """把事故链注入拓扑。节点若已被随机生成，强制用事故参数覆盖，
    保证这条链的行为稳定可复现。"""
    for sid, name, tier, domain, workers, ms, slo in INCIDENT_NODES:
        svc = Service(
            id=sid,
            name=name,
            tier=tier,
            domain=domain,
            workers=workers,
            service_ms=ms,
            slo_ms=slo,
            background_rps=0.0,
        )
        topo.services[sid] = svc

    specs = {
        ("edge-gateway-01", "order-bff-01"): (3000.0, 0, None, 0),
        ("order-bff-01", "order-svc-01"): (5000.0, 3, 5000.0, 1),
        ("order-svc-01", "inventory-svc-01"): (2000.0, 2, None, 0),
    }
    for (caller, callee), (t, r, mt, mr) in specs.items():
        eid = f"{caller}->{callee}"
        if eid in topo.edges:
            continue
        edge = Edge(id=eid, caller=caller, callee=callee, call_prob=1.0)
        # 代码注解：硬编码兜底
        edge.add_config(
            EdgeConfig(
                source="code",
                priority=0,
                label="代码注解 @Retryable",
                timeout_ms=t,
                retries=r,
                backoff_base_ms=200.0,
                backoff_mult=2.0,
                backoff_max_ms=2000.0,
                jitter=0.1,
            )
        )
        # 网格层：只对 bff->order 这条边额外设了 retries=1 和 5s
        if mt is not None or mr:
            edge.add_config(
                EdgeConfig(
                    source="mesh",
                    priority=10,
                    label="服务网格 route/incident",
                    timeout_ms=mt,
                    retries=mr,
                )
            )
        topo.add_edge(edge)

    if "edge-gateway-01" not in topo.roots:
        topo.roots.append("edge-gateway-01")
    topo.meta["incident"] = {
        "root": "edge-gateway-01",
        "chain": [n[0] for n in INCIDENT_NODES],
        "slo_ms": 3000.0,
        "slow_service": "inventory-svc-01",
        "story": (
            "inventory-service 只是慢了一点点（1.2s → 2.6s，变慢 2.2 倍），"
            "本来它 16rps 的容量扛 4rps 入口绰绰有余。"
            "但它一超过 order 给的 2s 超时，order 就重试 2 次（3 次尝试）；"
            "bff 对 order 又是 3 次应用重试 × 网格 1 次 = 8 次尝试。"
            "两层重试相乘，把一个入口请求放大成 18 倍流量压到 inventory，"
            "远超它的容量——网关早在 3s 就放弃了，底下还在反复重试，"
            "把一个『有点慢』的服务直接打死。"
        ),
        "expected_amplification": 18.0,
    }


# ---------------------------------------------------------------------------
# 大拓扑生成
# ---------------------------------------------------------------------------

def _mk_id(domain: str, word: str, idx: int) -> str:
    return f"{domain}-{word}-{idx:02d}"


def build_company(settings: Optional[GeneratorSettings] = None) -> Topology:
    s = settings or GeneratorSettings()
    rng = random.Random(s.seed)
    topo = Topology(services={}, edges={}, seed=s.seed)
    per_domain: Dict[str, List[str]] = {d: [] for d, _ in DOMAINS}

    # ---- 1. 生成服务 ----
    # 每个 domain 轮流分配，ID 带全局序号保证唯一
    generated = 0
    used: set = set(topo.services)
    dom_i = 0
    seq = 0
    while generated < s.n_services:
        domain, _cn = DOMAINS[dom_i % len(DOMAINS)]
        dom_i += 1
        words = SERVICE_WORDS[domain]
        word = words[seq % len(words)]
        seq += 1
        idx = 1
        sid = _mk_id(domain, word, idx)
        while sid in used:
            sid = _mk_id(domain, word, idx)
            idx += 1
            if idx > 99:
                sid = f"{domain}-{word}-svc-{seq:03d}"
        used.add(sid)
        tier = 0 if domain == "edge" else rng.choice([1, 2, 2, 3, 3, 4])
        workers = int(rng.choice([20, 40, 60, 80, 120, 200]))
        service_ms = round(rng.uniform(3, 60), 1)
        bg = round(rng.uniform(0, 12), 1) if tier > 0 else 0.0
        topo.add_service(
            Service(
                id=sid,
                name=f"{word}-{idx:02d}",
                tier=tier,
                domain=domain,
                workers=workers,
                service_ms=service_ms,
                background_rps=bg,
                latency_sigma=rng.uniform(0.15, 0.45),
                parallel_outgoing=rng.random() < 0.6,
            )
        )
        per_domain[domain].append(sid)
        generated += 1

    build_incident(topo)
    base_names = [n[0] for n in INCIDENT_NODES]

    # 给每个 domain 找入口（tier 最低的）
    by_tier: Dict[str, List[str]] = {}
    for sid, svc in topo.services.items():
        by_tier.setdefault(svc.domain, []).append(sid)
    for d, ids in by_tier.items():
        ids.sort(key=lambda x: topo.services[x].tier)

    # ---- 2. 生成调用边：按 tier 从小到大连边，形成 DAG ----
    # 入口服务
    roots = [n for n in base_names if n in topo.services]
    for d, ids in by_tier.items():
        if ids:
            roots.append(ids[0])
    topo.roots = sorted(set(roots))

    def by_tier_list():
        return sorted(topo.services.values(), key=lambda x: x.tier)

    svcs = by_tier_list()
    for svc in svcs:
        if svc.tier == 0:
            # 入口连 2~4 个 tier1/2
            targets = [x for x in svcs if x.tier in (1, 2) and x.id != svc.id]
            for t in rng.sample(targets, k=min(len(targets), rng.randint(2, 4))):
                _add_random_edge(topo, rng, svc.id, t.id)
            continue
        if svc.tier >= 3:
            continue
        # tier1/2 连 tier+1 的 1~3 个
        targets = [x for x in svcs if x.tier == svc.tier + 1]
        if not targets:
            continue
        for t in rng.sample(targets, k=min(len(targets), rng.randint(1, 3))):
            _add_random_edge(topo, rng, svc.id, t.id)

    # 保证事故链在 edges 里（build_incident 已加）
    topo.meta.setdefault("company", "Nimbus Commerce（模拟）")
    topo.meta["settings"] = {"n_services": len(topo.services), "seed": s.seed}
    return topo


def _add_random_edge(topo: Topology, rng: random.Random, caller: str, callee: str) -> None:
    eid = f"{caller}->{callee}"
    if eid in topo.edges or caller == callee:
        return
    edge = Edge(id=eid, caller=caller, callee=callee, call_prob=round(rng.uniform(0.3, 1.0), 2))
    _apply_messy_config(topo, rng, edge)
    topo.add_edge(edge)


def _apply_messy_config(topo: Topology, rng: random.Random, edge: Edge) -> None:
    """给一条边随机叠加 0~3 个来源的配置，制造真实的混乱。"""
    roll = rng.random()
    caller = topo.services.get(edge.caller)
    base_ms = max(50.0, (caller.service_ms if caller else 10) * 3)

    # 30% 的边什么都不配 -> 框架默认
    if roll < 0.30:
        return

    # 代码注解：约 45% 的边有
    if roll < 0.75:
        edge.add_config(
            EdgeConfig(
                source="code",
                priority=0,
                label="代码注解 @Timeout",
                timeout_ms=round(base_ms * rng.choice([1.0, 1.5, 2.0]), -1),
                retries=rng.choice([0, 0, 1, 2, 3]),
                backoff_base_ms=float(rng.choice([50, 100, 200, 500])),
                backoff_mult=rng.choice([1.5, 2.0, 2.0, 3.0]),
                backoff_max_ms=float(rng.choice([1000, 2000, 5000])),
                jitter=round(rng.uniform(0, 0.3), 2),
            )
        )

    # 配置中心：全局默认（低优先级），约 60% 的边能命中
    if rng.random() < 0.60:
        edge.add_config(
            EdgeConfig(
                source="config_center",
                priority=5,
                label="配置中心/全局默认",
                timeout_ms=float(rng.choice([800, 1000, 1500, 2000])),
                retries=rng.choice([0, 1, 1, 2]),
                backoff_base_ms=100.0,
                backoff_mult=2.0,
                backoff_max_ms=2000.0,
                propagate_deadline=rng.random() < 0.4,
            )
        )

    # 配置中心：精确匹配（高优先级），约 18% 的边有
    if rng.random() < 0.18:
        edge.add_config(
            EdgeConfig(
                source="config_center",
                priority=20,
                label="配置中心/精确匹配",
                timeout_ms=round(base_ms * rng.uniform(1.2, 3.0), -1),
                retries=rng.choice([0, 1, 2, 3, 4]),
                retry_budget=rng.choice([None, 0.1, 0.2]),
            )
        )

    # 服务网格：约 35% 的边有 route 级配置（与应用层叠加）
    if rng.random() < 0.35:
        edge.add_config(
            EdgeConfig(
                source="mesh",
                priority=10,
                label="服务网格/route",
                timeout_ms=float(rng.choice([1000, 2000, 3000, 5000])),
                retries=rng.choice([0, 0, 1, 1, 2]),
                backoff_base_ms=float(rng.choice([25, 50, 100])),
                backoff_mult=2.0,
                backoff_max_ms=1000.0,
            )
        )
