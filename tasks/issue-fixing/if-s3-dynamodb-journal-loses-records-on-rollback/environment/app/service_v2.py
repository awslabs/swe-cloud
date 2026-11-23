"""Build v2 — the prepared build.

Reads both envelopes, keeps writing the XML envelope, and performs counter
increments. This is the build the fleet rolls to before any JSON writer is
activated, and the build a rollback reinstalls.
"""

import records
from service import parse_args, serve

PROFILE = {
    "build": "v2",
    "reads": [records.XML_FORMAT, records.JSON_FORMAT],
    "writes": records.XML_FORMAT,
    "kinds": ["increment"],
}

if __name__ == "__main__":
    serve(parse_args(), PROFILE)
