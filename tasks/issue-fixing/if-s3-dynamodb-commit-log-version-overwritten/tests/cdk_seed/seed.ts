// Creates the logstore bucket and coordination table; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ddb from 'aws-cdk-lib/aws-dynamodb';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', {synthesizer:new cdk.BootstraplessSynthesizer()});
const param = (name:string) => new cdk.CfnParameter(stack,name).valueAsString;
new s3.CfnBucket(stack,'Bucket',{bucketName:param('BucketName')});
new ddb.CfnTable(stack,'Table',{tableName:param('TableName'),billingMode:'PAY_PER_REQUEST',
 keySchema:[{attributeName:'tablePath',keyType:'HASH'},{attributeName:'fileName',keyType:'RANGE'}],
 attributeDefinitions:[{attributeName:'tablePath',attributeType:'S'},{attributeName:'fileName',attributeType:'S'}]});
app.synth();
