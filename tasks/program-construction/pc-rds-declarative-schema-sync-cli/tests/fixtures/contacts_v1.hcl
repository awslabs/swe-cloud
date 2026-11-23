schema "public" {
  comment = "standard public schema"
}
schema "crm" {
  comment = "customer data"
}
table "contacts" {
  schema  = schema.crm
  comment = "people"
  column "id" {
    type = int
  }
  column "region" {
    type = text
  }
  column "email" {
    type    = text
    comment = "primary address"
  }
  column "phone" {
    type = text
  }
  primary_key {
    columns = [column.id]
  }
  index "contacts_lookup" {
    columns = [column.email]
  }
}
