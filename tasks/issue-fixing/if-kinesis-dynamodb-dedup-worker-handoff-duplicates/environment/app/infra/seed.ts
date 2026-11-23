// Creates the input/output streams and state table; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as kinesis from 'aws-cdk-lib/aws-kinesis';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name, { type: 'String' });
for (const name of ['InputStream', 'OutputStream']) {
  new kinesis.CfnStream(stack, name + 'Resource', { name: param(name).valueAsString, shardCount: 1 });
}
new dynamodb.CfnTable(stack, 'StateTableResource', {
  tableName: param('StateTable').valueAsString, billingMode: 'PAY_PER_REQUEST',
  keySchema: [{attributeName: 'pk', keyType: 'HASH'}, {attributeName: 'sk', keyType: 'RANGE'}],
  attributeDefinitions: [{attributeName: 'pk', attributeType: 'S'}, {attributeName: 'sk', attributeType: 'S'}]
});
app.synth();
