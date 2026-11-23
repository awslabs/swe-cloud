// Creates or updates the workflow after catalog writes; workflow.template.json is generated with cdk synth.
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
const machine=new sfn.CfnStateMachine(stack,'Machine',{stateMachineName:param('MachineName'),roleArn:param('RoleArn'),stateMachineType:'STANDARD',definitionString:param('Definition')});
output('MachineArn',machine.ref);
app.synth();
