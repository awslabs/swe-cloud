// Creates one resettable verifier table; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name, { type: 'String' });
const key = param('KeyName').valueAsString;
new dynamodb.CfnTable(stack, 'Table', {
  tableName: param('TableName').valueAsString, billingMode: 'PAY_PER_REQUEST',
  keySchema: [{attributeName: key, keyType: 'HASH'}],
  attributeDefinitions: [{attributeName: key, attributeType: 'S'}]
});
app.synth();
