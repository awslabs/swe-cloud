--[[
  Promote delayed jobs that are due, then take the next waiting job and lock it.

    Input:
      KEYS[1] wait list
      KEYS[2] active list
      KEYS[3] delayed zset
      KEYS[4] events stream

      ARGV[1] key prefix
      ARGV[2] lock token
      ARGV[3] lock duration (ms)
      ARGV[4] timestamp (ms)

    Output:
      false when nothing is waiting, else
      {jobId, name, data, deid, atm, stc}
]]
local rcall = redis.call

local waitKey = KEYS[1]
local activeKey = KEYS[2]
local delayedKey = KEYS[3]
local eventsKey = KEYS[4]
local prefix = ARGV[1]
local timestamp = tonumber(ARGV[4])

-- Delayed jobs whose backoff is over go back to the wait list.
local due = rcall("ZRANGEBYSCORE", delayedKey, "-inf", timestamp, "LIMIT", 0, 1000)
for _, delayedId in ipairs(due) do
  rcall("ZREM", delayedKey, delayedId)
  rcall("LPUSH", waitKey, delayedId)
  rcall("HSET", prefix .. "job:" .. delayedId, "state", "wait")
  rcall("XADD", eventsKey, "MAXLEN", "~", 1000, "*", "event", "waiting", "jobId", delayedId,
    "prev", "delayed")
end

for _ = 1, 100 do
  local jobId = rcall("LMOVE", waitKey, activeKey, "RIGHT", "LEFT")
  if not jobId then
    return false
  end
  local jobKey = prefix .. "job:" .. jobId
  if rcall("EXISTS", jobKey) == 1 then
    rcall("SET", jobKey .. ":lock", ARGV[2], "PX", ARGV[3])
    rcall("HSET", jobKey, "state", "active", "processedOn", ARGV[4])
    rcall("XADD", eventsKey, "MAXLEN", "~", 1000, "*", "event", "active", "jobId", jobId,
      "prev", "waiting")
    local fields = rcall("HMGET", jobKey, "name", "data", "deid", "atm", "stc")
    return {jobId, fields[1] or "", fields[2] or "null", fields[3] or "",
      fields[4] or "0", fields[5] or "0"}
  end
  -- The id outlived its job record; drop it.
  rcall("LREM", activeKey, 1, jobId)
end
return false
