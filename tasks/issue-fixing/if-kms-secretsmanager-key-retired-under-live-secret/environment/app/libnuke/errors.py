class HoldResource(Exception):
    """Raised by Resource.remove() to park an item in Hold; it is retried on the next pass."""


class WaitResource(Exception):
    """Raised by a resource's wait hook while a removal is still in progress."""
