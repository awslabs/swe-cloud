"""Logical contents of an Iceberg v2 table's current snapshot, read from its metadata file.

Applies position deletes (to data files with a lower or equal sequence number) and equality
deletes (to data files with a strictly lower sequence number) per the Iceberg specification,
so merge-on-read and copy-on-write tables read the same. Columns are matched by field id when
the Parquet file carries ids, otherwise by name; columns missing from a file read as null.
"""

import os

import pyarrow.parquet as pq
from pyarrow import fs as pafs
from pyiceberg.manifest import DataFileContent
from pyiceberg.table import StaticTable

ENDPOINT = "http://engines:4566"


def _properties():
    ak, sk = os.environ.get("AWS_ACCESS_KEY_ID", "test"), os.environ.get("AWS_SECRET_ACCESS_KEY", "test")
    return {"s3.endpoint": ENDPOINT, "s3.region": "us-east-1", "s3.access-key-id": ak,
            "s3.secret-access-key": sk, "s3.force-virtual-addressing": "false"}


def _filesystem():
    p = _properties()
    return pafs.S3FileSystem(endpoint_override=ENDPOINT, region="us-east-1", access_key=p["s3.access-key-id"],
                             secret_key=p["s3.secret-access-key"], force_virtual_addressing=False, scheme="http")


def _records(fs, path, schema=None):
    table = pq.read_table(path.split("://", 1)[1], filesystem=fs)
    if schema is None:
        return table.to_pylist()
    by_id = {}
    for i, field in enumerate(table.schema):
        meta = field.metadata or {}
        fid = meta.get(b"PARQUET:field_id")
        if fid is not None:
            by_id[int(fid)] = i
    columns = {}
    for field in schema.fields:
        if field.field_id in by_id:
            columns[field.name] = table.column(by_id[field.field_id]).to_pylist()
        elif not by_id and field.name in table.column_names:
            columns[field.name] = table.column(field.name).to_pylist()
        else:
            columns[field.name] = [None] * table.num_rows
    return [{name: columns[name][i] for name in columns} for i in range(table.num_rows)]


class IcebergTable:
    def __init__(self, schema_fields, rows, snapshot_count, identifier_fields):
        self.fields = schema_fields  # [(name, type string)]
        self.rows = rows  # list of dicts keyed by current schema field names
        self.snapshot_count = snapshot_count
        self.identifier_fields = identifier_fields

    @property
    def columns(self):
        return [name for name, _ in self.fields]


def read_table(metadata_location):
    table = StaticTable.from_metadata(metadata_location, properties=_properties())
    schema = table.schema()
    fields = [(f.name, str(f.field_type)) for f in schema.fields]
    identifiers = sorted(schema.find_field(i).name for i in schema.identifier_field_ids)
    snapshot = table.current_snapshot()
    if snapshot is None:
        return IcebergTable(fields, [], len(table.metadata.snapshots), identifiers)
    fs = _filesystem()
    data, positional, equality = [], [], []
    for manifest in snapshot.manifests(table.io):
        for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
            kind = entry.data_file.content
            target = {DataFileContent.DATA: data, DataFileContent.POSITION_DELETES: positional,
                      DataFileContent.EQUALITY_DELETES: equality}[kind]
            target.append((entry.sequence_number, entry.data_file))
    position_rows = [(seq, _records(fs, f.file_path)) for seq, f in positional]
    equality_rows = []
    for seq, f in equality:
        names = [schema.find_field(i).name for i in f.equality_ids]
        keys = {tuple(r[n] for n in names) for r in _records(fs, f.file_path, schema)}
        equality_rows.append((seq, names, keys))
    out = []
    for seq, f in data:
        rows = _records(fs, f.file_path, schema)
        keep = [True] * len(rows)
        for dseq, deletes in position_rows:
            if dseq >= seq:
                for d in deletes:
                    if d["file_path"] == f.file_path:
                        keep[d["pos"]] = False
        for dseq, names, keys in equality_rows:
            if dseq > seq:
                keep = [k and tuple(r[n] for n in names) not in keys for r, k in zip(rows, keep)]
        out += [r for r, k in zip(rows, keep) if k]
    return IcebergTable(fields, out, len(table.metadata.snapshots), identifiers)
