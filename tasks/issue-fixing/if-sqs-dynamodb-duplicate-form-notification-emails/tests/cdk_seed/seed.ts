// Creates notification queues and the table. seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name);
const Dlq = new sqs.CfnQueue(stack, 'Dlq', {queueName: param('DlqName').valueAsString});
new cdk.CfnOutput(stack, 'DlqUrl', {value: Dlq.ref});
const Submission = new sqs.CfnQueue(stack, 'Submission', {queueName: param('SubmissionName').valueAsString, visibilityTimeout: 12, delaySeconds: 0, redrivePolicy: {deadLetterTargetArn: Dlq.attrArn, maxReceiveCount: '5'}});
new cdk.CfnOutput(stack, 'SubmissionUrl', {value: Submission.ref});
const Reprocess = new sqs.CfnQueue(stack, 'Reprocess', {queueName: param('ReprocessName').valueAsString, visibilityTimeout: 12, delaySeconds: 0, redrivePolicy: {deadLetterTargetArn: Dlq.attrArn, maxReceiveCount: '5'}});
new cdk.CfnOutput(stack, 'ReprocessUrl', {value: Reprocess.ref});
new dynamodb.CfnTable(stack, 'Table', {tableName: param('TableName').valueAsString, billingMode: 'PAY_PER_REQUEST', keySchema: [{attributeName: 'SubmissionID', keyType: 'HASH'}], attributeDefinitions: [{attributeName: 'SubmissionID', attributeType: 'S'}]});
app.synth();
