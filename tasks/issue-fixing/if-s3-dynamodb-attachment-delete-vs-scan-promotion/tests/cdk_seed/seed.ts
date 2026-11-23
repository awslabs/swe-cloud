// Creates two attachment buckets, the indexed table and three queues; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const prefix = param('Prefix').valueAsString;
new s3.CfnBucket(stack, 'Quarantine', {bucketName: prefix + '-q'});
new s3.CfnBucket(stack, 'Downloads', {bucketName: prefix + '-d'});
new dynamodb.CfnTable(stack, 'Attachments', {tableName: prefix + '-table', billingMode: 'PAY_PER_REQUEST',
 keySchema: [{attributeName: 'path', keyType: 'HASH'}],
 attributeDefinitions: [{attributeName: 'path', attributeType: 'S'}, {attributeName: 'state', attributeType: 'S'}, {attributeName: 'expiresAt', attributeType: 'N'}],
 globalSecondaryIndexes: [{indexName: 'PendingExpiry', keySchema: [{attributeName: 'state', keyType: 'HASH'}, {attributeName: 'expiresAt', keyType: 'RANGE'}], projection: {projectionType: 'ALL'}}]});
for (const [role, visibility] of [['SCAN', 60], ['DELETE', 5], ['EVENTS', 60]] as const) {
 const queue = new sqs.CfnQueue(stack, role + 'Queue', {queueName: prefix + '-' + role, visibilityTimeout: visibility});
 new cdk.CfnOutput(stack, role, {value: queue.ref});
}
app.synth();
