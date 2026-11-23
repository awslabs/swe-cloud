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
