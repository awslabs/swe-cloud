"""Example handler for local runs: `python3 -m dq worker --queue Q --handler handlers:run`.

Production handlers live in the monolith; this one just pretends to send the
welcome email so a queue can be exercised end to end.
"""

import time


def run(job):
    time.sleep(float(job["data"].get("seconds", 0.2)))
    return {"sent_to": job["dedup_id"], "attempt": job["attempt"]}
