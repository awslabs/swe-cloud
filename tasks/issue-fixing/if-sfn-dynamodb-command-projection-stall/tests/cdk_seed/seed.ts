// Creates three tables, three queues, the workflow role and state machine; seed.template.json is generated with cdk synth.
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const ident = param('Ident').valueAsString;
for (const [name, keys] of [['commands', [['pk','S'],['sk','N']]], ['effects', [['pk','S'],['sk','S']]], ['projection', [['pk','S']]]] as const) {
 new dynamodb.CfnTable(stack, name, {tableName: name + '-' + ident, billingMode: 'PAY_PER_REQUEST',
 attributeDefinitions: keys.map(([name, type]) => ({attributeName: name, attributeType: type})),
 keySchema: keys.map(([name], i) => ({attributeName: name, keyType: i === 0 ? 'HASH' : 'RANGE'}))});
}
const queues: Record<string, sqs.CfnQueue> = {};
for (const [name, output] of [['wait-prev','WaitQueue'],['sync','SyncQueue'],['finish','FinishQueue']]) {
 queues[name] = new sqs.CfnQueue(stack, name, {queueName: name + '-' + ident, visibilityTimeout: 30});
 new cdk.CfnOutput(stack, output, {value: queues[name].ref});
}
const role = new iam.CfnRole(stack, 'Role', {roleName: 'command-' + ident, assumeRolePolicyDocument: {
 Version: '2012-10-17', Statement: [{Effect: 'Allow', Principal: {Service: 'states.amazonaws.com'}, Action: 'sts:AssumeRole'}]}});
const definition = {
 "StartAt": "route",
 "States": {
  "route": {
   "Type": "Choice",
   "Choices": [
    {
     "Variable": "$.version",
     "NumericEquals": 1,
     "Next": "sync_data"
    }
   ],
   "Default": "wait_prev_command"
  },
  "wait_prev_command": {
   "Type": "Task",
   "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
   "Parameters": {
    "QueueUrl": queues["wait-prev"].ref,
    "MessageBody": {
     "entity.$": "$.entity",
     "version.$": "$.version",
     "taskToken.$": "$$.Task.Token",
     "executionArn.$": "$$.Execution.Id"
    }
   },
   "TimeoutSeconds": 120,
   "ResultPath": "$.wait",
   "Next": "dup_check",
   "Catch": [
    {
     "ErrorEquals": [
      "States.Timeout"
     ],
     "Next": "wait_prev_command_timeout"
    }
   ]
  },
  "dup_check": {
   "Type": "Choice",
   "Choices": [
    {
     "Variable": "$.wait.result",
     "StringEquals": "duplicate",
     "Next": "duplicate_done"
    }
   ],
   "Default": "sync_data"
  },
  "sync_data": {
   "Type": "Task",
   "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
   "Parameters": {
    "QueueUrl": queues["sync"].ref,
    "MessageBody": {
     "entity.$": "$.entity",
     "version.$": "$.version",
     "taskToken.$": "$$.Task.Token",
     "executionArn.$": "$$.Execution.Id"
    }
   },
   "TimeoutSeconds": 120,
   "ResultPath": null,
   "Next": "finish"
  },
  "finish": {
   "Type": "Task",
   "Resource": "arn:aws:states:::sqs:sendMessage.waitForTaskToken",
   "Parameters": {
    "QueueUrl": queues["finish"].ref,
    "MessageBody": {
     "entity.$": "$.entity",
     "version.$": "$.version",
     "taskToken.$": "$$.Task.Token",
     "executionArn.$": "$$.Execution.Id"
    }
   },
   "TimeoutSeconds": 120,
   "ResultPath": null,
   "Next": "done"
  },
  "done": {
   "Type": "Succeed"
  },
  "duplicate_done": {
   "Type": "Succeed"
  },
  "wait_prev_command_timeout": {
   "Type": "Pass",
   "Next": "failed"
  },
  "failed": {
   "Type": "Fail"
  }
 }
};
const machine = new sfn.CfnStateMachine(stack, 'Machine', {stateMachineName: 'command-' + ident,
 roleArn: role.attrArn, definitionString: JSON.stringify(definition), stateMachineType: 'STANDARD'});
new cdk.CfnOutput(stack, 'MachineArn', {value: machine.attrArn});
app.synth();
