// Creates the workflow role; role.template.json is generated with cdk synth.
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const role = new iam.CfnRole(stack, 'Role', {roleName: 'command-role', assumeRolePolicyDocument: {
 Version: '2012-10-17', Statement: [{Effect: 'Allow', Principal: {Service: 'states.amazonaws.com'}, Action: 'sts:AssumeRole'}]}});
new cdk.CfnOutput(stack, 'RoleArn', {value: role.attrArn});
app.synth();
