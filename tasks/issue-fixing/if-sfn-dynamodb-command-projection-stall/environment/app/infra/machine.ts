// Creates the command state machine; machine.template.json is generated with cdk synth.
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
const machine = new sfn.CfnStateMachine(stack, 'Machine', {stateMachineName: 'command-chain',
 roleArn: param('RoleArn').valueAsString, definitionString: param('Definition').valueAsString, stateMachineType: 'STANDARD'});
new cdk.CfnOutput(stack, 'MachineArn', {value: machine.attrArn});
app.synth();
