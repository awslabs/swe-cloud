// Creates the source DynamoDB table; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const tableName = new cdk.CfnParameter(stack, 'TableName', { type: 'String' });
new dynamodb.CfnTable(stack, 'Source', {
  tableName: tableName.valueAsString,
  billingMode: 'PAY_PER_REQUEST',
  attributeDefinitions: [{ attributeName: 'id', attributeType: 'S' }],
  keySchema: [{ attributeName: 'id', keyType: 'HASH' }],
  streamSpecification: { streamViewType: 'NEW_AND_OLD_IMAGES' },
});
app.synth();
