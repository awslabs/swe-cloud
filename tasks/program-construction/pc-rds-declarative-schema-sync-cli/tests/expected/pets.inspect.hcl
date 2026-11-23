table "owners" {
  schema = schema.public
  column "id" {
    null = false
    type = serial
  }
  column "name" {
    null = false
    type = character_varying
  }
  primary_key {
    columns = [column.id]
  }
}
table "pets" {
  schema  = schema.public
  comment = "animals"
  column "id" {
    null = false
    type = bigint
    identity {
      generated = BY_DEFAULT
    }
  }
  column "owner_id" {
    null = true
    type = integer
  }
  column "born" {
    null = true
    type = timestamp(3)
  }
  column "weight" {
    null = true
    type = numeric
  }
  column "fur" {
    null    = true
    type    = enum.color
    default = "red"
  }
  column "nick" {
    null    = true
    type    = text
    default = "rex"
    comment = "short name"
  }
  column "flags" {
    null = true
    type = sql("boolean[]")
  }
  column "ok" {
    null    = true
    type    = boolean
    default = false
  }
  primary_key {
    columns = [column.id]
  }
  foreign_key "pets_owner_id_fkey" {
    columns     = [column.owner_id]
    ref_columns = [table.owners.column.id]
    on_update   = NO_ACTION
    on_delete   = CASCADE
  }
  index "pets_lower_nick" {
    where = "ok"
    on {
      expr = "lower(nick)"
    }
  }
  check "weight_pos" {
    expr = "(weight > (0)::numeric)"
  }
  unique "pets_nick_born" {
    columns = [column.nick, column.born]
  }
}
enum "color" {
  schema = schema.public
  values = ["red", "green"]
}
schema "public" {
  comment = "standard public schema"
}
