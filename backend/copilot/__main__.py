"""Explicit local commands; all indexing uses the same durable fenced worker."""
import argparse
import json

from .config import Settings
from .contracts import EvidenceError


def main():
    parser = argparse.ArgumentParser(description="Local evidence and durable application workspace")
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
    from .process_boundary import launch, settings_command
    try:
        fields = vars(args).copy()
        kind = fields.pop('command')
        command = settings_command(kind, Settings.from_env(), **fields)
    except Exception:
        print(json.dumps({'error': {'code': 'INDEX_NOT_READY',
            'message': 'Start the evidence service through the supported isolated launcher.'}}))
        raise SystemExit(1) from None
    raise SystemExit(launch(command))


def _dispatch(command, *, service_factory=None):
    from pathlib import Path

    from .process_boundary import require_sdk_boundary, validate_command
    validate_command(command)
    if service_factory is None:
        require_sdk_boundary()
    elif not callable(service_factory):
        raise EvidenceError('INDEX_NOT_READY', 'Start the evidence service through the supported isolated launcher.', 503)
    values = command['settings']
    settings = Settings(Path(values['data_dir']), Path(values['model_dir']),
                        values['chroma_host'], values['chroma_port'], values['port'])
    try:
        if command["kind"] == "serve":
            import uvicorn

            from .api import create_app
            uvicorn.run(create_app(settings) if service_factory is None else create_app(settings, service_factory), host="0.0.0.0" if command["container"] else "127.0.0.1",
                        port=settings.port, workers=1, access_log=False, log_level="critical")
        else:
            from .api import make_service
            from .store import Store
            from .worker import Worker
            store = Store(settings.data_dir)
            factory = make_service if service_factory is None else service_factory
            worker = Worker(store, lambda: factory(store, settings))
            if command["kind"] == 'worker':
                if command["once"]:
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
            elif command["kind"] == 'index':
                import uuid
                job = worker.jobs.enqueue(command["profile"], 'index', {'corpus': command["corpus"]}, str(uuid.uuid4()))
                result = worker.run_job(job.id)
                if not result or result.get('state') != 'completed':
                    raise EvidenceError('INDEX_NOT_READY', 'Index job did not complete; inspect its durable status', 503)
                print(json.dumps(worker.jobs.stage_result(command["profile"], job.id, 'index_published')))
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
