// Creates the versioned warehouse bucket; bucket.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new s3.CfnBucket(stack, 'Bucket', {
 bucketName: param('BucketName').valueAsString, versioningConfiguration: {status: 'Enabled'},
});
app.synth();
