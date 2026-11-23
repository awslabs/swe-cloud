// Creates the three command queues; queues.template.json is generated with cdk synth.
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
for (const [name, output] of [['wait-prev','WaitQueue'],['sync','SyncQueue'],['finish','FinishQueue']]) {
 const queue = new sqs.CfnQueue(stack, name, {queueName: name, visibilityTimeout: 30});
 new cdk.CfnOutput(stack, output, {value: queue.ref});
}
app.synth();
