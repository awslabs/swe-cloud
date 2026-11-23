"""Lua scripts the pool runs on Valkey, ported from gocraft/work redis.go."""

FETCH_KEYS_PER_JOB_TYPE = 6
REQUEUE_KEYS_PER_JOB_TYPE = 4

# Used to fetch the next job to run
#
# KEYS[1] = the 1st job queue we want to try, eg, "work:jobs:emails"
# KEYS[2] = the 1st job queue's in prog queue, eg, "work:jobs:emails:97c84119d13cb54119a38743:inprogress"
# KEYS[3] = the 1st job queue's paused key
# KEYS[4] = the 1st job queue's lock
# KEYS[5] = the 1st job queue's lock info hash
# KEYS[6] = the 1st job queue's max concurrency
# ... and so on for every job type
# ARGV[1] = job queue's workerPoolID
FETCH_JOB = """
local function acquireLock(lockKey, lockInfoKey, workerPoolID)
  redis.call('incr', lockKey)
  redis.call('hincrby', lockInfoKey, workerPoolID, 1)
end

local function haveJobs(jobQueue)
  return redis.call('llen', jobQueue) > 0
end

local function isPaused(pauseKey)
  return redis.call('get', pauseKey)
end

local function canRun(lockKey, maxConcurrency)
  local activeJobs = tonumber(redis.call('get', lockKey))
  if (not maxConcurrency or maxConcurrency == 0) or (not activeJobs or activeJobs < maxConcurrency) then
    -- default case: maxConcurrency not defined or set to 0 means no cap on concurrent jobs OR
    -- maxConcurrency set, but lock does not yet exist OR
    -- maxConcurrency set, lock is set, but not yet at max concurrency
    return true
  else
    -- we are at max capacity for running jobs
    return false
  end
end

local res, jobQueue, inProgQueue, pauseKey, lockKey, maxConcurrency, workerPoolID, concurrencyKey, lockInfoKey
local keylen = #KEYS
workerPoolID = ARGV[1]

for i=1,keylen,%d do
  jobQueue = KEYS[i]
  inProgQueue = KEYS[i+1]
  pauseKey = KEYS[i+2]
  lockKey = KEYS[i+3]
  lockInfoKey = KEYS[i+4]
  concurrencyKey = KEYS[i+5]

  maxConcurrency = tonumber(redis.call('get', concurrencyKey))

  if haveJobs(jobQueue) and not isPaused(pauseKey) and canRun(lockKey, maxConcurrency) then
    acquireLock(lockKey, lockInfoKey, workerPoolID)
    res = redis.call('rpoplpush', jobQueue, inProgQueue)
    return {res, jobQueue, inProgQueue}
  end
end
return nil""" % FETCH_KEYS_PER_JOB_TYPE

# Used by the reaper to re-enqueue jobs that were in progress
#
# KEYS[1] = the 1st job's in progress queue
# KEYS[2] = the 1st job's job queue
# KEYS[3] = the 1st job's lock
# KEYS[4] = the 1st job's lock info hash
# ... and so on for every job type
# ARGV[1] = workerPoolID for job queue
REENQUEUE_JOB = """
local function releaseLock(lockKey, lockInfoKey, workerPoolID)
  redis.call('decr', lockKey)
  redis.call('hincrby', lockInfoKey, workerPoolID, -1)
end

local keylen = #KEYS
local res, jobQueue, inProgQueue, workerPoolID, lockKey, lockInfoKey
workerPoolID = ARGV[1]

for i=1,keylen,%d do
  inProgQueue = KEYS[i]
  jobQueue = KEYS[i+1]
  lockKey = KEYS[i+2]
  lockInfoKey = KEYS[i+3]
  res = redis.call('rpoplpush', inProgQueue, jobQueue)
  if res then
    releaseLock(lockKey, lockInfoKey, workerPoolID)
    return {res, inProgQueue, jobQueue}
  end
end
return nil""" % REQUEUE_KEYS_PER_JOB_TYPE

# Used by the reaper to clean up stale locks
#
# KEYS[1] = the 1st job's lock
# KEYS[2] = the 1st job's lock info hash
# ... and so on for every job type
# ARGV[1] = the dead worker pool id
REAP_STALE_LOCKS = """
local keylen = #KEYS
local lock, lockInfo, deadLockCount
local deadPoolID = ARGV[1]

for i=1,keylen,2 do
  lock = KEYS[i]
  lockInfo = KEYS[i+1]
  deadLockCount = tonumber(redis.call('hget', lockInfo, deadPoolID))

  if deadLockCount then
    redis.call('decrby', lock, deadLockCount)
    redis.call('hdel', lockInfo, deadPoolID)

    if tonumber(redis.call('get', lock)) < 0 then
      redis.call('set', lock, 0)
    end
  end
end
return nil
"""

# Used to finish a job: the lock is given back only by the call that removed the job's in-progress
# entry, so the script can be re-sent after an unanswered attempt.
#
# KEYS[1] = the job's in progress queue
# KEYS[2] = the job's lock
# KEYS[3] = the job's lock info hash
# KEYS[4] = the job's queue
# KEYS[5] = the pool's runner leases
# ARGV[1] = the job's raw JSON
# ARGV[2] = workerPoolID
# ARGV[3] = '1' to put the job back on its queue, '' to finish it
RELEASE_JOB = """
redis.call('hdel', KEYS[5], ARGV[1])
if redis.call('lrem', KEYS[1], 1, ARGV[1]) == 1 then
  redis.call('decr', KEYS[2])
  redis.call('hincrby', KEYS[3], ARGV[2], -1)
  if ARGV[3] ~= '' then
    redis.call('rpush', KEYS[4], ARGV[1])
  end
  return 1
end
return 0
"""

# Used by a job's shim to say its runner is alive: (re)sets the claim's lease, but only while the claim
# is still in the pool's in-progress list, so a claim a reaper has already put back is never run.
#
# KEYS[1] = the job's in progress queue
# KEYS[2] = the pool's runner leases
# ARGV[1] = the job's raw JSON
# ARGV[2] = lease length, ms
HOLD_RUNNER = """
if not redis.call('lpos', KEYS[1], ARGV[1]) then
  return 0
end
local now = redis.call('time')
redis.call('hset', KEYS[2], ARGV[1], tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000) + tonumber(ARGV[2]))
return 1
"""

# Used by the reaper on a pool whose heartbeat looked stale: rechecks the heartbeat, puts back every
# claim no live runner holds (one unit each), and removes the pool once none is left. A claim whose
# runner outlived its pool process keeps its unit until that runner's shim gives it back.
#
# KEYS[1] = the pool's in progress queue
# KEYS[2] = the job queue
# KEYS[3] = the lock
# KEYS[4] = the lock info hash
# KEYS[5] = the pool's runner leases
# KEYS[6] = the pool's heartbeat
# KEYS[7] = the worker pools set
# ARGV[1] = the dead workerPoolID
# ARGV[2] = now (epoch s), ARGV[3] = DEAD_TIME_S
# returns the number of claims left with a live runner, or -1 if the pool is alive
REAP_DEAD_POOL = """
local beat = tonumber(redis.call('hget', KEYS[6], 'heartbeat_at'))
if beat and beat + tonumber(ARGV[3]) > tonumber(ARGV[2]) then
  return -1
end
local t = redis.call('time')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local held = 0
for _, raw in ipairs(redis.call('lrange', KEYS[1], 0, -1)) do
  local lease = tonumber(redis.call('hget', KEYS[5], raw))
  if lease and lease > now then
    held = held + 1
  elseif redis.call('lrem', KEYS[1], 1, raw) == 1 then
    redis.call('hdel', KEYS[5], raw)
    redis.call('lpush', KEYS[2], raw)
    redis.call('decr', KEYS[3])
    redis.call('hincrby', KEYS[4], ARGV[1], -1)
  end
end
if held > 0 then
  return held
end
local left = tonumber(redis.call('hget', KEYS[4], ARGV[1]))
if left and left ~= 0 then
  redis.call('decrby', KEYS[3], left)
  if tonumber(redis.call('get', KEYS[3])) < 0 then
    redis.call('set', KEYS[3], 0)
  end
end
redis.call('hdel', KEYS[4], ARGV[1])
redis.call('del', KEYS[5], KEYS[6])
redis.call('srem', KEYS[7], ARGV[1])
return 0
"""
