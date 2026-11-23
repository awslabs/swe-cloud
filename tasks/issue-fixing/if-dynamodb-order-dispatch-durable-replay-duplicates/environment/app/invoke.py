import argparse
import json
import signal
from handler import dispatch
from runtime import Context, Suspended


def main():
    # The scheduler revokes the ticket before replacement. Let an outstanding
    # callback unwind; runtime checkpoints and the wrapper handle its ticket.
    signal.signal(signal.SIGTERM, lambda *_: None)
    parser = argparse.ArgumentParser(description='Run one durable invocation')
    parser.add_argument('--request', required=True, help='JSON request file')
    args = parser.parse_args()
    with open(args.request) as stream:
        request = json.load(stream)
    try:
        result = dispatch(request['event'], Context(request))
        output = {'status': 'SUCCEEDED', 'result': result}
    except Suspended as suspension:
        output = {'status': 'SUSPENDED', 'resume_at_ms': suspension.resume_at_ms}
    except Exception as exc:
        output = {'status': 'FAILED', 'error': type(exc).__name__, 'detail': str(exc)}
    print(json.dumps(output), flush=True)


if __name__ == '__main__':
    main()
