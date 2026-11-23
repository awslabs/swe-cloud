// Creates a command/effects table; seed.template.json is generated with cdk synth.
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new dynamodb.CfnTable(stack, 'Table', {tableName: param('TableName').valueAsString, billingMode: 'PAY_PER_REQUEST',
 attributeDefinitions: [{attributeName: 'pk', attributeType: 'S'}, {attributeName: 'sk', attributeType: param('SortKeyType').valueAsString}],
 keySchema: [{attributeName: 'pk', keyType: 'HASH'}, {attributeName: 'sk', keyType: 'RANGE'}]});
app.synth();
