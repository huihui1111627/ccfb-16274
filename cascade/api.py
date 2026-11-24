"""Flask API。路由保持薄，逻辑在 :mod:`cascade.service`。"""
from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory

from .service import Service

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def create_app(service: Service = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    svc = service or Service()
    app.config["SVC"] = svc

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/static/<path:path>")
    def static_files(path):
        return send_from_directory(WEB_DIR, path)

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/api/overview")
    def overview():
        return jsonify(svc.overview())

    @app.get("/api/topology")
    def topology():
        domain = request.args.get("domain") or None
        max_nodes = int(request.args.get("max_nodes", 400))
        mode = request.args.get("mode", "incident")
        return jsonify(svc.topology(domain=domain, max_nodes=max_nodes, mode=mode))

    @app.get("/api/edge/<path:edge_id>")
    def edge_detail(edge_id):
        try:
            return jsonify(svc.edge_detail(edge_id))
        except KeyError:
            return jsonify({"error": "edge not found", "edge_id": edge_id}), 404

    @app.get("/api/inversions")
    def inversions():
        limit = int(request.args.get("limit", 200))
        return jsonify(svc.inversions(limit=limit))

    @app.get("/api/amplification")
    def amplification():
        limit = int(request.args.get("limit", 40))
        return jsonify(svc.amplification(limit=limit))

    @app.get("/api/proposals")
    def proposals():
        return jsonify(svc.proposals_summary())

    @app.post("/api/proposals/<key>/evaluate")
    def evaluate(key):
        body = request.get_json(silent=True) or {}
        try:
            return jsonify(
                svc.evaluate_proposal(
                    key,
                    fault_service=body.get("service"),
                    entry_rps=float(body.get("entry_rps", 4.0)),
                    duration_ms=float(body.get("duration_ms", 60000.0)),
                )
            )
        except KeyError:
            return jsonify({"error": "unknown proposal", "key": key}), 404

    @app.get("/api/incident")
    def incident():
        return jsonify(svc.reproduce_incident(
            entry_rps=float(request.args.get("entry_rps", 4.0)),
            duration_ms=float(request.args.get("duration_ms", 60000.0)),
        ))

    @app.post("/api/simulate")
    def simulate():
        body = request.get_json(silent=True) or {}
        service = body.get("service")
        if not service:
            return jsonify({"error": "service is required"}), 400
        sc = svc.scenario_for(
            service,
            slow_factor=float(body.get("slow_factor", 1.0)),
            error_rate=float(body.get("error_rate", 0.0)),
            start_ms=float(body.get("start_ms", 5000.0)),
            duration_ms=float(body.get("duration_ms", 60000.0)),
            entry_rps=float(body.get("entry_rps", 4.0)),
            root=body.get("root") or None,
            dt_ms=float(body.get("dt_ms", 25.0)),
            seed=int(body.get("seed", 7)),
        )
        try:
            data = svc.simulate(
                sc,
                proposal_key=body.get("proposal") or None,
                engine=body.get("engine", "stepped"),
            )
        except KeyError as exc:
            return jsonify({"error": "unknown proposal", "key": str(exc)}), 404
        return jsonify(data)

    @app.get("/api/verify/incident")
    def verify_incident():
        n = int(request.args.get("n_requests", 40))
        return jsonify(svc.verify_incident(n_requests=n))

    @app.post("/api/verify/scenario")
    def verify_scenario():
        body = request.get_json(silent=True) or {}
        service = body.get("service")
        if not service:
            return jsonify({"error": "service is required"}), 400
        return jsonify(
            svc.verify_scenario(
                service,
                slow_factor=float(body.get("slow_factor", 260.0)),
                error_rate=float(body.get("error_rate", 0.0)),
                entry_rps=float(body.get("entry_rps", 4.0)),
                duration_ms=float(body.get("duration_ms", 20000.0)),
            )
        )

    @app.post("/api/reload")
    def reload():
        body = request.get_json(silent=True) or {}
        svc.reload(
            seed=body.get("seed"),
            n_services=body.get("n_services"),
        )
        return jsonify({"ok": True, "overview": svc.overview()})

    return app
