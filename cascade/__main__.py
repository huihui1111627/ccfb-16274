"""`python -m cascade` —— 启动 Web 服务。"""
from __future__ import annotations

import argparse




def main(argv=None):
    from .api import create_app

    parser = argparse.ArgumentParser(description="级联故障推演工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--services", type=int, default=None)
    args = parser.parse_args(argv)

    from .generator import GeneratorSettings
    from .service import Service

    settings = GeneratorSettings()
    if args.seed is not None:
        settings.seed = args.seed
    if args.services is not None:
        settings.n_services = args.services
    svc = Service(settings)
    app = create_app(svc)
    print(f"级联故障推演工具  http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
