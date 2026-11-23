// Creates the backup bucket. seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const bucket = new cdk.CfnParameter(stack, 'BucketName');
new s3.CfnBucket(stack, 'Bucket', {bucketName: bucket.valueAsString});
app.synth();
