schema "public" {
  comment = "standard public schema"
}
table "tags" {
  schema = schema.public
  column "id" {
    type = bigint
    identity {
      generated = ALWAYS
    }
  }
  column "label" {
    type = text
  }
  column "scope" {
    type = text
  }
  primary_key {
    columns = [column.id]
  }
  unique "tags_label_scope" {
    columns = [column.label, column.scope]
  }
}
