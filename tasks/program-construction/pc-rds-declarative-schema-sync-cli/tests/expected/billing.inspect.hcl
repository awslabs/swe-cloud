table "invoices" {
  schema = schema.billing
  column "id" {
    null = false
    type = bigint
  }
  column "customer_id" {
    null = false
    type = integer
  }
  column "cur" {
    null    = false
    type    = enum.currency
    default = "usd"
  }
  column "amount" {
    null    = false
    type    = numeric(9,3)
    default = 0.5
  }
  primary_key {
    columns = [column.id, column.customer_id]
  }
  foreign_key "inv_customer" {
    columns     = [column.customer_id]
    ref_columns = [table.customers.column.id]
    on_update   = CASCADE
    on_delete   = SET_DEFAULT
  }
  index "inv_amount_brin" {
    columns = [column.amount]
    type    = BRIN
  }
}
table "customers" {
  schema = schema.public
  column "id" {
    null = false
    type = integer
  }
  column "name" {
    null = false
    type = text
  }
  primary_key {
    columns = [column.id]
  }
}
enum "currency" {
  schema = schema.billing
  values = ["usd", "eur"]
}
schema "billing" {
  comment = "money"
}
schema "public" {
  comment = "standard public schema"
}
