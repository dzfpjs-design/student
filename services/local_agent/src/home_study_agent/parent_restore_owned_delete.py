"""Owned Windows cleanup uses disposition on the original verified HANDLE.

No path-based DeleteFile/os.unlink fallback. POSIX is confined to original held
nofollow private directory FDs and the original publication writer boundary.
"""
from .registered_asset_restore import _TargetWin32,_win_target_check
from . import restore_publication as pub

class OwnedDeleteWin32(_TargetWin32):
    def open_delete(self,path):
        # DELETE|FILE_READ_ATTRIBUTES, share READ|WRITE but never DELETE,
        # OPEN_EXISTING, BACKUP_SEMANTICS|OPEN_REPARSE_POINT for both object types.
        handle=self.dll.CreateFileW('\\\\?\\'+str(path),0x10080,3,None,3,0x02200000,None)
        if handle in{None,0,self.ct.c_void_p(-1).value}:raise self.ct.WinError(self.ct.get_last_error())
        return handle
    def disposition(self,handle):
        ct=self.ct
        class Disposition(ct.Structure):_fields_=[('DeleteFile',ct.c_ubyte)]
        value=Disposition(1);fn=self.dll.SetFileInformationByHandle
        fn.argtypes=[ct.c_void_p,ct.c_int,ct.c_void_p,ct.c_uint32];fn.restype=ct.c_int
        pub._need(fn(handle,4,ct.byref(value),ct.sizeof(value)),'RESTORE_PUBLICATION_DELETE_UNCONFIRMED')

def erase_windows(owner,path,*,directory,identity=None,kernel=None,parent=None):
    parent=owner.parent if parent is None else parent
    parent.check()
    kernel=kernel if kernel is not None else OwnedDeleteWin32();handle=kernel.open_delete(path)
    try:
        parent.check();owner.parent.check()
        info=_win_target_check(kernel,handle,path,directory=directory,identity=tuple(identity)if identity is not None else None)
        pub._need(directory or info['links']==1,'RESTORE_PUBLICATION_OWNERSHIP_UNCONFIRMED')
        # The HANDLE denies delete sharing through ID verification and disposition.
        # A competing preexisting DELETE handle makes open fail, not a fallback.
        parent.check();owner.parent.check();kernel.disposition(handle)
    finally:
        try:kernel.close(handle)
        except BaseException:
            owner.gate.poisoned=True;owner.gate.retained.append(('owned_delete_handle',kernel,handle))
            raise pub.RestorePublicationError('RESTORE_PUBLICATION_CLOSE_UNCONFIRMED')from None
