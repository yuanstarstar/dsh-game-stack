# -*- coding: utf-8 -*-
"""最小键盘/鼠标注入（SendInput），只用来把「想做的操作」交给游戏窗口。

设计上的三条硬约束：
1. **必须前台**：目标窗口不在前台就拒绝发送（防止按键打到别的窗口里）。
2. **白名单**：只允许少量按键（空格/回车/Esc/方向/数字/字母），不开放组合键。
3. **可审计**：每次输入都由调用方记日志，便于回放和追责。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
SW_RESTORE = 9


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", INPUTUNION)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

VK = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "backspace": 0x08, "delete": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64, "num5": 0x65,
}
for _i, _c in enumerate("1234567890"):
    VK[_c] = 0x30 + _i
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
ALLOWED = set(VK)


def resolve_key(key: str) -> int:
    k = (key or "").strip().lower()
    if k.startswith("vk:"):
        n = int(k[3:], 0)
        if not 0 < n < 255:
            raise ValueError("vk 超出范围")
        return n
    if k not in ALLOWED:
        raise ValueError(f"不支持的按键：{key}（可用 {sorted(ALLOWED)[:12]} …）")
    return VK[k]


def window_title(hwnd: int) -> str:
    """窗口标题——回执里要能读出「真实前台到底是谁」。"""
    if not hwnd:
        return ""
    try:
        user32.GetWindowTextLengthW.argtypes = [wt.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        n = int(user32.GetWindowTextLengthW(wt.HWND(hwnd)) or 0)
        buf = ctypes.create_unicode_buffer(max(8, n + 2))
        user32.GetWindowTextW(wt.HWND(hwnd), buf, len(buf))
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def foreground_info() -> tuple[int, str]:
    fg = int(user32.GetForegroundWindow() or 0)
    return fg, window_title(fg)


def virtual_screen() -> tuple[int, int, int, int]:
    """虚拟桌面矩形 (x, y, w, h)——用来先挡住「点 (5000,9000) 也算成功」这种假回执。"""
    g = user32.GetSystemMetrics
    g.argtypes = [ctypes.c_int]
    g.restype = ctypes.c_int
    x, y = int(g(SM_XVIRTUALSCREEN)), int(g(SM_YVIRTUALSCREEN))
    w, h = int(g(SM_CXVIRTUALSCREEN)), int(g(SM_CYVIRTUALSCREEN))
    return x, y, w or 2560, h or 1440


def force_foreground(hwnd: int, tries: int = 3, settle_ms: int = 120) -> dict:
    """点击前的硬抢占：抢到才算数，抢不到把「真实前台是谁」原样带回来。

    顺序按实机验证过的那一套：
      ShowWindow(SW_RESTORE) → BringWindowToTop →
      AttachThreadInput(前台线程, 本线程) → SetForegroundWindow → SetActiveWindow → detach
    单纯 SetForegroundWindow 在"前台属于别的进程"时会被系统静默忽略——
    这就是之前回执写着前台是游戏、实际按键却落到 DSH 窗口的原因。
    """
    hwnd = int(hwnd or 0)
    if not hwnd:
        raise ValueError("缺少 hwnd：先 coach_anchor 锚定游戏窗口")
    user32.SetActiveWindow.argtypes = [wt.HWND]
    user32.SetActiveWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.AttachThreadInput.restype = wt.BOOL
    user32.BringWindowToTop.argtypes = [wt.HWND]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    tid_me = int(kernel32.GetCurrentThreadId())
    were = 0
    for _ in range(max(1, int(tries))):
        fg, fg_title = foreground_info()
        if fg == hwnd:
            return {"ok": True, "hwnd": hwnd, "actual": fg, "actualTitle": window_title(hwnd), "tries": were + 1}
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.18)
            tid_fg = int(user32.GetWindowThreadProcessId(wt.HWND(fg), None)) if fg else 0
            attached = False
            if tid_fg and tid_fg != tid_me:
                attached = bool(user32.AttachThreadInput(tid_fg, tid_me, True))
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(tid_fg, tid_me, False)
        except Exception:  # noqa: BLE001
            pass
        were += 1
        time.sleep(max(30, int(settle_ms)) / 1000.0)
        fg2, _t2 = foreground_info()
        if fg2 == hwnd:
            return {"ok": True, "hwnd": hwnd, "actual": fg2, "actualTitle": window_title(hwnd), "tries": were}
    fg3, title3 = foreground_info()
    return {"ok": False, "hwnd": hwnd, "actual": fg3, "actualTitle": title3, "tries": were,
            "error": f"抢不到前台：请求 {hwnd}（{window_title(hwnd) or '无标题'}），真实前台是 {fg3}（{title3 or '无标题'}）"}


def ensure_foreground(hwnd: int, restore: bool = True) -> int:
    """确认目标在前台；不在就尝试切过去（三条路），仍不行就报错。"""
    if not hwnd:
        raise ValueError("缺少 hwnd：把游戏窗口锚定一次再发操作")
    if user32.IsIconic(hwnd):
        if not restore:
            raise RuntimeError("窗口最小化了")
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.2)
    fg = int(user32.GetForegroundWindow() or 0)
    if fg == int(hwnd):
        return fg
    if not restore:
        raise RuntimeError(f"目标窗口不在前台（当前 {fg}），拒绝发送输入")
    for attempt in (_plain_foreground, _attach_foreground, _switch_foreground):
        try:
            attempt(hwnd)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.15)
        fg = int(user32.GetForegroundWindow() or 0)
        if fg == int(hwnd):
            return fg
    raise RuntimeError(f"切不到前台（当前 {fg}）——把游戏点一下，或改用 mode=\"post\" 的后台按键")


def _plain_foreground(hwnd: int) -> None:
    user32.SetForegroundWindow(hwnd)


def _attach_foreground(hwnd: int) -> None:
    """前台进程才有权 SetForegroundWindow：先把自己的输入队列挂到前台线程上。"""
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.AttachThreadInput.restype = wt.BOOL
    user32.BringWindowToTop.argtypes = [wt.HWND]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fg = int(user32.GetForegroundWindow() or 0)
    tid_fg = int(user32.GetWindowThreadProcessId(fg, None)) if fg else 0
    tid_me = int(kernel32.GetCurrentThreadId())
    attached = False
    try:
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)


def _switch_foreground(hwnd: int) -> None:
    user32.SwitchToThisWindow.argtypes = [wt.HWND, wt.BOOL]
    user32.SwitchToThisWindow(hwnd, True)


def _send(items: list[INPUT]) -> int:
    arr = (INPUT * len(items))(*items)
    return int(user32.SendInput(len(items), arr, ctypes.sizeof(INPUT)))


def press(key: str, hold_ms: int = 30) -> int:
    vk = resolve_key(key)
    down = INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=None)))
    up = INPUT(type=INPUT_KEYBOARD, u=INPUTUNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)))
    n = _send([down])
    time.sleep(max(10, hold_ms) / 1000.0)
    return n + _send([up])


def click(x: int, y: int, move_settle_ms: int = 250, down_up_ms: int = 80,
          button: str = "left") -> int:
    """屏幕绝对坐标左键单击（需要目标在前台）。

    为什么不是「移过去立刻按下」：游戏的按钮大多吃 hover——指针进了按钮区域要等一帧才变亮/变可点，
    不等这 250ms，点下去就落在"还没激活"的按钮上，回执一切正常但界面毫无反应（实机踩过）。
    """
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    ax = int(max(0, min(x, sw - 1)) * 65535 / max(1, sw - 1))
    ay = int(max(0, min(y, sh - 1)) * 65535 / max(1, sh - 1))
    move = INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(
        dx=ax, dy=ay, mouseData=0, dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, time=0, dwExtraInfo=None)))
    down = INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(
        dx=ax, dy=ay, mouseData=0, dwFlags=(MOUSEEVENTF_RIGHTDOWN if button == "right" else MOUSEEVENTF_LEFTDOWN) | MOUSEEVENTF_ABSOLUTE, time=0, dwExtraInfo=None)))
    up = INPUT(type=INPUT_MOUSE, u=INPUTUNION(mi=MOUSEINPUT(
        dx=ax, dy=ay, mouseData=0, dwFlags=(MOUSEEVENTF_RIGHTUP if button == "right" else MOUSEEVENTF_LEFTUP) | MOUSEEVENTF_ABSOLUTE, time=0, dwExtraInfo=None)))
    n = _send([move])
    time.sleep(max(0, int(move_settle_ms)) / 1000.0)
    n += _send([down])
    time.sleep(max(10, int(down_up_ms)) / 1000.0)
    return n + _send([up])


def SetForeground(hwnd: int) -> int:
    """尽量把某个窗口切到前台（三步兜底），返回切完后真实的前台窗口。"""
    for attempt in (_plain_foreground, _attach_foreground, _switch_foreground):
        try:
            attempt(int(hwnd))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
        cur = int(user32.GetForegroundWindow() or 0)
        if cur == int(hwnd):
            return cur
    return int(user32.GetForegroundWindow() or 0)


WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]


def post_key(hwnd: int, key: str) -> int:
    """后台按键：直接把消息投给窗口，不抢前台。有的引擎（含部分 Unity）照样收。"""
    vk = resolve_key(key)
    down = int(user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0x00000001))
    time.sleep(0.03)
    up = int(user32.PostMessageW(hwnd, WM_KEYUP, vk, 0xC0000001))
    return down + up


def post_click(hwnd: int, x: int, y: int) -> int:
    """后台点击：屏幕坐标换算成客户区坐标后投 WM_LBUTTONDOWN/UP。"""
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(origin))
    rc = wt.RECT()
    user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(rc))
    lx, ly = int(x) - origin.x, int(y) - origin.y
    if lx < 0 or ly < 0 or lx >= rc.right or ly >= rc.bottom:
        raise ValueError("点击点不在窗口客户区内")
    lparam = (ly << 16) | (lx & 0xFFFF)
    n = int(user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam))
    time.sleep(0.05)
    return n + int(user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam))


if __name__ == "__main__":
    print({"keys": sorted(ALLOWED), "foreground": int(user32.GetForegroundWindow() or 0)})
