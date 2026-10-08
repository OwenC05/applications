"""Explicit model setup and local index builds; no background pseudo-job."""
import argparse
import json
from dataclasses import asdict

from .config import Settings, data_lock
from .contracts import EvidenceError


def main():
    parser = argparse.ArgumentParser(description="Local evidence workbench (not application drafting)")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--container", action="store_true", help="Bind container network; publish only loopback")
    index = commands.add_parser("index")
    index.add_argument("--profile", required=True)
    index.add_argument("--corpus", choices=["facts", "documents"], required=True)
    commands.add_parser("cleanup")
    args = parser.parse_args()
    settings = Settings.from_env()
    try:
        if args.command == "serve":
            import uvicorn

            from .api import create_app
            uvicorn.run(create_app(settings), host="0.0.0.0" if args.container else "127.0.0.1",
                        port=settings.port, workers=1, access_log=False, log_level="critical")
        else:
            from .api import make_service
            from .store import Store
            with data_lock(settings.data_dir, timeout=5):
                store = Store(settings.data_dir)
                service = make_service(store, settings)
                service.recover()
                if args.command == "index":
                    result = service.build(args.profile, args.corpus)
                    print(json.dumps(asdict(result)))
                else:
                    print(json.dumps({"pending_cleanup": len(store.pending_cleanup())}))
    except EvidenceError as error:
        print(json.dumps({"error": {"code": error.code, "message": error.safe_message}}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"error": {"code": "UNAVAILABLE", "message": "Check local database/model setup. No successful operation is claimed."}}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
