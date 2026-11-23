"""Custom::CrossRegionExportWriter handler.

Properties: ``{"WriterProps": {"region": ..., "exports": {parameterName: value}}}``.  Publishes one
SSM String parameter per export.  An export whose parameter is in use (carries an
``aws-cdk:strong-ref:<stack>`` tag, see reader.py) may not be removed or changed.
"""
import sys

import boto3

from . import entrypoint, tagstore


def handler(event):
    props = event["ResourceProperties"]["WriterProps"]
    exports = props["exports"]

    ssm = boto3.client("ssm", region_name=props["region"])
    try:
        request_type = event["RequestType"]
        if request_type == "Create":
            print(f"Creating new SSM Parameter exports in region {props['region']}", file=sys.stderr)
            throw_if_any_in_use(ssm, exports)
            put_parameters(ssm, exports)
            return None
        if request_type == "Update":
            old_exports = event["OldResourceProperties"]["WriterProps"]["exports"]
            new_exports = except_(exports, old_exports)

            # throw an error to fail the deployment if any export value is changing
            changed_exports = changed(old_exports, exports)
            if changed_exports:
                raise Exception("Some exports have changed!\n" + "\n".join(changed_exports))
            # if we are removing any exports that are in use, then throw an
            # error to fail the deployment
            removed_exports = except_(old_exports, exports)
            throw_if_any_in_use(ssm, removed_exports)
            # if the ones we are removing are not in use then delete them
            # (this skips if no export names are to be deleted)
            tagstore.delete_parameters(ssm, list(removed_exports))

            # also throw an error if we are creating a new export that already exists for some reason
            throw_if_any_in_use(ssm, new_exports)
            print(f"Creating new SSM Parameter exports in region {props['region']}", file=sys.stderr)
            put_parameters(ssm, new_exports)
            return None
        if request_type == "Delete":
            # if any of the exports are currently in use then throw an error to fail
            # the stack deletion.
            throw_if_any_in_use(ssm, exports)
            # if none are in use then delete all of them
            tagstore.delete_parameters(ssm, list(exports))
            return None
        return None
    except Exception as exc:
        print(f"Error processing event: {exc!r}", file=sys.stderr)
        raise


def put_parameters(ssm, parameters):
    """Create parameters for existing exports."""
    for name, value in parameters.items():
        ssm.put_parameter(
            Name=name,
            Value=value,
            Type="String",
            Overwrite=True,  # if, for whatever reason, we encounter this parameter lying around, just overwrite it.
        )


def throw_if_any_in_use(ssm, parameters):
    """Query for existing parameters that are in use."""
    tag_results = {}
    for name in parameters:
        result = is_in_use(ssm, name)
        if result:
            tag_results[name] = result
    if tag_results:
        message = "\n".join(f"{name} is in use by stack(s) {' '.join(sorted(stacks))}"
                            for name, stacks in tag_results.items())
        raise Exception(f"Exports cannot be updated: \n{message}")


def is_in_use(ssm, parameter_name):
    """Check if a parameter is in use: the set of stacks holding a strong reference to it."""
    tag_results = set()
    try:
        tag_list = tagstore.list_tags_for_resource(ssm, parameter_name)
    except tagstore.InvalidResourceId:
        # an InvalidResourceId means that the parameter doesn't exist
        # which we should ignore since that means it's not in use
        return set()
    for tag in tag_list:
        parts = tag["Key"].split(":")
        if len(parts) > 2 and parts[0] == "aws-cdk" and parts[1] == "strong-ref":
            tag_results.add(parts[2])
    return tag_results


def except_(source, filter_):
    """Return only the items from source that do not exist in the filter."""
    return {key: value for key, value in source.items() if key not in filter_}


def changed(old_params, new_params):
    """Return keys that exist in both the old and the new parameters but have different values."""
    return [key for key in old_params if key in new_params and old_params[key] != new_params[key]]


if __name__ == "__main__":
    entrypoint.run(handler)
