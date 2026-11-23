-- Ledger outbox schema. Owned by the platform team; applied as shipped.
create sequence outbox_position_seq cache 1;

create table outbox (
    -- delivery position; consumers apply events in this order
    position        bigint primary key default nextval('outbox_position_seq'),
    -- transaction that wrote the row
    transaction_id  xid8 not null default pg_current_xact_id(),
    -- producer-assigned id, unique; a producer retry with the same id is dropped
    message_id      text not null unique,
    message_type    text not null,
    data            jsonb not null,
    scheduled       timestamptz not null default now()
);
create index outbox_transaction_id_position on outbox (transaction_id, position);

-- The relay's durable state: one row.
create table relay_checkpoint (
    id          int primary key check (id = 1),
    -- every outbox position <= this has been handled: sent, or known never to commit
    position    bigint not null,
    -- free-form relay bookkeeping
    meta        jsonb not null default '{}'::jsonb,
    updated_at  timestamptz not null default now()
);
insert into relay_checkpoint (id, position) values (1, 0);
