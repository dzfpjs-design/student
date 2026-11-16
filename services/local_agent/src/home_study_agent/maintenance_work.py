"""Own background thread work through cancellation without interrupting commits."""
import asyncio
import threading


class MaintenanceStopped(RuntimeError):
    pass


class MaintenanceDrainUnconfirmed(RuntimeError):
    pass


class MaintenanceWork:
    def __init__(self):
        self._stop = threading.Event()
        self._active = set()

    def request_stop(self):
        self._stop.set()

    def is_stopping(self):
        return self._stop.is_set()

    def _invoke(self, work, args, kwargs):
        if self.is_stopping():
            raise MaintenanceStopped('MAINTENANCE_STOPPED_BEFORE_WORK')
        # Once begun, non-supply work runs all its original durable finalizers.
        return work(*args, **kwargs)

    async def run(self, work, *args, **kwargs):
        if self.is_stopping():
            raise MaintenanceStopped('MAINTENANCE_STOPPED_BEFORE_DISPATCH')
        original = asyncio.create_task(asyncio.to_thread(self._invoke, work, args, kwargs))
        self._active.add(original)
        try:
            try:
                return await asyncio.shield(original)
            except asyncio.CancelledError:
                self.request_stop()
                while not original.done():
                    try:
                        await asyncio.shield(original)
                    except asyncio.CancelledError:
                        if original.cancelled():
                            break
                    except MaintenanceStopped:
                        break
                if original.cancelled():
                    # Keep the original task; it cannot prove its thread closed.
                    raise MaintenanceDrainUnconfirmed('MAINTENANCE_DRAIN_UNCONFIRMED')
                try:
                    original.result()
                except MaintenanceStopped:
                    pass
                # All other job/close errors propagate, including poisoned SQL.
                raise
        finally:
            if original.done() and not original.cancelled():
                self._active.discard(original)
