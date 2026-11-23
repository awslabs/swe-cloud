-- documents database schema. Owned and applied by the platform team as the database owner;
-- docserver connects as the `docserver` role and cannot change any of it.

CREATE TABLE IF NOT EXISTS documents (
    name    text   PRIMARY KEY,
    state   bytea  NOT NULL,  -- the full Yjs update of the document (encode_state_as_update)
    version bigint NOT NULL   -- 1 when the row is created; previous committed version + 1 on every write
);

-- Every committed version of every row, kept for audit and point-in-time restore of a single
-- document. Written by the trigger below, never by applications.
CREATE TABLE IF NOT EXISTS documents_history (
    id      bigserial   PRIMARY KEY,
    name    text        NOT NULL,
    version bigint      NOT NULL,
    state   bytea       NOT NULL,
    writer  text        NOT NULL DEFAULT session_user,
    txid    bigint      NOT NULL DEFAULT txid_current(),
    at      timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION documents_history_capture() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    INSERT INTO documents_history (name, version, state) VALUES (NEW.name, NEW.version, NEW.state);
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS documents_history_capture ON documents;
CREATE TRIGGER documents_history_capture AFTER INSERT OR UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION documents_history_capture();

-- docserver: the document server. doccompact: the platform's row compaction job (see the README).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'docserver') THEN
        CREATE ROLE docserver LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'doccompact') THEN
        CREATE ROLE doccompact LOGIN;
    END IF;
END
$$;

REVOKE ALL ON documents, documents_history FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO docserver;
GRANT SELECT, INSERT, UPDATE ON documents TO docserver;
GRANT USAGE ON SCHEMA public TO doccompact;
GRANT SELECT, UPDATE ON documents TO doccompact;
REVOKE EXECUTE ON FUNCTION documents_history_capture() FROM PUBLIC;
