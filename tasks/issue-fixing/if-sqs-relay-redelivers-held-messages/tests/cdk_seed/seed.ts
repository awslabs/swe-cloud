// Creates a relay case queue; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const queue = new sqs.CfnQueue(stack, 'Jobs', {
 queueName: param('QueueName').valueAsString,
 visibilityTimeout: param('VisibilityTimeout', 'Number').valueAsNumber,
});
new cdk.CfnOutput(stack, 'QueueUrl', { value: queue.ref });
app.synth();
