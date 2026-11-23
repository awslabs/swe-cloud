// Creates one queue and its optional SNS subscription; queue.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as sns from 'aws-cdk-lib/aws-sns';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Queue', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name, { type: 'String' });
const queue = new sqs.CfnQueue(stack, 'QueueResource', { queueName: param('QueueName').valueAsString, visibilityTimeout: 2 });
const enabled = new cdk.CfnCondition(stack, 'HasSubscription', { expression: cdk.Fn.conditionEquals(param('Subscribe').valueAsString, 'true') });
const subscription = new sns.CfnSubscription(stack, 'Subscription', { topicArn: param('TopicArn').valueAsString, protocol: 'sqs', endpoint: queue.attrArn });
subscription.cfnOptions.condition = enabled;
new cdk.CfnOutput(stack, 'QueueUrl', { value: queue.ref });
new cdk.CfnOutput(stack, 'QueueArn', { value: queue.attrArn });
app.synth();
