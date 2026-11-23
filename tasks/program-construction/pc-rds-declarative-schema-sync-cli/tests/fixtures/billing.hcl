schema "public" {
  comment = "standard public schema"
}
schema "billing" {
  comment = "money"
}
enum "currency" {
  schema = schema.billing
  values = ["usd", "eur"]
}
table "customers" {
  schema = schema.public
  column "id" {
    type = int
  }
  column "name" {
    type = text
  }
  primary_key {
    columns = [column.id]
  }
}
table "invoices" {
  schema = schema.billing
  column "id" {
    type = bigint
  }
  column "customer_id" {
    type = int
  }
  column "cur" {
    type = enum.currency
    default = "usd"
  }
  column "amount" {
    type = numeric(9,3)
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
    type = BRIN
    columns = [column.amount]
  }
}
