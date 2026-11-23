"""The run loop: scan once into a queue, then process the queue in passes until nothing is left.

Ported from libnuke v1.3.0 pkg/nuke/nuke.go. Item states and their handling:

  new / hold       -> remove() is called. Success -> pending; HoldResource -> hold; error -> failed.
  failed           -> remove() is called again, then the item is re-listed.
  pending/waiting  -> the item's type is re-listed; once the resource is gone (or no longer passes
                      its own filter) the item is finished.
  new-dependency   -> (wait_on_dependencies only) waits until no item of a type it depends on is
                      still new/pending/waiting/held, then remove() is called.
  filtered         -> never touched again in this run.
"""
import time

from . import registry
from .errors import HoldResource, WaitResource
from .queue import Item, ItemState, Queue
from .seam import seam

# Outcomes of Nuke.run(); nuke.py maps them to exit codes.
COMPLETE = "complete"
GAVE_UP = "gave-up"


class Nuke:
    def __init__(self, filters, settings=None, pass_sleep_s=5.0, wait_on_dependencies=False,
                 resource_types=None):
        self.filters = filters
        self.settings = settings or {}
        self.pass_sleep_s = pass_sleep_s
        self.wait_on_dependencies = wait_on_dependencies
        self.resource_types = resource_types
        self.queue = Queue()
        self.failed_count = 0  # consecutive passes where only failed items were left

    # -- scan ---------------------------------------------------------------------------------

    def registrations(self):
        regs = registry.ordered()
        if self.resource_types:
            regs = [r for r in regs if r.name in self.resource_types]
        return regs

    def scan(self):
        queue = Queue()
        for reg in self.registrations():
            settings = self.settings.get(reg.name, {})
            for resource in reg.lister.list(settings):
                item = Item(resource, reg.name, reg.lister)
                if self.wait_on_dependencies and reg.depends_on:
                    item.state = ItemState.NEW_DEPENDENCY
                queue.items.append(item)
                self.filter(item)
                item.print()
        self.queue = queue
        print(f"Scan complete: {queue.total()} total, "
              f"{queue.count(ItemState.NEW, ItemState.NEW_DEPENDENCY)} nukeable, "
              f"{queue.count(ItemState.FILTERED)} filtered.", flush=True)

    def filter(self, item):
        check = getattr(item.resource, "filter", None)
        if check is not None:
            reason = check()
            if reason:
                item.state = ItemState.FILTERED
                item.reason = reason
                return
        for f in self.filters.get(item.type) or []:
            try:
                prop = item.get_property(f.property) if f.property else item.resource.string()
            except KeyError:
                continue
            match = f.match(prop)
            if f.invert:
                match = not match
            if match:
                item.state = ItemState.FILTERED
                item.reason = "filtered by config"
                return

    # -- passes -------------------------------------------------------------------------------

    def run(self):
        pass_no = 0
        while True:
            pass_no += 1
            seam("pass_start", pass_no=pass_no)

            self.handle_queue()

            if self.handle_failure():
                return GAVE_UP

            unfinished = self.queue.count(
                ItemState.NEW, ItemState.NEW_DEPENDENCY, ItemState.PENDING,
                ItemState.PENDING_DEPENDENCY, ItemState.FAILED, ItemState.WAITING, ItemState.HOLD)
            if unfinished == 0:
                return COMPLETE

            time.sleep(self.pass_sleep_s)

    def handle_failure(self):
        """True once the run should give up: three passes in a row with only failed items left."""
        processing = self.queue.count(
            ItemState.PENDING, ItemState.PENDING_DEPENDENCY, ItemState.HOLD,
            ItemState.WAITING, ItemState.NEW, ItemState.NEW_DEPENDENCY)
        failed = self.queue.count(ItemState.FAILED)

        if processing == 0 and failed > 0:
            if self.failed_count >= 2:
                print("There are resources in failed state, but none are ready for deletion, anymore.",
                      flush=True)
                for item in self.queue.items:
                    if item.state == ItemState.FAILED:
                        item.print()
                return True
            self.failed_count += 1
        else:
            self.failed_count = 0
        return False

    def handle_queue(self):
        list_cache = {}
        for item in list(self.queue.items):
            state = item.state
            if state in (ItemState.NEW, ItemState.HOLD):
                self.handle_remove(item)
                item.print()
            elif state in (ItemState.NEW_DEPENDENCY, ItemState.PENDING_DEPENDENCY):
                self.handle_wait_dependency(item)
                item.print()
            elif state == ItemState.FAILED:
                self.handle_remove(item)
                self.handle_wait(item, list_cache)
                item.print()
            elif state == ItemState.PENDING:
                self.handle_wait(item, list_cache)
                item.state = ItemState.WAITING if item.state == ItemState.PENDING else item.state
                item.print()
            elif state == ItemState.WAITING:
                self.handle_wait(item, list_cache)
                item.print()

        q = self.queue
        print(f"Removal requested: "
              f"{q.count(ItemState.WAITING, ItemState.PENDING, ItemState.PENDING_DEPENDENCY, ItemState.NEW_DEPENDENCY, ItemState.HOLD)} waiting, "
              f"{q.count(ItemState.FAILED)} failed, {q.count(ItemState.FILTERED)} skipped, "
              f"{q.count(ItemState.FINISHED)} finished\n", flush=True)

    def handle_remove(self, item):
        try:
            item.resource.remove()
        except HoldResource as hold:
            item.state = ItemState.HOLD
            item.reason = str(hold)
            return
        except Exception as err:  # any other error: failed, retried next pass
            item.state = ItemState.FAILED
            item.reason = f"{type(err).__name__}: {err}"
            return
        item.state = ItemState.PENDING
        item.reason = ""

    def handle_wait_dependency(self, item):
        reg = registry.get(item.type)
        left = 0
        for dep in reg.depends_on:
            left += self.queue.count_by_type(
                dep, ItemState.NEW, ItemState.NEW_DEPENDENCY, ItemState.PENDING,
                ItemState.PENDING_DEPENDENCY, ItemState.WAITING, ItemState.HOLD)
        if left == 0:
            self.handle_remove(item)
            return
        item.state = ItemState.PENDING_DEPENDENCY
        item.reason = f"left: {left}"

    def handle_wait(self, item, cache):
        hook = getattr(item.resource, "handle_wait", None)
        if hook is not None:
            try:
                hook()
            except WaitResource:
                item.state = ItemState.WAITING
                return
            except Exception as err:
                item.state = ItemState.FAILED
                item.reason = f"{type(err).__name__}: {err}"
                return

        if item.type not in cache:
            try:
                cache[item.type] = item.lister.list(self.settings.get(item.type, {}))
            except Exception as err:
                item.state = ItemState.FAILED
                item.reason = f"{type(err).__name__}: {err}"
                return

        for other in cache[item.type]:
            if not item.equals(other):
                continue
            check = getattr(other, "filter", None)
            if check is not None and check():
                break  # no longer something this run would remove: done
            return  # still there
        item.state = ItemState.FINISHED
        item.reason = ""
