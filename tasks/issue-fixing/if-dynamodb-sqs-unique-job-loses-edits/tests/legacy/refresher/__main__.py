import argparse
import sys

from . import dispatch, provision, worker


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m refresher")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("dispatch", help="queue a refresh for one entity")
    p.add_argument("--entity", required=True)
    p.add_argument("--delay", type=int, default=0, help="seconds before the job becomes visible")
    p.add_argument("--unique-for", type=int, default=None,
                   help="dedup lock lifetime in seconds (default UNIQUE_FOR)")
    p.set_defaults(run=dispatch.main)

    p = commands.add_parser("worker", help="process refresh jobs until SIGTERM")
    p.add_argument("--id", default="1")
    p.set_defaults(run=worker.main)

    p = commands.add_parser("provision", help="create the queue and tables")
    p.set_defaults(run=provision.main)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
