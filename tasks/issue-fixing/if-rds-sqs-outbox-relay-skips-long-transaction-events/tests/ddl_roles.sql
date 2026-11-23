-- Roles and grants. Passwords are set out of band (ops/devstack.py does it for the dev stack).
create role app_writer login;
create role outbox_relay login;

grant select, insert on outbox to app_writer;
grant usage on sequence outbox_position_seq to app_writer;

grant select on outbox to outbox_relay;
grant select on outbox_position_seq to outbox_relay;
grant select, update on relay_checkpoint to outbox_relay;
grant pg_read_all_stats to outbox_relay;
