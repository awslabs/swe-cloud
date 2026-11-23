"""Multi-writer transaction log store for tables held in object storage.

Object storage does not give writers mutual exclusion, so every version is
coordinated through a per-version entry in a DynamoDB table before it becomes
visible in the log:

  key      (tablePath, fileName)     one entry per table version
  tempPath                           object holding this version's actions
  complete                           publication of the version has finished
  expireTime                         set only once complete: when the entry has
                                     done its job and may be reclaimed

To commit version N:
  1. ensure version n-1 exists, finishing its publication if it was left
     incomplete;
  2. PREPARE: write the actions to a temporary object, then insert the entry
     for N with mutual exclusion (this is the writer's claim);
  3. COMMIT: copy the temporary object onto the version's log object, and only
     if that object is still absent;
  4. ACKNOWLEDGE: overwrite the entry with complete=true and an expiry.

The mutual-exclusion entry only excludes other writers while it exists; once
the reclaimer has removed it, the only durable evidence that version N was ever
committed is the log object itself, so the object's presence decides. A writer
that finds the object already there has not necessarily lost the version: the
entry says whose publication that object finished, because a claim is only ever
completed by a publication of the temp object it records. A writer whose own
claim comes back complete was overtaken by its own recovery and owes its caller
the truth; any other claim on the version belongs to an earlier writer, who is
reported a conflict and whose claim is settled so the reclaimer can drop it.
Completing an entry is a conditional overwrite of this writer's own claim, and
the reclaimer only ever takes completed entries -- so a writer whose entry has
gone learns that its version's publication was already finished, which is the
answer it owes its caller.

Readers list from the log after finishing the latest incomplete entry the same
way. Completed entries are bookkeeping only: the reclaimer removes them once
their expiry passes, while incomplete entries are kept because another process
may still need to finish them.
"""

import re
import sys
import time
import uuid

from storage import EntryExists, EntryGone

FILE_NAME_DIGITS = 20
LOG_DIR = "log"
TEMP_DIR = ".tmp"
RETENTION_SECONDS = 2

_FILE_NAME_RE = re.compile(r"^(\d{%d})\.json$" % FILE_NAME_DIGITS)


def file_name(version):
    return "%0*d.json" % (FILE_NAME_DIGITS, version)


def parse_version(name):
    match = _FILE_NAME_RE.match(name)
    return int(match.group(1)) if match else None


class ExternalLogStore:
    def __init__(self, storage):
        self.st = storage
        self.table_path = storage.table_path

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def snapshot(self):
        """Latest visible version of this table, or None for a new table."""
        versions = self._visible_versions()
        return max(versions) if versions else None

    def list_from(self, start_version):
        entry = self.st.latest_entry()
        if entry is not None and not entry["complete"]:
            self.fix(entry)
        result = []
        for version in self._visible_versions():
            if version >= start_version:
                actions = self.st.read_object(self._final_key(file_name(version))).decode()
                result.append([version, actions])
        return result

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def write(self, version, actions):
        name = file_name(version)
        final = self._final_key(name)

        self._ensure_previous(version)

        # PREPARE the commit
        temp_path = self._new_temp_path(name)
        entry = {
            "tablePath": self.table_path,
            "fileName": name,
            "tempPath": temp_path,
            "complete": False,
            "expireTime": None,
        }
        self.st.write_object(self._temp_key(temp_path), actions.encode())
        try:
            self.st.put_entry_new(entry)
        except EntryExists:
            return "conflict"

        # COMMIT the commit to the log, without replacing an existing object
        if self.st.object_exists(final):
            # The version's object is already there. This entry's publication
            # is finished by whoever completed the entry -- and a claim is
            # only completed over the temp object it records, so an entry that
            # is still this writer's own and already complete means the
            # version holds these actions. Anything else is an earlier
            # writer's version.
            current = self.st.get_entry(name)
            if (
                current is not None
                and current["tempPath"] == temp_path
                and current["complete"]
            ):
                return "committed"
            self._complete(entry)
            return "conflict"
        self.st.write_object(final, self.st.read_object(self._temp_key(temp_path)))

        # ACKNOWLEDGE the commit
        self._complete(entry)
        return "committed"

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------

    def fix(self, entry):
        """Finish the publication recorded by an incomplete entry."""
        copied = False
        for retry in range(4):
            try:
                final = self._final_key(entry["fileName"])
                if not copied and not self.st.object_exists(final):
                    self.st.write_object(
                        final, self.st.read_object(self._temp_key(entry["tempPath"]))
                    )
                    copied = True
                self._complete(entry)
                return
            except EntryGone:
                # The entry was finished and reclaimed elsewhere.
                return
            except Exception as exc:
                print("fix attempt %d failed: %r" % (retry, exc), file=sys.stderr)
                if retry >= 3:
                    raise

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _ensure_previous(self, version):
        if version > 0:
            prev_name = file_name(version - 1)
            prev = self.st.get_entry(prev_name)
            if prev is not None and not prev["complete"]:
                self.fix(prev)
            elif not self.st.object_exists(self._final_key(prev_name)):
                raise FileSystemError("previous commit %s doesn't exist" % prev_name)
        else:
            existing = self.st.get_entry(file_name(0))
            if (
                existing is not None
                and existing["complete"]
                and not self.st.object_exists(self._final_key(file_name(0)))
            ):
                raise FileSystemError(
                    "old entries for table %s still exist" % self.table_path
                )

    def _complete(self, entry):
        """ACKNOWLEDGE: give this writer's claim its expiry.

        An entry that is already gone was completed before this call -- the
        reclaimer only ever takes completed entries -- so the publication this
        acknowledges was finished either way.
        """
        try:
            self.st.put_entry_existing(self._as_complete(entry))
        except EntryGone:
            pass

    def _as_complete(self, entry):
        complete = dict(entry)
        complete["complete"] = True
        complete["expireTime"] = int(time.time()) + RETENTION_SECONDS
        return complete

    def _new_temp_path(self, name):
        return "%s/%s.%s" % (TEMP_DIR, name, uuid.uuid4())

    def _final_key(self, name):
        return "%s/%s/%s" % (self.table_path, LOG_DIR, name)

    def _temp_key(self, temp_path):
        return "%s/%s/%s" % (self.table_path, LOG_DIR, temp_path)

    def _visible_versions(self):
        prefix = "%s/%s/" % (self.table_path, LOG_DIR)
        versions = []
        for key in self.st.list_object_keys(prefix):
            version = parse_version(key.rsplit("/", 1)[-1])
            if version is not None:
                versions.append(version)
        return sorted(versions)
