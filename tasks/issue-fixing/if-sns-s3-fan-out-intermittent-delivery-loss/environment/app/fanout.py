#!/usr/bin/env python3
import argparse

from transport import load_config
import publisher
import pump


def main():
    parser = argparse.ArgumentParser(description='Dispatch event transport')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('setup')
    publish = commands.add_parser('publish')
    publish.add_argument('--event-id', required=True)
    publish.add_argument('--body-file', required=True)
    subscriber = commands.add_parser('subscriber')
    subscriber.add_argument('--name', required=True)
    args = parser.parse_args()
    config = load_config()
    if args.command == 'setup':
        pump.setup(config)
    elif args.command == 'publish':
        publisher.publish(config, args.event_id, args.body_file)
    else:
        pump.run(config, args.name)


if __name__ == '__main__':
    main()
