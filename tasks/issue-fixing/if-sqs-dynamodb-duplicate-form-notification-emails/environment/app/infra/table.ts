// Creates the notification table. table.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name);
new dynamodb.CfnTable(stack, 'Table', {tableName: 'ReliabilityQueue', billingMode: 'PAY_PER_REQUEST', keySchema: [{attributeName: 'SubmissionID', keyType: 'HASH'}], attributeDefinitions: [{attributeName: 'SubmissionID', attributeType: 'S'}]});
app.synth();
