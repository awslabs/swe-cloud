// Creates the job token queue; queue.template.json is generated with cdk synth.
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const queue = new sqs.CfnQueue(stack, 'Queue', { queueName: param('QueueName').valueAsString });
new cdk.CfnOutput(stack, 'QueueUrl', { value: queue.ref });
app.synth();
