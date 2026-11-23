schema "public" {
  comment = "standard public schema"
}
table "customers" {
  schema = schema.public
  column "id" {
    type = int
  }
  column "name" {
    type = text
  }
  column "email" {
    type = text
    null = true
  }
  primary_key {
    columns = [column.id]
  }
}
table "items" {
  schema = schema.public
  column "id" {
    type = int
  }
  primary_key {
    columns = [column.id]
  }
}
