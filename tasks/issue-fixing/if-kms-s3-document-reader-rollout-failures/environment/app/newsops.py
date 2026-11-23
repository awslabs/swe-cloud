import argparse
from codec import open_payload
from migration import recover
from transport import client, load


def main():
    parser = argparse.ArgumentParser(description='Read a document using the new client.')
    parser.add_argument('operation', choices=['decrypt'])
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--key', required=True)
    args = parser.parse_args()
    doc, _ = load(client('s3'), args.bucket, args.key)
    data_key = recover(client('kms'), doc, args.key)
    print(open_payload(doc, data_key, args.key))


if __name__ == '__main__':
    main()
