schema "public" {
  comment = "standard public schema"
}
enum "mood" {
  schema = schema.public
  values = ["happy", "sad"]
}
table "authors" {
  schema = schema.public
  comment = "people who write"
  column "id" {
    type = bigserial
  }
  column "name" {
    type = varchar(100)
    comment = "display name"
  }
  column "bio" {
    type = text
    null = true
  }
  column "active" {
    type = boolean
    default = true
  }
  column "score" {
    type = decimal(8,2)
    default = 1.5
  }
  column "created" {
    type = timestamptz
    default = sql("now()")
  }
  column "tags" {
    type = sql("text[]")
    null = true
  }
  column "mood" {
    type = enum.mood
    null = true
  }
  column "code" {
    type = char(2)
    default = "xx"
  }
  column "ratio" {
    type = double_precision
    null = true
  }
  column "uid" {
    type = uuid
    null = true
  }
  column "meta" {
    type = jsonb
    default = sql("'{}'::jsonb")
  }
  primary_key {
    columns = [column.id]
  }
  index "authors_name_lower" {
    on {
      expr = "lower((name)::text)"
    }
  }
  index "authors_created_desc" {
    on {
      column = column.created
      desc = true
    }
    on {
      column = column.id
    }
  }
  index "authors_code_partial" {
    unique = true
    columns = [column.code]
    where = "active"
  }
  index "authors_tags_gin" {
    type = GIN
    columns = [column.tags]
  }
  index "authors_uid_hash" {
    type = HASH
    columns = [column.uid]
  }
  check "score_range" {
    expr = "score >= 0 AND score <= 100"
  }
}
table "books" {
  schema = schema.public
  column "id" {
    type = int
  }
  column "author_id" {
    type = bigint
    null = true
  }
  column "title" {
    type = text
  }
  primary_key {
    columns = [column.id]
  }
  foreign_key "books_author_fk" {
    columns = [column.author_id]
    ref_columns = [table.authors.column.id]
    on_delete = SET_NULL
    on_update = RESTRICT
  }
}
table "a_independent" {
  schema = schema.public
  column "x" {
    type = smallint
  }
}
