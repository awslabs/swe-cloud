// Creates the shared interruption-notice queue; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
const app = new cdk.App();
const stack = new cdk.Stack(app,'Seed',{synthesizer:new cdk.BootstraplessSynthesizer()});
const name=new cdk.CfnParameter(stack,'QueueName');
const queue=new sqs.CfnQueue(stack,'Queue',{queueName:name.valueAsString,visibilityTimeout:30});
new cdk.CfnOutput(stack,'QueueUrl',{value:queue.ref});
app.synth();
