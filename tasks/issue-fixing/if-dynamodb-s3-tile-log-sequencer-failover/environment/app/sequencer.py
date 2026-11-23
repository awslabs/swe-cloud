#!/usr/bin/env python3
"""Tile log sequencing worker.

Serves submissions over HTTP.  The entries of a submission become the
next entries of the log's Merkle tree, in one checkpoint; when it is
committed the worker answers with the index of the first entry and the
checkpoint that includes them.

State of a log lives in two places:

  * the checkpoint record in DynamoDB (the authority for what has been
    committed), advanced with a conditional write, and
  * the object bucket, holding the tile objects at their public
    coordinates and the published `checkpoint` object that readers
    fetch.

The worker announces its pauses on the control channel (see
control.py); the driver may hold any of them.
"""

import argparse
import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import control
import lockdb
import objstore
import treekit


class Sequencer:
    def __init__(self, config):
        self.worker_id = config["worker_id"]
        self.origin = config["origin"]
        self.time_step = int(config.get("checkpoint_time_step", 1))
        self.lock = lockdb.LockDb(config)
        self.objects = objstore.ObjectStore(config)
        self.control_path = config.get("control_socket", "")

        # In-memory view of the committed tree.
        self.lock_checkpoint = None  # exact bytes of the record we can advance
        self.size = 0
        self.root = treekit.empty_root()
        self.time = 0
        self.payloads = []

        self.mu = threading.Lock()

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
                # The published copy lags the record.  Maybe the object
                # GETs were cached; that is ok as long as the tiles we
                # load below check out against the record.
                print("published checkpoint is older than the record, "
                      "continuing", flush=True)

        # Rebuild the leaf list from the tile objects so this process
        # can extend the tree, and check it against the record.
        payloads = []
        for t, w in treekit.data_tiles_for_size(self.size):
            blob = self.objects.get(treekit.data_tile_path(t, w))
            payloads.extend(treekit.decode_data_tile(blob))
        if treekit.tree_root(payloads) != self.root:
            print("tile objects do not match the committed root",
                  file=sys.stderr, flush=True)
            raise SystemExit(1)
        self.payloads = payloads

        control.announce(self.control_path, "startup_complete", {
            "worker": self.worker_id,
            "checkpoint": base64.b64encode(self.lock_checkpoint).decode(),
            "size": self.size,
            "root": self.root.hex(),
        })
        print("loaded log size=%d time=%d" % (self.size, self.time),
              flush=True)

    # -- sequencing ------------------------------------------------------

    def submit(self, request_id, entries):
        with self.mu:
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

            tiles = []
            for t, w in treekit.data_tiles_for_size(size):
                chunk = payloads[t * treekit.TILE_WIDTH:
                                 t * treekit.TILE_WIDTH + w]
                tiles.append((treekit.data_tile_path(t, w),
                              treekit.encode_data_tile(chunk)))
            for level, t, w in treekit.hash_tiles_for_size(size):
                tiles.append((treekit.hash_tile_path(level, t, w),
                              treekit.build_hash_tile(level, t, w, payloads)))

            checkpoint = treekit.format_checkpoint(
                self.origin, size, root, time_)

            # Make the new tile objects available at their coordinates.
            for key, blob in tiles:
                self.objects.put(key, blob)

            try:
                self.lock.replace(parent, checkpoint)
            except lockdb.ConflictError:
                control.announce(self.control_path, "before_reply", {
                    "worker": self.worker_id,
                    "request_id": request_id,
                    "status": "conflict",
                })
                return {"status": "conflict"}

            self.lock_checkpoint = checkpoint
            self.size, self.root, self.time = size, root, time_
            self.payloads = payloads

            self.objects.put("checkpoint", checkpoint)

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
    sequencer = None  # set before serving

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
