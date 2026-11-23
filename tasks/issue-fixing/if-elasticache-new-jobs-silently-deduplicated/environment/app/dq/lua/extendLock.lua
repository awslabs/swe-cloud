--[[
  Extend the lock of a job this worker is processing.

    Input:
      KEYS[1] job lock key
      KEYS[2] stalled set

      ARGV[1] lock token
      ARGV[2] lock duration (ms)
      ARGV[3] job id

    Output:
      1 extended, 0 the lock is gone or held by another token
]]
local rcall = redis.call

if rcall("GET", KEYS[1]) == ARGV[1] then
  rcall("PEXPIRE", KEYS[1], ARGV[2])
  rcall("SREM", KEYS[2], ARGV[3])
  return 1
end
return 0
