// Creates existing sandbox resources; secret.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as sm from 'aws-cdk-lib/aws-secretsmanager';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const protectedTag = new cdk.CfnCondition(stack, 'IsProtected', {expression: cdk.Fn.conditionEquals(param('Protected').valueAsString, 'true')});
const secret = new sm.CfnSecret(stack, 'Secret', {name:param('Name').valueAsString, kmsKeyId:param('KeyRef').valueAsString,
 secretString: new cdk.CfnParameter(stack, 'Value', {noEcho:true}).valueAsString,
 tags: [{key:'team',value:'payments'}, cdk.Fn.conditionIf(protectedTag.logicalId, {Key:'DeletionProtection',Value:'true'}, cdk.Aws.NO_VALUE) as any]});
new cdk.CfnOutput(stack, 'Arn', {value:secret.ref});
app.synth();
