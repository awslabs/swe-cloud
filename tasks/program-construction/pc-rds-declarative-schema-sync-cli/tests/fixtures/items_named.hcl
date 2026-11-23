schema "public" {
  comment = "standard public schema"
}
table "items" {
  schema = schema.public
  column "id" {
    type = int
  }
  column "name" {
    type = text
    null = true
  }
  primary_key {
    columns = [column.id]
  }
}
