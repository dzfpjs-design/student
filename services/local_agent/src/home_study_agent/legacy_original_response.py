"""Hold the privacy admission fence until the local original is sent or cancelled."""
import asyncio
from threading import Event

from fastapi.responses import JSONResponse, Response

from .database import ExternalEffectBusyError
from .parent_auth import ParentSessionRequiredError
from .parent_reports import LegacyOriginalUnavailableError
from .repository import UnknownReportError

LEGACY_SEND_TIMEOUT_SECONDS = 5
PARENT_SEND_MONITOR_INTERVAL_SECONDS = 0.02


class LegacyOriginalResponse(Response):
    def __init__(self, database, reports, auth, token, child_id, report_id, *, status_only=False):
        super().__init__(content=b'')
        self.database, self.reports, self.auth, self.token = database, reports, auth, token
        self.child_id, self.report_id, self.status_only = child_id, report_id, status_only

    async def __call__(self, scope, receive, send):
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        release = Event()
        headers = {'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'}

        def publish(response):
            def complete():
                if not ready.done():
                    ready.set_result(response)
            loop.call_soon_threadsafe(complete)

        def worker():
            try:
                # Lock is owned by this worker, not the event-loop thread: the
                # database lock is thread-reentrant and may not span awaits on
                # the event-loop thread. The release event also covers aborts.
                with self.database.external_effect_guard(timeout=0):
                    before = self.reports.legacy_privacy_marker(self.child_id)
                    result = self.reports.read_legacy_daily_original(self.child_id,self.report_id)
                    if self.reports.legacy_privacy_marker(self.child_id) != before:
                        raise LegacyOriginalUnavailableError('LEGACY_PRIVACY_STATE_CHANGED')
                    self.auth.require(self.token)
                    result['expires_at'] = self.auth.expires_at(self.token).isoformat()
                    if self.status_only:
                        result = {key:result[key] for key in ('child_id','report_id','verification','source_digest','expires_at')}
                    publish(JSONResponse(result,headers=headers))
                    release.wait()
            except UnknownReportError:
                publish(JSONResponse({'detail':'找不到原报告。'},status_code=404,headers=headers))
            except ParentSessionRequiredError:
                publish(JSONResponse({'detail':'家长身份已过期，请重新解锁。'},status_code=401,headers=headers))
            except (LegacyOriginalUnavailableError,ExternalEffectBusyError):
                publish(JSONResponse({'detail':'原报告暂无法安全读取，请稍后核对。'},status_code=409,headers=headers))
            except Exception:
                # Never return partial original content on malformed privacy state.
                publish(JSONResponse({'detail':'原报告暂无法安全读取，请稍后核对。'},status_code=409,headers=headers))

        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            response = await ready
            try:
                self.auth.require(self.token)
            except ParentSessionRequiredError:
                response = JSONResponse({'detail':'家长身份已过期，请重新解锁。'},status_code=401,headers=headers)
            async def authorized_send(message):
                if response.status_code != 200:
                    await send(message)
                    return
                self.auth.require(self.token)
                async def monitor():
                    while True:
                        await asyncio.sleep(PARENT_SEND_MONITOR_INTERVAL_SECONDS)
                        self.auth.require(self.token)
                sending = asyncio.create_task(send(message))
                watching = asyncio.create_task(monitor())
                try:
                    done, _ = await asyncio.wait((sending,watching),return_when=asyncio.FIRST_COMPLETED)
                    if watching in done:
                        watching.result()
                    await sending
                    self.auth.require(self.token)
                finally:
                    sending.cancel()
                    watching.cancel()
                    await asyncio.gather(sending,watching,return_exceptions=True)
            # A client that stops reading must not own the privacy fence
            # indefinitely. Cancellation closes the response before release.
            await asyncio.wait_for(response(scope,receive,authorized_send),timeout=LEGACY_SEND_TIMEOUT_SECONDS)
        finally:
            release.set()
            # Cancellation must not leave an owned lock or sensitive result in
            # a detached worker. Its release event has already been signalled.
            from .local_async_work import _drain
            await _drain(task)
