"""boto3 clients for one controller. Every AWS call goes through the cluster's egress endpoint."""
import boto3
from botocore.config import Config


def make_clients(endpoint_url, region="us-east-1"):
    config = Config(connect_timeout=5, read_timeout=20)
    session = boto3.session.Session(region_name=region)
    elbv2 = session.client("elbv2", endpoint_url=endpoint_url, config=config)
    dynamodb = session.client("dynamodb", endpoint_url=endpoint_url, config=config)
    return elbv2, dynamodb
