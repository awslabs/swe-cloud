// Creates the verifier hosted zone; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as route53 from 'aws-cdk-lib/aws-route53';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const zone = new route53.CfnHostedZone(stack, 'Zone', {
 name: param('ZoneName').valueAsString, hostedZoneConfig: {comment: 'reconciler run'},
});
new cdk.CfnOutput(stack, 'ZoneId', {value: zone.ref});
app.synth();
