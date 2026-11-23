// Creates the FIFO stream and observation table; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name:string, type='String') => new cdk.CfnParameter(stack,name,{type});
const queue = new sqs.CfnQueue(stack,'Queue',{queueName:param('QueueName').valueAsString,fifoQueue:true,delaySeconds:0,messageRetentionPeriod:300,contentBasedDeduplication:false,visibilityTimeout:8});
new cdk.CfnOutput(stack,'QueueUrl',{value:queue.ref});
new dynamodb.CfnTable(stack,'Table',{
 tableName:param('TableName').valueAsString,billingMode:'PAY_PER_REQUEST',
 keySchema:[{attributeName:'pk',keyType:'HASH'},{attributeName:'sk',keyType:'RANGE'}],
 attributeDefinitions:[{attributeName:'pk',attributeType:'S'},{attributeName:'sk',attributeType:'S'}],
});
app.synth();
