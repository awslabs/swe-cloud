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

-- keep-last: if the job holding the id is running, keep this add's name and
-- data in dn:<id>, replacing whatever an earlier add kept there. The job is
-- created from it once the running job finishes (moveToFinished, or the stall
-- checker when it fails the job). "Running" is a handler holding the job's
-- lock, or the job sitting in active: the id may have been taken out of the
-- active list by hand while its handler still runs.
local function storeDeduplicatedNextJob(currentDeduplicatedJobId)
  if keepLast and currentDeduplicatedJobId then
    local ownerKey = prefix .. "job:" .. currentDeduplicatedJobId
    if rcall("EXISTS", ownerKey .. ":lock") == 1
      or rcall("LPOS", prefix .. "active", currentDeduplicatedJobId) then
      local deduplicationNextKey = prefix .. "dn:" .. deduplicationId
      rcall("DEL", deduplicationNextKey)
      rcall("HSET", deduplicationNextKey, "name", ARGV[2], "data", ARGV[3],
        "timestamp", timestamp)
      rcall("PERSIST", prefix .. "de:" .. deduplicationId)
      return true
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

    -- A key with a TTL is a throttle window: it holds until it expires,
    -- whatever happened to its job. A key without one is an ownership
    -- pointer and only holds while the job it names is still in flight:
    -- a handler holds its lock, or the record is in a list a worker or the
    -- stall checker will take it from. The state field alone does not say
    -- that: the job may have been finished by a path that never released
    -- the key, its record deleted outright, or its id taken out of the lists.
    if rcall("PTTL", deduplicationKey) == -1 then
      local ownerKey = prefix .. "job:" .. currentDeduplicatedJobId
      local inFlight = rcall("EXISTS", ownerKey .. ":lock") == 1
      if not inFlight and rcall("EXISTS", ownerKey) == 1 then
        inFlight = rcall("LPOS", prefix .. "wait", currentDeduplicatedJobId) ~= false
          or rcall("LPOS", prefix .. "active", currentDeduplicatedJobId) ~= false
          or rcall("ZSCORE", prefix .. "delayed", currentDeduplicatedJobId) ~= false
      end
      if not inFlight then
        -- Data kept for the gone owner's successor is older than this add's.
        rcall("DEL", prefix .. "dn:" .. deduplicationId)
        if ttl > 0 then
          rcall("SET", deduplicationKey, jobId, "PX", ttl)
        else
          rcall("SET", deduplicationKey, jobId)
        end
        deduplicationKeyExists = false
      end
    end
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
