"""Record envelopes for the object journal.

A record is one S3 object. Two physical formats exist: an XML envelope
(format_version 1) and a JSON envelope (format_version 2). The version tag
travels inside the body so a reader can tell what it is looking at without
trusting metadata.
"""

import json
import xml.etree.ElementTree as ET

XML_FORMAT = 1
JSON_FORMAT = 2

FORMAT_NAMES = {XML_FORMAT: "xml", JSON_FORMAT: "json"}


class RecordError(Exception):
    """The body is not a readable record in the format claimed."""


class Record:
    """One logical record.

    ``counter`` is the field every build interprets. ``attrs`` holds every
    other attribute the envelope carried, keyed by name.
    """

    __slots__ = ("counter", "attrs")

    def __init__(self, counter=0, attrs=None):
        self.counter = counter
        self.attrs = dict(attrs or {})

    def to_view(self):
        return {"counter": self.counter, "attrs": dict(self.attrs)}


def sniff_format(text):
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return XML_FORMAT
    return JSON_FORMAT


def from_xml(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RecordError("bad xml envelope: %s" % exc)
    if root.tag != "record":
        raise RecordError("bad xml envelope root: %s" % root.tag)
    counter_node = root.find("counter")
    if counter_node is None or counter_node.text is None:
        raise RecordError("xml envelope has no counter")
    try:
        counter = int(counter_node.text.strip())
    except ValueError:
        raise RecordError("xml counter is not an integer")
    return Record(counter)


def from_json(text):
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise RecordError("bad json envelope: %s" % exc)
    if not isinstance(doc, dict):
        raise RecordError("json envelope is not an object")
    if "counter" not in doc:
        raise RecordError("json envelope has no counter")
    counter = doc["counter"]
    if not isinstance(counter, int) or isinstance(counter, bool):
        raise RecordError("json counter is not an integer")
    return Record(counter)


def decode(text, fmt=None):
    """Decode a body in the given format (or the sniffed one)."""
    if fmt is None:
        fmt = sniff_format(text)
    if fmt == XML_FORMAT:
        return XML_FORMAT, from_xml(text)
    if fmt == JSON_FORMAT:
        return JSON_FORMAT, from_json(text)
    raise RecordError("unknown format_version %r" % (fmt,))


def to_xml(rec):
    root = ET.Element("record", {"format_version": str(XML_FORMAT)})
    ET.SubElement(root, "counter").text = str(rec.counter)
    for name in sorted(rec.attrs):
        node = ET.SubElement(root, "attr", {"name": name})
        node.text = json.dumps(rec.attrs[name], sort_keys=True)
    return ET.tostring(root, encoding="unicode")


def to_json(rec):
    doc = {
        "format_version": JSON_FORMAT,
        "counter": rec.counter,
        "attrs": {name: rec.attrs[name] for name in sorted(rec.attrs)},
    }
    return json.dumps(doc, sort_keys=True)


def encode(rec, fmt):
    if fmt == XML_FORMAT:
        return to_xml(rec)
    if fmt == JSON_FORMAT:
        return to_json(rec)
    raise RecordError("unknown format_version %r" % (fmt,))
