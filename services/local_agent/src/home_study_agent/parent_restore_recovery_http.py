"""Restricted parent recovery surface; no models, normal services or schema SQL."""
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import ExitStack,asynccontextmanager
import asyncio
from .parent_auth import ParentAuth
from .privacy_operation_receipt import PrivacyOperationReceipts
from .parent_restore_security import install_local_session,install_parent_auth_handlers,verify_native_privacy_proof
from .parent_restore_workflow import recover_original
from . import restore_publication as pub
from . import native_privacy as native
from .build_info import BUILD_COMPONENT, BUILD_VERSION

def create_recovery_app(database,settings,pin_model,*,original_auth=None,clear=None):
    auth=original_auth if original_auth is not None else ParentAuth(database)
    @asynccontextmanager
    async def lifespan(app):
        from .parent_restore_parent import bind_recovery_auth,retire_recovery_auth
        if original_auth is None:bind_recovery_auth(database,auth,native_key_present=bool(settings.native_launch_attestation_key))
        try:yield
        finally:
            if original_auth is None:retire_recovery_auth(database,auth)
    app=FastAPI(title='Home Study Local Agent',version=BUILD_VERSION,lifespan=lifespan)
    app.state.settings=settings;app.state.recovery_only=True;app.state.request_shutdown=None
    pub._need(type(auth)is ParentAuth,'RESTORE_PUBLICATION_PARENT_AUTH_REQUIRED')
    receipts=PrivacyOperationReceipts(database)
    with pub.publication_owner(database,timeout=0)as initial_owner:
        original_request=pub.reconcile_publication(initial_owner)['request_id']
    @asynccontextmanager
    async def owner_scope(parent_token=None):
        from .parent_restore_parent import PARENT_RESTORE_ADMISSION_SECONDS,PARENT_RESTORE_ADMISSION_MAX_ATTEMPTS
        loop=asyncio.get_running_loop();until=loop.time()+PARENT_RESTORE_ADMISSION_SECONDS
        for attempt in range(PARENT_RESTORE_ADMISSION_MAX_ATTEMPTS):
            if parent_token is not None:auth.require(parent_token)
            stack=ExitStack()
            try:owner=stack.enter_context(pub.publication_owner(database,timeout=0))
            except pub.RestorePublicationError as error:
                stack.close()
                remaining=until-loop.time()
                if(error.args!=('RESTORE_PUBLICATION_BUSY',)or remaining<=0
                    or attempt+1==PARENT_RESTORE_ADMISSION_MAX_ATTEMPTS):raise
                await asyncio.sleep(min(0.01,remaining))
            else:
                # Entry alone retries. Reconciliation, authority, body and the
                # original exit are outside that catch and execute at most once.
                with stack:
                    pub.reconcile_publication(owner)
                    from .parent_restore_assets import admit_original_recovery
                    admit_original_recovery(owner,original_request)
                    if parent_token is not None:auth.require(parent_token)
                    yield owner
                return
    app.add_middleware(CORSMiddleware,allow_origins=[],allow_origin_regex=r'^http://(?:localhost|127\.0\.0\.1):\d+$',allow_credentials=False,allow_methods=['GET','POST'],allow_headers=['Content-Type','X-HSC-Session','X-HSC-Parent','X-HSC-Trace'])
    @app.middleware('http')
    async def restricted(request,call_next):
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        return response
    install_local_session(app,settings);install_parent_auth_handlers(app)
    @app.exception_handler(pub.RestorePublicationError)
    async def blocked(request,error):
        if error.args==('RESTORE_PUBLICATION_BUSY',):
            return JSONResponse({'code':'RESTORE_PUBLICATION_BUSY','message':'数据正在使用，请稍后重试原操作。','retryable':True,'recovery_action':'retry_original_request'},status_code=409,headers={'Cache-Control':'no-store','Retry-After':'1'})
        return JSONResponse({'code':'RESTORE_PUBLICATION_RECOVERY_REQUIRED','message':'原恢复记录尚未核对完成。请核对原操作，不要重新恢复。'},status_code=409,headers={'Cache-Control':'no-store'})
    @app.get('/v1/health')
    async def health():
        return {'status':'recovery_only','version':app.version,'build_version':BUILD_VERSION,
                'build_component':BUILD_COMPONENT,'normal_business_ready':False}
    @app.post('/v1/runtime/shutdown',status_code=202)
    async def shutdown():
        callback=app.state.request_shutdown
        if not callable(callback):
            return JSONResponse({'code':'RUNTIME_SHUTDOWN_NOT_READY'},status_code=503)
        callback()
        return {'status':'shutting_down'}
    @app.get('/v1/parent/status')
    async def status():
        async with owner_scope():return auth.status()
    @app.post('/v1/parent/unlock')
    async def unlock(payload:pin_model):
        async with owner_scope():return {'parent_token':auth.unlock(payload.pin),'expires_in_seconds':1800}
    @app.get('/v1/parent/privacy-operations/generation')
    async def generation(request:Request):
        auth.require(request.headers.get('X-HSC-Parent'))
        async with owner_scope(request.headers.get('X-HSC-Parent')):return receipts.current()
    @app.get('/v1/parent/privacy-operations/{request_id}')
    async def read(request_id:str,request:Request):
        auth.require(request.headers.get('X-HSC-Parent'))
        async with owner_scope(request.headers.get('X-HSC-Parent')):
            try:return receipts.read(request_id)
            except KeyError:return JSONResponse({'code':'PRIVACY_OPERATION_NOT_FOUND'},status_code=404)
    @app.get('/v1/parent/recovery-status')
    async def recovery_status(request:Request):
        auth.require(request.headers.get('X-HSC-Parent'))
        async with owner_scope(request.headers.get('X-HSC-Parent'))as owner:
            result=pub.reconcile_publication(owner)
            receipt=receipts.read(original_request)
            state=result['state']
            if state=='NO_ACTIVE_JOURNAL':state='RESOLVED_COMMITTED'if receipt['status']=='committed'else'RESOLVED_NOT_APPLIED'if receipt['status']=='failed'else'UNKNOWN'
            return {'schema':1,'state':state,'request_id':original_request,'kind':receipt['kind'],'normal_business_ready':False,'restart_required':state in{'RESOLVED_COMMITTED','RESOLVED_NOT_APPLIED'}}
    def recovery_permit(owner,metadata):
        # This extra fact belongs only to the restricted restore surface. The
        # original native status/nonce is never rewritten to request cleanup.
        pub._need(metadata.get('kind')in{'restore','portable_restore'}and metadata.get('request_id')==original_request
            and receipts.read(original_request)['kind']==metadata['kind'],'RESTORE_PUBLICATION_NATIVE_PERMIT_REQUIRED')
        raw=pub._read(owner.parent,pub._names(owner.path)[1])
        if raw is not None:
            pub._need(pub._decode(raw)['binding']['kind']==metadata['kind'],'RESTORE_PUBLICATION_NATIVE_PERMIT_REQUIRED')
        state=pub.reconcile_publication(owner)
        pending=state['state']!='NO_ACTIVE_JOURNAL'
        if pending:pub.original_publication_metadata(owner,metadata)
        with database.connect()as c:result=native.read(c,metadata)
        return {**result,'restore_recovery_pending':pending}
    @app.post('/v1/parent/native-privacy-permits/read')
    async def permit_read(payload:dict,request:Request):
        auth.require(request.headers.get('X-HSC-Parent'));metadata=verify_native_privacy_proof(payload,settings.native_launch_attestation_key)
        async with owner_scope(request.headers.get('X-HSC-Parent'))as owner:return recovery_permit(owner,metadata)
    @app.post('/v1/parent/native-privacy-permits/fence')
    async def permit_fence(payload:dict,request:Request):
        auth.require(request.headers.get('X-HSC-Parent'));metadata=verify_native_privacy_proof(payload,settings.native_launch_attestation_key)
        async with owner_scope(request.headers.get('X-HSC-Parent'))as owner:
            before=recovery_permit(owner,metadata)
            if before['restore_recovery_pending']:recover_original(database,metadata,clear)
            result=recovery_permit(owner,metadata)
            pub._need(result['restore_recovery_pending']is False and result['status']in{'failed','finished'},'RESTORE_PUBLICATION_ORIGINAL_TAILS_PENDING')
        app.state.recovery_resolved=True
        return result
    @app.post('/v1/parent/privacy-operations/{request_id}/finalize')
    async def parent_finalize(request_id:str,request:Request):
        auth.require(request.headers.get('X-HSC-Parent'))
        pub._need(request_id==original_request,'RESTORE_PARENT_AUTHORITY_UNCONFIRMED')
        from .parent_restore_parent import finalize_original
        async with owner_scope(request.headers.get('X-HSC-Parent'))as owner:
            result=finalize_original(database,auth,request.headers.get('X-HSC-Parent'),owner,
                request_id,route=request.url.path,clear=clear)
        app.state.recovery_resolved=True
        return JSONResponse(result,headers={'Cache-Control':'no-store'})
    @app.api_route('/{path:path}',methods=['GET','POST','PUT','DELETE','PATCH','OPTIONS','HEAD'])
    async def unavailable(path:str):
        return JSONResponse({'code':'RESTORE_PUBLICATION_RECOVERY_REQUIRED','message':'请先核对原恢复。完成后重新启动本地学习服务。','retryable':False,'recovery_action':'review_original_operation'},status_code=503)
    return app


class RestoreRecoveryDispatch:
    """Sticky restricted HTTP after in-process failure; original lifespan stays.

    This is inside the outer money-owner lifecycle/endpoint policy. A competing
    original writer is rejected by fresh owner admission, never reentered.
    """
    def __init__(self,app,*,database,settings,pin_model,original_auth,clear,normal_state):
        self.app=app;self.database=database;self.settings=settings;self.pin_model=pin_model;self.auth=original_auth;self.clear=clear;self.normal_state=normal_state;self.restricted=None
    async def __call__(self,scope,receive,send):
        if scope['type']!='http'or scope.get('path')=='/v1/private/money-egress'or(scope.get('path')=='/v1/runtime/shutdown'and scope.get('method')=='POST'):
            # Exact outer money endpoint and authenticated graceful shutdown only.
            # The original session middleware/router still rejects foreign callers.
            # Original money session+HMAC/router
            # remains authoritative; no wildcard private-prefix exemption.
            return await self.app(scope,receive,send)
        from .parent_restore_workflow import has_active_publication
        if self.restricted is None and has_active_publication(self.database):
            try:
                self.restricted=create_recovery_app(self.database,self.settings,self.pin_model,original_auth=self.auth,clear=self.clear)
            except pub.RestorePublicationError:
                return await JSONResponse({'code':'RESTORE_PUBLICATION_RECOVERY_REQUIRED','message':'原操作仍在执行或原恢复事实尚未核对。学习功能保持暂停。'},status_code=503,headers={'Cache-Control':'no-store'})(scope,receive,send)
        if self.restricted is not None:
            self.restricted.state.request_shutdown=self.normal_state.request_shutdown
            return await self.restricted(scope,receive,send)
        return await self.app(scope,receive,send)
