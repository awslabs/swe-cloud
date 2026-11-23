import enum


class ItemState(enum.Enum):
    NEW = "new"
    NEW_DEPENDENCY = "new-dependency"
    HOLD = "hold"
    PENDING = "pending"
    PENDING_DEPENDENCY = "pending-dependency"
    WAITING = "waiting"
    FAILED = "failed"
    FILTERED = "filtered"
    FINISHED = "finished"


class Item:
    """One resource in the queue and its current state."""

    def __init__(self, resource, type_name, lister):
        self.resource = resource
        self.type = type_name
        self.lister = lister
        self.state = ItemState.NEW
        self.reason = ""

    def get_property(self, key):
        props = self.resource.properties()
        if key not in props:
            raise KeyError(key)
        return props[key]

    def equals(self, other):
        return type(other) is type(self.resource) and other.string() == self.resource.string()

    def print(self):
        line = f"{self.type} - {self.resource.string()} - {self.state.value}"
        if self.reason:
            line += f" - {self.reason}"
        print(line, flush=True)


class Queue:
    def __init__(self):
        self.items = []

    def total(self):
        return len(self.items)

    def count(self, *states):
        return sum(1 for item in self.items if item.state in states)

    def count_by_type(self, type_name, *states):
        return sum(1 for item in self.items if item.type == type_name and item.state in states)
