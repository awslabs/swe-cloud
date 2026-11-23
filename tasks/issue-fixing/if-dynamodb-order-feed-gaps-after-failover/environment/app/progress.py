"""Ordered delivery settlement and batched checkpoint persistence."""
from collections import deque


class Progress:
    def __init__(self, store, limit, positions):
        self.store = store
        self.limit = limit
        self.shards = {}
        self.deliveries = {}
        self.positions = positions

    def track(self, shard, sequence):
        state = self.shards.setdefault(shard, {
            "queue": deque(), "acks": 0,
            "frontier": self.positions.get(shard),
            "persisted": self.positions.get(shard),
        })
        batch = {"seq": sequence, "acked": False, "shard": shard}
        state["queue"].append(batch)
        self.deliveries[sequence] = batch

    def ack(self, sequence):
        batch = self.deliveries.get(sequence)
        if batch is None or batch["acked"]:
            return
        batch["acked"] = True
        shard = batch["shard"]
        state = self.shards[shard]
        state["acks"] += 1
        while state["queue"] and state["queue"][0]["acked"]:
            done = state["queue"].popleft()
            state["frontier"] = done["seq"]
            del self.deliveries[done["seq"]]
        if state["acks"] >= self.limit:
            self.persist(shard, state)

    def persist(self, shard, state):
        frontier = state["frontier"]
        if frontier is None or frontier == state["persisted"]:
            return
        self.store.save(shard, frontier)
        state["persisted"] = frontier
        state["acks"] = 0

    def flush(self):
        for shard, state in self.shards.items():
            self.persist(shard, state)
