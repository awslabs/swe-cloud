schema "public" {
  comment = "standard public schema"
}
schema "crm" {
  comment = "customer records"
}
table "contacts" {
  schema = schema.crm
  column "id" {
    type = int
  }
  column "region" {
    type = text
  }
  column "email" {
    type    = text
    comment = "work address"
  }
  column "phone" {
    type = text
    null = true
  }
  primary_key {
    columns = [column.id, column.region]
  }
  index "contacts_lookup" {
    unique  = true
    columns = [column.region, column.email]
  }
}
