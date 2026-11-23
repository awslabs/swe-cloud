"""Entry points.

    python3 -m reliability worker --worker-id <id>
    python3 -m reliability callback [--port 8026]
"""
import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m reliability")
    commands = parser.add_subparsers(dest="command", required=True)

    worker = commands.add_parser("worker", help="poll the work queues and deliver submissions")
    worker.add_argument("--worker-id", required=True, help="unique id of this worker incarnation")

    callback = commands.add_parser("callback", help="serve the Notify delivery-status callback")
    callback.add_argument("--port", type=int, default=int(os.environ.get("CALLBACK_PORT", "8026")))

    args = parser.parse_args(argv)
    if args.command == "worker":
        from .worker import main as run_worker

        run_worker(args.worker_id)
    else:
        from .callback import main as run_callback

        run_callback(args.port)


if __name__ == "__main__":
    main()
