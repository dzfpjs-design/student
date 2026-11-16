"""One original session/PIN/native verification implementation for both modes."""
from fastapi import Request,HTTPException
from fastapi.responses import JSONResponse
from .parent_auth import ParentSessionRequiredError,InvalidParentPinFormatError,InvalidParentPinError,ParentLockedError,ParentAlreadyConfiguredError,ParentNotConfiguredError

def install_local_session(application,resolved_settings):
    @application.middleware("http")
    async def require_local_session(request: Request, call_next):
        if (
            resolved_settings.session_token
            and request.method != "OPTIONS"
            and request.url.path.startswith("/v1/")
            and request.headers.get("X-HSC-Session") != resolved_settings.session_token
        ):
            return JSONResponse(
                status_code=401,
                content={"code": "LOCAL_SESSION_REQUIRED", "message": "本地应用会话无效。"},
                headers={"Cache-Control": "no-store"} if request.url.path.endswith(
                    ("/diagnostic-visual", "/diagnostic-visual/image", "/tutor-visual", "/tutor-visual/image")) else None,
            )
        return await call_next(request)


def install_parent_auth_handlers(application):
    @application.exception_handler(ParentSessionRequiredError)
    async def parent_session_required(_: Request, __: ParentSessionRequiredError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"code": "PARENT_SESSION_REQUIRED", "message": "请先用家长 PIN 解锁。"},
        )

    @application.exception_handler(InvalidParentPinFormatError)
    async def invalid_parent_pin_format(_: Request, __: InvalidParentPinFormatError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": "INVALID_PARENT_PIN_FORMAT", "message": "PIN 必须是4到8位数字。"},
        )

    @application.exception_handler(InvalidParentPinError)
    async def invalid_parent_pin(_: Request, error: InvalidParentPinError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "code": "INVALID_PARENT_PIN",
                "message": f"PIN 不正确，还可尝试{error.attempts_remaining}次。",
            },
        )

    @application.exception_handler(ParentLockedError)
    async def parent_locked(_: Request, error: ParentLockedError) -> JSONResponse:
        minutes = max(1, (error.locked_seconds + 59) // 60)
        return JSONResponse(
            status_code=429,
            content={
                "code": "PARENT_LOCKED",
                "message": f"尝试次数过多，请约{minutes}分钟后再试。",
                "details": {"locked_seconds": error.locked_seconds},
            },
        )

    @application.exception_handler(ParentAlreadyConfiguredError)
    async def parent_already_configured(_: Request, __: ParentAlreadyConfiguredError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"code": "PARENT_ALREADY_CONFIGURED", "message": "家长 PIN 已经设置。"},
        )

    @application.exception_handler(ParentNotConfiguredError)
    async def parent_not_configured(_: Request, __: ParentNotConfiguredError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"code": "PARENT_NOT_CONFIGURED", "message": "请先设置家长 PIN。"},
        )


    from .restore_publication import RestorePublicationError
    @application.exception_handler(RestorePublicationError)
    async def publication_required(request,error):
        if error.args==('RESTORE_PUBLICATION_BUSY',):
            return JSONResponse({'code':'RESTORE_PUBLICATION_BUSY','message':'数据正在使用，请稍后重试原操作。',
                'retryable':True,'recovery_action':'retry_original_request'},status_code=409,
                headers={'Cache-Control':'no-store','Retry-After':'1'})
        return JSONResponse({'code':'RESTORE_PUBLICATION_RECOVERY_REQUIRED','message':'原恢复尚未核对完成，请核对原操作，不要重复恢复。'},status_code=409,headers={'Cache-Control':'no-store'})


def verify_native_privacy_proof(value, key):
    from .native_privacy import verify, NativePrivacyConflict
    try:
        return verify(value, key)
    except NativePrivacyConflict as error:
        raise HTTPException(status_code=409,detail={'code':str(error),
            'message':'本机活动关闭或恢复记录尚未核对，没有执行删除或恢复。'},headers={'Cache-Control':'no-store'})

