#!/usr/bin/env python3
"""Tile log sequencing worker (reference implementation).

Same contract as the shipped worker (CLI, config, HTTP API, control
pauses, storage layout); the difference is in when bytes reach the
public coordinates and what a restart does with a record the published
copy lags behind.
"""

import argparse
import base64
import gzip
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import control
import lockdb
import objstore
import treekit


def bundle_key(size, root):
    # Private, checkpoint-derived location for a candidate's objects.
    return "staging/%d-%s" % (size, root.hex())


def marshal_bundle(tiles):
    return gzip.compress(json.dumps(
        [[key, base64.b64encode(blob).decode()] for key, blob in tiles],
        separators=(",", ":")).encode("ascii"))


def unmarshal_bundle(blob):
    return [[key, base64.b64decode(b64)] for key, b64 in
            json.loads(gzip.decompress(bytes(blob)).decode("ascii"))]


class Sequencer:
    def __init__(self, config):
        self.worker_id = config["worker_id"]
        self.origin = config["origin"]
        self.time_step = int(config.get("checkpoint_time_step", 1))
        self.lock = lockdb.LockDb(config)
        self.objects = objstore.ObjectStore(config)
        self.control_path = config.get("control_socket", "")

        self.lock_checkpoint = None
        self.size = 0
        self.root = treekit.empty_root()
        self.time = 0
        self.payloads = []

        self.mu = threading.Lock()

    # -- helpers ---------------------------------------------------------

    def apply_bundle(self, tiles):
        # The same routine serves a won candidate and a restart: public
        # coordinates are written only from a bundle that the record
        # already selected.
        for key, blob in tiles:
            self.objects.put(key, blob)

    def recover_publication(self, committed, checkpoint):
        # The record names a tree the published copy does not cover yet:
        # the writer died between the two.  The objects for the
        # committed tree are in the bundle the record's own size and
        # root point at; put them where readers look, then the head.
        print("published checkpoint is older than the record, recovering",
              flush=True)
        blob = self.objects.get(
            bundle_key(committed["size"], committed["root"]))
        self.apply_bundle(unmarshal_bundle(blob))
        self.publish_head(checkpoint, committed["size"])

    def publish_head(self, checkpoint, size):
        # The head object is last-writer-wins: a worker that learns its
        # own commit late must not drag the head back off a larger tree
        # another worker has committed and published in the meantime.
        try:
            current = self.objects.get("checkpoint")
        except objstore.NotFoundError:
            current = None
        if current == checkpoint:
            return
        if current is not None:
            try:
                published = treekit.parse_checkpoint(current)
            except ValueError:
                published = None
            if published is not None and published["size"] > size:
                record = self.lock.fetch()
                covered = record is not None and \
                    treekit.parse_checkpoint(bytes(record))["size"] >= \
                    published["size"]
                if covered:
                    print("published head is at size %d, past this tree "
                          "(size %d); leaving it"
                          % (published["size"], size), flush=True)
                    return
        self.objects.put("checkpoint", checkpoint)

    def read_back(self, size, root):
        # Rebuild the leaf list from the public coordinates and check
        # it against the committed root.
        payloads = []
        for t, w in treekit.data_tiles_for_size(size):
            blob = self.objects.get(treekit.data_tile_path(t, w))
            payloads.extend(treekit.decode_data_tile(blob))
        if treekit.tree_root(payloads) != root:
            raise RuntimeError("tile objects do not match the committed root")
        return payloads

    # -- startup ---------------------------------------------------------

    def load(self):
        self.lock_checkpoint = self.lock.fetch()
        if self.lock_checkpoint is None:
            print("no checkpoint record for this log, refusing to start",
                  file=sys.stderr, flush=True)
            raise SystemExit(1)
        committed = treekit.parse_checkpoint(self.lock_checkpoint)
        self.size, self.root, self.time = (
            committed["size"], committed["root"], committed["time"])

        public = None
        try:
            public = self.objects.get("checkpoint")
        except objstore.NotFoundError:
            pass
        if public is not None:
            published = treekit.parse_checkpoint(public)
            if published["size"] == committed["size"] and \
                    published["root"] != committed["root"]:
                print("published checkpoint conflicts with the record",
                      file=sys.stderr, flush=True)
                raise SystemExit(1)
            if published["size"] > committed["size"]:
                print("published checkpoint is newer than the record",
                      file=sys.stderr, flush=True)
                raise SystemExit(1)
            if published["size"] < committed["size"]:
                self.recover_publication(committed, self.lock_checkpoint)
        elif self.size > 0:
            print("no published checkpoint, recovering", flush=True)
            self.recover_publication(committed, self.lock_checkpoint)

        self.payloads = self.read_back(self.size, self.root)

        control.announce(self.control_path, "startup_complete", {
            "worker": self.worker_id,
            "checkpoint": base64.b64encode(self.lock_checkpoint).decode(),
            "size": self.size,
            "root": self.root.hex(),
        })
        print("loaded log size=%d time=%d" % (self.size, self.time),
              flush=True)

    def sync_view(self):
        """Take the record as it stands before an attempt fixes its parent.

        A worker whose previous attempt lost the fence, or that has been
        blocked inside one while the world moved, is holding a
        checkpoint that is no longer the record: the tree the record
        names is the one the next attempt must build on.  Caller holds
        self.mu.
        """
        record = self.lock.fetch()
        if record is None:
            return
        record = bytes(record)
        if record == self.lock_checkpoint:
            return
        committed = treekit.parse_checkpoint(record)
        public = None
        try:
            public = self.objects.get("checkpoint")
        except objstore.NotFoundError:
            pass
        published = None
        if public is not None:
            published = treekit.parse_checkpoint(public)
        if published is None or published["size"] < committed["size"]:
            self.recover_publication(committed, record)
        self.lock_checkpoint = record
        self.size, self.root, self.time = (
            committed["size"], committed["root"], committed["time"])
        self.payloads = self.read_back(self.size, self.root)
        print("adopted the committed log size=%d time=%d"
              % (self.size, self.time), flush=True)

    # -- sequencing ------------------------------------------------------

    def build_tiles(self, payloads):
        size = len(payloads)
        tiles = []
        for t, w in treekit.data_tiles_for_size(size):
            chunk = payloads[t * treekit.TILE_WIDTH:
                             t * treekit.TILE_WIDTH + w]
            tiles.append((treekit.data_tile_path(t, w),
                          treekit.encode_data_tile(chunk)))
        for level, t, w in treekit.hash_tiles_for_size(size):
            tiles.append((treekit.hash_tile_path(level, t, w),
                          treekit.build_hash_tile(level, t, w, payloads)))
        return tiles

    def submit(self, request_id, entries):
        with self.mu:
            self.sync_view()

            parent = self.lock_checkpoint
            parent_size = self.size
            payloads = self.payloads + [bytes(e) for e in entries]
            size = len(payloads)
            root = treekit.tree_root(payloads)
            time_ = self.time + self.time_step

            control.announce(self.control_path, "batch_assembled", {
                "worker": self.worker_id,
                "request_id": request_id,
                "parent": base64.b64encode(parent).decode(),
                "parent_size": parent_size,
                "index": parent_size,
                "candidate_size": size,
                "candidate_root": root.hex(),
                "candidate_time": time_,
            })

            checkpoint = treekit.format_checkpoint(
                self.origin, size, root, time_)

            # Everything this candidate would add at the public
            # coordinates goes to its own bundle first: it survives the
            # writer, and it is the only copy a restart could use.
            tiles = self.build_tiles(payloads)
            self.objects.put(bundle_key(size, root), marshal_bundle(tiles))

            try:
                self.lock.replace(parent, checkpoint)
            except lockdb.ConflictError:
                # Lost the record: nothing of ours reached the public
                # coordinates, and nothing of ours may reach them now.
                control.announce(self.control_path, "before_reply", {
                    "worker": self.worker_id,
                    "request_id": request_id,
                    "status": "conflict",
                })
                return {"status": "conflict"}

            self.lock_checkpoint = checkpoint
            self.size, self.root, self.time = size, root, time_
            self.payloads = payloads

            # The record selected this candidate; now the public
            # coordinates may carry its bytes.
            self.apply_bundle(tiles)
            self.publish_head(checkpoint, size)

            control.announce(self.control_path, "before_reply", {
                "worker": self.worker_id,
                "request_id": request_id,
                "status": "committed",
                "index": parent_size,
            })
            return {
                "status": "committed",
                "index": parent_size,
                "time": time_,
                "checkpoint": base64.b64encode(checkpoint).decode(),
            }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sequencer = None

    def log_message(self, fmt, *args):
        print("http: " + fmt % args, flush=True)

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps({"ready": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/submit":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(length) or b"{}")
        try:
            entries = [base64.b64decode(x) for x in req.get("entries", [])]
        except Exception:
            self.send_error(400)
            return
        if not entries:
            self.send_error(400)
            return
        result = Handler.sequencer.submit(req.get("request_id", ""), entries)
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True,
                        help="path to the worker JSON config")
    args = parser.parse_args()

    with open(args.config) as fh:
        config = json.load(fh)

    sequencer = Sequencer(config)
    sequencer.load()
    Handler.sequencer = sequencer

    server = ThreadingHTTPServer(
        (config.get("listen_host", "127.0.0.1"),
         int(config["listen_port"])), Handler)
    server.daemon_threads = True
    print("listening on %s:%d" % server.server_address[:2], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
