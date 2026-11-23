// Creates storage, queue and workflow role before catalog writes; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ddb from 'aws-cdk-lib/aws-dynamodb';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
const app = new cdk.App();
const stack = new cdk.Stack(app,'Seed',{synthesizer:new cdk.BootstraplessSynthesizer()});
const param = (name:string) => new cdk.CfnParameter(stack,name).valueAsString;
const output = (name:string,value:string) => {const o=new cdk.CfnOutput(stack,name+'Output',{value});o.overrideLogicalId(name);};
const role=new iam.CfnRole(stack,'Role',{roleName:'manifest-gen-role',assumeRolePolicyDocument:{Version:'2012-10-17',Statement:[{Effect:'Allow',Principal:{Service:'states.amazonaws.com'},Action:'sts:AssumeRole'}]}});
const queue=new sqs.CfnQueue(stack,'Queue',{queueName:param('QueueName')});
new s3.CfnBucket(stack,'Bucket',{bucketName:param('BucketName')});
new ddb.CfnTable(stack,'Table',{tableName:param('TableName'),billingMode:'PAY_PER_REQUEST',
 attributeDefinitions:[{attributeName:'catalog',attributeType:'S'},{attributeName:'file_id',attributeType:'S'}],
 keySchema:[{attributeName:'catalog',keyType:'HASH'},{attributeName:'file_id',keyType:'RANGE'}]});
output('QueueUrl',queue.ref);output('RoleArn',role.attrArn);
app.synth();
