"""Runtime settings for the delayed-task runner.

Values here are the fleet's operating configuration. They are read once at
startup; a restart is required for a change to take effect.
"""

# Seconds a received delivery stays hidden in the queue after this worker
# takes it. The queue's own default is 4; this override was put in place
# after the duplicate-execution incidents and has kept them away since.
VISIBILITY_S = 40

# How often the run loop looks at the queue and at the local schedule.
POLL_INTERVAL_S = 0.2

# Bounded SDK attempts, so a slow call cannot stall the loop.
API_TIMEOUT_S = 1
API_ATTEMPTS = 2
