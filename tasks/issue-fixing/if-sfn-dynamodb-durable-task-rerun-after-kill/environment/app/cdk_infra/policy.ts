// Creates the worker inline policy; policy.template.json is generated with cdk synth.
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new iam.CfnPolicy(stack, 'Policy', {
 policyName: 'worker', roles: [param('RoleName').valueAsString],
 policyDocument: param('PolicyDocument').valueAsString,
});
app.synth();
