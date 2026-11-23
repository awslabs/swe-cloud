// Creates one verifier fixture table; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const key = param('KeyName').valueAsString;
new dynamodb.CfnTable(stack, 'Table', {
 tableName: param('TableName').valueAsString,
 keySchema: [{attributeName: key, keyType: 'HASH'}],
 attributeDefinitions: [{attributeName: key, attributeType: 'S'}],
 billingMode: 'PAY_PER_REQUEST',
});
app.synth();
