"""Merkle tree, tile and checkpoint codec for the tile log.

Everything in this module is deterministic and shared by every worker:
the same leaves always produce the same root, the same tiles and the
same checkpoint bytes.  The formats are part of the deployed contract
(see README.md) and must not change.

Leaf hash:      SHA-256(0x00 || payload)
Internal hash:  SHA-256(0x01 || left || right)
Empty tree:     SHA-256("")
"""

import base64
import hashlib
import json

TILE_WIDTH = 4


def leaf_hash(payload: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + payload).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def empty_root() -> bytes:
    return hashlib.sha256(b"").digest()


def _split(n: int) -> int:
    # Largest power of two strictly smaller than n.
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def tree_root(payloads) -> bytes:
    payloads = list(payloads)
    if not payloads:
        return empty_root()

    def rec(lo: int, hi: int) -> bytes:
        if hi - lo == 1:
            return leaf_hash(payloads[lo])
        k = lo + _split(hi - lo)
        return node_hash(rec(lo, k), rec(k, hi))

    return rec(0, len(payloads))


def path_number(n: int) -> str:
    # Three-digit chunks, the way the public key space is laid out:
    # 0 -> "000", 1234 -> "x001/234".
    parts = ["%03d" % (n % 1000)]
    n //= 1000
    while n > 0:
        parts.append("x%03d" % (n % 1000))
        n //= 1000
    return "/".join(reversed(parts))


def data_tile_path(tile_index: int, width: int) -> str:
    if width >= TILE_WIDTH:
        return "tile/data/" + path_number(tile_index)
    return "tile/data/%s.p/%d" % (path_number(tile_index), width)


def hash_tile_path(level: int, tile_index: int, width: int) -> str:
    if width >= TILE_WIDTH:
        return "tile/%d/%s" % (level, path_number(tile_index))
    return "tile/%d/%s.p/%d" % (level, path_number(tile_index), width)


def entry_span(level: int) -> int:
    # Number of leaves covered by one hash entry at this level.
    return TILE_WIDTH ** level


def tile_span(level: int) -> int:
    # Number of leaves covered by one whole tile at this level.
    return TILE_WIDTH ** (level + 1)


def hash_tiles_for_size(n: int):
    """The canonical set of hash tiles for a tree of n leaves.

    Yields (level, tile_index, width).  A tile exists whenever it holds
    at least one complete entry.
    """
    level = 0
    while True:
        s = entry_span(level)
        ts = tile_span(level)
        t = 0
        emitted = False
        while t * ts + s <= n:
            w = min(TILE_WIDTH, (n - t * ts) // s)
            yield level, t, w
            emitted = True
            t += 1
        if not emitted:
            break
        level += 1


def data_tiles_for_size(n: int):
    """The canonical set of data tiles for a tree of n leaves."""
    t = 0
    remaining = n
    while remaining > 0:
        w = min(remaining, TILE_WIDTH)
        yield t, w
        remaining -= w
        t += 1


def encode_data_tile(payloads) -> bytes:
    # Canonical JSON array of base64 payloads.
    return (
        json.dumps(
            [base64.b64encode(p).decode("ascii") for p in payloads],
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def decode_data_tile(blob: bytes):
    return [base64.b64decode(x) for x in json.loads(bytes(blob).decode("ascii"))]


def encode_hash_tile(hashes) -> bytes:
    return b"".join(hashes)


def decode_hash_tile(blob: bytes):
    blob = bytes(blob)
    if len(blob) % 32:
        raise ValueError("hash tile length %d is not a multiple of 32" % len(blob))
    return [blob[i : i + 32] for i in range(0, len(blob), 32)]


def entry_hash(payloads, lo: int, hi: int) -> bytes:
    """Root of payloads[lo:hi]; hi-lo is a power of two."""
    if hi - lo == 1:
        return leaf_hash(payloads[lo])
    k = lo + _split(hi - lo)
    return node_hash(entry_hash(payloads, lo, k), entry_hash(payloads, k, hi))


def build_hash_tile(level: int, tile_index: int, width: int, payloads) -> bytes:
    span = entry_span(level)
    base = tile_index * tile_span(level)
    hashes = []
    for j in range(width):
        hashes.append(entry_hash(payloads, base + j * span, base + (j + 1) * span))
    return encode_hash_tile(hashes)


def format_checkpoint(origin: str, size: int, root: bytes, time: int) -> bytes:
    return ("%s\n%d\n%s\n%d\n" % (origin, size, root.hex(), time)).encode("ascii")


def parse_checkpoint(blob: bytes):
    lines = bytes(blob).decode("ascii").splitlines()
    if len(lines) != 4:
        raise ValueError("checkpoint must have 4 lines")
    return {
        "origin": lines[0],
        "size": int(lines[1]),
        "root": bytes.fromhex(lines[2]),
        "time": int(lines[3]),
    }
