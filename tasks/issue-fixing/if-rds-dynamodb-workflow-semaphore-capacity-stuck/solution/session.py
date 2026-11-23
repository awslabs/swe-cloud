import time
import threading
import psycopg


class SessionProxy:
    def __init__(self, dsn):
        self.lock = threading.RLock()
        self.dsn = dsn
        self.conn = None
        self.closed = False

    def reconnect(self):
        if self.conn is not None:
            self.conn.close()
        self.closed = True
        last = None
        for delay in (0, .1, .2, .4, .8, 1.6):
            time.sleep(delay)
            try:
                self.conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=1)
                self.closed = False
                return
            except psycopg.OperationalError as exc:
                last = exc
        raise last

    def run(self, fn):
        with self.lock:
            return self._run(fn)

    def _run(self, fn):
        if self.closed or self.conn is None:
            self.reconnect()
        try:
            return fn(self.conn)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            self.reconnect()
            return fn(self.conn)
