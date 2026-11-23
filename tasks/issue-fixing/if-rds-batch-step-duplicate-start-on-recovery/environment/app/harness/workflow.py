"""Business dependencies and retry policy for the nightly batch."""
DEPENDENCIES = {'extract': (), 'load': ('extract',), 'audit': ('extract',),
                'publish': ('load', 'audit')}
RETRIES = {'extract': 1, 'load': 0, 'audit': 0, 'publish': 0}
BATCHES = {'extract': 10, 'load': 2, 'audit': 3, 'publish': 1}
