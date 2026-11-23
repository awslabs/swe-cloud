"""Build v1 — the released baseline.

Reads and writes the XML envelope only, and performs counter increments.
"""

import records
from service import parse_args, serve

PROFILE = {
    "build": "v1",
    "reads": [records.XML_FORMAT],
    "writes": records.XML_FORMAT,
    "kinds": ["increment"],
}

if __name__ == "__main__":
    serve(parse_args(), PROFILE)
