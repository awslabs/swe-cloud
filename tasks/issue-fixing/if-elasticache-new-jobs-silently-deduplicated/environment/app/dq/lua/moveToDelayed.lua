--[[
  A failed attempt with attempts left: release the lock and park the job
  in the delayed zset until its backoff is over.

    Input:
      KEYS[1] active list
      KEYS[2] stalled set
      KEYS[3] delayed zset
      KEYS[4] events stream

      ARGV[1] key prefix
      ARGV[2] job id
      ARGV[3] lock token
      ARGV[4] timestamp (ms)
      ARGV[5] backoff delay (ms)
      ARGV[6] failed reason

    Output:
       0 OK
      -1 missing job
      -2 missing lock
      -3 job not in active list
      -6 lock held by another token
]]
local rcall = redis.call

local prefix = ARGV[1]
local jobId = ARGV[2]
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

if rcall("LREM", KEYS[1], -1, jobId) < 1 then
  return -3
end

local dueAt = tonumber(ARGV[4]) + tonumber(ARGV[5])
rcall("HINCRBY", jobKey, "atm", 1)
rcall("ZADD", KEYS[3], dueAt, jobId)
rcall("HSET", jobKey, "state", "delayed", "failedReason", ARGV[6])
rcall("XADD", KEYS[4], "MAXLEN", "~", 1000, "*", "event", "delayed", "jobId", jobId,
  "delay", dueAt, "prev", "active")

return 0
