"""命令行：无需起 Web 也能跑分析、复现、建议与核对。

    python -m cascade.cli overview
    python -m cascade.cli incident
    python -m cascade.cli verify
    python -m cascade.cli proposals
    python -m cascade.cli edge order-bff-01->order-svc-01
"""
from __future__ import annotations

import argparse

from .service import Service


def main(argv=None):
    parser = argparse.ArgumentParser(description="级联故障推演工具 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("overview", help="公司拓扑与分析概览")
    sub.add_parser("incident", help="复现上周故障")
    sub.add_parser("verify", help="三方核对事故链放大倍数")
    sub.add_parser("proposals", help="列出修改建议及静态效果")

    pe = sub.add_parser("edge", help="查看一条边的三来源配置合并")
    pe.add_argument("edge_id")

    psim = sub.add_parser("simulate", help="对指定服务注入故障并推演")
    psim.add_argument("service")
    psim.add_argument("--slow", type=float, default=2.2)
    psim.add_argument("--error", type=float, default=0.0)
    psim.add_argument("--rps", type=float, default=4.0)

    args = parser.parse_args(argv)
    svc = Service()

    if args.cmd == "overview":
        ov = svc.overview()
        print(f"公司：{ov['company']}  服务 {ov['n_services']}  调用边 {ov['n_edges']}")
        t = ov["totals"]
        print(f"倒挂边 {t['n_inverted_edges']} / {t['n_edges']}；"
              f"有重试的边 {t['n_retrying_edges']}；最大放大 ×{t['max_leaf_amplification']}")
        print(f"事故链放大 ×{t['incident_amplification']}")
        for d in ov["domains"]:
            print(f"  {d['domain']:<10} {d['count']}")

    elif args.cmd == "incident":
        d = svc.reproduce_incident(entry_rps=4.0, duration_ms=60000.0)
        print(d["story"])
        print()
        for c in d["chain_stats"]:
            print(f"  {c['edge_id']:<40} 尝试 {c['attempts']:>6}  "
                  f"放大 ×{c['amplification']:.2f}  超时 {c['timeouts']}  "
                  f"拒绝 {c['rejected']}")
        v = d["verdict"]
        print(f"\n入口错误率 {v['root_error_rate']:.1%}；"
              f"被打满：{', '.join(v['downstream_saturated']) or '无'}")

    elif args.cmd == "verify":
        v = svc.verify_incident()
        c = v["converged"]
        print(f"三方核对：{'一致' if c['ok'] else '有偏差'}；"
              f"最大相对偏差 {c['max_rel_err']}")
        for r in c["rows"]:
            print(f"  {r['edge_id']:<40} 解析 ×{r['analytical']:<3} "
                  f"步进 ×{r['stepped']:<6} 离散事件 ×{r['des']}")

    elif args.cmd == "proposals":
        d = svc.proposals_summary()
        for p in d["proposals"]:
            print(f"[{p['key']}] {p['title']}  ({p['n_changes']} 处改动)")
            print(f"  事故链放大 {p['before']['incident_amplification']} → "
                  f"{p['after']['incident_amplification']}；"
                  f"倒挂边 {p['before']['n_inverted_edges']} → "
                  f"{p['after']['n_inverted_edges']}")

    elif args.cmd == "edge":
        d = svc.edge_detail(args.edge_id)
        r = d["resolved"]
        print(f"{d['edge']['caller']} → {d['edge']['callee']}")
        print(f"  单次超时 {r['timeout_ms']}ms；尝试 {r['attempts']} 次"
              f"（应用层 {r['app_retries']} × 网格层 {r['mesh_retries']}）")
        print(f"  最坏 {d['metrics']['downstream_ms']:.0f}ms；"
              f"上游预算 {d['metrics']['upstream_budget_ms']}ms；"
              f"倒挂 {d['metrics']['inverted']}")
        for s in d["sources"]:
            print(f"  [{s['layer']}] {s['label']}：{s['describe']}")
        print("  逐字段来源：")
        for f, o in d["origins"].items():
            print(f"    {f:<20} {o['value']}  ← {o['label']}")

    elif args.cmd == "simulate":
        sc = svc.scenario_for(args.service, slow_factor=args.slow,
                              error_rate=args.error, entry_rps=args.rps)
        d = svc.simulate(sc)
        print(f"推演 {args.service} 慢 {args.slow}× 错 {args.error:.0%}  "
              f"入口 {args.rps}rps")
        print(f"  入口请求 {d['root_requests']}  错误率 "
              f"{d['saturation']['root_error_rate']:.1%}")
        for x in d["saturation"]["saturated_services"]:
            print(f"  过载 {x['service']}: 队列 {x['max_queue']} "
                  f"拒绝 {x['rejected']}")
        for eid, st in sorted(d["edge_stats"].items(),
                              key=lambda kv: -kv[1]["amplification"])[:5]:
            print(f"  {eid:<44} 放大 ×{st['amplification']:.2f}")


if __name__ == "__main__":
    main()
