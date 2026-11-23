// Creates the five dispatch tables. seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name);
new dynamodb.CfnTable(stack, 'Dispatch', { tableName: param('DispatchTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "id", "keyType": "HASH"}], "attributeDefinitions": [{"attributeName": "id", "attributeType": "S"}]} });
new dynamodb.CfnTable(stack, 'Execution', { tableName: param('ExecutionTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "execution_id", "keyType": "HASH"}], "attributeDefinitions": [{"attributeName": "execution_id", "attributeType": "S"}]} });
new dynamodb.CfnTable(stack, 'Idempotency', { tableName: param('IdempotencyTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "id", "keyType": "HASH"}], "attributeDefinitions": [{"attributeName": "id", "attributeType": "S"}]} });
new dynamodb.CfnTable(stack, 'Checkpoint', { tableName: param('CheckpointTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "execution_id", "keyType": "HASH"}, {"attributeName": "seq", "keyType": "RANGE"}], "attributeDefinitions": [{"attributeName": "execution_id", "attributeType": "S"}, {"attributeName": "seq", "attributeType": "N"}]} });
new dynamodb.CfnTable(stack, 'Effects', { tableName: param('EffectsTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "payload_key", "keyType": "HASH"}, {"attributeName": "execution_id", "keyType": "RANGE"}], "attributeDefinitions": [{"attributeName": "payload_key", "attributeType": "S"}, {"attributeName": "execution_id", "attributeType": "S"}]} });
app.synth();
