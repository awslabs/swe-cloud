"""The node's local database file: --db-dir/data.sqlite.

Same schema as the db.sqlite snapshot objects:
  rows(id INTEGER PRIMARY KEY, payload TEXT)
  meta(k TEXT PRIMARY KEY, v INTEGER)  -- k='seq': commits applied, k='head': last committed id
"""
import os
import sqlite3

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS rows(id INTEGER PRIMARY KEY, payload TEXT);"
    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v INTEGER);"
    "INSERT OR IGNORE INTO meta(k, v) VALUES ('seq', 0);"
    "INSERT OR IGNORE INTO meta(k, v) VALUES ('head', 0);"
)

DB_FILE = "data.sqlite"


def create_empty(path):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def apply_records(conn, records):
    for rec in records:
        conn.execute(
            "INSERT OR REPLACE INTO rows(id, payload) VALUES (?, ?)", (rec["id"], rec["payload"])
        )
        conn.execute("UPDATE meta SET v = ? WHERE k = 'seq'", (rec["seq"],))
        conn.execute("UPDATE meta SET v = ? WHERE k = 'head'", (rec["id"],))


def read_state(path):
    """Returns (seq, head, sorted ids) of a database file in the strata schema."""
    conn = sqlite3.connect(path)
    try:
        meta = dict(conn.execute("SELECT k, v FROM meta").fetchall())
        ids = [r[0] for r in conn.execute("SELECT id FROM rows ORDER BY id")]
    finally:
        conn.close()
    return int(meta.get("seq", 0)), int(meta.get("head", 0)), ids


class LocalDb:
    def __init__(self, db_dir):
        os.makedirs(db_dir, exist_ok=True)
        self.dir = db_dir
        self.path = os.path.join(db_dir, DB_FILE)
        if not os.path.exists(self.path):
            create_empty(self.path)

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        return conn

    def seq(self):
        conn = self._connect()
        try:
            return int(conn.execute("SELECT v FROM meta WHERE k = 'seq'").fetchone()[0])
        finally:
            conn.close()

    def state(self):
        return read_state(self.path)

    def apply(self, record):
        conn = self._connect()
        try:
            with conn:
                apply_records(conn, [record])
        finally:
            conn.close()

    def snapshot(self):
        """Consistent copy of the database file: returns (bytes, seq)."""
        tmp = self.path + ".snap"
        src = self._connect()
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        try:
            with open(tmp, "rb") as f:
                data = f.read()
            seq, _, _ = read_state(tmp)
        finally:
            os.remove(tmp)
        return data, seq

    def replace_with(self, path):
        os.replace(path, self.path)
