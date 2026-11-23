"""Trusted invocation boundary; this module is never imported from /app."""
import ctypes
import os
import sys

# Workers may execute arbitrary buyer code but must not gain the verifier's uid
# from executable bits on an exported artifact.
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
    raise OSError(ctypes.get_errno(), 'unable to isolate purchase worker')
os.execve(sys.executable, [sys.executable, *sys.argv[1:]], dict(os.environ))
