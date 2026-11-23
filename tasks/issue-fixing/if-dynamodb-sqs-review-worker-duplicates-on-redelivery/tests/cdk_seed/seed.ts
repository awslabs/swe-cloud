// Creates review tables and two queues. seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name);
new dynamodb.CfnTable(stack, 'Compose', {tableName: param('ComposeTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "req_id", "keyType": "HASH"}], "attributeDefinitions": [{"attributeName": "req_id", "attributeType": "S"}]}});
new dynamodb.CfnTable(stack, 'Responses', {tableName: param('ResponsesTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "req_id", "keyType": "HASH"}, {"attributeName": "part", "keyType": "RANGE"}], "attributeDefinitions": [{"attributeName": "req_id", "attributeType": "S"}, {"attributeName": "part", "attributeType": "S"}]}});
new dynamodb.CfnTable(stack, 'Reviews', {tableName: param('ReviewsTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "review_id", "keyType": "HASH"}], "attributeDefinitions": [{"attributeName": "review_id", "attributeType": "S"}]}});
new dynamodb.CfnTable(stack, 'Movies', {tableName: param('MoviesTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "movie_id", "keyType": "HASH"}], "attributeDefinitions": [{"attributeName": "movie_id", "attributeType": "S"}]}});
new dynamodb.CfnTable(stack, 'Idempotency', {tableName: param('IdempotencyTable').valueAsString, ...{"billingMode": "PAY_PER_REQUEST", "keySchema": [{"attributeName": "id", "keyType": "HASH"}], "attributeDefinitions": [{"attributeName": "id", "attributeType": "S"}]}});
const Upload = new sqs.CfnQueue(stack, 'Upload', {queueName: param('UploadQueue').valueAsString, visibilityTimeout: 6});
new cdk.CfnOutput(stack, 'UploadUrl', {value: Upload.ref});
const UserReview = new sqs.CfnQueue(stack, 'UserReview', {queueName: param('UserReviewQueue').valueAsString});
new cdk.CfnOutput(stack, 'UserReviewUrl', {value: UserReview.ref});
app.synth();
