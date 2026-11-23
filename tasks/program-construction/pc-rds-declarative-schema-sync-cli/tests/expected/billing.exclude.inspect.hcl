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
schema "public" {
  comment = "standard public schema"
}
