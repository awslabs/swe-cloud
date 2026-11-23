schema "public" {
  comment = "standard public schema"
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
