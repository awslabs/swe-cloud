// Creates the task table and queue; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new dynamodb.CfnTable(stack, 'Tasks', {
 tableName: param('TableName').valueAsString, billingMode: 'PAY_PER_REQUEST',
 keySchema: [{attributeName: 'pk', keyType: 'HASH'}],
 attributeDefinitions: [{attributeName: 'pk', attributeType: 'S'}],
});
const queue = new sqs.CfnQueue(stack, 'Queue', {
 queueName: param('QueueName').valueAsString, delaySeconds: 0,
 visibilityTimeout: param('VisibilityTimeout', 'Number').valueAsNumber,
 messageRetentionPeriod: param('RetentionSeconds', 'Number').valueAsNumber,
});
new cdk.CfnOutput(stack, 'QueueUrl', {value: queue.ref});
app.synth();
