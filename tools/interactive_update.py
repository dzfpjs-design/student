"""Native, non-console UI around the unchanged reviewed offline transaction."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as W
from pathlib import Path
import threading

import offline_update as U


class ProgressWindow:
    UPDATE = 0x8001
    FINISH = 0x8002
    CODES = ("CHECKING", "CLOSING", "BACKUP", "REPLACING", "RECOVERING")

    def __init__(self, platform):
        self.p = platform
        self.ready = threading.Event()
        self.error = False
        self.hwnd = None
        self.label = None
        self.thread = threading.Thread(target=self._loop, name="offline-update-ui", daemon=True)

    def start(self):
        self.thread.start()
        if not self.ready.wait(5) or self.error or not self.hwnd or not self.thread.is_alive():
            raise OSError("PROGRESS_WINDOW_UNAVAILABLE")

    def update(self, code):
        if not self.thread.is_alive() or self.error:
            raise OSError("PROGRESS_WINDOW_UNAVAILABLE")
        if code in self.CODES and not self.p.u.PostMessageW(self.hwnd, self.UPDATE, self.CODES.index(code), 0):
            raise OSError("PROGRESS_WINDOW_UNAVAILABLE")

    def close(self):
        if self.hwnd and self.thread.is_alive():
            if not self.p.u.PostMessageW(self.hwnd, self.FINISH, 0, 0):
                raise OSError("PROGRESS_WINDOW_CLOSE_UNCONFIRMED")
            self.thread.join(5)
            if self.thread.is_alive():
                raise OSError("PROGRESS_WINDOW_CLOSE_UNCONFIRMED")

    def _loop(self):
        u, k = self.p.u, self.p.k
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        class WindowClass(ctypes.Structure):
            _fields_ = [
                ("style", W.UINT), ("callback", callback_type),
                ("extra_class", ctypes.c_int), ("extra_window", ctypes.c_int),
                ("instance", W.HANDLE), ("icon", W.HANDLE), ("cursor", W.HANDLE),
                ("background", W.HANDLE), ("menu", W.LPCWSTR), ("name", W.LPCWSTR),
            ]
        k.GetModuleHandleW.argtypes = (W.LPCWSTR,)
        k.GetModuleHandleW.restype = W.HANDLE
        u.RegisterClassW.argtypes = (ctypes.POINTER(WindowClass),)
        u.RegisterClassW.restype = W.WORD
        u.UnregisterClassW.argtypes = (W.LPCWSTR, W.HANDLE)
        u.CreateWindowExW.argtypes = (W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      W.HWND, W.HANDLE, W.HANDLE, ctypes.c_void_p)
        u.CreateWindowExW.restype = W.HWND
        u.DefWindowProcW.argtypes = (W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.DestroyWindow.argtypes = (W.HWND,)
        u.SetWindowTextW.argtypes = (W.HWND, W.LPCWSTR)
        u.ShowWindow.argtypes = (W.HWND, ctypes.c_int)
        u.GetMessageW.argtypes = (ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = (ctypes.POINTER(W.MSG),)
        u.DispatchMessageW.argtypes = (ctypes.POINTER(W.MSG),)
        u.DispatchMessageW.restype = ctypes.c_ssize_t
        u.PostQuitMessage.argtypes = (ctypes.c_int,)
        instance = k.GetModuleHandleW(None)
        class_name = "HomeStudyOfflineUpdateProgressV1"
        registered = False

        @callback_type
        def callback(hwnd, message, wparam, lparam):
            if message == 0x0010:  # WM_CLOSE: do not terminate a running transaction.
                if self.label:
                    u.SetWindowTextW(self.label, "更新正在确保程序版本一致，请等待安全收尾。\n不要强制结束更新器；应用不会被强制关闭。")
                return 0
            if message == self.UPDATE:
                if self.label and 0 <= wparam < len(self.CODES):
                    u.SetWindowTextW(self.label, U.MESSAGES[self.CODES[wparam]])
                return 0
            if message == self.FINISH:
                u.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:  # WM_DESTROY, only our own UI window.
                u.PostQuitMessage(0)
                return 0
            return u.DefWindowProcW(hwnd, message, wparam, lparam)

        self.callback = callback  # Keep the native callback alive for the entire loop.
        try:
            wc = WindowClass(0, callback, 0, 0, instance, None, None, 6, None, class_name)
            if not u.RegisterClassW(ctypes.byref(wc)):
                raise OSError("PROGRESS_CLASS_UNAVAILABLE")
            registered = True
            self.hwnd = u.CreateWindowExW(
                1, class_name, "家庭学习伙伴 · 安全离线更新", 0x10C80000,
                -2147483648, -2147483648, 560, 180, None, None, instance, None)
            if not self.hwnd:
                raise OSError("PROGRESS_WINDOW_UNAVAILABLE")
            self.label = u.CreateWindowExW(0, "STATIC", "正在核对离线更新，请稍候。",
                                          0x50000000, 20, 25, 510, 85,
                                          self.hwnd, None, instance, None)
            if not self.label:
                raise OSError("PROGRESS_LABEL_UNAVAILABLE")
            # Override a hidden launcher's initial STARTUPINFO and verify that
            # a visible window exists before permitting any update transaction.
            u.ShowWindow(self.hwnd, 5)
            if not u.IsWindowVisible(self.hwnd):
                u.ShowWindow(self.hwnd, 5)
            if not u.IsWindowVisible(self.hwnd):
                raise OSError("PROGRESS_WINDOW_NOT_VISIBLE")
            self.ready.set()
            message = W.MSG()
            while True:
                result = u.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise OSError("PROGRESS_MESSAGE_LOOP_FAILED")
                u.TranslateMessage(ctypes.byref(message))
                u.DispatchMessageW(ctypes.byref(message))
        except BaseException:
            self.error = True
        finally:
            self.ready.set()
            if self.hwnd:
                u.DestroyWindow(self.hwnd)
            if registered:
                u.UnregisterClassW(class_name, instance)


def run_interactive(manifest, payload, platform_factory=None, progress_factory=None):
    platform = progress = engine = None
    status = code = None
    try:
        platform = (platform_factory or U.WindowsPlatform)()
        accepted = platform.u.MessageBoxW(
            None, "请先保存孩子和家长的工作。\n\n更新器会请求应用正常退出，再备份和更新程序。"
            "资料、媒体和草稿保留原位；未能正常退出会停止，不会强制结束进程。"
            "\n\n开始后请等待安全收尾。确定开始更新吗？",
            "家庭学习伙伴离线更新", 0x41)
        if accepted != 1:
            return 2  # Explicit cancellation, before profile/installation/state writes.
        progress = (progress_factory or ProgressWindow)(platform)
        progress.start()  # A working, nonblocking UI must exist before entering the engine.
        engine = U.UpdateEngine(platform.current_layout(), platform, progress=progress.update)
        status = engine.run(manifest.absolute(), payload.absolute())
    except Exception as error:
        code = error.code if isinstance(error, U.UpdateError) else "UPDATE_FAILED"
        if engine and engine.l.state.is_dir():
            try:
                engine.emit(code)
            except Exception:
                pass
    finally:
        if progress:
            try:
                progress.close()
            except Exception:
                code = code or ("FINALIZATION_PENDING" if status else "UPDATE_FAILED")
    if code:
        if platform:
            platform.failure_message(code)
        return 20 if code == "RECOVERY_HOLD" else 10
    platform.u.MessageBoxW(None, U.MESSAGES["ALREADY_INSTALLED" if status == "already_installed" else "INSTALLED"],
                          "家庭学习伙伴离线更新", 0x40)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--payload-dir", type=Path, required=True)
    parser.add_argument("--interactive", action="store_true", required=True)
    args = parser.parse_args()
    return run_interactive(args.manifest, args.payload_dir)


if __name__ == "__main__":
    raise SystemExit(main())
