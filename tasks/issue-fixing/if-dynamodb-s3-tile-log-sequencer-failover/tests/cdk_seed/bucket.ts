// Creates a drill bucket; bucket.template.json is generated with cdk synth.
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new s3.CfnBucket(stack, 'Bucket', { bucketName: param('BucketName').valueAsString });
app.synth();
