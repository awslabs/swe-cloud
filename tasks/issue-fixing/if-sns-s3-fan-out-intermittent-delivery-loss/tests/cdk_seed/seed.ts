// Creates the body bucket, topic and verifier tables; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const tag = new cdk.CfnParameter(stack, 'Tag', { type: 'String' }).valueAsString;
new s3.CfnBucket(stack, 'Bodies', { bucketName: cdk.Fn.join('', [tag, '-bodies']) });
const topic = new sns.CfnTopic(stack, 'Topic', { topicName: tag });
for (const name of ['receipts', 'control']) {
  new dynamodb.CfnTable(stack, name, {
    tableName: cdk.Fn.join('', [tag, '_' + name]), billingMode: 'PAY_PER_REQUEST',
    keySchema: [{attributeName: 'pk', keyType: 'HASH'}],
    attributeDefinitions: [{attributeName: 'pk', attributeType: 'S'}]
  });
}
new cdk.CfnOutput(stack, 'TopicArn', { value: topic.ref });
app.synth();
