// Creates the Postgres instance; seed.template.json is generated with cdk synth.
import * as rds from 'aws-cdk-lib/aws-rds';
import * as cdk from 'aws-cdk-lib';
const app = new cdk.App();
const stack = new cdk.Stack(app, 'Seed', { synthesizer: new cdk.BootstraplessSynthesizer() });
const param = (name: string, type = 'String') => new cdk.CfnParameter(stack, name, { type });
new rds.CfnDBInstance(stack, 'Database', {
 dbInstanceIdentifier: param('InstanceName').valueAsString,
 engine: 'postgres', engineVersion: '16.3', dbInstanceClass: 'db.t3.micro',
 allocatedStorage: '20', masterUsername: 'syncadmin', masterUserPassword: 'local-password', dbName: 'sync',
});
app.synth();
