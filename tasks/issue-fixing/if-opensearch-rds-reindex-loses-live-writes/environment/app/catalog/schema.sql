-- The catalog (owned by the catalog team). Hard deletes; ids come from the sequence and are never reused.
CREATE TABLE products (
    id          bigserial   PRIMARY KEY,
    name        text        NOT NULL,
    price_cents integer     NOT NULL,
    brand       text        NOT NULL,
    updated_at  timestamptz NOT NULL      -- the app host's wall clock at the change (display only)
);

-- The search indexer's role sees none of the catalog tables; it reads products through the export API
-- and keeps its own state in the schema it owns:
--   CREATE ROLE catalog_indexer LOGIN PASSWORD '...';
--   CREATE SCHEMA reindex_aux AUTHORIZATION catalog_indexer;
