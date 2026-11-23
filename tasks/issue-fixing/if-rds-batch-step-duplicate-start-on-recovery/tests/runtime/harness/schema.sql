CREATE ROLE solver_pods NOLOGIN;
CREATE ROLE run_worker LOGIN PASSWORD 'worker-local';
CREATE SCHEMA dagster;
CREATE SCHEMA solver AUTHORIZATION run_worker;
GRANT USAGE, CREATE ON SCHEMA solver TO solver_pods;
ALTER DEFAULT PRIVILEGES FOR ROLE run_worker IN SCHEMA solver GRANT ALL ON TABLES TO solver_pods;
ALTER DEFAULT PRIVILEGES FOR ROLE run_worker IN SCHEMA solver GRANT ALL ON SEQUENCES TO solver_pods;
CREATE SEQUENCE dagster.seq;
CREATE TABLE dagster.runs(run_id text PRIMARY KEY, status text NOT NULL CHECK(status IN ('QUEUED','STARTED','SUCCESS','FAILURE')), updated_at timestamptz DEFAULT clock_timestamp());
CREATE TABLE dagster.run_inputs(run_id text PRIMARY KEY, reject_load boolean NOT NULL DEFAULT false);
CREATE TABLE dagster.event_logs(id bigint PRIMARY KEY DEFAULT nextval('dagster.seq'), run_id text NOT NULL, step_key text, attempt int, pod_name text, dagster_event_type text NOT NULL, ts timestamptz DEFAULT clock_timestamp(), event jsonb);
CREATE TABLE dagster.ledger(id bigint PRIMARY KEY DEFAULT nextval('dagster.seq'), run_id text NOT NULL, step_key text NOT NULL, attempt int NOT NULL, pod_name text, batch_no int NOT NULL, payload text, ts timestamptz DEFAULT clock_timestamp());
CREATE TABLE dagster.k8s_jobs(job_name text PRIMARY KEY, run_id text, step_key text, attempt int, argv jsonb, created_at timestamptz DEFAULT clock_timestamp());
CREATE TABLE dagster.k8s_pods(pod_name text PRIMARY KEY, job_name text, phase text, deletion_ts timestamptz, exit_code int, node_ready text);
CREATE FUNCTION dagster.writer_identity() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.pod_name := session_user; RETURN NEW; END $$;
CREATE TRIGGER identity_event BEFORE INSERT ON dagster.event_logs FOR EACH ROW EXECUTE FUNCTION dagster.writer_identity();
CREATE TRIGGER identity_ledger BEFORE INSERT ON dagster.ledger FOR EACH ROW EXECUTE FUNCTION dagster.writer_identity();
GRANT USAGE ON SCHEMA dagster TO run_worker, solver_pods;
GRANT SELECT, INSERT ON dagster.event_logs, dagster.ledger, dagster.k8s_jobs TO run_worker, solver_pods;
GRANT SELECT ON dagster.run_inputs, dagster.k8s_pods, dagster.runs TO run_worker, solver_pods;
GRANT UPDATE ON dagster.runs TO run_worker;
GRANT USAGE ON SEQUENCE dagster.seq TO run_worker, solver_pods;

-- Production's deferred batch validation is represented by bounded SQL work.
-- It runs inside COMMIT, so connection failure need not mean commit is resolved.
CREATE FUNCTION dagster.validate_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.payload <> NEW.step_key || ':' || NEW.batch_no::text THEN
        RAISE EXCEPTION 'invalid batch payload';
    END IF;
    IF (NEW.step_key = 'extract' AND NEW.batch_no = 1) OR
       (NEW.step_key = 'audit' AND NEW.batch_no = 0) THEN
        PERFORM pg_sleep(4);
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER validate_batch AFTER INSERT ON dagster.ledger
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION dagster.validate_batch();
