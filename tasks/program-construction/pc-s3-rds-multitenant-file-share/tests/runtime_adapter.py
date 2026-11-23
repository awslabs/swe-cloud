"""AWS Reconstruction's side of the shared native runtime; it ships beside it in tests/ and tools/."""

# Generated task names and the private preview plugin are named after the pipeline.
NAME = "aws-reconstruction"
# The venue's fixed endpoint and local credentials; tests pass engine endpoints explicitly.
CANDIDATE_ENV = {
    "AWS_ENDPOINT_URL": "http://engines:4566",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_EC2_METADATA_DISABLED": "true",
}
# Started before the verifier becomes the subreaper, so the venue's endpoint watcher outlives candidate cleanup.
VENUE_COMMAND = ["floci-venue", "up"]
