// Creates the existing build queue and DLQ with redrive settings; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const dlq = new sqs.CfnQueue(stack, 'DLQ', {queueName:param('DlqName').valueAsString});
const queue = new sqs.CfnQueue(stack, 'Queue', {queueName:param('QueueName').valueAsString,
 visibilityTimeout:param('VisibilityTimeout','Number').valueAsNumber,
 messageRetentionPeriod:param('RetentionPeriod','Number').valueAsNumber,
 redrivePolicy:{deadLetterTargetArn:dlq.attrArn, maxReceiveCount:param('MaxReceiveCount').valueAsString}});
new cdk.CfnOutput(stack,'DlqUrl',{value:dlq.ref});
new cdk.CfnOutput(stack,'DlqArn',{value:dlq.attrArn});
new cdk.CfnOutput(stack,'QueueUrl',{value:queue.ref});
new cdk.CfnOutput(stack,'QueueArn',{value:queue.attrArn});
app.synth();
