// Creates the five String parameters; seed.template.json is generated with cdk synth.
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type, noEcho: true });
new ssm.CfnParameter(stack, 'Parameter0', {name: "/LEGACY_BILLING_URL", type: 'String', value: param('Value0').valueAsString, tags: {"owner": "ops"}});
new ssm.CfnParameter(stack, 'Parameter1', {name: "/SENTRY_DSN", type: 'String', value: param('Value1').valueAsString, tags: {"owner": "ops"}});
new ssm.CfnParameter(stack, 'Parameter2', {name: "/STATUSPAGE_KEY", type: 'String', value: param('Value2').valueAsString});
new ssm.CfnParameter(stack, 'Parameter3', {name: "/FRAUD_SCORE_THRESHOLD", type: 'String', value: param('Value3').valueAsString, tags: {"owner": "risk"}});
new ssm.CfnParameter(stack, 'Parameter4', {name: "/ops/oncall/ROTATION", type: 'String', value: param('Value4').valueAsString, tags: {"owner": "ops"}});
app.synth();
