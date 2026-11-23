CREATE TABLE sync_limit (name varchar(256), sizelimit int, PRIMARY KEY(name));
CREATE TABLE sync_controller (controller varchar(64) NOT NULL, time timestamp, PRIMARY KEY(controller));
CREATE TABLE sync_state (
 name varchar(256), workflowkey varchar(256), controller varchar(64) NOT NULL,
 held boolean, priority int, time timestamp, execution_id varchar(64) NOT NULL,
 PRIMARY KEY(name, workflowkey, controller)
);
CREATE INDEX istate_name ON sync_state(name);
CREATE INDEX istate_workflowkey ON sync_state(workflowkey);
CREATE INDEX istate_controller ON sync_state(controller);
CREATE INDEX istate_held ON sync_state(held);
