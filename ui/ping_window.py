import ctypes
import ctypes.wintypes
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import re
import sys

from services.ping_service import create_ping_process
from services.dns_service import flush_dns
from services.vnc_service import open_vnc
from services.network_service import get_mac_address
from services.host_cache import (
    resolve_host_cached,
    resolve_all_list,
    upsert_host_cache_entry,
    normalize_host_key,
    dns_resolve as cache_dns_resolve,
    get_cached_value,
    LIST_FILE as CONFIG_LIST_FILE,
    HOST_CACHE,
    HOST_JSON as CONFIG_HOST_JSON,
)
from utils.validators import parse_hostname
from models.ping_stats import PingStats


BATCH_MAX_WORKERS = 50
BATCH_TIMEOUT = 500

VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_MENU = 0x12  # Alt (both left and right use this VK in LL hook)
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_SPACE = 0x20
HC_ACTION = 0
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105

# ===== message-only HWND + RegisterHotKey 常量 =====
WM_HOTKEY = 0x0312
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_SYSCOMMAND = 0x0112
MOD_WIN = 0x0008
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
HWND_MESSAGE = -3
GWL_WNDPROC = -4
GWL_STYLE = -16
WS_SYSMENU = 0x00080000
WS_EX_OVERLAPPEDWINDOW = 0x00000300

# RegisterClassEx WNDCLASSEX
CS_GLOBALCLASS = 0x4000
COLOR_WINDOW = 5

# 热键 ID：Alt + Space 唤出工具
HOTKEY_ID = 1001
# 同时保持别名，兼容旧引用
HOTKEY_ID_WINSPACE = HOTKEY_ID

# ctypes.wintypes 未导出的句柄类型，都是 HANDLE (void*)
HCURSOR = ctypes.c_void_p
HICON = ctypes.c_void_p
HBRUSH = ctypes.c_void_p
HINSTANCE = ctypes.wintypes.HINSTANCE
HWND = ctypes.wintypes.HWND

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("style", ctypes.wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", ctypes.wintypes.LPCWSTR),
        ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ("hIconSm", HICON),
    ]


LOWLEVELKEYBOARDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM
)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.wintypes.ULONG)),
    ]


def batch_ping_host(host, count=1):
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        cmd = [
            "ping",
            "-n", str(count),
            "-w", str(BATCH_TIMEOUT),
            host
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=startupinfo
        )
        output = result.stdout
        alive = "TTL=" in output.upper()
        return alive, output
    except Exception as e:
        return False, str(e)


class PingApp:

    def __init__(self, root):

        self.root = root
        self.root.title("自动 Ping 工具")

        window_width = 720
        window_height = 430

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        x = int((screen_width - window_width) / 2)
        y = int((screen_height - window_height) / 2)

        self.root.geometry(
            f"{window_width}x{window_height}+{x}+{y}"
        )

        self.process = None
        self.hostname = None
        self.host = None

        self.stats = PingStats()
        self.pings_received = False
        self.ping_failed = False

        # 缓存失败 → 切外部DNS的过渡段：不输出统计/不写"跳过MAC/VNC"
        self._suppress_next_statistics = False

        self.is_topmost = False

        self.batch_running = False
        self.batch_stop_event = threading.Event()
        self.last_log_file = None

        self._hook_hwnd = None
        self._hook_cb = None
        self._hook_thread = None
        # Alt+Space LL 钩子回退时用的状态
        self._lalt_down = False
        self._ralt_down = False
        self._space_swallowed_down = False
        self._pending_alt_key_to_swallow = 0  # 0 / VK_LMENU / VK_RMENU
        self._last_hotkey_time = 0

        # RegisterHotKey 专用字段
        self._hotkey_thread = None
        self._hotkey_hwnd = None
        self._hotkey_wndproc_ref = None  # 防止 GC 回收
        self._hotkey_registered = False
        self._hotkey_stop_event = threading.Event()
        self._hotkey_last_log = None  # 注册结果日志，用于 UI 提示

        # Tk 根窗口子类化（拦 Alt+Space 系统菜单）
        self._orig_tk_wndproc = None
        self._tk_wndproc_cb_ref = None

        self.create_widgets()

        self.root.attributes("-topmost", False)
        self.topmost_btn.config(text="置顶")

        self.root.bind(
            "<KeyPress>",
            self.any_key_stop
        )

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.entry.focus_set()

        # 子类化 Tk 根窗口，拦截 Alt+Space 弹出的系统菜单
        self.root.after(10, self._subclass_tk_root)

        self._install_hotkey_or_hook()

    # ============================================================
    # 全局快捷键: 优先 RegisterHotKey (独立 message-only HWND)，失败再回退 LL 钩子
    # ============================================================
    def _install_hotkey_or_hook(self):
        # 先在独立线程里尝试 RegisterHotKey
        self._hotkey_stop_event.clear()
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_thread_func,
            daemon=True
        )
        self._hotkey_thread.start()

        # 等最多 300ms 看注册结果
        self._hotkey_thread.join(timeout=0.3)
        if self._hotkey_registered:
            # RegisterHotKey 成功，后续靠该线程自己的 GetMessage 循环跑
            if self._hotkey_last_log:
                self.root.after(0, self.append, self._hotkey_last_log + "\n")
            return

        # RegisterHotKey 没注册成功（超时或失败），回退 LL 钩子
        if self._hotkey_last_log:
            self.root.after(0, self.append, self._hotkey_last_log + "\n")
        self.root.after(0, self.append,
                        "[热键] RegisterHotKey 失败，回退到低级键盘钩子方案\n")
        self._install_ll_hook()

    def _install_ll_hook(self):
        self._hook_thread = threading.Thread(
            target=self._hook_thread_func,
            daemon=True
        )
        self._hook_thread.start()

    # ---- RegisterHotKey 实现 ----
    def _hotkey_thread_func(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 1. 注册窗口类（只注册一次，失败也继续，可能是已存在）
        class_name = "AutoPingHotkeyMsgOnlyWnd"
        hinst = kernel32.GetModuleHandleW(None)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_HOTKEY:
                hid = int(wparam)
                if hid == HOTKEY_ID:
                    now = time.monotonic()
                    if now - self._last_hotkey_time > 0.3:
                        self._last_hotkey_time = now
                        self.root.after(0, self._show_window)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(
                ctypes.wintypes.HWND(hwnd),
                ctypes.wintypes.UINT(msg),
                ctypes.wintypes.WPARAM(wparam),
                ctypes.wintypes.LPARAM(lparam),
            )

        self._hotkey_wndproc_ref = WNDPROC(wndproc)

        wcx = WNDCLASSEXW()
        wcx.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wcx.lpfnWndProc = self._hotkey_wndproc_ref
        wcx.hInstance = hinst
        wcx.lpszClassName = class_name
        wcx.hbrBackground = COLOR_WINDOW + 1
        # 不指定 hIcon/hCursor（message-only 不需要），减少资源查找失败概率

        reg_ret = user32.RegisterClassExW(ctypes.byref(wcx))
        if reg_ret == 0:
            last_err = kernel32.GetLastError()
            # 错误码 1410 = CLASS_ALREADY_EXISTS，允许继续
            if last_err != 1410:
                self._hotkey_last_log = (
                    f"[热键] RegisterClassExW 失败，错误码={last_err}"
                )
                return

        # 2. 创建 message-only HWND (HWND_MESSAGE 作为父窗口)
        HWND_MESSAGE_P = ctypes.wintypes.HWND(HWND_MESSAGE)
        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "AutoPingHotkey",
            0,
            0, 0, 0, 0,
            HWND_MESSAGE_P,
            None,
            hinst,
            None,
        )
        if not hwnd:
            self._hotkey_last_log = (
                f"[热键] CreateWindowExW(message-only) 失败，错误码={kernel32.GetLastError()}"
            )
            return
        self._hotkey_hwnd = hwnd

        # 3. 先注销一次上一个进程可能残留的同 ID（防御性），再注册 Alt + Space
        user32.UnregisterHotKey(ctypes.wintypes.HWND(hwnd), HOTKEY_ID)

        # 先试 MOD_ALT | MOD_NOREPEAT：避免长按产生重复触发
        fsModifiers = MOD_ALT | MOD_NOREPEAT
        ok = user32.RegisterHotKey(
            ctypes.wintypes.HWND(hwnd),
            HOTKEY_ID,
            ctypes.wintypes.UINT(fsModifiers),
            ctypes.wintypes.UINT(VK_SPACE),
        )
        if not ok:
            err = kernel32.GetLastError()
            # MOD_NOREPEAT 在 Win7 上可能不支持；回退试纯 MOD_ALT
            fsModifiers = MOD_ALT
            ok = user32.RegisterHotKey(
                ctypes.wintypes.HWND(hwnd),
                HOTKEY_ID,
                ctypes.wintypes.UINT(fsModifiers),
                ctypes.wintypes.UINT(VK_SPACE),
            )
            if not ok:
                err2 = kernel32.GetLastError()
                if err2 == 1409:
                    self._hotkey_last_log = (
                        "[热键] RegisterHotKey(Alt+Space) 失败: 错误码1409，该组合键已被其他进程占用"
                    )
                else:
                    self._hotkey_last_log = (
                        f"[热键] RegisterHotKey(Alt+Space) 失败: 错误码={err}->{err2}"
                    )
                # 销毁临时窗口（但保持线程可退出）
                try:
                    user32.DestroyWindow(hwnd)
                except Exception:
                    pass
                self._hotkey_hwnd = None
                return

        self._hotkey_registered = True
        self._hotkey_last_log = (
            f"[热键] RegisterHotKey(Alt+Space, hwnd=message-only) 成功，id={HOTKEY_ID}"
        )

        # 4. 阻塞 GetMessage 消息泵
        msg = ctypes.wintypes.MSG()
        while True:
            if self._hotkey_stop_event.is_set():
                break
            bRet = user32.PeekMessageW(
                ctypes.byref(msg),
                ctypes.wintypes.HWND(hwnd),
                0, 0,
                1  # PM_REMOVE
            )
            if bRet:
                if msg.message == WM_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                # 短时睡眠，确保 stop_event 能及时响应
                time.sleep(0.01)

        # 退出清理
        try:
            user32.UnregisterHotKey(ctypes.wintypes.HWND(hwnd), HOTKEY_ID)
        except Exception:
            pass
        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass
        self._hotkey_hwnd = None
        self._hotkey_registered = False

    def _uninstall_hotkey(self):
        self._hotkey_stop_event.set()
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            self._hotkey_thread.join(timeout=0.8)
        self._hotkey_registered = False
        self._hotkey_hwnd = None
        self._hotkey_wndproc_ref = None

    # ---- LL 钩子回退实现 ----
    def _hook_thread_func(self):
        user32 = ctypes.windll.user32

        try:
            self._hook_cb = LOWLEVELKEYBOARDPROC(self._keyboard_proc)

            hhook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                self._hook_cb,
                None,
                0
            )

            if not hhook:
                return
        except Exception:
            return

        self._hook_hwnd = hhook

        msg = ctypes.wintypes.MSG()
        while self._hook_hwnd:
            try:
                ret = user32.PeekMessageW(
                    ctypes.byref(msg),
                    None,
                    0,
                    0,
                    1
                )
                if ret:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    time.sleep(0.005)
            except Exception:
                time.sleep(0.01)

        try:
            if hhook:
                user32.UnhookWindowsHookEx(hhook)
        except Exception:
            pass

    def _keyboard_proc(self, nCode, wParam, lParam):
        user32 = ctypes.windll.user32

        if nCode == HC_ACTION:
            kb = ctypes.cast(
                lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)
            ).contents

            vk = kb.vkCode

            is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = wParam in (WM_KEYUP, WM_SYSKEYUP)

            alt_any_down = self._lalt_down or self._ralt_down

            # -------- 1. 精确跟踪左右 Alt 键物理状态（绝不因为吞键而失步） --------
            if vk in (VK_LMENU, VK_RMENU, VK_MENU):
                # VK_MENU (0x12) 在 LL 钩子上不会被发出来，但保留判断容错
                if vk == VK_MENU:
                    # 无法区分左右，默认算左
                    track_vk = VK_LMENU
                else:
                    track_vk = vk
                if is_down:
                    if track_vk == VK_LMENU:
                        self._lalt_down = True
                    else:
                        self._ralt_down = True
                    if self._pending_alt_key_to_swallow not in (0, track_vk):
                        self._pending_alt_key_to_swallow = 0
                else:
                    was_pending = (self._pending_alt_key_to_swallow == track_vk)
                    if track_vk == VK_LMENU:
                        self._lalt_down = False
                    else:
                        self._ralt_down = False
                    if was_pending:
                        self._pending_alt_key_to_swallow = 0
                        if not (self._lalt_down or self._ralt_down):
                            self._space_swallowed_down = False
                        return 1
                    # 其他 Alt UP：用户按了其他顺序，清理挂起状态
                    self._pending_alt_key_to_swallow = 0
                    self._space_swallowed_down = False

            # -------- 2. Space 键：只在"确实有 Alt 按下"时才匹配热键 --------
            elif vk == VK_SPACE:
                if is_down:
                    if (self._lalt_down or self._ralt_down):
                        if self._lalt_down:
                            self._pending_alt_key_to_swallow = VK_LMENU
                        else:
                            self._pending_alt_key_to_swallow = VK_RMENU
                        self._space_swallowed_down = True

                        now = time.monotonic()
                        triggered = now - self._last_hotkey_time > 0.3
                        if triggered:
                            self._last_hotkey_time = now
                            self.root.after(0, self._show_window)
                        return 1
                else:
                    if self._space_swallowed_down and (
                        self._pending_alt_key_to_swallow != 0 or
                        self._lalt_down or self._ralt_down
                    ):
                        return 1
                    self._space_swallowed_down = False

            # -------- 3. 其他任意键：一旦用户在按住 Alt 时按了其他键，放弃本次组合接管 --------
            else:
                if is_down and (self._lalt_down or self._ralt_down):
                    # 例如 Alt+Tab、Alt+F4、Alt+字母菜单栏激活等，全交给系统默认处理
                    self._pending_alt_key_to_swallow = 0
                    self._space_swallowed_down = False

        return user32.CallNextHookEx(
            0,
            nCode,
            ctypes.wintypes.WPARAM(wParam),
            ctypes.wintypes.LPARAM(lParam)
        )

    # ===== Tk 根窗口子类化：拦截 Alt+Space 弹出的系统菜单 =====
    def _subclass_tk_root(self):
        user32 = ctypes.windll.user32
        try:
            hwnd = int(self.root.winfo_id())
            if not hwnd:
                return

            SC_KEYMENU = 0xF100

            def tk_wndproc(hwnd_p, msg, wparam, lparam):
                # WM_SYSCOMMAND: wParam 的低 16 位是命令类型；SC_KEYMENU + wParam=' '(32) 就是 Alt+Space 拉起菜单
                if msg == WM_SYSCOMMAND:
                    cmd = int(wparam) & 0xFFFF
                    if cmd == SC_KEYMENU:
                        key = (int(lparam) >> 16) & 0xFFFF
                        if key in (0x20, 32, VK_SPACE):
                            # 直接吞掉，不弹系统菜单
                            return 0
                        # SC_KEYMENU 且 lParam 高位=0 时，是 Alt 键本身释放时触发菜单
                        if key == 0:
                            # 允许 Alt 键弹起时的默认行为（通常不会单独打开菜单）
                            pass
                return user32.CallWindowProcW(
                    ctypes.c_void_p(self._orig_tk_wndproc) if self._orig_tk_wndproc else 0,
                    HWND(hwnd_p),
                    ctypes.wintypes.UINT(msg),
                    ctypes.wintypes.WPARAM(wparam),
                    ctypes.wintypes.LPARAM(lparam),
                )

            self._tk_wndproc_cb_ref = WNDPROC(tk_wndproc)

            # 两种 SetWindowLong 版本：优先 SetWindowLongPtrW (64 位指针安全)
            if hasattr(user32, 'SetWindowLongPtrW'):
                set_func = user32.SetWindowLongPtrW
                set_func.restype = ctypes.c_ssize_t
            else:
                set_func = user32.SetWindowLongW
                set_func.restype = ctypes.c_long
            set_func.argtypes = [HWND, ctypes.c_int, ctypes.c_ssize_t]

            if hasattr(user32, 'GetWindowLongPtrW'):
                get_func = user32.GetWindowLongPtrW
                get_func.restype = ctypes.c_ssize_t
            else:
                get_func = user32.GetWindowLongW
                get_func.restype = ctypes.c_long
            get_func.argtypes = [HWND, ctypes.c_int]

            prev = get_func(HWND(hwnd), GWL_WNDPROC)
            if prev == 0:
                return
            self._orig_tk_wndproc = prev

            new_proc = ctypes.cast(self._tk_wndproc_cb_ref, ctypes.c_void_p).value
            set_func(HWND(hwnd), GWL_WNDPROC, ctypes.c_ssize_t(new_proc))
        except Exception:
            pass

    def _restore_tk_wndproc(self):
        user32 = ctypes.windll.user32
        try:
            hwnd = int(self.root.winfo_id())
            if hwnd and self._orig_tk_wndproc:
                if hasattr(user32, 'SetWindowLongPtrW'):
                    set_func = user32.SetWindowLongPtrW
                    set_func.restype = ctypes.c_ssize_t
                else:
                    set_func = user32.SetWindowLongW
                    set_func.restype = ctypes.c_long
                set_func.argtypes = [HWND, ctypes.c_int, ctypes.c_ssize_t]
                set_func(HWND(hwnd), GWL_WNDPROC, ctypes.c_ssize_t(self._orig_tk_wndproc))
        except Exception:
            pass
        self._orig_tk_wndproc = None
        self._tk_wndproc_cb_ref = None

    def _show_window(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        self.root.deiconify()
        self.root.lift()
        # 按用户设置的置顶状态来，不再强制 True
        self.root.attributes("-topmost", bool(self.is_topmost))

        try:
            fg_hwnd = user32.GetForegroundWindow()
            my_hwnd = int(self.root.winfo_id())

            if fg_hwnd and fg_hwnd != my_hwnd:
                fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
                my_tid = kernel32.GetCurrentThreadId()

                if fg_tid != my_tid:
                    user32.AttachThreadInput(
                        ctypes.wintypes.DWORD(my_tid),
                        ctypes.wintypes.DWORD(fg_tid),
                        True
                    )

            SW_RESTORE = 9
            user32.ShowWindow(my_hwnd, SW_RESTORE)
            user32.SetForegroundWindow(my_hwnd)
            user32.BringWindowToTop(my_hwnd)

            if fg_hwnd and fg_hwnd != my_hwnd and fg_tid != my_tid:
                user32.AttachThreadInput(
                    ctypes.wintypes.DWORD(my_tid),
                    ctypes.wintypes.DWORD(fg_tid),
                    False
                )

            entry_hwnd = int(self.entry.winfo_id())
            user32.SetFocus(entry_hwnd)
        except Exception:
            pass

        try:
            self.entry.focus_set()
            self.entry.select_range(0, tk.END)
            self.entry.icursor(tk.END)
        except Exception:
            pass

        self.root.after(50, self._refocus_entry)
        self.root.after(250, self._restore_topmost)

    def _refocus_entry(self):
        try:
            user32 = ctypes.windll.user32
            my_hwnd = int(self.root.winfo_id())
            fg = user32.GetForegroundWindow()
            if fg != my_hwnd:
                user32.SetForegroundWindow(my_hwnd)
            entry_hwnd = int(self.entry.winfo_id())
            user32.SetFocus(entry_hwnd)
            self.entry.focus_set()
            self.entry.select_range(0, tk.END)
            self.entry.icursor(tk.END)
        except Exception:
            pass

    def _restore_topmost(self):
        # 严格按用户选择：想置顶就 True，不想就 False；之前的 set True 是 bug
        self.root.attributes("-topmost", bool(self.is_topmost))

    def _uninstall_hook(self):
        # 先还原 Tk 根窗口的窗口过程（防 GC 后回调失效崩溃）
        self._restore_tk_wndproc()
        # 再卸载 RegisterHotKey（若用了）
        self._uninstall_hotkey()
        # 再卸载 LL 钩子（若回退用了）
        hhook = self._hook_hwnd
        self._hook_hwnd = None
        if hhook:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(hhook)
            except Exception:
                pass
        self._hook_cb = None

        if self._hook_thread and self._hook_thread.is_alive():
            self._hook_thread.join(timeout=0.5)

    def on_close(self):
        self._uninstall_hook()
        self.root.destroy()

    def create_widgets(self):

        top = tk.Frame(self.root)
        top.pack(pady=8)

        tk.Label(
            top,
            text="设备编号(1-999) 或 IP:"
        ).pack(
            side=tk.LEFT,
            padx=5
        )

        self.entry = tk.Entry(
            top,
            width=15
        )

        self.entry.pack(
            side=tk.LEFT
        )

        self.entry.bind(
            "<Return>",
            self.start_ping
        )

        self.start_btn = tk.Button(
            top,
            text="开始",
            command=self.start_ping
        )

        self.start_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.stop_btn = tk.Button(
            top,
            text="结束",
            command=self.stop_ping,
            state=tk.DISABLED
        )

        self.stop_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.vnc_btn = tk.Button(
            top,
            text="VNC",
            command=self.do_open_vnc
        )

        self.vnc_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.flushdns_btn = tk.Button(
            top,
            text="刷新DNS",
            command=self.do_flush_dns
        )

        self.flushdns_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.resolve_btn = tk.Button(
            top,
            text="解析",
            command=self.do_resolve_hosts
        )

        self.resolve_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.offline_btn = tk.Button(
            top,
            text="离线检测",
            command=self.confirm_offline_check
        )

        self.offline_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.topmost_btn = tk.Button(
            top,
            text="置顶",
            command=self.toggle_topmost
        )

        self.topmost_btn.pack(
            side=tk.LEFT,
            padx=2
        )

        self.text = tk.Text(
            self.root,
            bg="black",
            fg="lime",
            cursor="arrow"
        )

        self.text.pack(
            fill=tk.BOTH,
            expand=True,
            padx=10,
            pady=(2, 10)
        )

        self.text.config(
            state="disabled"
        )

    def append(self, msg):

        self.text.config(
            state="normal"
        )

        self.text.insert(
            tk.END,
            msg
        )

        self.text.see(tk.END)

        self.text.config(
            state="disabled"
        )

    def get_current_hostname(self):

        value = self.entry.get().strip()

        if value:

            self.hostname = parse_hostname(value)

            return self.hostname

        if self.hostname:

            return self.hostname

        raise ValueError(
            "请输入设备编号、主机名或IP"
        )

    def update_hostname(self):

        self.hostname = parse_hostname(
            self.entry.get()
        )

        return self.hostname

    def start_ping(self, event=None):

        if self.process:

            self.stop_ping()

            # 二次回车自动开 VNC：
            # - 收到有效回复 或 外部DNS已更新出缓存新IP => 自动启动；使用新的缓存 IP
            if self.pings_received or self._cached_resolved_ip(self.host):
                self.do_open_vnc()

            return "break"

        if self.batch_running:
            self.stop_ping()
            return "break"

        try:

            hostname = self.get_current_hostname()
            self.hostname = hostname

            self.entry.delete(
                0,
                tk.END
            )

        except Exception as e:

            messagebox.showerror(
                "错误",
                str(e)
            )

            return "break"

        self.do_ping()

        return "break"

    def _cached_resolved_ip(self, hostname):
        """返回该主机当前在缓存里的 IP（如果有且非空），否则 None；不做任何外部DNS"""
        if not hostname:
            return None
        ip, used = resolve_host_cached(hostname, force_dns=False)
        if used and ip and ip != (hostname.strip() if isinstance(hostname, str) else hostname):
            return ip
        return None

    def do_ping(self):

        self.stats = PingStats()
        self.pings_received = False
        self.ping_failed = False

        self.host = self.hostname

        # ===== 缓存优先：先查缓存 + 直接IP（resolve_host_cached force_dns=False 不做外部DNS） =====
        first_target, used_cache = resolve_host_cached(self.host, force_dns=False)
        self._ping_cached_target = first_target
        self._ping_cached_ok = False
        self._ping_fallback_to_dns = False
        self._ping_display_host = self.host

        self.text.config(
            state="normal"
        )

        self.text.delete(
            1.0,
            tk.END
        )

        self.text.config(
            state="disabled"
        )

        self.start_btn.config(
            state=tk.DISABLED
        )

        self.stop_btn.config(
            state=tk.NORMAL
        )

        if used_cache:
            # 命中缓存（或本身就是 IP）：直接启动 ping 进程，不再做任何外部 DNS
            if first_target != self.host:
                self.append(
                    f"开始 Ping {self.host} (使用缓存IP: {first_target})\n\n"
                )
            else:
                self.append(f"开始 Ping {self.host}\n\n")
            self._launch_ping_process(first_target if first_target else self.host)
            return

        # 缓存 miss：后台线程跑外部 DNS + 落盘（不阻塞UI），完成后回到主线程启动 ping
        self.append(f"开始 Ping {self.host}（查缓存未命中，后台外部DNS解析中...）\n")

        host_src = self.host

        # 记录旧缓存值：决定外部DNS失败时是否要写空值占位
        old_cached_any = get_cached_value(host_src, include_empty=True)  # None / "" / "IP"
        old_was_nonempty = bool(old_cached_any)

        def worker():
            t2, _ = resolve_host_cached(host_src, force_dns=True)
            if not t2:
                t2 = host_src

            key = normalize_host_key(host_src)
            update_log = None
            # 按用户规则：
            # - 解析成功 t2 != host => 写入新IP，替换旧IP（无论旧值空不空）
            # - 解析失败 t2 == host:
            #       * 旧缓存不是空值 => 写一个空值占位
            #       * 旧值一开始就是空 / 根本没key => 不写空，直接退出不启进程
            resolve_success = bool(t2 and t2 != host_src)

            if key:
                if resolve_success:
                    r = upsert_host_cache_entry(key, t2)
                    if r["changed"]:
                        update_log = (
                            f"[缓存更新] {r['key']}: "
                            f"{r['old']!r} -> {r['new']!r} "
                            f"(config/host.json)"
                        )
                elif old_was_nonempty:
                    # 之前缓存有值（比如有旧IP）现在外部DNS解析失败 → 清空占位
                    r = upsert_host_cache_entry(key, "")
                    if r["changed"]:
                        update_log = (
                            f"[缓存占位] {r['key']}: "
                            f"旧缓存有IP，但本次外部DNS未解析到，JSON改为空值占位"
                        )

            self.root.after(
                0,
                lambda: self._do_ping_after_dns(
                    host_src, t2, update_log, resolve_success=resolve_success,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _do_ping_after_dns(self, host_src, target, update_log, *, resolve_success: bool):
        if host_src != self.host:
            # 用户已切换到别的 ping，这次结果丢弃
            return
        if update_log:
            self.append(update_log + "\n")
        self._ping_cached_target = target
        if resolve_success:
            self.append(f"外部 DNS 解析：{host_src} -> {target}\n\n")
            self._launch_ping_process(target if target else host_src)
            return

        # 外部 DNS 也没解析到：直接退出 ping，不启动进程，不显示"找不到主机"的错误
        self.append(f"外部 DNS 仍无法解析 {host_src}，直接退出 Ping（未启动进程）\n")
        # 恢复按钮
        try:
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
        except Exception:
            pass

    def _launch_ping_process(self, target):
        try:
            self.process = create_ping_process(target)
        except Exception as e:
            self.append(f"启动 Ping 失败: {e}\n")
            self.stop_ping()
            return

        threading.Thread(
            target=self.read_output,
            daemon=True
        ).start()

    def read_output(self):

        if not self.process:
            return

        dns_stopped = False
        cached_failure_count = 0      # 缓存IP失败计数（连续超时/不可达累计到阈值就切外部DNS）
        CACHED_FAIL_THRESHOLD = 2     # 连续失败 2 次判断"缓存IP不通"
        used_cache_at_start = bool(
            getattr(self, "_ping_cached_target", None) and (
                getattr(self, "_ping_cached_target", None) != self.host
            )
        )

        try:

            for line in self.process.stdout:

                if not line:
                    continue

                line = line.strip()

                is_dns_fail = (
                    "无法解析" in line
                    or "找不到" in line
                )

                if is_dns_fail and not dns_stopped:
                    dns_stopped = True
                    if used_cache_at_start and not getattr(self, "_ping_fallback_to_dns", False):
                        # 过渡段：杀进程→外部DNS→重开ping。下一次 statistics 应该跳过不要输出
                        self._suppress_next_statistics = True
                        self.root.after(0, self.append,
                                        f"!!! 缓存IP解析异常，尝试使用外部 DNS 重新解析 {self.host}...\n")
                        self.root.after(200, self._ping_fallback_force_dns)
                        return

                    # 外部 DNS 也解析不到（或缓存为空也没命中）：直接退出 ping，不显示"1秒后停止"那段
                    # 占位规则：旧缓存值非空才写空；原值空/不存在就不动
                    host_src = self.host
                    key = normalize_host_key(host_src)
                    old_nonempty = bool(get_cached_value(host_src, include_empty=True))
                    if key and old_nonempty:
                        upsert_host_cache_entry(key, "")

                    self.root.after(0, self.append,
                                    f"!!! 解析不到IP，直接退出 Ping{ '（原缓存有值，已写空值占位）' if old_nonempty else ''}\n")
                    # 立刻关进程 + 恢复按钮；不输出统计（也不要让 finally 再输出统计）
                    self._suppress_next_statistics = True
                    self.root.after(0, self.stop_ping)
                    return

                is_any_result = (
                    "来自" in line
                    or "请求超时" in line
                    or "无法访问目标主机" in line
                )

                if is_any_result:

                    self.stats.sent += 1

                    is_success = (
                        "来自" in line
                        and "无法访问目标主机" not in line
                    )
                    is_failure = (
                        "请求超时" in line
                        or "无法访问目标主机" in line
                    )

                    if is_success:
                        self.stats.received += 1
                        self.pings_received = True
                        cached_failure_count = 0
                        self._ping_cached_ok = True

                        m = re.search(r"时间[=<](\d+)ms", line)
                        if m:
                            delay_ms = int(m.group(1))
                            if delay_ms > 1000:
                                self.root.after(
                                    0,
                                    self.append,
                                    f"\n!!! 延迟超过 1000ms ({delay_ms}ms)，自动停止 Ping\n"
                                )
                                self.root.after(0, self.stop_ping)
                                return

                    if is_failure:
                        self.ping_failed = True
                        cached_failure_count += 1
                        # 缓存 IP 连续失败达到阈值 → 切换为外部 DNS 解析再 ping
                        if (used_cache_at_start and
                                not getattr(self, "_ping_fallback_to_dns", False) and
                                cached_failure_count >= CACHED_FAIL_THRESHOLD):
                            self._ping_fallback_to_dns = True
                            # 过渡段：这段统计不要输出（否则会显示"跳过MAC/VNC"，我们要等第二段真正结束的统计）
                            self._suppress_next_statistics = True
                            self.root.after(
                                0, self.append,
                                f"\n!!! 缓存IP {getattr(self, '_ping_cached_target', '?')} 连续"
                                f"{CACHED_FAIL_THRESHOLD}次不通，切换外部DNS重新解析 {self.host}...\n"
                            )
                            self.root.after(100, self._ping_fallback_force_dns)
                            return

                self.root.after(
                    0,
                    self.append,
                    line + "\n"
                )

        except Exception as e:

            self.root.after(
                0,
                self.append,
                f"\n读取输出异常: {e}\n"
            )

        finally:

            if self.process:

                self.process = None

                if getattr(self, "_suppress_next_statistics", False):
                    # 过渡段（缓存失败重连）：不打印统计，但要把按钮恢复正常（稍后重开第二段 ping）
                    def _restore_btns_only():
                        self._suppress_next_statistics = False
                        try:
                            self.start_btn.config(state=tk.NORMAL)
                            self.stop_btn.config(state=tk.DISABLED)
                        except Exception:
                            pass
                    self.root.after(0, _restore_btns_only)
                else:
                    self.root.after(
                        0,
                        self.show_statistics
                    )

    def _ping_fallback_force_dns(self):
        """缓存IP不通 → 后台线程做：外部DNS解析 + 更新host.json落盘 → 回到UI线程启动新 ping（避免主UI卡死）"""
        if self.process:
            try:
                self.process.kill()
            except Exception:
                pass
            self.process = None

        host_src = self.host

        # 记录旧缓存值：规则同 do_ping worker —— 只有旧值非空才在解析失败时写空值占位
        old_was_nonempty = bool(get_cached_value(host_src, include_empty=True))

        def worker():
            # 下面两步都是阻塞：DNS 解析失败在 Windows 上可卡 2~12s；upsert 落盘也会写文件
            new_target, _used = resolve_host_cached(host_src, force_dns=True)
            if not new_target:
                new_target = host_src

            key_for_upsert = normalize_host_key(host_src)
            log_line = None
            resolve_success = bool(new_target and new_target != host_src)
            if key_for_upsert:
                if resolve_success and new_target != key_for_upsert:
                    r = upsert_host_cache_entry(key_for_upsert, new_target)
                    if r["changed"]:
                        log_line = (
                            f"[缓存更新] {r['key']}: "
                            f"{r['old']!r} -> {r['new']!r} "
                            f"(已写入 config/host.json)"
                        )
                elif not resolve_success and old_was_nonempty:
                    # 只有旧缓存有值（之前有旧IP）才清空占位；否则原本就是空的不用动
                    r = upsert_host_cache_entry(key_for_upsert, "")
                    if r["changed"]:
                        log_line = (
                            f"[缓存占位] {r['key']}: "
                            f"旧缓存有IP但本次外部DNS未解析到，JSON改为空值（下次成功自动覆盖）"
                        )
            self.root.after(0, lambda: self._ping_apply_dns_result(new_target, log_line, resolve_success=resolve_success))

        threading.Thread(target=worker, daemon=True).start()

    def _ping_apply_dns_result(self, new_target, log_line, *, resolve_success: bool):
        if log_line:
            self.append(log_line + "\n")

        host_src = self.host
        if resolve_success:
            self.append(f"外部 DNS 解析结果：{host_src} -> {new_target}\n继续 Ping...\n\n")
            self._ping_cached_target = new_target
            self._ping_cached_ok = False
            self._ping_fallback_to_dns = True
            try:
                self.process = create_ping_process(new_target if new_target else host_src)
            except Exception as e:
                self.append(f"启动 Ping 失败: {e}\n")
                self.stop_ping()
                return
            threading.Thread(target=self.read_output, daemon=True).start()
            return

        # 二次外部 DNS 仍解析不到：直接退出，不启动第二次 ping
        self.append(f"外部 DNS 仍无法解析 {host_src}，停止重试。\n")
        # 按钮恢复
        try:
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
        except Exception:
            pass

    def stop_ping(self):

        if self.batch_running:
            self.batch_stop_event.set()
            self.append("\n正在停止批量检测...\n")
            return

        if not self.process:
            return

        process = self.process

        self.process = None

        try:
            process.kill()

        except:
            pass

        # === 过渡段统计抑制：缓存失败→外部DNS重连时，第一段"---本次统计---/跳过MAC/VNC"整个不输出 ===
        if getattr(self, "_suppress_next_statistics", False):
            self._suppress_next_statistics = False
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            return

        self.show_statistics()

        self.start_btn.config(
            state=tk.NORMAL
        )

        self.stop_btn.config(
            state=tk.DISABLED
        )

    def any_key_stop(self, event):

        if not self.process:
            if not self.batch_running:
                return

        if event.keysym == "Return":
            return

        self.stop_ping()

    def do_flush_dns(self):

        try:

            self.append(
                "\n正在刷新 DNS 缓存...\n"
            )

            result = flush_dns()

            self.append(
                "\n--- DNS 刷新结果 ---\n"
            )

            self.append(
                result + "\n"
            )

        except Exception as e:

            messagebox.showerror(
                "错误",
                f"刷新DNS失败:\n{e}"
            )

    def do_open_vnc(self):
        try:
            host_src = self.get_current_hostname()
        except Exception as e:
            messagebox.showerror("错误", f"获取主机失败:\n{e}")
            return

        def worker():
            try:
                # 缓存 IP 优先
                target, used_cache = resolve_host_cached(host_src, force_dns=False)
                if not target:
                    target = host_src

                fell_back_dns = False
                update_log = None

                if used_cache:
                    # 缓存 IP 快速探测（500ms）；失败再外部DNS
                    import subprocess as _sp
                    try:
                        quick = _sp.run(
                            ["ping", "-n", "1", "-w", "500", target],
                            capture_output=True, text=True, encoding="gbk",
                            errors="ignore", creationflags=_sp.CREATE_NO_WINDOW,
                            timeout=2,
                        )
                        if "TTL=" not in quick.stdout.upper():
                            new_target, _ = resolve_host_cached(host_src, force_dns=True)
                            if new_target:
                                target = new_target
                            fell_back_dns = True
                    except Exception:
                        fell_back_dns = True
                        new_target, _ = resolve_host_cached(host_src, force_dns=True)
                        if new_target:
                            target = new_target

                # 落盘更新
                key = normalize_host_key(host_src)
                if fell_back_dns and key:
                    if target != key:
                        r = upsert_host_cache_entry(key, target)
                        if r["changed"]:
                            update_log = (
                                f"[缓存更新] {r['key']}: {r['old']!r} -> {r['new']!r} "
                                f"(config/host.json)"
                            )
                    else:
                        r = upsert_host_cache_entry(key, "")
                        if r["changed"]:
                            update_log = f"[缓存占位] {r['key']}: 外部DNS未解析到IP，JSON写空值"

                self.root.after(
                    0,
                    lambda: self._open_vnc_apply(
                        host_src=host_src,
                        target=target,
                        used_cache=used_cache,
                        fell_back_dns=fell_back_dns,
                        update_log=update_log,
                    ),
                )
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("错误", f"启动VNC失败:\n{e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _open_vnc_apply(self, host_src, target, used_cache, fell_back_dns, update_log):
        try:
            if update_log:
                self.append(update_log + "\n")
            open_vnc(target)

            note = ""
            if used_cache and target != host_src and not fell_back_dns:
                note = f" (缓存IP: {target})"
            elif fell_back_dns and target != host_src:
                note = f" (外部DNS: {target})"
            self.append(f"\n启动VNC连接: {host_src}{note}\n")
        except Exception as e:
            messagebox.showerror("错误", f"启动VNC失败:\n{e}")

    def toggle_topmost(self):

        self.is_topmost = (
            not self.is_topmost
        )

        self.root.attributes(
            "-topmost",
            self.is_topmost
        )

        self.topmost_btn.config(
            text=(
                "取消置顶"
                if self.is_topmost
                else "置顶"
            )
        )

    def show_statistics(self):

        if not self.host:
            return

        self.append(
            "\n--- 本次 Ping 统计 ---\n"
        )

        self.append(
            f"主机 = {self.host}\n"
        )

        self.append(
            f"发送 = {self.stats.sent}\n"
        )

        self.append(
            f"接收 = {self.stats.received}\n"
        )

        self.append(
            f"丢失 = {self.stats.lost} "
            f"({self.stats.loss_rate:.1f}% 丢失)\n"
        )

        # MAC 查询：
        # - 收到有效回复 => 直接查；
        # - 即便"未收到 ping 回复"，只要外部 DNS 已经把缓存更新为新 IP，也用新 IP 查询（不再按旧逻辑"跳过"）
        cached_ip = self._cached_resolved_ip(self.host)
        should_query_mac = bool(self.pings_received or cached_ip)

        if should_query_mac:
            # 统一按"缓存优先"取 IP：resolve_host_cached(force_dns=False) 内部不会做外部 DNS，安全
            target, used_cache = resolve_host_cached(self.host, force_dns=False)
            if not target:
                target = self.host

            if used_cache and target and target != self.host:
                # 先尝试用缓存 IP 直接查 ARP，查不到再回退默认 get_mac_address
                try:
                    import subprocess
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    out = subprocess.check_output(
                        ["arp", "-a", target],
                        text=True, encoding="gbk", errors="ignore",
                        startupinfo=startupinfo
                    )
                    ip, mac = None, None
                    for ln in out.splitlines():
                        if target in ln:
                            parts = ln.split()
                            if len(parts) >= 2:
                                ip, mac = parts[0], parts[1]
                                break
                    if not mac:
                        ip, mac = target, "未找到（ARP未缓存/跨网段）"
                except Exception:
                    ip, mac = get_mac_address(target)
            else:
                # 没命中缓存或本身就是主机名：用目标查
                ip, mac = get_mac_address(target if target else self.host)
            self.append(f"IP 地址 = {ip}\n")
            self.append(f"MAC 地址 = {mac}\n")
        else:
            self.append("未收到有效回复，且缓存未解析到IP，跳过 MAC 查询；二次回车仍可手动启动 VNC\n")

        self.start_btn.config(
            state=tk.NORMAL
        )

        self.stop_btn.config(
            state=tk.DISABLED
        )

    def do_resolve_hosts(self):
        """解析按钮：对 config/list.txt 的全部设备做外部DNS解析，结果存入 config/host.json 并刷新内存常量"""
        if hasattr(self, "_resolve_running") and self._resolve_running:
            messagebox.showinfo("提示", "正在解析，请稍后...")
            return

        list_file = CONFIG_LIST_FILE
        if not Path(list_file).is_file():
            messagebox.showerror("错误", f"未找到 {list_file}")
            return

        self.append("\n" + "=" * 60 + "\n")
        self.append("开始批量解析 list.txt ...\n")
        self.append(f"list 文件: {list_file}\n")
        self.append("=" * 60 + "\n\n")
        self.resolve_btn.config(state=tk.DISABLED)
        self._resolve_running = True

        def _worker():
            try:
                result = resolve_all_list()
                self.root.after(0, self._resolve_finished, result)
            except Exception as e:
                self.root.after(
                    0, self._resolve_finished,
                    {
                        "ok": False,
                        "total": 0, "success": 0, "failed": 0,
                        "list_file": str(list_file),
                        "output": f"解析异常: {e}",
                        "mapping": {},
                    }
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _resolve_finished(self, result):
        self._resolve_running = False
        try:
            self.resolve_btn.config(state=tk.NORMAL)
        except Exception:
            pass

        ok = result.get("ok")
        total = result.get("total", 0)
        success = result.get("success", 0)
        failed = result.get("failed", 0)
        mapping = result.get("mapping") or {}
        list_file = result.get("list_file") or ""
        output = result.get("output") or ""

        self.append("解析完成：\n")
        if list_file:
            self.append(f"  清单文件 : {list_file}\n")
        self.append(f"  总计     : {total}\n")
        self.append(f"  成功     : {success}\n")
        self.append(f"  失败     : {failed}\n")
        self.append(f"  缓存条目 : {len(mapping)}（已写入 config/host.json）\n")

        failed_keys = sorted([k for k, v in mapping.items() if not v])
        if failed_keys:
            preview = failed_keys[:20]
            suffix = "" if len(failed_keys) <= 20 else f" ...(共{len(failed_keys)}个失败)"
            self.append(f"  失败设备 : {' '.join(preview)}{suffix}\n")
        self.append("\n")

        if not ok:
            last_line = ""
            for ln in output.splitlines():
                if ln.strip():
                    last_line = ln.strip()
            messagebox.showerror(
                "解析失败",
                last_line or "解析失败"
            )

    def confirm_offline_check(self):

        if self.batch_running:
            messagebox.showinfo(
                "提示",
                "离线检测正在进行中，请等待完成"
            )
            return

        list_file = CONFIG_LIST_FILE
        if not Path(list_file).exists():
            messagebox.showerror(
                "错误",
                f"未找到 {list_file} 文件"
            )
            return

        try:
            with open(list_file, "r", encoding="utf-8") as f:
                hosts = [
                    line.strip()
                    for line in f
                    if line.strip()
                ]
        except Exception as e:
            messagebox.showerror(
                "错误",
                f"读取 {list_file} 失败:\n{e}"
            )
            return

        total = len(hosts)

        result = messagebox.askyesno(
            "确认离线检测",
            f"将对 {list_file.name} 中 {total} 台主机进行批量离线检测。\n"
            f"第1轮使用缓存IP，失败的机器将在第2/3轮使用外部DNS重新解析。\n"
            f"此过程需要几分钟时间，是否继续？"
        )

        if result:
            self.run_offline_check(hosts)

    def run_offline_check(self, hosts):

        self.batch_running = True
        self.batch_stop_event.clear()
        self.offline_btn.config(state=tk.DISABLED)

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        self.text.config(state="normal")
        self.text.delete(1.0, tk.END)
        self.text.config(state="disabled")

        total = len(hosts)

        self.append("=" * 60 + "\n")
        self.append(f"开始离线检测，共 {total} 台主机\n")
        self.append("规则: 第1轮=缓存IP；第1轮失败 → 第2/3轮=外部DNS解析后再Ping\n")
        self.append("=" * 60 + "\n\n")

        threading.Thread(
            target=self._batch_check_worker,
            args=(hosts, total),
            daemon=True
        ).start()

    def _batch_check_worker(self, hosts, total):
        offline_hosts = []
        unstable_hosts = []
        batch_lock = threading.Lock()

        def _normalize(host):
            try:
                return parse_hostname(host)
            except Exception:
                h = (host or "").strip()
                return h if h else None

        def check_host(raw_host):
            raw_host = (raw_host or "").strip()
            if not raw_host:
                return

            norm = _normalize(raw_host)
            display = norm or raw_host

            # ========== 第 1 轮：优先使用内存缓存的 IP ==========
            if self.batch_stop_event.is_set():
                return

            t1, used_cache = (None, False)
            if norm:
                t1, used_cache = resolve_host_cached(norm, force_dns=False)
            if not t1:
                t1 = display

            alive, _ = batch_ping_host(t1, 1)
            if alive:
                self.root.after(
                    0, self.append,
                    f"[ONLINE ] {display}"
                    f"{' (缓存IP: ' + t1 + ')' if used_cache and t1 != display else ''}\n"
                )
                return

            # ========== 第 2/3 轮：强制外部 DNS 解析 ==========
            if self.batch_stop_event.is_set():
                return

            t2 = None
            dns_changed = False
            if norm:
                t2, _ = resolve_host_cached(norm, force_dns=True)
            if not t2:
                t2 = display

            # 外部 DNS 结果写回 host.json（成功填新IP，失败填空值占位）
            if norm and t2 != t1:
                dns_changed = True
                if t2 != norm:
                    upsert_host_cache_entry(norm, t2)
                else:
                    upsert_host_cache_entry(norm, "")

            # 第 2 轮（外部DNS，1 包）
            alive, _ = batch_ping_host(t2, 1)
            if alive:
                tag_extra = ""
                if dns_changed:
                    if t2 != norm:
                        tag_extra = f"缓存IP {t1} 未通，外部DNS: {t2}"
                    else:
                        tag_extra = f"缓存IP {t1} 未通，外部DNS未解析到IP（JSON留空）"
                self.root.after(
                    0, self.append,
                    f"[ONLINE ] {display} (Retry, "
                    f"{tag_extra if tag_extra else ('缓存IP ' + t1 + '未通，外部DNS:' + t2)})\n"
                )
                return

            if self.batch_stop_event.is_set():
                return

            # 第 3 轮（外部DNS，3 包）
            alive, raw_log = batch_ping_host(t2, 3)
            if alive:
                with batch_lock:
                    unstable_hosts.append({
                        "host": display,
                        "log": raw_log
                    })
                tag_extra2 = ""
                if dns_changed:
                    if t2 != norm:
                        tag_extra2 = f"缓存IP {t1} 未通，外部DNS: {t2}"
                    else:
                        tag_extra2 = f"缓存IP {t1} 未通，外部DNS未解析到IP（JSON留空）"
                else:
                    tag_extra2 = f"缓存IP {t1} 未通，外部DNS: {t2}"
                self.root.after(
                    0, self.append,
                    f"[UNSTABLE] {display} ({tag_extra2})\n"
                )
                return

            with batch_lock:
                offline_hosts.append(display)
            tag_extra3 = ""
            if dns_changed:
                if t2 != norm:
                    tag_extra3 = f"缓存IP {t1} 未通，外部DNS: {t2}"
                else:
                    tag_extra3 = f"缓存IP {t1} 未通，外部DNS未解析到IP（JSON留空）"
            else:
                tag_extra3 = f"缓存IP {t1} 未通，外部DNS: {t2}"
            self.root.after(
                0, self.append,
                f"[OFFLINE ] {display} ({tag_extra3})\n"
            )

        with ThreadPoolExecutor(max_workers=BATCH_MAX_WORKERS) as pool:
            pool.map(check_host, hosts)

        self.root.after(
            0, self._batch_check_finished,
            hosts, total, offline_hosts, unstable_hosts
        )

    def _batch_check_finished(self, hosts, total, offline_hosts, unstable_hosts):

        log_dir = Path("log")
        log_dir.mkdir(exist_ok=True)

        now = datetime.now()

        log_name = (
            f"{len(offline_hosts)}个离线_"
            f"{now.strftime('%Y%m%d_%H%M%S')}.log"
        )

        log_file = log_dir / log_name

        with open(log_file, "w", encoding="utf-8") as f:

            f.write("=" * 80 + "\n")
            f.write(f"检查时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"总主机数：{total}\n")
            f.write(f"不稳定主机：{len(unstable_hosts)}\n")
            f.write(f"离线主机：{len(offline_hosts)}\n")
            f.write("=" * 80 + "\n\n")

            f.write("【离线主机】\n\n")

            if offline_hosts:
                for host in sorted(offline_hosts):
                    f.write(host + "\n")
            else:
                f.write("无\n")

            f.write("\n\n")
            f.write("=" * 80 + "\n")
            f.write("【不稳定主机】\n")
            f.write("=" * 80 + "\n\n")

            if unstable_hosts:

                for item in sorted(
                        unstable_hosts,
                        key=lambda x: x["host"]):

                    f.write(f"主机：{item['host']}\n")
                    f.write("-" * 80 + "\n")
                    f.write(item["log"])
                    f.write("\n")

                    if not item["log"].endswith("\n"):
                        f.write("\n")

                    f.write("=" * 80 + "\n\n")
            else:
                f.write("无\n")

        self.append("\n")
        self.append("=" * 60 + "\n")

        if self.batch_stop_event.is_set():
            self.append("离线检测已手动停止\n")

        self.append("离线检测完成\n")
        self.append(f"总数     : {total}\n")
        self.append(f"离线     : {len(offline_hosts)}\n")
        self.append(f"不稳定   : {len(unstable_hosts)}\n")
        self.append(f"日志文件 : {log_file}\n")
        self.append("=" * 60 + "\n")

        self.last_log_file = log_file
        self._last_offline = offline_hosts
        self._last_unstable = unstable_hosts

        self.batch_running = False
        self.offline_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)

        if not self.batch_stop_event.is_set():
            self.root.after(100, self._ask_open_log, log_file)

    def _ask_open_log(self, log_file):
        result = messagebox.askyesno(
            "检测完成",
            f"离线检测完成，共 {len(self._last_offline)} 台离线，"
            f"{len(self._last_unstable)} 台不稳定。\n\n"
            f"是否查看日志文件？"
        )
        if result:
            self._open_file(log_file)

    def _open_file(self, path):
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            messagebox.showerror(
                "错误",
                f"无法打开文件: {e}"
            )
