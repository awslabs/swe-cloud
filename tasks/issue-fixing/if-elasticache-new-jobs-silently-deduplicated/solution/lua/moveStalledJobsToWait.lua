--[[
  Stall checker. Runs every stalledInterval in every worker; the
  stalled-check key makes sure only one pass runs per interval.

  Pass n marks every active job in the stalled set. A job whose worker is
  alive gets unmarked by its next lock extension. On pass n+1, a job that is
  still marked and has no lock has lost its worker: it goes back to wait,
  or is failed once it has stalled more than maxStalledCount times.

    Input:
      KEYS[1] stalled set
      KEYS[2] wait list
      KEYS[3] active list
      KEYS[4] stalled-check key
      KEYS[5] failed zset
      KEYS[6] events stream

      ARGV[1] max stalled count
      ARGV[2] key prefix
      ARGV[3] timestamp (ms)
      ARGV[4] max check time (ms)

    Output:
      ids of the jobs handled as stalled in this pass
]]
local rcall = redis.call

local stalledKey = KEYS[1]
local waitKey = KEYS[2]
local activeKey = KEYS[3]
local stalledCheckKey = KEYS[4]
local failedKey = KEYS[5]
local eventsKey = KEYS[6]
local maxStalledJobCount = tonumber(ARGV[1])
local prefix = ARGV[2]
local timestamp = ARGV[3]
local maxCheckTime = ARGV[4]

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

if rcall("EXISTS", stalledCheckKey) == 1 then
  return {}
end

rcall("SET", stalledCheckKey, timestamp, "PX", maxCheckTime)

local stalling = rcall("SMEMBERS", stalledKey)
local stalled = {}
if #stalling > 0 then
  rcall("DEL", stalledKey)

  for _, jobId in ipairs(stalling) do
    local jobKey = prefix .. "job:" .. jobId

    -- Check that the lock is also missing, then we can handle this job as really stalled.
    if rcall("EXISTS", jobKey .. ":lock") == 0 then
      local removed = rcall("LREM", activeKey, 1, jobId)

      if removed > 0 then
        local stalledCount = rcall("HINCRBY", jobKey, "stc", 1)

        if stalledCount > maxStalledJobCount then
          local failedReason = "job stalled more than allowable limit"
          rcall("ZADD", failedKey, timestamp, jobId)
          rcall("HSET", jobKey, "state", "failed", "failedReason", failedReason,
            "finishedOn", timestamp)
          -- Same as moveToFinished: a finished job gives up its dedup id, and
          -- the job kept by a keep-last add is created.
          local deduplicationId = rcall("HGET", jobKey, "deid")
          if deduplicationId then
            local deduplicationKey = prefix .. "de:" .. deduplicationId
            local pttl = rcall("PTTL", deduplicationKey)
            if pttl == 0 or (pttl == -1 and rcall("GET", deduplicationKey) == jobId) then
              rcall("DEL", deduplicationKey)
            end
            requeueDeduplicatedJob(prefix, deduplicationId, timestamp, eventsKey)
          end
          rcall("XADD", eventsKey, "MAXLEN", "~", 1000, "*", "event", "failed", "jobId", jobId,
            "failedReason", failedReason, "prev", "active")
        else
          rcall("RPUSH", waitKey, jobId)
          rcall("HSET", jobKey, "state", "wait")
          rcall("XADD", eventsKey, "MAXLEN", "~", 1000, "*", "event", "stalled", "jobId", jobId)
        end
        table.insert(stalled, jobId)
      end
    end
  end
end

-- Mark potentially stalled jobs
local active = rcall("LRANGE", activeKey, 0, -1)
for from = 1, #active, 5000 do
  rcall("SADD", stalledKey, unpack(active, from, math.min(from + 4999, #active)))
end

return stalled
