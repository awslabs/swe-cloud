// Creates the manifest index and versioned bucket; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new dynamodb.CfnTable(stack, 'Table', {
 tableName: param('TableName').valueAsString, billingMode: 'PAY_PER_REQUEST',
 keySchema: [{attributeName: 'base_uri', keyType: 'HASH'}, {attributeName: 'version', keyType: 'RANGE'}],
 attributeDefinitions: [{attributeName: 'base_uri', attributeType: 'S'}, {attributeName: 'version', attributeType: 'N'}],
});
new s3.CfnBucket(stack, 'Bucket', {
 bucketName: param('BucketName').valueAsString, versioningConfiguration: {status: 'Enabled'},
});
app.synth();
