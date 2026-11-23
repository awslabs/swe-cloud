// Creates the drill record table; seed.template.json is generated with cdk synth.
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new dynamodb.CfnTable(stack, 'Records', {
  tableName: param('TableName').valueAsString,
  keySchema: [{ attributeName: 'logID', keyType: 'HASH' }],
  attributeDefinitions: [{ attributeName: 'logID', attributeType: 'B' }],
  billingMode: 'PAY_PER_REQUEST',
});
app.synth();
