"""Create an empty local installation. Existing installations are left alone."""
from pathlib import Path
import sys
import boto3
sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy
from aws_store import Store, ENTRIES, META

store = Store()
cfn = boto3.client("cloudformation", endpoint_url=store.ddb.meta.endpoint_url,
                   region_name=store.ddb.meta.region_name,
                   aws_access_key_id="test", aws_secret_access_key="test")
deploy(cfn, "seal-dev-tables", Path(__file__).parent / "infra/seed.template.json")
for table in [ENTRIES, META]:
    store.ddb.get_waiter("table_exists").wait(TableName=table)
if not store.read(META, {"pk": "config"}):
    deploy(cfn, "seal-dev-key", Path(__file__).parent / "infra/seal.template.json")
    store.put(META, {"pk": "config", "key_id": "alias/vault-seal", "config_version": 1})
print("seal tables ready")
