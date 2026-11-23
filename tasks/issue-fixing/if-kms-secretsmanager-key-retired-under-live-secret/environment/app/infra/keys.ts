// Creates existing sandbox resources; keys.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as sm from 'aws-cdk-lib/aws-secretsmanager';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const prefix = param('Prefix').valueAsString;
const App = new kms.CfnKey(stack, 'App', {description: `${prefix} app data`});
new cdk.CfnOutput(stack, 'AppId', {value:App.ref});
new cdk.CfnOutput(stack, 'AppArn', {value:App.attrArn});
const Cache = new kms.CfnKey(stack, 'Cache', {description: `${prefix} cache`});
new cdk.CfnOutput(stack, 'CacheId', {value:Cache.ref});
new cdk.CfnOutput(stack, 'CacheArn', {value:Cache.attrArn});
app.synth();
