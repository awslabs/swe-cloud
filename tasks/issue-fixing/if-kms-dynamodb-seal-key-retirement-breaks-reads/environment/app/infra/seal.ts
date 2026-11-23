// Creates seal installation resources. Templates are generated from this app with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kms from 'aws-cdk-lib/aws-kms';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const key = new kms.CfnKey(stack, 'Key', {description: 'Local seal'});
new kms.CfnAlias(stack, 'Alias', {aliasName: 'alias/vault-seal', targetKeyId: key.ref});
app.synth();
