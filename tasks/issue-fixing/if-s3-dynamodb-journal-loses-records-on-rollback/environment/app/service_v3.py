"""Build v3 — the activated build.

Reads both envelopes, writes the JSON envelope, and additionally carries a
new operation: attaching a named attribute with an arbitrary JSON value. The
attribute is part of the same whole record the other builds rewrite.
"""

import records
from service import parse_args, serve

PROFILE = {
    "build": "v3",
    "reads": [records.XML_FORMAT, records.JSON_FORMAT],
    "writes": records.JSON_FORMAT,
    "kinds": ["increment", "set_attr"],
}

if __name__ == "__main__":
    serve(parse_args(), PROFILE)
