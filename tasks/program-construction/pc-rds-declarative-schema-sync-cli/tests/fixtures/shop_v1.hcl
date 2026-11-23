schema "public" {
  comment = "standard public schema"
}
enum "status" {
  schema = schema.public
  values = ["active", "inactive"]
}
table "users" {
  schema = schema.public
  column "id" {
    type = bigint
  }
  column "email" {
    type = varchar(255)
  }
  column "status" {
    type    = enum.status
    default = "active"
  }
  column "nickname" {
    type = text
    null = true
  }
  column "age" {
    type = int
    null = true
  }
  primary_key {
    columns = [column.id]
  }
  index "users_email" {
    unique  = true
    columns = [column.email]
  }
}
table "orders" {
  schema = schema.public
  column "id" {
    type = serial
  }
  column "user_id" {
    type = bigint
  }
  column "total" {
    type    = numeric(10,2)
    default = 0
  }
  column "note" {
    type = text
    null = true
  }
  primary_key {
    columns = [column.id]
  }
  index "orders_user_idx" {
    columns = [column.user_id]
  }
  foreign_key "orders_user" {
    columns     = [column.user_id]
    ref_columns = [table.users.column.id]
    on_delete   = CASCADE
  }
  check "total_nonneg" {
    expr = "total >= 0"
  }
}
table "legacy" {
  schema = schema.public
  column "id" {
    type = int
  }
  column "user_id" {
    type = bigint
    null = true
  }
  foreign_key "legacy_user" {
    columns     = [column.user_id]
    ref_columns = [table.users.column.id]
  }
}
