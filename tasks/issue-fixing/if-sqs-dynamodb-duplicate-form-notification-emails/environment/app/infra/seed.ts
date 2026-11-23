// Creates notification queues. seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name);
const Dlq = new sqs.CfnQueue(stack, 'Dlq', {queueName: 'submission-dlq'});
new cdk.CfnOutput(stack, 'DlqUrl', {value: Dlq.ref});
const Submission = new sqs.CfnQueue(stack, 'Submission', {queueName: 'submission-queue', visibilityTimeout: 12, delaySeconds: 0, redrivePolicy: {deadLetterTargetArn: Dlq.attrArn, maxReceiveCount: '5'}});
new cdk.CfnOutput(stack, 'SubmissionUrl', {value: Submission.ref});
const Reprocess = new sqs.CfnQueue(stack, 'Reprocess', {queueName: 'reprocess-queue', visibilityTimeout: 12, delaySeconds: 0, redrivePolicy: {deadLetterTargetArn: Dlq.attrArn, maxReceiveCount: '5'}});
new cdk.CfnOutput(stack, 'ReprocessUrl', {value: Reprocess.ref});
app.synth();
