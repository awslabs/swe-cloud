// Creates seal installation resources. Templates are generated from this app with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
new dynamodb.CfnTable(stack, 'Entries', { tableName: 'seal_entries', billingMode: 'PAY_PER_REQUEST', keySchema: [{attributeName: 'id', keyType: 'HASH'}], attributeDefinitions: [{attributeName: 'id', attributeType: 'S'}] });
new dynamodb.CfnTable(stack, 'Meta', { tableName: 'seal_meta', billingMode: 'PAY_PER_REQUEST', keySchema: [{attributeName: 'pk', keyType: 'HASH'}], attributeDefinitions: [{attributeName: 'pk', attributeType: 'S'}] });
app.synth();
