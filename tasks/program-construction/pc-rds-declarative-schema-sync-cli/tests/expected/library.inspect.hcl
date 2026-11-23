table "a_independent" {
  schema = schema.public
  column "x" {
    null = false
    type = smallint
  }
}
table "authors" {
  schema  = schema.public
  comment = "people who write"
  column "id" {
    null = false
    type = bigserial
  }
  column "name" {
    null    = false
    type    = character_varying(100)
    comment = "display name"
  }
  column "bio" {
    null = true
    type = text
  }
  column "active" {
    null    = false
    type    = boolean
    default = true
  }
  column "score" {
    null    = false
    type    = numeric(8,2)
    default = 1.5
  }
  column "created" {
    null    = false
    type    = timestamptz
    default = sql("now()")
  }
  column "tags" {
    null = true
    type = sql("text[]")
  }
  column "mood" {
    null = true
    type = enum.mood
  }
  column "code" {
    null    = false
    type    = character(2)
    default = "xx"
  }
  column "ratio" {
    null = true
    type = double_precision
  }
  column "uid" {
    null = true
    type = uuid
  }
  column "meta" {
    null    = false
    type    = jsonb
    default = "{}"
  }
  primary_key {
    columns = [column.id]
  }
  index "authors_code_partial" {
    unique  = true
    columns = [column.code]
    where   = "active"
  }
  index "authors_created_desc" {
    on {
      desc   = true
      column = column.created
    }
    on {
      column = column.id
    }
  }
  index "authors_name_lower" {
    on {
      expr = "lower((name)::text)"
    }
  }
  index "authors_tags_gin" {
    columns = [column.tags]
    type    = GIN
  }
  index "authors_uid_hash" {
    columns = [column.uid]
    type    = HASH
  }
  check "score_range" {
    expr = "((score >= (0)::numeric) AND (score <= (100)::numeric))"
  }
}
table "books" {
  schema = schema.public
  column "id" {
    null = false
    type = integer
  }
  column "author_id" {
    null = true
    type = bigint
  }
  column "title" {
    null = false
    type = text
  }
  primary_key {
    columns = [column.id]
  }
  foreign_key "books_author_fk" {
    columns     = [column.author_id]
    ref_columns = [table.authors.column.id]
    on_update   = RESTRICT
    on_delete   = SET_NULL
  }
}
enum "mood" {
  schema = schema.public
  values = ["happy", "sad"]
}
schema "public" {
  comment = "standard public schema"
}
