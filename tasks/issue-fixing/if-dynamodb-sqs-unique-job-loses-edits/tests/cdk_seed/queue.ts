// Creates the job queue; queue.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name, {type: 'String'});
const queue = new sqs.CfnQueue(stack, 'Queue', {queueName:param('QueueName').valueAsString, visibilityTimeout:60});
new cdk.CfnOutput(stack,'QueueUrl',{value:queue.ref});
app.synth();
