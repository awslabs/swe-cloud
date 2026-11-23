// Creates the ownership table; seed.template.json is generated with cdk synth.
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new dynamodb.CfnTable(stack, 'Ownership', {
 tableName: param('TableName').valueAsString, billingMode: 'PAY_PER_REQUEST',
 keySchema: [{ attributeName: 'owner', keyType: 'HASH' }, { attributeName: 'target', keyType: 'RANGE' }],
 attributeDefinitions: [{ attributeName: 'owner', attributeType: 'S' }, { attributeName: 'target', attributeType: 'S' }],
});
app.synth();
