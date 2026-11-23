// Creates the shared target group; target.template.json is generated with cdk synth.
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const group = new elbv2.CfnTargetGroup(stack, 'Targets', {
 name: param('GroupName').valueAsString, protocol: 'TCP', port: param('Port', 'Number').valueAsNumber,
 targetType: 'ip', vpcId: 'vpc-0shared',
});
new cdk.CfnOutput(stack, 'TargetGroupArn', { value: group.ref });
app.synth();
