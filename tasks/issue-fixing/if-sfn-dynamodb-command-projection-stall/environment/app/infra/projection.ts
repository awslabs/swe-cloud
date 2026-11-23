// Creates the projection table; projection.template.json is generated with cdk synth.
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new dynamodb.CfnTable(stack, 'Table', {tableName: 'projection', billingMode: 'PAY_PER_REQUEST',
 attributeDefinitions: [{attributeName: 'pk', attributeType: 'S'}], keySchema: [{attributeName: 'pk', keyType: 'HASH'}]});
app.synth();
