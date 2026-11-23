"""What this process can see of the executions it has dispatched.

The running record in the table says who owns a job; this is the dispatcher's
own picture of that, built from the attempts it started itself and closed as
their completions arrive.  A forbid job is skipped while this process holds it
in view.
"""


class RunningView:
    def __init__(self, st):
        self.st = st
        self._dispatched = {}

    def note_dispatch(self, job, run_id):
        self._dispatched[job] = run_id

    def forget(self, job):
        self._dispatched.pop(job, None)

    def busy(self, job):
        return job in self._dispatched

    def sweep(self):
        """Drop attempts whose record shows them closed."""
        for job in list(self._dispatched):
            row = self.st.active(job)
            if row is None or row.get("run_id") != self._dispatched[job]:
                self._dispatched.pop(job, None)
