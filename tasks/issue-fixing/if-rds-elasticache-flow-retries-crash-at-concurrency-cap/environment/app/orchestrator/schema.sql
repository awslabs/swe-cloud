-- Applied by the orchestrator and the repossessor on start-up (idempotent).
--
-- Shared tables: the fleet (worker, engines) and the operator tooling read and write these
-- directly. Columns may be added; existing columns keep their names and meaning.

CREATE TABLE IF NOT EXISTS concurrency_limit_v2 (
    id            uuid PRIMARY KEY,
    name          text NOT NULL,
    "limit"       integer NOT NULL,
    active_slots  integer NOT NULL DEFAULT 0,
    active        boolean NOT NULL DEFAULT true,
    updated       timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS deployment (
    id                    uuid PRIMARY KEY,
    name                  text NOT NULL UNIQUE,
    concurrency_limit_id  uuid REFERENCES concurrency_limit_v2 (id),
    collision_strategy    text NOT NULL DEFAULT 'ENQUEUE'   -- ENQUEUE | CANCEL_NEW
);

CREATE TABLE IF NOT EXISTS flow_run (
    id              uuid PRIMARY KEY,
    deployment_id   uuid REFERENCES deployment (id),
    created_seq     bigserial,
    name            text,
    parameters      jsonb NOT NULL DEFAULT '{}'::jsonb,
    retries         integer NOT NULL DEFAULT 0,
    retry_delay_s   integer NOT NULL DEFAULT 0,
    run_count       integer NOT NULL DEFAULT 0,
    retry_type      text,                 -- empirical policy: 'in_process' while retrying in-process
    -- current state (mirrors the newest flow_run_state row)
    state_type      text NOT NULL,
    state_name      text NOT NULL,
    lease_id        uuid,                 -- state_details.deployment_concurrency_lease_id
    scheduled_time  timestamptz,
    state_ts        timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS flow_run_state (
    id              bigserial PRIMARY KEY,
    flow_run_id     uuid NOT NULL REFERENCES flow_run (id),
    type            text NOT NULL,
    name            text NOT NULL,
    lease_id        uuid,
    scheduled_time  timestamptz,
    message         text,
    ts              timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS flow_run_state_run_idx ON flow_run_state (flow_run_id, id);

-- Written by engines: one row per attempt of a flow body.
CREATE TABLE IF NOT EXISTS attempt_log (
    id           bigserial PRIMARY KEY,
    flow_run_id  uuid NOT NULL,
    attempt      integer NOT NULL,
    lease_id     uuid,
    body_start   timestamptz NOT NULL DEFAULT clock_timestamp(),
    body_end     timestamptz,
    outcome      text
);
