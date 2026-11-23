"""Source database fixtures (plain SQL), adapted from upstream tests/unit/setup and tests/filtering."""

# A varied database for full-clone fidelity: several schemas, NULLs, escapes, arrays, jsonb,
# bytea, generated and identity columns, a zero-column table, inheritance, list partitions,
# exclusion and NOT VALID check constraints, partial/expression indexes, views, a matview,
# sequences in every state, quoted identifiers and one large object.
RICH = r"""
create schema shop;
create table shop.customers (id bigserial primary key, email text not null unique, name text);
create table shop.orders (
    id bigserial primary key,
    customer_id bigint not null references shop.customers(id),
    amount numeric(10,2) not null check (amount > 0),
    note text, tags text[], meta jsonb, raw bytea,
    placed timestamptz default '2024-01-02 03:04:05+00');
create index orders_customer_idx on shop.orders(customer_id);
create index orders_note_lower_idx on shop.orders(lower(note)) where note is not null;
insert into shop.customers(email, name)
     select 'u' || g || '@x.test', case when g % 9 = 0 then null else 'User ' || g end from generate_series(1, 40) g;
insert into shop.orders(customer_id, amount, note, tags, meta, raw)
     select 1 + (g % 40), g * 1.25,
            case when g % 7 = 0 then null when g % 5 = 0 then E'tab\there\nnew "line" \\ back' else 'n' || g end,
            array['a' || g, null], jsonb_build_object('g', g, 'odd', g % 2 = 1), decode(md5(g::text), 'hex')
       from generate_series(1, 3000) g;
create table public.exclcon (a int, b int);
insert into public.exclcon values (1, 2), (3, 4);
alter table public.exclcon add constraint exclcon_a_excl exclude using btree (a with =);
create table public.gen (id int primary key, nom text, up text generated always as (upper(nom)) stored);
insert into public.gen(id, nom) values (1, 'abc'), (2, null), (3, 'Été');
create table public.ident (id bigint generated always as identity primary key, f1 text);
insert into public.ident(f1) select 'x' || g from generate_series(1, 7) g;
create table public.nocols();
insert into public.nocols default values;
insert into public.nocols default values;
create table public.employees (name varchar not null);
insert into public.employees values ('zed'), ('abc');
alter table public.employees add constraint chk_name check (name !~ 'abc') not valid;
create table public.parent_model (id int primary key, a int not null);
create table public.child_model (c text) inherits (public.parent_model);
insert into public.parent_model values (1, 1);
insert into public.child_model values (2, 2, 'c'), (3, 3, null);
create table public.sellers (id bigint, archive smallint not null) partition by list (archive);
create table public.sellers_active partition of public.sellers default;
create table public.sellers_archive partition of public.sellers for values in (1);
insert into public.sellers values (1, 0), (2, 1), (3, 0), (3, 0);
create view public.v_orders as select id, amount from shop.orders where amount > 100;
create materialized view public.mv as select count(*) as n, sum(amount) as total from shop.orders;
create sequence public.ticket_seq start 100;
select setval('public.ticket_seq', 4242);
create sequence public.unused_seq minvalue 671;
create sequence public.called_false_seq;
select setval('public.called_false_seq', 50, false);
create sequence public.small_seq as smallint;
select setval('public.small_seq', 670);
create schema "Mixed Case";
create table "Mixed Case"."Tab le" ("iD" int primary key, "nA M" text);
insert into "Mixed Case"."Tab le" values (1, 'a'), (2, 'b'), (3, null);
select lo_from_bytea(424242, 'hello large object');
"""

# Schemas for filter semantics (adapted from tests/filtering/extra.sql).
FILTERING = r"""
create schema app;
create schema arch;
create schema misc;
create table app.users (id serial primary key, name text not null unique);
create table app.orders (id serial primary key, user_id int not null references app.users(id),
                         total numeric(8,2) check (total >= 0));
create index orders_user_idx on app.orders(user_id);
create index orders_total_idx on app.orders(total);
create table app.events (id bigserial primary key, kind text);
create sequence app.notes_seq;
create table app.notes (id int primary key default nextval('app.notes_seq'), body text);
create table arch.old_orders (id int primary key, user_id int references app.users(id));
create sequence arch.arch_seq;
select setval('arch.arch_seq', 77);
create table misc.kv (k text primary key, v text);
create sequence public.counter_seq;
select setval('public.counter_seq', 9001);
create view app.big_orders as select * from app.orders where total > 50;
create view app.user_names as select name from app.users;
create materialized view misc.mv_kv as select k from misc.kv;
create view misc.v_events as select kind from app.events;
insert into app.users(name) select 'n' || g from generate_series(1, 10) g;
insert into app.orders(user_id, total) select 1 + g % 10, g from generate_series(1, 100) g;
insert into app.events(kind) select 'k' || g from generate_series(1, 5) g;
insert into app.notes(body) select 'b' || g from generate_series(1, 4) g;
insert into arch.old_orders select g, 1 + g % 10 from generate_series(1, 3) g;
insert into misc.kv select 'k' || g, 'v' || g from generate_series(1, 6) g;
refresh materialized view misc.mv_kv;
"""

# Public-schema-only database for the step-by-step commands.
STEPS = r"""
create table public.big (id int primary key, pad text);
insert into public.big select g, repeat(md5(g::text), 3) from generate_series(1, 20000) g;
create table public.mid (id int primary key, big_id int references public.big(id), v text);
insert into public.mid select g, g, 'm' || g from generate_series(1, 3000) g;
create index mid_v_idx on public.mid(v);
create table public.small (id serial primary key, label text unique);
insert into public.small(label) select 'L' || g from generate_series(1, 5) g;
create table public.heap (a text, b text);
insert into public.heap select md5((g % 500)::text), 'x' from generate_series(1, 2000) g;
create sequence public.gseq;
select setval('public.gseq', 31337);
"""

# Target-side failure injection: once the program creates public.small on the target, a
# CHECK (false) constraint makes every row written into it fail, whatever the write method.
FAIL_SMALL_ON_TARGET = r"""
create function public.inject_failure() returns event_trigger language plpgsql as $$
declare r record;
begin
  for r in select * from pg_event_trigger_ddl_commands() where command_tag = 'CREATE TABLE' loop
    if r.object_identity = 'public.small' then
      execute 'alter table public.small add constraint injected_failure check (false) not valid';
    end if;
  end loop;
end $$;
create event trigger inject_failure on ddl_command_end execute function public.inject_failure();
"""

REMOVE_FAILURE = r"""
drop event trigger inject_failure;
drop function public.inject_failure();
alter table public.small drop constraint injected_failure;
"""
