from session import SessionProxy


class Database:
    def __init__(self, dsn, owner, inactive):
        self.session = SessionProxy(dsn)
        self.owner = owner
        self.inactive = inactive

    def query(self, sql, args=()):
        def perform(conn):
            cur = conn.execute(sql, args)
            return cur.fetchall() if cur.description else []
        return self.session.run(perform)

    def heartbeat(self):
        self.query("INSERT INTO sync_controller VALUES (%s, CURRENT_TIMESTAMP) "
                   "ON CONFLICT(controller) DO UPDATE SET time=EXCLUDED.time", (self.owner,))

    def enqueue(self, item):
        self.query("INSERT INTO sync_state VALUES (%s,%s,%s,false,%s,%s,%s) "
                   "ON CONFLICT DO NOTHING", ("sem/" + item['semaphore'], item['wf_key'],
                                              self.owner, int(item['priority']), item['created_at'], item['uid']))

    def acquire(self, item):
        name, key = "sem/" + item['semaphore'], item['wf_key']
        self.enqueue(item)

        def transaction(conn):
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                own = conn.execute("SELECT held FROM sync_state WHERE name=%s AND workflowkey=%s "
                                   "AND controller=%s", (name, key, self.owner)).fetchone()
                if own and own[0]:
                    return True
                size = conn.execute("SELECT sizelimit FROM sync_limit WHERE name=%s",
                                    (item['semaphore'],)).fetchone()[0]
                count = conn.execute("SELECT count(*) FROM sync_state WHERE name=%s AND held", (name,)).fetchone()[0]
                front = conn.execute("SELECT s.workflowkey,s.controller FROM sync_state s "
                                     "JOIN sync_controller c ON s.controller=c.controller "
                                     "WHERE s.name=%s AND NOT s.held AND c.time > "
                                     "CURRENT_TIMESTAMP - (%s * interval '1 second') "
                                     "ORDER BY s.priority DESC,s.time ASC,s.workflowkey,s.controller LIMIT 1",
                                     (name, self.inactive)).fetchone()
                if count >= size or front != (key, self.owner):
                    return False
                changed = conn.execute("UPDATE sync_state SET held=true WHERE name=%s AND workflowkey=%s "
                                       "AND controller=%s AND NOT held", (name, key, self.owner)).rowcount
                return changed == 1
        return self.session.run(transaction)

    def release(self, name, key):
        self.query("DELETE FROM sync_state WHERE name=%s AND workflowkey=%s AND controller=%s AND held",
                   (name, key, self.owner))

    def dequeue(self, name, key):
        self.query("DELETE FROM sync_state WHERE name=%s AND workflowkey=%s AND controller=%s AND NOT held",
                   (name, key, self.owner))

    def has_hold(self, name, key):
        return bool(self.query("SELECT 1 FROM sync_state WHERE name=%s AND workflowkey=%s AND controller=%s AND held",
                               (name, key, self.owner)))
