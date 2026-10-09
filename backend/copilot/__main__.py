"""Explicit local commands; all indexing uses the same durable fenced worker."""
import argparse
import json

from .config import Settings
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
    worker_command = commands.add_parser('worker')
    worker_command.add_argument('--once', action='store_true', help='Handle at most one queued job and reconcile registered cleanup')

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
            from .worker import Worker
            store = Store(settings.data_dir)
            worker = Worker(store, lambda: make_service(store, settings))
            if args.command == 'worker':
                if args.once:
                    from .config import worker_lock
                    with worker_lock(store.root):
                        worker.run_once()
                        print(json.dumps(worker.last_outcome))
                        if worker.outcome_failed(worker.last_outcome):
                            raise SystemExit(1)
                else:
                    import signal
                    signal.signal(signal.SIGTERM, lambda *_args: worker.stop.set())
                    signal.signal(signal.SIGINT, lambda *_args: worker.stop.set())
                    worker.run_forever()
            elif args.command == 'index':
                import uuid
                job = worker.jobs.enqueue(args.profile, 'index', {'corpus': args.corpus}, str(uuid.uuid4()))
                result = worker.run_job(job.id)
                if not result or result.get('state') != 'completed':
                    raise EvidenceError('INDEX_NOT_READY', 'Index job did not complete; inspect its durable status', 503)
                print(json.dumps(worker.jobs.stage_result(args.profile, job.id, 'index_published')))
            else:
                print(json.dumps(worker.recover()))
    except EvidenceError as error:
        print(json.dumps({"error": {"code": error.code, "message": error.safe_message}}))
        raise SystemExit(1) from None
    except Exception:
        print(json.dumps({"error": {"code": "UNAVAILABLE", "message": "Check local database/model setup. No successful operation is claimed."}}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
