"""Native-observed plans and catalogs shared by several modules (fixtures/library.hcl, fixtures/shop_v1.hcl)."""

LIBRARY_PLAN = [
    """CREATE TYPE "public"."mood" AS ENUM ('happy', 'sad')""",
    """CREATE TABLE "public"."a_independent" ("x" smallint NOT NULL)""",
    """CREATE TABLE "public"."authors" (
      "id" bigserial NOT NULL, "name" character varying(100) NOT NULL, "bio" text NULL,
      "active" boolean NOT NULL DEFAULT true, "score" numeric(8,2) NOT NULL DEFAULT 1.5,
      "created" timestamptz NOT NULL DEFAULT now(), "tags" text[] NULL, "mood" "public"."mood" NULL,
      "code" character(2) NOT NULL DEFAULT 'xx', "ratio" double precision NULL, "uid" uuid NULL,
      "meta" jsonb NOT NULL DEFAULT '{}', PRIMARY KEY ("id"),
      CONSTRAINT "score_range" CHECK ((score >= (0)::numeric) AND (score <= (100)::numeric)))""",
    """CREATE UNIQUE INDEX "authors_code_partial" ON "public"."authors" ("code") WHERE active""",
    """CREATE INDEX "authors_created_desc" ON "public"."authors" ("created" DESC, "id")""",
    """CREATE INDEX "authors_name_lower" ON "public"."authors" ((lower((name)::text)))""",
    """CREATE INDEX "authors_tags_gin" ON "public"."authors" USING gin ("tags")""",
    """CREATE INDEX "authors_uid_hash" ON "public"."authors" USING hash ("uid")""",
    """COMMENT ON TABLE "public"."authors" IS 'people who write'""",
    """COMMENT ON COLUMN "public"."authors"."name" IS 'display name'""",
    """CREATE TABLE "public"."books" ("id" integer NOT NULL, "author_id" bigint NULL, "title" text NOT NULL,
      PRIMARY KEY ("id"), CONSTRAINT "books_author_fk" FOREIGN KEY ("author_id") REFERENCES "public"."authors" ("id")
      ON UPDATE RESTRICT ON DELETE SET NULL)""",
]
# The enum precedes the table using it; indexes and comments follow their table; books follows authors.
LIBRARY_ORDER = [(0, 2), (2, 3), (2, 4), (2, 5), (2, 6), (2, 7), (2, 8), (2, 9), (2, 10)]

LIBRARY_CATALOG = """
col|public|a_independent|x|smallint|int2|None|16|0|NO|None
col|public|authors|id|bigint|int8|None|64|0|NO|nextval('authors_id_seq'::regclass)
col|public|authors|name|character varying|varchar|100|None|None|NO|None
col|public|authors|bio|text|text|None|None|None|YES|None
col|public|authors|active|boolean|bool|None|None|None|NO|true
col|public|authors|score|numeric|numeric|None|8|2|NO|1.5
col|public|authors|created|timestamp with time zone|timestamptz|None|None|None|NO|now()
col|public|authors|tags|ARRAY|_text|None|None|None|YES|None
col|public|authors|mood|USER-DEFINED|mood|None|None|None|YES|None
col|public|authors|code|character|bpchar|2|None|None|NO|'xx'::bpchar
col|public|authors|ratio|double precision|float8|None|53|None|YES|None
col|public|authors|uid|uuid|uuid|None|None|None|YES|None
col|public|authors|meta|jsonb|jsonb|None|None|None|NO|'{}'::jsonb
col|public|books|id|integer|int4|None|32|0|NO|None
col|public|books|author_id|bigint|int8|None|64|0|YES|None
col|public|books|title|text|text|None|None|None|NO|None
con|public|authors|authors_pkey|p|PRIMARY KEY (id)
con|public|authors|score_range|c|CHECK (((score >= (0)::numeric) AND (score <= (100)::numeric)))
con|public|books|books_author_fk|f|FOREIGN KEY (author_id) REFERENCES authors(id) ON UPDATE RESTRICT ON DELETE SET NULL
con|public|books|books_pkey|p|PRIMARY KEY (id)
idx|public|authors_code_partial|CREATE UNIQUE INDEX authors_code_partial ON public.authors USING btree (code) WHERE active
idx|public|authors_created_desc|CREATE INDEX authors_created_desc ON public.authors USING btree (created DESC, id)
idx|public|authors_name_lower|CREATE INDEX authors_name_lower ON public.authors USING btree (lower((name)::text))
idx|public|authors_pkey|CREATE UNIQUE INDEX authors_pkey ON public.authors USING btree (id)
idx|public|authors_tags_gin|CREATE INDEX authors_tags_gin ON public.authors USING gin (tags)
idx|public|authors_uid_hash|CREATE INDEX authors_uid_hash ON public.authors USING hash (uid)
idx|public|books_pkey|CREATE UNIQUE INDEX books_pkey ON public.books USING btree (id)
enum|public|mood|happy,sad
schema|public|standard public schema
cmt|public|authors||people who write
cmt|public|authors|name|display name
"""

SHOP_V1_CATALOG = """
col|public|legacy|id|integer|int4|None|32|0|NO|None
col|public|legacy|user_id|bigint|int8|None|64|0|YES|None
col|public|orders|id|integer|int4|None|32|0|NO|nextval('orders_id_seq'::regclass)
col|public|orders|user_id|bigint|int8|None|64|0|NO|None
col|public|orders|total|numeric|numeric|None|10|2|NO|0
col|public|orders|note|text|text|None|None|None|YES|None
col|public|users|id|bigint|int8|None|64|0|NO|None
col|public|users|email|character varying|varchar|255|None|None|NO|None
col|public|users|status|USER-DEFINED|status|None|None|None|NO|'active'::status
col|public|users|nickname|text|text|None|None|None|YES|None
col|public|users|age|integer|int4|None|32|0|YES|None
con|public|users|users_pkey|p|PRIMARY KEY (id)
con|public|legacy|legacy_user|f|FOREIGN KEY (user_id) REFERENCES users(id)
con|public|orders|orders_pkey|p|PRIMARY KEY (id)
con|public|orders|orders_user|f|FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
con|public|orders|total_nonneg|c|CHECK ((total >= (0)::numeric))
idx|public|orders_pkey|CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)
idx|public|orders_user_idx|CREATE INDEX orders_user_idx ON public.orders USING btree (user_id)
idx|public|users_email|CREATE UNIQUE INDEX users_email ON public.users USING btree (email)
idx|public|users_pkey|CREATE UNIQUE INDEX users_pkey ON public.users USING btree (id)
enum|public|status|active,inactive
schema|public|standard public schema
"""
