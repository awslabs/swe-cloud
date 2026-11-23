// Creates the three transaction-service test tables; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name, { type: 'String' });
for (const [logicalId, parameterName, keyName] of [
  ['Transactions', 'TxTableName', 'txid'],
  ['Items', 'ItemTableName', 'item_id'],
  ['Images', 'ImageTableName', 'img'],
]) {
  new dynamodb.CfnTable(stack, logicalId, {
    tableName: param(parameterName).valueAsString,
    billingMode: 'PAY_PER_REQUEST',
    attributeDefinitions: [{ attributeName: keyName, attributeType: 'S' }],
    keySchema: [{ attributeName: keyName, keyType: 'HASH' }],
  });
}
app.synth();
