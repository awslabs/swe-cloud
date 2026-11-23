// Creates the initial bucket and wrapping keys; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const name = new cdk.CfnParameter(stack, 'BucketName', {type:'String'});
new s3.CfnBucket(stack,'Bucket',{bucketName:name.valueAsString});
const key0 = new kms.CfnKey(stack,'Key0',{description:'document deployment'});
new cdk.CfnOutput(stack,'Key0Arn',{value:key0.attrArn});
app.synth();
