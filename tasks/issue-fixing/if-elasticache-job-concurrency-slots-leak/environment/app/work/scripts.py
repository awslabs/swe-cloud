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
