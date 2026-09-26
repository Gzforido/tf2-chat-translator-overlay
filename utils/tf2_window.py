"""Определение состояния окна Team Fortress 2 через WinAPI."""

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Optional


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TF2_EXECUTABLES = {"tf_win64.exe", "hl2.exe"}
TF2_WINDOW_TITLE = "team fortress 2"


def tf2_overlay_should_be_visible() -> Optional[bool]:
    """Показывать оверлей только при активной TF2 или окне приложения."""
    if sys.platform != "win32":
        return None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [enum_callback_type, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    get_window_text_length = user32.GetWindowTextLengthW
    get_window_text_length.argtypes = [wintypes.HWND]
    get_window_text_length.restype = ctypes.c_int
    get_window_text = user32.GetWindowTextW
    get_window_text.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int
    ]
    get_window_text.restype = ctypes.c_int
    get_window_pid = user32.GetWindowThreadProcessId
    get_window_pid.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    get_window_pid.restype = wintypes.DWORD
    is_iconic = user32.IsIconic
    is_iconic.argtypes = [wintypes.HWND]
    is_iconic.restype = wintypes.BOOL
    get_foreground_window = user32.GetForegroundWindow
    get_foreground_window.argtypes = []
    get_foreground_window.restype = wintypes.HWND

    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    query_process_image = kernel32.QueryFullProcessImageNameW
    query_process_image.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query_process_image.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    tf2_window: Optional[int] = None
    tf2_process_id: Optional[int] = None

    @enum_callback_type
    def check_window(hwnd: int, _unused: int) -> bool:
        nonlocal tf2_window, tf2_process_id

        title_length = get_window_text_length(hwnd)
        if title_length <= 0:
            return True
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        get_window_text(hwnd, title_buffer, len(title_buffer))
        if TF2_WINDOW_TITLE not in title_buffer.value.casefold():
            return True

        process_id = wintypes.DWORD()
        get_window_pid(hwnd, ctypes.byref(process_id))
        if not process_id.value:
            return True

        process = open_process(
            PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value
        )
        if not process:
            return True
        try:
            image_buffer = ctypes.create_unicode_buffer(32_768)
            image_length = wintypes.DWORD(len(image_buffer))
            if not query_process_image(
                process, 0, image_buffer, ctypes.byref(image_length)
            ):
                return True
            executable = image_buffer.value.rsplit("\\", 1)[-1].casefold()
            if executable not in TF2_EXECUTABLES:
                return True
        finally:
            close_handle(process)

        tf2_window = int(hwnd)
        tf2_process_id = process_id.value
        return False

    enum_windows(check_window, 0)
    if tf2_window is None or tf2_process_id is None:
        return False
    if is_iconic(tf2_window):
        return False

    foreground_window = get_foreground_window()
    if not foreground_window:
        return False
    foreground_process_id = wintypes.DWORD()
    get_window_pid(foreground_window, ctypes.byref(foreground_process_id))
    return foreground_process_id.value in {tf2_process_id, os.getpid()}
