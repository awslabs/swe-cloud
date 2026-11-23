// Creates the ledger FIFO queue; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as sqs from 'aws-cdk-lib/aws-sqs';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name).valueAsString;

const queue = new sqs.CfnQueue(stack, 'Queue', {queueName:param('QueueName'),
 fifoQueue:true,contentBasedDeduplication:false,delaySeconds:0});
new cdk.CfnOutput(stack,'QueueUrl',{value:queue.ref});
app.synth();
