// Creates the rehearsal bucket and run table; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new s3.CfnBucket(stack, 'Objects', { bucketName: param('BucketName').valueAsString });
new dynamodb.CfnTable(stack, 'Runs', {
 tableName: param('TableName').valueAsString, billingMode: 'PAY_PER_REQUEST',
 keySchema: [{ attributeName: 'pk', keyType: 'HASH' }, { attributeName: 'sk', keyType: 'RANGE' }],
 attributeDefinitions: [{ attributeName: 'pk', attributeType: 'S' }, { attributeName: 'sk', attributeType: 'S' }],
});
app.synth();
