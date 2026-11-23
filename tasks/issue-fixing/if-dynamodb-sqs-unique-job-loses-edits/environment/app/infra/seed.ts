// Creates one table; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string) => new cdk.CfnParameter(stack, name, {type: 'String'});
new dynamodb.CfnTable(stack, 'Table', {
 tableName:param('TableName').valueAsString, billingMode:'PAY_PER_REQUEST',
 attributeDefinitions:[{attributeName:'pk',attributeType:'S'}],
 keySchema:[{attributeName:'pk',keyType:'HASH'}],
});
app.synth();
