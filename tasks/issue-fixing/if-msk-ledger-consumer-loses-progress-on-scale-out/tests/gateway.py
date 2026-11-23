import asyncio
import contextlib
import random
import time
import traceback
from protocol import Reader, num, request, response


class Gateway:
    def __init__(self, broker, topic, produce):
        self.broker = broker.rsplit(':', 1)
        self.topic, self.produce = topic, produce
        self.events = []
        self.phase = 'warm'
        self.members = {}
        self.gen = None
        self.V, self.R = set(), set()
        self.markers = {}
        self.marker_rejected = set()
        self.sync1 = None
        self.release1_scheduled = False
        self.join2 = None
        self.commit2 = None
        self.blocked = None
        self.waitq = []
        self.g2seen = set()
        self.sync2seen = set()
        self.errors = []
        self.connections = set()
        self.tasks = set()
        self.ch = None
        self.start_scale = None

    def log(self, kind, **facts):
        e = dict(seq=len(self.events), ts=time.monotonic(), kind=kind, **facts)
        self.events.append(e)
        return e

    def task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def start(self):
        self.server = await asyncio.start_server(self.connection, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def send(self, held):
        writer, b, req = held
        if writer.is_closing():
            return
        writer.write(num('i', len(b))+b)
        await writer.drain()
        self.log('forward', **req)

    async def release1(self):
        if self.sync1:
            held, self.sync1 = self.sync1, None
            self.phase = 'round1sync'
            await self.send(held)

    async def release1_after_dispatch(self):
        # A short transport drain allows the bounded dispatcher to admit the
        # burst. Slow HTTP calls continue independently through SyncGroup.
        await asyncio.sleep(.3)
        await self.release1()

    async def timeout1(self):
        await asyncio.sleep(7)
        await self.release1()

    async def timeout2(self):
        # Five seconds for application plus its one-second attempt budget.
        await asyncio.sleep(7)
        await self.release_join2()

    async def release_join2(self):
        if self.join2:
            held, self.join2 = self.join2, None
            await self.send(held)

    async def phase2_record(self):
        p = min(self.R)
        self.ch = await self.produce(p)
        self.log('produced_retained', partition=p, offset=self.ch)
        self.task(self.timeout2())

    async def route(self, writer, b, req):
        held = writer, b, req
        w, api = req['worker'], req['api']
        self.log('request', **req)
        if w == 'W2' and api == 11 and self.phase == 'warm':
            self.start_scale = time.monotonic()
            self.phase = 'round1'
        if api == 14 and w == 'W1' and self.phase == 'round1' and req['plans']:
            own = set(req['plans'].get(self.members['W1'], {}).get(self.topic, []))
            self.R = own
            self.V = set(range(6))-own
            self.log('partition_plan', revoked=sorted(self.V), retained=sorted(self.R))
            self.sync1 = held
            self.phase = 'hold1'
            # One revoked partition gets only its marker: once that call is done and its
            # commit is rejected, nothing is in flight there, but its progress is still owed.
            quiet = random.choice(sorted(self.V))
            self.log('quiet_partition', partition=quiet)
            for p in sorted(self.V):
                self.markers[p] = await self.produce(p)
                self.log('produced_marker', partition=p, offset=self.markers[p])
                for _ in range(0 if p == quiet else 15):
                    offset = await self.produce(p)
                    self.log('produced_handover', partition=p, offset=offset)
            self.task(self.timeout1())
            return
        if api == 11 and w == 'W1' and self.phase == 'round1done':
            self.phase = 'wait2'
            self.log('rejoin_boundary', **req)
            await self.send(held)
            self.task(self.phase2_record())
            return
        if api == 11 and w == 'W2' and self.phase == 'wait2':
            self.join2 = held
            if self.commit2:
                await self.release_join2()
            return
        if w == 'W1' and self.blocked is not None:
            self.waitq.append(held)
            return
        if (api == 8 and w == 'W1' and self.phase == 'wait2' and not self.commit2
                and self.ch is not None
                and req['offsets'].get(f'{self.topic}:{min(self.R)}', -1) >= self.ch+1):
            self.commit2 = held
            self.blocked = writer
            self.log('held_commit', **req)
            await self.release_join2()
            return
        await self.send(held)

    async def replied(self, req, resp):
        self.log('response', **resp)
        w, api = req['worker'], req['api']
        if api == 11 and resp['error'] == 0:
            self.members.setdefault(w, resp['member'])
            if self.gen is None:
                self.gen = resp['gen']
            if self.phase == 'wait2' and resp['gen'] == self.gen+2:
                self.g2seen.add(w)
                if self.g2seen == {'W1', 'W2'}:
                    self.phase = 'round2'
                    if self.commit2:
                        await self.send(self.commit2)
                    self.blocked = None
                    for held in self.waitq:
                        await self.send(held)
                    self.waitq = []
        if api == 8 and self.phase == 'hold1' and w == 'W1':
            for key, part in resp['parts'].items():
                p = int(key.rsplit(':', 1)[1])
                if p in self.markers and part['offset'] >= self.markers[p]+1:
                    self.log('marker_reply', partition=p, error=part['error'])
                    if part['error'] == 27:
                        self.marker_rejected.add(p)
            if self.marker_rejected == self.V and self.V and not self.release1_scheduled:
                self.release1_scheduled = True
                self.task(self.release1_after_dispatch())
        if api == 14 and w == 'W1' and self.phase == 'round1sync':
            self.phase = 'round1done'
            self.log('handover_boundary', **resp)
        if api == 14 and resp.get('gen') == self.gen+2 and resp.get('error') == 0:
            self.sync2seen.add(w)
            if self.sync2seen == {'W1', 'W2'}:
                self.phase = 'quiet'
                self.log('settled')

    async def connection(self, reader, writer):
        upstream = None
        try:
            ur, upstream = await asyncio.open_connection(self.broker[0], int(self.broker[1]))
            self.connections.update([writer, upstream])
            pending = {}

            async def upstream_loop():
                while True:
                    size = int.from_bytes(await ur.readexactly(4), 'big')
                    b = await ur.readexactly(size)
                    corr = int.from_bytes(b[:4], 'big', signed=True)
                    req = pending.pop(corr)
                    out, resp = response(b, req, self.port)
                    writer.write(num('i', len(out))+out)
                    await writer.drain()
                    await self.replied(req, resp)

            replies = self.task(upstream_loop())
            try:
                while True:
                    size = int.from_bytes(await reader.readexactly(4), 'big')
                    b = await reader.readexactly(size)
                    req = request(b)
                    pending[req['corr']] = req
                    await self.route(upstream, b, req)
            finally:
                replies.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await replies
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            self.errors.append(traceback.format_exc())
        finally:
            for conn in (writer, upstream):
                if conn:
                    conn.close()
                    self.connections.discard(conn)

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for conn in list(self.connections):
            conn.close()
