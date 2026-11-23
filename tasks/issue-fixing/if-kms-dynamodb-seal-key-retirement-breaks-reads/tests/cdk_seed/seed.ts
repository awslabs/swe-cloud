// Creates seal installation resources. Templates are generated from this app with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
new dynamodb.CfnTable(stack, 'Entries', { tableName: 'seal_entries', billingMode: 'PAY_PER_REQUEST', keySchema: [{attributeName: 'id', keyType: 'HASH'}], attributeDefinitions: [{attributeName: 'id', attributeType: 'S'}] });
new dynamodb.CfnTable(stack, 'Meta', { tableName: 'seal_meta', billingMode: 'PAY_PER_REQUEST', keySchema: [{attributeName: 'pk', keyType: 'HASH'}], attributeDefinitions: [{attributeName: 'pk', attributeType: 'S'}] });
const label = new cdk.CfnParameter(stack, 'Label').valueAsString;
const key0 = new kms.CfnKey(stack, 'Key0', {description: cdk.Fn.join('', ['seal-', label, '-0'])});
new cdk.CfnOutput(stack, 'Key0Id', {value: key0.ref});
const key1 = new kms.CfnKey(stack, 'Key1', {description: cdk.Fn.join('', ['seal-', label, '-1'])});
new cdk.CfnOutput(stack, 'Key1Id', {value: key1.ref});
const key2 = new kms.CfnKey(stack, 'Key2', {description: cdk.Fn.join('', ['seal-', label, '-2'])});
new cdk.CfnOutput(stack, 'Key2Id', {value: key2.ref});
new kms.CfnAlias(stack, 'Alias', {aliasName: cdk.Fn.join('', ['alias/seal-', label]), targetKeyId: key0.ref});
app.synth();
