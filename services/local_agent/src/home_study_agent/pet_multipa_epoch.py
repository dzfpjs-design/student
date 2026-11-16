"""Windows immutable-install reader lease for the one fixed MultiPA worker.

The registry is intentionally empty. Adding a deployment requires a separate
review of the actual installation, permission, feature-map and dependency tree.
No caller path, manifest status or six hashes is authority to install a model.
"""
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
from contextlib import ExitStack

COMMIT='a8d83ec1d80f9db60e6d32227cb1c562ee5e6022'
GROUPS={'multipa','charsiu','hubert','roberta','whisper_medium_en','whisper_base_en'}
MODEL_ROLES={'multipa_checkpoint','hubert_checkpoint','roberta_directory','roberta_encoder_json','roberta_vocab_bpe','whisper_medium_en','whisper_base_en','charsiu_directory','tokenizer_directory','nltk_directory'}
AUTHORITIES={'.install.lock','deployment.json','permission-review.json','training-phonemes.json','dependency-lock.json'}
_REVIEWED_DEPLOYMENTS={}


def need(ok,code):
    if not ok:raise ValueError('PET_MULTIPA_'+code)


def configured(key):
    need(type(key)is str and key in _REVIEWED_DEPLOYMENTS,'DEPLOYMENT_UNCONFIGURED')
    return _REVIEWED_DEPLOYMENTS[key]


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def safe_relative(value):
    need(type(value)is str and 0<len(value)<=240,'EPOCH_PATH_REJECTED')
    parts=value.split('/')
    need(all(p and p not in{'.','..'}and not p.endswith((' ','.'))and not any(c in p for c in '\\:< >"|?*')and all(ord(c)>=32 for c in p)for p in parts),'EPOCH_PATH_REJECTED')
    need(not any(p.split('.')[0].upper()in{'CON','PRN','AUX','NUL','CONIN$','CONOUT$',*(f'{a}{n}'for a in('COM','LPT')for n in range(1,10))}for p in parts),'EPOCH_PATH_REJECTED')
    return parts


def readonly_acl(owner,current,protected,aces):
    """Installation owned by SYSTEM/Admin; the actual standard actor has RX only."""
    need(owner in{b'SYSTEM',b'ADMINISTRATORS'}and protected,'EPOCH_ACL_REJECTED')
    need(0<len(aces)<=16,'EPOCH_ACL_REJECTED');reader=False
    for kind,flags,mask,sid in aces:
        need(kind==0 and flags&~0x0f==0 and sid in{current,b'SYSTEM',b'ADMINISTRATORS'},'EPOCH_ACL_REJECTED')
        if sid==current:
            need(mask&~0x001200a9==0 and mask&0x00020000,'EPOCH_ACL_REJECTED')
            if not flags&8:reader=True
    need(reader,'EPOCH_ACL_REJECTED')


class _Security:
    def __init__(self,k):
        c=k.ct;P=c.c_void_p;D=c.c_uint32;self.c=c;self.k=k;self.a=c.WinDLL('advapi32',use_last_error=True)
        specs={'OpenProcessToken':([P,D,c.POINTER(P)],c.c_int),'GetTokenInformation':([P,c.c_int,P,D,c.POINTER(D)],c.c_int),'GetLengthSid':([P],D),'IsValidSid':([P],c.c_int),'ConvertStringSidToSidW':([c.c_wchar_p,c.POINTER(P)],c.c_int),'GetSecurityInfo':([P,c.c_int,D,c.POINTER(P),c.POINTER(P),c.POINTER(P),c.POINTER(P),c.POINTER(P)],D),'GetSecurityDescriptorControl':([P,c.POINTER(c.c_uint16),c.POINTER(D)],c.c_int),'GetSecurityDescriptorLength':([P],D),'GetAclInformation':([P,P,D,c.c_int],c.c_int),'GetAce':([P,D,c.POINTER(P)],c.c_int),'CheckTokenMembership':([P,P,c.POINTER(c.c_int)],c.c_int)}
        for name,(args,result)in specs.items():f=getattr(self.a,name);f.argtypes=args;f.restype=result
        k.dll.GetCurrentProcess.argtypes=[];k.dll.GetCurrentProcess.restype=P;k.dll.LocalFree.argtypes=[P];k.dll.LocalFree.restype=P
        self.system=self.known('S-1-5-18');self.admin=self.known('S-1-5-32-544');self.current=self.actor()
    def sid(self,p):
        c=self.c;need(bool(p)and self.a.IsValidSid(p),'EPOCH_SECURITY_UNAVAILABLE');n=self.a.GetLengthSid(p);need(8<=n<=68,'EPOCH_SECURITY_UNAVAILABLE');return c.string_at(p,n)
    def known(self,text):
        c=self.c;p=c.c_void_p()
        try:need(self.a.ConvertStringSidToSidW(text,c.byref(p)),'EPOCH_SECURITY_UNAVAILABLE');return self.sid(p)
        finally:
            if p:self.k.dll.LocalFree(p)
    def actor(self):
        c=self.c;t=c.c_void_p();n=c.c_uint32()
        need(self.a.OpenProcessToken(self.k.dll.GetCurrentProcess(),8,c.byref(t)),'EPOCH_SECURITY_UNAVAILABLE')
        try:
            elevated=c.c_uint32();size=c.c_uint32()
            need(self.a.GetTokenInformation(t,20,c.byref(elevated),4,c.byref(size))and not elevated.value,'STANDARD_TOKEN_REQUIRED')
            # UAC deny-only administrator membership is not an enabled admin token.
            admin=c.create_string_buffer(self.admin);member=c.c_int()
            need(self.a.CheckTokenMembership(None,c.cast(admin,c.c_void_p),c.byref(member))and not member.value,'STANDARD_TOKEN_REQUIRED')
            ok=self.a.GetTokenInformation(t,1,None,0,c.byref(n));need(not ok and c.get_last_error()==122 and 8<=n.value<=4096,'EPOCH_SECURITY_UNAVAILABLE')
            b=c.create_string_buffer(n.value);need(self.a.GetTokenInformation(t,1,b,n.value,c.byref(n)),'EPOCH_SECURITY_UNAVAILABLE');return self.sid(c.cast(b,c.POINTER(c.c_void_p))[0])
        finally:self.k.close(t)
    def check(self,h,expected):
        c=self.c;o=c.c_void_p();d=c.c_void_p();sd=c.c_void_p()
        try:
            need(self.a.GetSecurityInfo(h,1,5,c.byref(o),None,c.byref(d),None,c.byref(sd))==0 and d,'EPOCH_SECURITY_UNAVAILABLE')
            control=c.c_uint16();revision=c.c_uint32();need(self.a.GetSecurityDescriptorControl(sd,c.byref(control),c.byref(revision)),'EPOCH_SECURITY_UNAVAILABLE')
            length=self.a.GetSecurityDescriptorLength(sd);need(20<=length<=16384,'EPOCH_SECURITY_UNAVAILABLE')
            need(hashlib.sha256(c.string_at(sd,length)).hexdigest()==expected,'EPOCH_ACL_CHANGED')
            class Size(c.Structure):_fields_=[('count',c.c_uint32),('used',c.c_uint32),('free',c.c_uint32)]
            size=Size();need(self.a.GetAclInformation(d,c.byref(size),c.sizeof(size),2)and size.count<=16,'EPOCH_ACL_REJECTED')
            aces=[]
            for i in range(size.count):
                p=c.c_void_p();need(self.a.GetAce(d,i,c.byref(p)),'EPOCH_SECURITY_UNAVAILABLE');at=p.value;kind=c.c_ubyte.from_address(at).value;flags=c.c_ubyte.from_address(at+1).value;length=c.c_uint16.from_address(at+2).value;need(16<=length<=256,'EPOCH_ACL_REJECTED');mask=c.c_uint32.from_address(at+4).value;s=self.sid(at+8);need(8+len(s)<=length,'EPOCH_ACL_REJECTED');s=b'SYSTEM'if s==self.system else b'ADMINISTRATORS'if s==self.admin else s;aces.append((kind,flags,mask,s))
            owner=self.sid(o);owner=b'SYSTEM'if owner==self.system else b'ADMINISTRATORS'if owner==self.admin else owner
            readonly_acl(owner,self.current,bool(control.value&0x1000),aces)
        finally:
            if sd:self.k.dll.LocalFree(sd)


class WindowsEpochLease:
    """Hold the full installation read-only until the worker is gone.

    FILE_SHARE_READ denies existing/new writer/delete handles. A separate reader
    lease on .install.lock also excludes reviewed installers taking a write
    lease. SYSTEM/Admin remain trusted installers; no defense against malicious
    elevated administrators is asserted. No POSIX path-hash fallback exists.
    """
    def __init__(self,key,*,parent_approved=None):
        self.key=key;self.parent_approved=parent_approved;self.stack=ExitStack();self.opened={};self.seen_ids=set();self.signatures={}
    def __enter__(self):
        try:
            # Only the fixed worker receives parent_approved through its private pipe.
            # Host entry always resolves the source-reviewed registry. This avoids
            # a manifest->worker registry->manifest circular hash dependency.
            self.approved=configured(self.key) if self.parent_approved is None else self.parent_approved
            need(type(self.approved)is dict and os.name=='nt','WINDOWS_EPOCH_REQUIRED')
            from .pet_media_files import _Win32,_windows_parts
            self.k=_Win32();self.security=_Security(self.k);self.root=PureWindowsPath(self.approved['root']);parts=_windows_parts(str(self.root))
            need(self.k.drive_type(parts[0])==3,'FIXED_LOCAL_EPOCH_REQUIRED')
            need(self.security.current==self.security.known(self.approved['actor_sid']),'EPOCH_ACTOR_CHANGED')
            for i in range(len(parts)):
                path=str(PureWindowsPath(*parts[:i+1]));self._open(path,True,None)
            root_info=self.k.info(self.opened[str(self.root)])
            need(list(root_info['identity'])==self.approved['root_identity'],'EPOCH_ROOT_CHANGED')
            self.security.check(self.opened[str(self.root)],self.approved['root_security_sha256'])
            # Authority files are pinned by reviewed source, not by their labels.
            authority={}
            for name in sorted(AUTHORITIES):
                h=self._open(str(self.root/name),False,None);b=self._read(h,1048576)
                need(name=='.install.lock' or bool(b),'EPOCH_AUTHORITY_CHANGED')
                need(hashlib.sha256(b).hexdigest()==self.approved['authority_sha256'][name],'EPOCH_AUTHORITY_CHANGED');authority[name]=b
                self.security.check(h,self.approved['authority_security_sha256'][name])
            self.authority=authority
            self.doc=json.loads(authority['deployment.json'])
            need(set(self.doc)=={'schema','epoch','commit','files','directories','roles'}and self.doc['schema']==2 and self.doc['commit']==COMMIT and self.doc['epoch']==self.approved['epoch'],'EXACT_EPOCH_REQUIRED')
            files=self.doc['files'];dirs=self.doc['directories'];need(type(files)is list and 0<len(files)<=32768 and type(dirs)is list and len(dirs)<=4096,'EPOCH_TREE_BOUND')
            expected_files=set(AUTHORITIES);expected_dirs={''};groups={g:[]for g in GROUPS};total=0
            for row in dirs:
                need(set(row)=={'path','security_sha256'}and row['path']not in expected_dirs,'EXACT_EPOCH_ROW_REQUIRED');safe_relative(row['path']);expected_dirs.add(row['path']);h=self._open(str(self.root.joinpath(*safe_relative(row['path']))),True,None);self.security.check(h,row['security_sha256'])
            for row in files:
                need(set(row)=={'path','bytes','sha256','group','security_sha256'}and row['group']in GROUPS and row['path']not in expected_files and type(row['bytes'])is int and 0<=row['bytes']<=4294967296,'EXACT_EPOCH_ROW_REQUIRED')
                safe_relative(row['path']);expected_files.add(row['path']);total+=row['bytes'];need(total<=12884901888,'EPOCH_TREE_BOUND')
                h=self._open(str(self.root.joinpath(*safe_relative(row['path']))),False,row['bytes']);self.security.check(h,row['security_sha256']);need(self._hash(h)==row['sha256'],'EPOCH_ASSET_CHANGED');groups[row['group']].append(row)
            need(all(groups.values()),'SIX_COMPLETE_ASSET_GROUPS_REQUIRED')
            self._tree_exact(expected_files,expected_dirs)
            self.group_hashes={g:hashlib.sha256(canonical(sorted(rows,key=lambda r:r['path']))).hexdigest()for g,rows in groups.items()}
            need(self.group_hashes==self.approved['asset_hashes'],'EPOCH_ASSET_CHANGED')
            self.roles=self.doc['roles'];fixed={'worker_script':'code/home_study_agent/pet_multipa_worker.py','python_executable':'python/python.exe','code_directory':'code','python_stdlib':'python/Lib','python_dlls':'python/DLLs','python_packages':'python/Lib/site-packages'}
            need(type(self.roles)is dict and set(self.roles)==set(fixed)|MODEL_ROLES and all(self.roles.get(k)==v for k,v in fixed.items()),'FIXED_WORKER_REQUIRED')
            for value in self.roles.values():need(value in expected_files or value in expected_dirs,'UNMAPPED_ROLE_REJECTED')
            order=json.loads(authority['training-phonemes.json'])['phonemes'];need(type(order)is list and len(order)==71 and len(set(order))==71 and order[-1]=='' and all(type(v)is str and len(v)<=8 for v in order),'TRAINING_MAPPING_REQUIRED')
            self.order=order;self.check();return self
        except BaseException:
            self.stack.close();raise
    def _open(self,path,directory,size):
        if path in self.opened:return self.opened[path]
        c=self.k.ct;access=0x20080 if directory else 0x80020000
        h=self.k.dll.CreateFileW('\\\\?\\'+path,access,1,None,3,0x00200000|(0x02000000 if directory else 0),None)
        need(h not in(None,0,c.c_void_p(-1).value),'EPOCH_OPEN_REJECTED');self.stack.callback(self.k.close,h);info=self.k.info(h)
        need(info['file_type']==1 and bool(info['attributes']&0x10)==directory and not info['attributes']&(0x400|0x1000),'EPOCH_ALIAS_REJECTED')
        need(self.k.final_path(h).removeprefix('\\\\?\\').rstrip('\\').casefold()==path.rstrip('\\').casefold(),'EPOCH_ALIAS_REJECTED')
        if not directory:need(info['links']==1 and (size is None or info['size']==size),'EPOCH_FILE_CHANGED')
        need(info['identity']not in self.seen_ids,'EPOCH_ALIAS_REJECTED');self.seen_ids.add(info['identity']);self.opened[path]=h;self.signatures[path]=info;return h
    def _read(self,h,limit):
        before=self.k.info(h);need(before['size']<=limit,'EPOCH_METADATA_BOUND');b=self.k.read(h,limit);need(len(b)==before['size']and self.k.info(h)==before,'EPOCH_FILE_CHANGED');return b
    def _hash(self,h):
        c=self.k.ct;before=self.k.info(h);digest=hashlib.sha256();read=0
        while True:
            b=c.create_string_buffer(1048576);n=c.c_uint32();need(self.k.dll.ReadFile(h,b,len(b),c.byref(n),None),'EPOCH_READ_REJECTED')
            if not n.value:break
            read+=n.value;need(read<=before['size'],'EPOCH_FILE_CHANGED');digest.update(b.raw[:n.value])
        need(read==before['size']and self.k.info(h)==before,'EPOCH_FILE_CHANGED');return digest.hexdigest()
    def _tree_exact(self,files,dirs):
        actual_files=set();actual_dirs={''};stack=[('',Path(str(self.root)))];visited=0
        while stack:
            rel,path=stack.pop()
            with os.scandir(path)as entries:
                for e in entries:
                    visited+=1;need(visited<=36864 and not e.is_symlink(),'EPOCH_ALIAS_REJECTED');name=(rel+'/'if rel else'')+e.name;safe_relative(name)
                    if e.is_dir(follow_symlinks=False):need(name in dirs,'UNMAPPED_RESOURCE_REJECTED');actual_dirs.add(name);stack.append((name,Path(e.path)))
                    else:need(e.is_file(follow_symlinks=False)and name in files,'UNMAPPED_RESOURCE_REJECTED');actual_files.add(name)
        need(actual_files==files and actual_dirs==dirs,'EPOCH_TREE_CHANGED')
    def path(self,role):return Path(str(self.root.joinpath(*safe_relative(self.roles[role]))))
    def check(self):
        for path,h in self.opened.items():
            info=self.k.info(h);need(info==self.signatures[path],'EPOCH_FILE_CHANGED');need(not info['attributes']&(0x400|0x1000)and self.k.final_path(h).removeprefix('\\\\?\\').rstrip('\\').casefold()==path.rstrip('\\').casefold(),'EPOCH_ALIAS_REJECTED')
    def __exit__(self,*args):
        try:self.check()
        finally:self.stack.close()


class _AttestedWindowsEpochLease(WindowsEpochLease):
    """Same original lease; observe each SAME CloseHandle BOOL exactly once.

    The original/default class is unchanged. Unknown closure retains this
    ledger and its original owner; no retry of a possibly recycled handle.
    """
    def __init__(self,key):
        self._completion_kernel=None
        self._completion_handles={}
        self._completion_unknown=False
        super().__init__(key)

    @property
    def k(self):return self._completion_kernel

    @k.setter
    def k(self,kernel):
        need(self._completion_kernel is None,'WORKER_CLEANUP_UNCONFIRMED')
        self._completion_kernel=kernel
        self._completion_native_close=kernel.dll.CloseHandle
        # Installed before the original _Security construction, including its
        # temporary process-token close, and before original stack callbacks.
        kernel.close=self._completion_closer()[1]

    def _completion_closer(self):
        owner=object();used=False
        self._completion_handles[owner]=dict(handle=None,status='RESERVED')
        def close(handle):
            nonlocal used
            need(not used,'WORKER_CLEANUP_UNCONFIRMED');used=True
            row=dict(handle=handle,status='UNKNOWN');self._completion_handles[owner]=row
            try:closed=self._completion_native_close(handle)
            except BaseException:
                self._completion_unknown=True
                raise ValueError('PET_MULTIPA_WORKER_CLEANUP_UNCONFIRMED')from None
            if not closed:
                self._completion_unknown=True
                raise ValueError('PET_MULTIPA_WORKER_CLEANUP_UNCONFIRMED')from None
            row['status']='CLOSED'
        return owner,close

    def _open(self,path,directory,size):
        if path in self.opened:return super()._open(path,directory,size)
        owner,close=self._completion_closer();previous=self.k.close;self.k.close=close
        try:
            handle=super()._open(path,directory,size)
            self._completion_handles[owner].update(handle=handle,status='OPEN')
            return handle
        except BaseException:
            # If CreateFileW did not return, no callback was registered. If a
            # later check failed, the captured callback adds its owned row as
            # the original __enter__ unwinds. No numeric handle is retried.
            if self._completion_handles[owner]['status']=='RESERVED':
                del self._completion_handles[owner]
            raise
        finally:self.k.close=previous

    def _completion_held_evidence(self):
        need(self._completion_kernel is not None and not self._completion_unknown
             and self.parent_approved is None,'COMPLETION_EPOCH_REQUIRED')
        hashes={name:hashlib.sha256(self.authority[name]).hexdigest()for name in AUTHORITIES}
        need(hashes==self.approved['authority_sha256']and self.doc['epoch']==self.approved['epoch'],
             'COMPLETION_EPOCH_REQUIRED')
        canonical=lambda v:json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        return dict(installation_epoch_sha256=hashlib.sha256(self.doc['epoch'].encode()).hexdigest(),
            authority_set_sha256=hashlib.sha256(canonical(hashes)).hexdigest(),
            deployment_manifest_sha256=hashes['deployment.json'])

    def _completion_release_confirmed(self):
        return bool(self._completion_handles)and not self._completion_unknown and all(
            v['status']=='CLOSED'for v in self._completion_handles.values())
