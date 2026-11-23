import argparse
import logging

from manifest_service.server import serve


def main():
    parser = argparse.ArgumentParser(prog='python3 -m manifest_service',
                                     description='Run one instance of the manifest service.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)7s %(threadName)s %(name)s: %(message)s')
    logging.getLogger('botocore').setLevel(logging.WARNING)
    serve(args.host, args.port)


if __name__ == '__main__':
    main()
