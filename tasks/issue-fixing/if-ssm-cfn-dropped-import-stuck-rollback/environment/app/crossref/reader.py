"""Custom::CrossRegionExportReader handler.

Properties: ``{"ReaderProps": {"region": ..., "prefix": <consumer stack name>,
"imports": {parameterName: resolvedValue}}}``.  Marks every imported parameter with the tag
``aws-cdk:strong-ref:<prefix>`` so the producer's writer will not remove or change it, and returns
the imports as ``Data``.
"""
import sys

import boto3

from . import entrypoint, tagstore


def handler(event):
    props = event["ResourceProperties"]["ReaderProps"]
    imports = props["imports"]
    import_names = list(imports)
    key_name = f"aws-cdk:strong-ref:{props['prefix']}"

    ssm = boto3.client("ssm", region_name=props["region"])
    try:
        request_type = event["RequestType"]
        if request_type == "Create":
            print("Tagging SSM Parameter imports", file=sys.stderr)
            add_tags(ssm, import_names, key_name)
        elif request_type == "Update":
            old_exports = event["OldResourceProperties"]["ReaderProps"]["imports"]
            new_exports = except_(import_names, list(old_exports))
            params_to_release = except_(list(old_exports), import_names)
            print("Releasing unused SSM Parameter imports", file=sys.stderr)
            if params_to_release:
                remove_tags(ssm, params_to_release, key_name)
            print("Tagging new SSM Parameter imports", file=sys.stderr)
            add_tags(ssm, new_exports, key_name)
        elif request_type == "Delete":
            print("Releasing all SSM Parameter exports by removing tags", file=sys.stderr)
            remove_tags(ssm, import_names, key_name)
            return None
    except Exception as exc:
        print(f"Error importing cross region stack exports: {exc!r}", file=sys.stderr)
        raise
    return {"Data": imports}


def add_tags(ssm, parameters, key_name):
    """Add tag to parameters for existing exports."""
    for name in parameters:
        try:
            tagstore.add_tags_to_resource(ssm, name, {key_name: "true"})
        except Exception as exc:
            raise Exception(f"Error importing {name}: {exc}") from exc


def remove_tags(ssm, parameters, key_name):
    """Remove tags from parameters."""
    for name in parameters:
        try:
            tagstore.remove_tags_from_resource(ssm, name, [key_name])
        except tagstore.InvalidResourceId:
            continue
        except Exception as exc:
            raise Exception(f"Error releasing import {name}: {exc}") from exc


def except_(source, filter_):
    """Return only the items from source that do not exist in the filter."""
    return [key for key in source if key not in filter_]


if __name__ == "__main__":
    entrypoint.run(handler)
