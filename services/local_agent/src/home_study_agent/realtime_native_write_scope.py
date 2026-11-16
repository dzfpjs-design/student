"""Write fence scoped to an authenticated native executor, inert for legacy SQL.

The connection/cursor wrappers check every statement and after lock acquisition;
no SQL prefix parsing can bypass the fence. Checks suspend themselves to permit
read-only provenance queries without recursion. Native scripts are split into
complete statements by StudyConnection so they cannot implicitly commit unfenced.
"""
from contextvars import ContextVar
from contextlib import contextmanager
_check=ContextVar('native_effect_write_check',default=None)
_checking=ContextVar('native_effect_write_check_recursion',default=False)

@contextmanager
def native_write_scope(check):
 token=_check.set(check)
 try:yield
 finally:_check.reset(token)

def verify(c):
 check=_check.get()
 if check is None or _checking.get():return
 token=_checking.set(True)
 try:
  if check(c) is not True:
   from .agent_runtime import InvalidAgentPlanError
   raise InvalidAgentPlanError('NATIVE_EXECUTION_SCOPE_REVOKED')
 finally:_checking.reset(token)

def script_allowed():return _check.get() is None or _checking.get()


def active():return _check.get() is not None
