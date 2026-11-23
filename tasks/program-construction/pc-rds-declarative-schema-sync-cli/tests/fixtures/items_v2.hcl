schema "public" {
  comment = "standard public schema"
}
enum "kind" {
  schema = schema.public
  values = ["a", "b"]
}
table "items" {
  schema = schema.public
  column "id" {
    type = int
  }
  column "k" {
    type = enum.kind
  }
  primary_key {
    columns = [column.id]
  }
}
