"""Held owner-directory/policy/ledger identity. No provisioning or ACL repair.
Windows kernel file identity comes from the existing checked project adapter;
DACL reads are native and fail closed. Nothing exports SID/path/security data.
"""
from contextlib import ExitStack,contextmanager
import os,stat
from pathlib import Path,PureWindowsPath

class OwnerFileError(RuntimeError):pass

def need(value,code):
 if not value:raise OwnerFileError(code)

def acl_allowed(owner,current,protected,aces,*,directory):
 """Only the fixed current-user/SYSTEM/Administrators allow-ACE pattern.
 Native parser has already validated ACE/SID byte bounds. No broad read grants.
 """
 trusted={current,b'SYSTEM',b'ADMINISTRATORS'}
 need(owner in trusted and(not directory or protected),'OWNER_FILE_PERMISSIONS')
 need(0<len(aces)<=64,'OWNER_FILE_PERMISSIONS')
 full=False
 for kind,flags,mask,sid in aces:
  need(kind==0 and sid in trusted and flags&~0x1f==0,'OWNER_FILE_PERMISSIONS')
  if sid==current and not flags&8 and(mask&0x001f01ff==0x001f01ff or mask&0x10000000):full=True
 need(full,'OWNER_FILE_PERMISSIONS')

def _windows_kernel():
 from home_study_agent.registered_asset_restore import _TargetWin32
 class Kernel(_TargetWin32):
  def rewind(self,handle):
   ct=self.ct;position=ct.c_int64();fn=self.dll.SetFilePointerEx
   fn.argtypes=[ct.c_void_p,ct.c_int64,ct.POINTER(ct.c_int64),ct.c_uint32];fn.restype=ct.c_int
   need(fn(handle,0,ct.byref(position),0)and position.value==0,'OWNER_POLICY_SEEK_UNCONFIRMED')
  def guarded_open(self,path,*,directory=False,policy=False):
   ct=self.ct;access=0x20080|(0x80000000 if policy else 0)
   h=self.dll.CreateFileW('\\\\?\\'+str(path),access,1 if policy else 3,None,3,0x00200000|(0x02000000 if directory else 0),None)
   if h in{None,0,ct.c_void_p(-1).value}:raise ct.WinError(ct.get_last_error())
   return h
 k=Kernel();return k,_WindowsSecurity(k)

class _WindowsSecurity:
 def __init__(self,k):
  ct=k.ct;self.ct=ct;self.k=k;self.adv=ct.WinDLL('advapi32',use_last_error=True);P=ct.c_void_p;D=ct.c_uint32
  signatures={'OpenProcessToken':([P,D,ct.POINTER(P)],ct.c_int),'GetTokenInformation':([P,ct.c_int,P,D,ct.POINTER(D)],ct.c_int),'GetLengthSid':([P],D),'IsValidSid':([P],ct.c_int),'ConvertStringSidToSidW':([ct.c_wchar_p,ct.POINTER(P)],ct.c_int),'GetSecurityInfo':([P,ct.c_int,D,ct.POINTER(P),ct.POINTER(P),ct.POINTER(P),ct.POINTER(P),ct.POINTER(P)],D),'GetSecurityDescriptorControl':([P,ct.POINTER(ct.c_uint16),ct.POINTER(D)],ct.c_int),'GetAclInformation':([P,P,D,ct.c_int],ct.c_int),'GetAce':([P,D,ct.POINTER(P)],ct.c_int)}
  for name,(args,result)in signatures.items():f=getattr(self.adv,name);f.argtypes=args;f.restype=result
  k.dll.GetCurrentProcess.argtypes=[];k.dll.GetCurrentProcess.restype=P;k.dll.LocalFree.argtypes=[P];k.dll.LocalFree.restype=P
  self.current=self._user();self.system=self._known('S-1-5-18');self.admin=self._known('S-1-5-32-544')
 def _sid(self,value):
  ct=self.ct;need(bool(value)and bool(self.adv.IsValidSid(value)),'OWNER_NATIVE_SECURITY_UNCONFIRMED');size=self.adv.GetLengthSid(value);need(8<=size<=68,'OWNER_NATIVE_SECURITY_UNCONFIRMED');return ct.string_at(value,size)
 def _free(self,value):
  if value:need(not self.k.dll.LocalFree(value),'OWNER_NATIVE_SECURITY_UNCONFIRMED')
 def _known(self,text):
  ct=self.ct;sid=ct.c_void_p()
  try:need(self.adv.ConvertStringSidToSidW(text,ct.byref(sid)),'OWNER_NATIVE_SECURITY_UNCONFIRMED');return self._sid(sid)
  finally:self._free(sid)
 def _user(self):
  ct=self.ct;token=ct.c_void_p();size=ct.c_uint32()
  need(self.adv.OpenProcessToken(self.k.dll.GetCurrentProcess(),8,ct.byref(token)),'OWNER_NATIVE_SECURITY_UNCONFIRMED')
  try:
   ok=self.adv.GetTokenInformation(token,1,None,0,ct.byref(size));need(not ok and ct.get_last_error()==122 and 8<=size.value<=4096,'OWNER_NATIVE_SECURITY_UNCONFIRMED')
   value=ct.create_string_buffer(size.value);need(self.adv.GetTokenInformation(token,1,value,size.value,ct.byref(size)),'OWNER_NATIVE_SECURITY_UNCONFIRMED');return self._sid(ct.cast(value,ct.POINTER(ct.c_void_p))[0])
  finally:self.k.close(token)
 def check(self,handle,*,directory=False):
  ct=self.ct;owner=ct.c_void_p();dacl=ct.c_void_p();sd=ct.c_void_p()
  try:
   need(self.adv.GetSecurityInfo(handle,1,5,ct.byref(owner),None,ct.byref(dacl),None,ct.byref(sd))==0,'OWNER_NATIVE_SECURITY_UNCONFIRMED');need(bool(dacl),'OWNER_FILE_PERMISSIONS')
   control=ct.c_uint16();revision=ct.c_uint32();need(self.adv.GetSecurityDescriptorControl(sd,ct.byref(control),ct.byref(revision)),'OWNER_NATIVE_SECURITY_UNCONFIRMED')
   class Size(ct.Structure):_fields_=[('count',ct.c_uint32),('used',ct.c_uint32),('free',ct.c_uint32)]
   info=Size();need(self.adv.GetAclInformation(dacl,ct.byref(info),ct.sizeof(info),2)and info.count<=64 and 8<=info.used<=8192,'OWNER_FILE_PERMISSIONS')
   records=[]
   for i in range(info.count):
    ptr=ct.c_void_p();need(self.adv.GetAce(dacl,i,ct.byref(ptr)),'OWNER_NATIVE_SECURITY_UNCONFIRMED');at=ptr.value;kind=ct.c_ubyte.from_address(at).value;flags=ct.c_ubyte.from_address(at+1).value;length=ct.c_uint16.from_address(at+2).value
    need(kind==0 and 16<=length<=256,'OWNER_FILE_PERMISSIONS');mask=ct.c_uint32.from_address(at+4).value;sid=self._sid(at+8);need(8+len(sid)<=length,'OWNER_FILE_PERMISSIONS')
    records.append((kind,flags,mask,sid if sid==self.current else b'SYSTEM'if sid==self.system else b'ADMINISTRATORS'if sid==self.admin else sid))
   owner_sid=self._sid(owner);owner_sid=owner_sid if owner_sid==self.current else b'SYSTEM'if owner_sid==self.system else b'ADMINISTRATORS'if owner_sid==self.admin else owner_sid
   acl_allowed(owner_sid,self.current,bool(control.value&0x1000),records,directory=directory)
  finally:self._free(sd)

class OwnerDirectory:
 def __init__(self,path):
  self.path=Path(path);self.stack=ExitStack();self.closed=False;self.windows=os.name=='nt';self.chain=[]
  try:
   need(self.path.is_absolute()and self.path==Path(os.path.abspath(self.path))and len(str(self.path))<=4096,'OWNER_DIRECTORY_PATH')
   if self.windows:
    from home_study_agent.pet_media_files import _windows_parts
    self.kernel,self.security=_windows_kernel();parts=_windows_parts(str(self.path));need(self.kernel.drive_type(parts[0])in{2,3},'OWNER_DIRECTORY_PATH')
    for i in range(len(parts)):
     name=PureWindowsPath(*parts[:i+1]);h=self.kernel.guarded_open(name,directory=True)if i==len(parts)-1 else self.kernel.open(str(name),directory=True)
     self.stack.callback(self.kernel.close,h);identity=self._win_check(h,name,True);self.chain.append((h,name,identity))
    self.parent_handle=self.chain[-1][0]
   else:
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY);self.stack.callback(os.close,fd)
    for part in self.path.parts[1:]:
     parent=fd;fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);self.stack.callback(os.close,fd);s=os.fstat(fd);self.chain.append((parent,part,fd,(s.st_dev,s.st_ino)))
    self.parent_fd=fd
   self.check()
  except BaseException:
   self.close();raise
 def __enter__(self):return self
 def __exit__(self,*args):self.close()
 def _win_check(self,handle,path,directory,identity=None):
  from home_study_agent.registered_asset_restore import _win_target_check
  info=_win_target_check(self.kernel,handle,path,directory=directory,identity=identity)
  if not directory:need(info['links']==1,'OWNER_FILE_ALIAS_OR_OWNER')
  return info['identity']
 def check(self):
  need(not self.closed,'OWNER_FILES_CLOSED')
  if self.windows:
   for h,path,identity in self.chain:self._win_check(h,path,True,identity)
   self.security.check(self.parent_handle,directory=True)
  else:
   for parent,name,fd,identity in self.chain:
    a=os.stat(name,dir_fd=parent,follow_symlinks=False);b=os.fstat(fd);need(stat.S_ISDIR(a.st_mode)and(a.st_dev,a.st_ino)==(b.st_dev,b.st_ino)==identity,'OWNER_DIRECTORY_PATH')
   s=os.fstat(self.parent_fd);need(s.st_uid==os.getuid()and s.st_mode&0o077==0,'OWNER_DIRECTORY_PERMISSIONS')
 def flush(self):
  self.check()
  if self.windows:
   h=self.kernel.open_target(str(self.path),write=True)
   try:self._win_check(h,self.path,True,self.chain[-1][2]);self.kernel.flush(h)
   finally:self.kernel.close(h)
  else:os.fsync(self.parent_fd)
 def close(self):
  if not self.closed:
   self.stack.close();self.closed=True

class OwnerFiles(OwnerDirectory):
 def __init__(self,policy):
  self.policy_path=Path(policy);super().__init__(self.policy_path.parent)
  try:
   self.policy=self._open(self.policy_path.name,policy=True);self.database=self._open('money-budget.sqlite3');self.db_identity=self.database[1]
  except BaseException:self.close();raise
 def _open(self,name,*,policy=False,sidecar=False):
  try:
   if self.windows:
    h=self.kernel.guarded_open(self.path/name,policy=policy);identity=self._win_check(h,self.path/name,False);self.security.check(h);result=(h,identity,name)
   else:
    # Do not open/close auxiliary DB/WAL/SHM FDs: on POSIX that can
    # interfere with process-scoped SQLite locks. Preserve r1 nofollow stat
    # identity under the retained private directory; policy has its own FD.
    h=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=self.parent_fd)if policy else None
    info=os.fstat(h)if h is not None else os.stat(name,dir_fd=self.parent_fd,follow_symlinks=False)
    identity=(info.st_dev,info.st_ino);result=(h,identity,name);self._posix_check(result)
  except OSError as e:
   if'h'in locals()and h is not None:
    if self.windows:self.kernel.close(h)
    else:os.close(h)
   if getattr(e,'winerror',None)in{2,3}or e.errno==2:
    if sidecar:return None
    raise OwnerFileError('OWNER_FILE_MISSING')from None
   raise OwnerFileError('OWNER_FILE_ALIAS_OR_OWNER')from None
  except BaseException:
   if'h'in locals()and h is not None:
    if self.windows:self.kernel.close(h)
    else:os.close(h)
   raise
  if not sidecar and h is not None:self.stack.callback(self.kernel.close if self.windows else os.close,h)
  return result
 def _posix_check(self,held):
  fd,identity,name=held;n=os.stat(name,dir_fd=self.parent_fd,follow_symlinks=False);s=os.fstat(fd)if fd is not None else n
  need(stat.S_ISREG(s.st_mode)and s.st_nlink==n.st_nlink==1 and s.st_uid==os.getuid()and(s.st_dev,s.st_ino)==(n.st_dev,n.st_ino)==identity,'OWNER_FILE_ALIAS_OR_OWNER');need(s.st_mode&0o077==0,'OWNER_FILE_PERMISSIONS')
 def _check_file(self,held):
  h,identity,name=held
  if self.windows:self._win_check(h,self.path/name,False,identity);self.security.check(h)
  else:self._posix_check(held)
 def read_policy(self):
  self.check();self._check_file(self.policy);h=self.policy[0]
  if self.windows:
   before=self.kernel.info(h);need(0<before['size']<=8192,'OWNER_POLICY_SIZE');self.kernel.rewind(h);raw=self.kernel.read(h,8192);need(self.kernel.info(h)==before,'OWNER_FILE_CHANGED')
  else:
   before=os.fstat(h);need(0<before.st_size<=8192,'OWNER_POLICY_SIZE');os.lseek(h,0,os.SEEK_SET);raw=os.read(h,8193);after=os.fstat(h);need((before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_size,after.st_mtime_ns,after.st_ctime_ns),'OWNER_FILE_CHANGED')
  self._check_file(self.policy);return raw
 @contextmanager
 def guard(self):
  held=[]
  try:
   self.check();self._check_file(self.policy);self._check_file(self.database)
   for suffix in['-wal','-shm','-journal']:
    row=self._open('money-budget.sqlite3'+suffix,sidecar=True)
    if row:held.append(row)
   yield
   self.check();self._check_file(self.database)
   for row in held:self._check_file(row)
   for suffix in['-wal','-shm','-journal']:
    name='money-budget.sqlite3'+suffix
    if name not in{row[2]for row in held}:
     row=self._open(name,sidecar=True)
     if row:held.append(row)
  finally:
   for row in reversed(held):
    if self.windows:self.kernel.close(row[0])
    elif row[0]is not None:os.close(row[0])
