import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import (
    UUID,
    uuid5,
)

from botocore.exceptions import ClientError

from manifest_service import aws
from manifest_service.config import config

JSON = dict[str, Any]

#: Namespace for the generation IDs derived from manifest keys
generation_namespace = UUID('5e1c3b8a-6f43-4d7e-9a8e-2b8f0f2c9d41')

operators = ('is', 'within')


class BadRequestError(Exception):
    pass


def parse_filters(filters: str | None) -> JSON:
    """
    Parse and validate the `filters` query parameter.

    >>> parse_filters('{"organ": {"is": ["brain"]}}')
    {'organ': {'is': ['brain']}}

    >>> parse_filters('{"organ": "brain"}')
    Traceback (most recent call last):
    ...
    manifest_service.manifests.BadRequestError: Filter for 'organ' must be an object
    """
    if filters is None:
        return {}
    try:
        filters = json.loads(filters)
    except json.JSONDecodeError:
        raise BadRequestError('The filters parameter is not valid JSON')
    if not isinstance(filters, dict):
        raise BadRequestError('Filters must be an object')
    for field, filter in filters.items():
        if not field:
            raise BadRequestError('Empty field name')
        if not isinstance(filter, dict):
            raise BadRequestError(f'Filter for {field!r} must be an object')
        if len(filter) != 1:
            raise BadRequestError(f'Need exactly one operator for {field!r}')
        (operator, values), = filter.items()
        if operator not in operators:
            raise BadRequestError(f'Unsupported operator {operator!r} for {field!r}')
        if not isinstance(values, list) or not values:
            raise BadRequestError(f'Values for {field!r} must be a non-empty list')
    return filters


@dataclass(frozen=True, kw_only=True)
class ManifestKey:
    """
    Identifies a manifest: requests that would produce the same manifest have
    the same key, and the generated object is cached under it.
    """
    catalog: str
    format: str
    #: Hash of the filters. Two filters that differ only in the order of their
    #: fields select the same files and therefore have the same hash.
    filter_hash: str

    @classmethod
    def create(cls, *, catalog: str, format: str, filters: JSON) -> 'ManifestKey':
        canonical = json.dumps(filters, sort_keys=True, separators=(',', ':'))
        filter_hash = hashlib.sha256(canonical.encode()).hexdigest()
        return cls(catalog=catalog, format=format, filter_hash=filter_hash)

    @property
    def uuid(self) -> UUID:
        return uuid5(generation_namespace, f'{self.catalog}/{self.format}/{self.filter_hash}')

    @property
    def object_key(self) -> str:
        extension = config.manifest_formats[self.format]
        return f'{self.uuid}.{extension}'

    def to_json(self) -> JSON:
        return {
            'catalog': self.catalog,
            'format': self.format,
            'filter_hash': self.filter_hash
        }

    @classmethod
    def from_json(cls, json: JSON) -> 'ManifestKey':
        return cls(catalog=json['catalog'],
                   format=json['format'],
                   filter_hash=json['filter_hash'])


class ManifestStorage:
    """
    The bucket the generator writes manifests to. A manifest that is present
    there is served from there.
    """

    def has_manifest(self, key: ManifestKey) -> bool:
        try:
            self._s3.head_object(Bucket=config.manifest_bucket, Key=key.object_key)
        except ClientError as e:
            if e.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                return False
            raise
        else:
            return True

    def manifest_location(self, key: ManifestKey) -> str:
        return f's3://{config.manifest_bucket}/{key.object_key}'

    @property
    def _s3(self):
        return aws.client('s3')
