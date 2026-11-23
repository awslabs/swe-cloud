// Creates a workflow or execution table; table.template.json is generated with cdk synth.
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const key = param('KeyName').valueAsString;
new dynamodb.CfnTable(stack, 'Records', {
 tableName: param('TableName').valueAsString, billingMode: 'PAY_PER_REQUEST',
 keySchema: [{ attributeName: key, keyType: 'HASH' }],
 attributeDefinitions: [{ attributeName: key, attributeType: 'S' }],
});
app.synth();
