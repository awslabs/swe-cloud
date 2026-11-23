// Creates the checkpoint DynamoDB table; checkpoint.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const tableName = new cdk.CfnParameter(stack, 'TableName', { type: 'String' });
new dynamodb.CfnTable(stack, 'Checkpoints', {
  tableName: tableName.valueAsString,
  billingMode: 'PAY_PER_REQUEST',
  attributeDefinitions: [
    { attributeName: 'StreamArn', attributeType: 'S' },
    { attributeName: 'ShardID', attributeType: 'S' },
  ],
  keySchema: [
    { attributeName: 'StreamArn', keyType: 'HASH' },
    { attributeName: 'ShardID', keyType: 'RANGE' },
  ],
});
app.synth();
