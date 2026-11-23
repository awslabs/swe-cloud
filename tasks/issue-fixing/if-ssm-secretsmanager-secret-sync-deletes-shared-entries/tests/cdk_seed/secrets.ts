// Creates the seven initial secrets; secrets.template.json is generated with cdk synth.
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type, noEcho: true });
new secretsmanager.CfnSecret(stack, 'Secret0', {name: "app/other-1", secretString: param('Value0').valueAsString, tags: [{"key": "owner", "value": "web"}]});
new secretsmanager.CfnSecret(stack, 'Secret1', {name: "app/other-2", secretString: param('Value1').valueAsString, tags: [{"key": "owner", "value": "web"}]});
new secretsmanager.CfnSecret(stack, 'Secret2', {name: "db/creds", secretString: param('Value2').valueAsString, tags: [{"key": "owner", "value": "dba"}]});
new secretsmanager.CfnSecret(stack, 'Secret3', {name: "INFISICAL_LEGACY", secretString: param('Value3').valueAsString, tags: [{"key": "owner", "value": "ops"}]});
new secretsmanager.CfnSecret(stack, 'Secret4', {name: "INFISICAL_DB_PASSWORD", secretString: param('Value4').valueAsString});
new secretsmanager.CfnSecret(stack, 'Secret5', {name: "reports/legacy-export", secretString: param('Value5').valueAsString, tags: [{"key": "owner", "value": "finance"}]});
new secretsmanager.CfnSecret(stack, 'Secret6', {name: "INFISICAL_SENTRY_DSN", secretString: param('Value6').valueAsString, tags: [{"key": "owner", "value": "ops"}]});
app.synth();
