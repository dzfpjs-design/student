"""Keep the exported ASGI application inert until explicit runtime startup."""
from threading import RLock


class LazyApplication:
    def __init__(self, factory):
        self._factory = factory
        self._application = None
        self._initialization_error = None
        self._lock = RLock()

    def _resolve(self):
        with self._lock:
            if self._initialization_error is not None:
                raise RuntimeError('local application initialization failed; restart required') from self._initialization_error
            if self._application is None:
                try:
                    self._application = self._factory()
                except Exception as error:
                    # A failed constructor may already have touched storage.
                    # Retrying initialization requires a fresh service process.
                    self._initialization_error = error
                    raise
            return self._application

    async def __call__(self, scope, receive, send):
        await self._resolve()(scope, receive, send)

    def __getattr__(self, name):
        # Inspection by Uvicorn or tooling is not permission to open a store.
        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self._resolve(), name)
