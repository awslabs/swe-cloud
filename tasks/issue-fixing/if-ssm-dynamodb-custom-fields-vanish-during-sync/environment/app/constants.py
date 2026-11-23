# Shared constants for the account-factory tooling.

# Custom-field parameters are deployed one parameter per field under this path,
# named <path><field-key>, and consumed by account customizations via
# GetParametersByPath(Path=SSM_PARAMETER_PATH, Recursive=True).
SSM_PARAMETER_PATH = "/aft/account-request/custom-fields/"

# DynamoDB tables owned by the account-factory control plane.
ACCOUNT_REQUEST_TABLE = "aft-request"
SYNC_STATE_TABLE = "aft-sync-state"

# DeleteParameters accepts at most this many names per call.
DELETE_PARAMETER_BATCH_SIZE = 10
