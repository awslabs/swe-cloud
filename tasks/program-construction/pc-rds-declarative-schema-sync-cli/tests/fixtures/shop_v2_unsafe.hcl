schema "public" {
  comment = "standard public schema"
}
enum "status" {
  schema = schema.public
  values = ["active", "pending", "inactive", "banned"]
}
table "users" {
  schema = schema.public
  column "id" {
    type = bigint
  }
  column "email" {
    type = varchar(320)
  }
  column "status" {
    type    = enum.status
    default = "pending"
  }
  column "age" {
    type = bigint
    null = true
  }
  column "created_at" {
    type    = timestamptz
    default = sql("now()")
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
    type    = numeric(12,2)
    default = 1
  }
  column "note" {
    type = text
    default = ""
  }
  primary_key {
    columns = [column.id]
  }
  index "orders_user_total" {
    on {
      column = column.user_id
    }
    on {
      column = column.total
      desc   = true
    }
  }
  foreign_key "orders_user" {
    columns     = [column.user_id]
    ref_columns = [table.users.column.id]
    on_delete   = RESTRICT
  }
  check "total_nonneg" {
    expr = "total >= 1"
  }
}
table "audit" {
  schema = schema.public
  column "id" {
    type = bigserial
  }
  column "order_id" {
    type = int
  }
  primary_key {
    columns = [column.id]
  }
  foreign_key "audit_order" {
    columns     = [column.order_id]
    ref_columns = [table.orders.column.id]
    on_delete   = SET_NULL
  }
}
