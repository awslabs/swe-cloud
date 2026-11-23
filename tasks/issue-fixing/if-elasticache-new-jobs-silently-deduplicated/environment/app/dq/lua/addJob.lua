--[[
  Add a job to the wait list, honouring deduplication.

    Input:
      KEYS[1] id counter
      KEYS[2] wait list
      KEYS[3] events stream

      ARGV[1] key prefix ("dq:<queue>:")
      ARGV[2] job name
      ARGV[3] job data (JSON text)
      ARGV[4] deduplication id ("" for none)
      ARGV[5] deduplication ttl in ms ("0" for simple mode)
      ARGV[6] timestamp (ms)
      ARGV[7] keep last: "1" or "0" (with "1" the ttl is ignored)

    Output:
      {1, jobId}   job created
      {0, jobId}   deduplicated: no job created, jobId is the existing job
]]
local rcall = redis.call

local prefix = ARGV[1]
local deduplicationId = ARGV[4]
local ttl = tonumber(ARGV[5]) or 0
local timestamp = ARGV[6]
local keepLast = ARGV[7] == "1"
local eventsKey = KEYS[3]

if keepLast then
  ttl = 0
end

-- keep-last: if the job holding the id is active, keep this add's name and
-- data in dn:<id>, replacing whatever an earlier add kept there. The job is
-- created from it once the active job finishes (moveToFinished).
local function storeDeduplicatedNextJob(currentDeduplicatedJobId)
  if keepLast and currentDeduplicatedJobId then
    local activeItems = rcall("LRANGE", prefix .. "active", 0, -1)
    for _, item in ipairs(activeItems) do
      if item == currentDeduplicatedJobId then
        local deduplicationNextKey = prefix .. "dn:" .. deduplicationId
        rcall("DEL", deduplicationNextKey)
        rcall("HSET", deduplicationNextKey, "name", ARGV[2], "data", ARGV[3],
          "timestamp", timestamp)
        rcall("PERSIST", prefix .. "de:" .. deduplicationId)
        return true
      end
    end
  end
  return false
end

local jobId = tostring(rcall("INCR", KEYS[1]))

if deduplicationId ~= "" then
  local deduplicationKey = prefix .. "de:" .. deduplicationId
  local deduplicationKeyExists
  if ttl > 0 then
    deduplicationKeyExists = not rcall("SET", deduplicationKey, jobId, "PX", ttl, "NX")
  else
    deduplicationKeyExists = not rcall("SET", deduplicationKey, jobId, "NX")
  end

  if deduplicationKeyExists then
    local currentDeduplicatedJobId = rcall("GET", deduplicationKey)
    storeDeduplicatedNextJob(currentDeduplicatedJobId)
    rcall("XADD", eventsKey, "MAXLEN", "~", 1000, "*", "event", "deduplicated",
      "jobId", currentDeduplicatedJobId, "deduplicationId", deduplicationId,
      "deduplicatedJobId", jobId)
    return {0, currentDeduplicatedJobId}
  end
end

local jobKey = prefix .. "job:" .. jobId
rcall("HSET", jobKey, "name", ARGV[2], "data", ARGV[3], "state", "wait",
  "atm", 0, "stc", 0, "timestamp", timestamp)
if deduplicationId ~= "" then
  rcall("HSET", jobKey, "deid", deduplicationId)
end
rcall("LPUSH", KEYS[2], jobId)
rcall("XADD", eventsKey, "MAXLEN", "~", 1000, "*", "event", "waiting", "jobId", jobId)

return {1, jobId}
