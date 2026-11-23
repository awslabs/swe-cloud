#!/usr/bin/env python3
"""aft-sync: apply an account request's custom fields to the member account.

One invocation stands in for one provisioning execution's account-metadata
task. It is started whenever an account request record is committed, and it
converges the member account's custom-field parameters — one String parameter
per field under /aft/account-request/custom-fields/ — to the account request
carried in the payload.

Usage:
    aft-sync --payload <file.json>

The payload file is the execution input: a snapshot of the aft-request item
at the commit that triggered this invocation. Shape:

    {
      "customization_request_id": "<uuid>",
      "account_info": {"account": {"id": "<12-digit account id>"}},
      "account_request": {
        "id": "<account email>",
        "custom_fields": "<JSON-encoded map of field key to string value>",
        "record_version": <int>
      }
    }

The AWS endpoint is taken from AWS_ENDPOINT_URL (region us-east-1).
Exit status 0 on success, 1 on failure.
"""
import argparse
import inspect
import json
import logging
import sys
from typing import Any, Dict

from constants import SSM_PARAMETER_PATH
from ssm_ops import (
    delete_ssm_parameters,
    get_ssm_parameters_names_by_path,
    put_ssm_parameters,
    service_client,
)

logger = logging.getLogger("aft")


def sync_custom_fields(event_payload: Dict[str, Any]) -> None:
    request_id = event_payload["customization_request_id"]
    account_request = event_payload["account_request"]
    custom_fields = json.loads(account_request.get("custom_fields", "{}"))

    logger.info(
        f"Account metadata sync started: request_id={request_id} "
        f"account={event_payload['account_info']['account']['id']}"
    )

    ssm = service_client("ssm")

    params = get_ssm_parameters_names_by_path(ssm, SSM_PARAMETER_PATH)

    existing_keys = set(params)
    new_keys = set(custom_fields.keys())

    # Delete SSM parameters which do not exist in new custom fields
    params_to_remove = list(existing_keys.difference(new_keys))
    logger.info(f"Deleting SSM params: {params_to_remove}")
    delete_ssm_parameters(ssm, params_to_remove)

    # Update / Add SSM parameters for custom fields provided
    logger.info(f"Adding/Updating SSM params: {custom_fields}")
    put_ssm_parameters(ssm, custom_fields, SSM_PARAMETER_PATH)

    logger.info("Account metadata sync finished")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply an account request's custom fields to the member "
        "account's Parameter Store.",
        epilog="The payload format is documented in this module's docstring; "
        "an example is /app/payload-example.json.",
    )
    parser.add_argument("--payload", required=True, help="path to the payload JSON file")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='{"module": "%(name)s", "log_level": "%(levelname)s", "log_message": "%(message)s"}',
        stream=sys.stderr,
    )

    try:
        with open(args.payload) as f:
            event = json.load(f)
        sync_custom_fields(event)
    except Exception as error:
        message = {
            "FILE": __file__.split("/")[-1],
            "METHOD": inspect.stack()[0][3],
            "EXCEPTION": str(error),
        }
        logger.exception(message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
