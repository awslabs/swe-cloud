import os


class Config:
    """
    Service configuration. Everything a deployment provides comes in through
    the environment; boto3 picks up the endpoint, region and credentials from
    the standard AWS_* variables.
    """

    @property
    def manifest_state_machine_arn(self) -> str:
        return os.environ['MANIFEST_STATE_MACHINE_ARN']

    @property
    def manifest_bucket(self) -> str:
        return os.environ['MANIFEST_BUCKET']

    @property
    def catalogs(self) -> frozenset[str]:
        return frozenset(os.environ.get('MANIFEST_CATALOGS', 'dcp,lungmap').split(','))

    #: Number of days a manifest object is kept in the bucket before the
    #: bucket's lifecycle rule expires it
    manifest_expiration = 7

    #: The manifest formats the generator can produce, with the file
    #: extension of the resulting object
    manifest_formats = {
        'compact': 'tsv',
        'verbatim.jsonl': 'jsonl',
    }


config = Config()
