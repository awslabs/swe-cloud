--[[
  Move a job from active to completed or failed.
  The job must be locked by the caller; the lock is released here.
  Releases the job's dedup id, and creates the job kept by a keep-last add.

    Input:
      KEYS[1] active list
      KEYS[2] stalled set
      KEYS[3] completed or failed zset (the target)
      KEYS[4] events stream

      ARGV[1] key prefix
      ARGV[2] job id
      ARGV[3] lock token
      ARGV[4] timestamp (ms)
      ARGV[5] target state: "completed" | "failed"
      ARGV[6] field to store: "returnvalue" | "failedReason"
      ARGV[7] value of that field

    Output:
       0 OK
      -1 missing job
      -2 missing lock
      -6 lock held by another token
]]
local rcall = redis.call

local function removeDeduplicationKeyIfNeededOnFinalization(prefix, deduplicationId, jobId)
  if deduplicationId then
    local deduplicationKey = prefix .. "de:" .. deduplicationId
    local pttl = rcall("PTTL", deduplicationKey)

    if pttl == 0 then
      return rcall("DEL", deduplicationKey)
    end

    if pttl == -1 then
      local currentJobId = rcall("GET", deduplicationKey)
      if currentJobId and currentJobId == jobId then
        return rcall("DEL", deduplicationKey)
      end
    end
  end
end

-- keep-last: an add made while this job was active kept its data in
-- dn:<id>. Create the next job from it; it takes over the dedup id.
local function requeueDeduplicatedJob(prefix, deduplicationId, timestamp, eventsKey)
  local deduplicationNextKey = prefix .. "dn:" .. deduplicationId
  if rcall("EXISTS", deduplicationNextKey) == 1 then
    local nextData = rcall("HMGET", deduplicationNextKey, "name", "data")
    local newJobId = tostring(rcall("INCR", prefix .. "id"))
    local newJobKey = prefix .. "job:" .. newJobId
    rcall("SET", prefix .. "de:" .. deduplicationId, newJobId)
    rcall("HSET", newJobKey, "name", nextData[1] or "", "data", nextData[2] or "null",
      "state", "wait", "atm", 0, "stc", 0, "timestamp", timestamp, "deid", deduplicationId)
    rcall("LPUSH", prefix .. "wait", newJobId)
    rcall("XADD", eventsKey, "MAXLEN", "~", 1000, "*", "event", "waiting", "jobId", newJobId)
    rcall("DEL", deduplicationNextKey)
  end
end

local prefix = ARGV[1]
local jobId = ARGV[2]
local timestamp = ARGV[4]
local target = ARGV[5]
local jobKey = prefix .. "job:" .. jobId
local lockKey = jobKey .. ":lock"

if rcall("EXISTS", jobKey) == 0 then
  return -1
end

local lockToken = rcall("GET", lockKey)
if not lockToken then
  return -2
end
if lockToken ~= ARGV[3] then
  return -6
end
rcall("DEL", lockKey)
rcall("SREM", KEYS[2], jobId)

-- The lock token proves this worker still owns the job, even when its id
-- was taken out of the active list by hand: finish it all the same, or its
-- dedup id and kept data would never be released.
rcall("LREM", KEYS[1], -1, jobId)

local deduplicationId = rcall("HGET", jobKey, "deid")
removeDeduplicationKeyIfNeededOnFinalization(prefix, deduplicationId, jobId)

rcall("HINCRBY", jobKey, "atm", 1)
rcall("ZADD", KEYS[3], timestamp, jobId)
rcall("HSET", jobKey, "state", target, ARGV[6], ARGV[7], "finishedOn", timestamp)
rcall("XADD", KEYS[4], "MAXLEN", "~", 1000, "*", "event", target, "jobId", jobId,
  ARGV[6], ARGV[7], "prev", "active")

if deduplicationId then
  requeueDeduplicatedJob(prefix, deduplicationId, timestamp, KEYS[4])
end

return 0
