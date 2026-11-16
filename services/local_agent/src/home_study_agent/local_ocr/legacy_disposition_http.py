"""Parent-authorized, explicit disposition; never part of automatic maintenance."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .legacy_disposition import LegacyDispositionConflict, LegacyDispositionInput, LegacyDispositionIncomplete


class DispositionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_id: str = Field(min_length=1, max_length=200)
    expected_fingerprint: str = Field(min_length=64, max_length=64)
    client_action_id: str = Field(min_length=8, max_length=200)
    confirmation: str = Field(min_length=1, max_length=100)


def create_legacy_disposition_router(service, parent_auth):
    router = APIRouter(prefix='/v1/parent/privacy/legacy-ocr')

    def authorize(request, response):
        parent_auth.require(request.headers.get('X-HSC-Parent'))
        response.headers['Cache-Control'] = 'no-store'

    def execute(call):
        try:
            return call()
        except KeyError:
            raise HTTPException(404, detail={'code': 'LEGACY_DISPOSITION_NOT_FOUND',
                'message': '没有找到这次旧缓存清理的回执，请先核对是否恢复过数据库。'}) from None
        except LegacyDispositionInput:
            raise HTTPException(422, detail={'code': 'LEGACY_DISPOSITION_INPUT_INVALID',
                'message': '请先核对旧缓存数量，并输入完整确认文字。'}) from None
        except LegacyDispositionConflict:
            raise HTTPException(409, detail={'code': 'LEGACY_DISPOSITION_CONFLICT',
                'message': '旧缓存或确认范围已经变化，没有扩大清理范围。请核对原回执或重新预览。'}) from None
        except LegacyDispositionIncomplete:
            raise HTTPException(409, detail={'code': 'LEGACY_DISPOSITION_INCOMPLETE',
                'message': '本次旧缓存清理尚未完成。请使用原操作重试，不会自动清理新增数据。'}) from None

    @router.post('/preview')
    def preview(request: Request, response: Response):
        authorize(request, response)
        return execute(service.preview_unknown)

    @router.post('/dispose')
    def dispose(payload: DispositionRequest, request: Request, response: Response):
        authorize(request, response)
        return execute(lambda: service.dispose_unknown(**payload.model_dump(), reviewer_id='household-parent'))

    @router.get('/receipts/{receipt_id}')
    def receipt(receipt_id: str, request: Request, response: Response):
        authorize(request, response)
        return execute(lambda: service.get_receipt(receipt_id))

    return router
