// Creates three deployment tables; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name:string) => new cdk.CfnParameter(stack,name,{type:'String'});
for (const [name,key] of [['StateTable','resource_id'],['LedgerTable','secret_id'],['DbTable','user']]) {
 new dynamodb.CfnTable(stack,name+'Resource',{
  tableName:param(name).valueAsString,billingMode:'PAY_PER_REQUEST',
  keySchema:[{attributeName:key,keyType:'HASH'}],
  attributeDefinitions:[{attributeName:key,attributeType:'S'}],
 });
}
app.synth();
