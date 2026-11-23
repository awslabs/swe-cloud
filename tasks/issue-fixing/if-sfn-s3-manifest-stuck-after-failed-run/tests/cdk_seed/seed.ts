// Creates the isolated manifest test deployment; seed.template.json is generated with cdk synth.
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
const name=param('Name');
const role=new iam.CfnRole(stack,'Role',{roleName:name,assumeRolePolicyDocument:{Version:'2012-10-17',Statement:[{Effect:'Allow',Principal:{Service:'states.amazonaws.com'},Action:'sts:AssumeRole'}]}});
const queue=new sqs.CfnQueue(stack,'Queue',{queueName:name});
new s3.CfnBucket(stack,'Bucket',{bucketName:name});
const machine=new sfn.CfnStateMachine(stack,'Machine',{stateMachineName:name,roleArn:role.attrArn,stateMachineType:'STANDARD',
 definitionString:cdk.Fn.sub(JSON.stringify({StartAt:'Generate',States:{Generate:{Type:'Task',Resource:'arn:aws:states:::sqs:sendMessage.waitForTaskToken',
 Parameters:{QueueUrl:'${QueueUrl}',MessageBody:{'input.$':'$','execution_arn.$':'$$.Execution.Id','task_token.$':'$$.Task.Token'}},TimeoutSeconds:900,End:true}}}),{QueueUrl:queue.ref})});
output('QueueUrl',queue.ref);output('MachineArn',machine.ref);
app.synth();
