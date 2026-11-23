"""Classic static consumer membership and incremental sticky assignment."""
import json
import urllib.request
from collections import defaultdict
from wire import (s, i, a, b, Broker, subscription, read_subscription,
                  assignment, read_assignment, check)

SESSION_MS = 45000
REBALANCE_MS = 30000
HEARTBEAT_S = 1.0


class Group:
    def __init__(self, bootstrap, group, instance, topic, metadata):
        self.broker = Broker(bootstrap).coordinator(group)
        self.group, self.instance, self.topic = group, instance, topic
        self.metadata = metadata
        self.member = ''
        self.generation = -1
        self.owned = set()
        self.user_data = b''
        self.initial_metadata = None
        state = self.broker.describe(group)
        if state['state'] == 'Stable':
            self.initial_metadata = next((m['metadata'] for m in state['members']
                                          if m['instance'] == instance), None)

    def balance(self, members):
        subscriptions = {}
        prior = {}
        instances = {}
        for mid, instance, metadata in members:
            topics, owned = read_subscription(metadata)
            subscriptions[mid] = topics
            prior[mid] = owned
            instances[mid] = instance or mid
        result = {mid: set() for mid in subscriptions}
        owners = {tp: mid for mid, parts in prior.items() for tp in parts}
        topics = sorted({topic for items in subscriptions.values() for topic in items})
        for topic in topics:
            eligible = sorted((mid for mid in subscriptions if topic in subscriptions[mid]),
                              key=lambda mid: instances[mid])
            count = len(self.metadata.list_topics(topic, timeout=10).topics[topic].partitions)
            quota = {mid: count//len(eligible)+(index < count%len(eligible))
                     for index, mid in enumerate(eligible)}
            free = set((topic, p) for p in range(count))
            for mid in eligible:
                keep = sorted(prior[mid] & free)[:quota[mid]]
                result[mid].update(keep)
                free.difference_update(keep)
            for mid in eligible:
                take = sorted(free)[:quota[mid]-len(result[mid] & {(topic, p) for p in range(count)})]
                result[mid].update(take)
                free.difference_update(take)
        # Cooperative adjustment: a new owner waits until the prior owner has revoked.
        for mid in result:
            result[mid] = {tp for tp in result[mid] if tp not in owners or owners[tp] == mid}
        return result

    def join_sync(self):
        metadata = self.initial_metadata
        if metadata is None: metadata = subscription(self.topic, self.owned, self.user_data)
        body = s(self.group)+i(SESSION_MS)+i(REBALANCE_MS)+s(self.member)+s(self.instance)
        body += s('consumer')+a([s('cooperative-sticky')+b(metadata)])
        r = self.broker.rpc(11, 5, body)
        r.i(); error = r.h(); generation = r.i()
        protocol, leader, member = r.s(), r.s(), r.s()
        members = [(r.s(), r.s(), r.b()) for _ in range(r.i())]
        check(error)
        self.member, self.generation = member, generation
        self.initial_metadata = None
        if protocol != 'cooperative-sticky': raise ValueError('unexpected assignor')
        plan = self.balance(members) if leader == member else {}
        body = s(self.group)+i(generation)+s(member)+s(self.instance)
        body += a([s(mid)+b(assignment(parts)) for mid, parts in plan.items()])
        r = self.broker.rpc(14, 3, body)
        r.i(); check(r.h())
        new = read_assignment(r.b())
        print(json.dumps(dict(event='sync', instance=self.instance, generation=generation,
                              partitions=sorted(new))), flush=True)
        return new

    def heartbeat(self):
        body = s(self.group)+i(self.generation)+s(self.member)+s(self.instance)
        r = self.broker.rpc(12, 3, body, timeout=5)
        r.i(); check(r.h())

    def revoked(self, parts, barrier_url):
        if not barrier_url: return
        body = json.dumps(dict(instance_id=self.instance, partitions=[p for _, p in sorted(parts)],
                               topic=self.topic)).encode()
        request = urllib.request.Request(barrier_url.rstrip('/')+'/revoked', data=body,
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=25) as response: response.read()

    def leave(self):
        self.broker.leave(self.group, self.instance, self.member)
