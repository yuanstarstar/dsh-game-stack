# -*- coding: utf-8 -*-
"""这双手：SendInput 发鼠标键盘（纯 ctypes，不依赖 pyautogui）。

几条硬规矩，都是踩过的坑：

1. **坐标一律屏幕绝对坐标**。客户区→屏幕的换算在 `wincap.client_to_screen` 里，
   每帧现算——窗口被拖走后旧坐标就作废了。
2. **不用 mouse_event**。它已被 SendInput 取代；而且一次 SendInput 可以原子地发
   "移动+按下+抬起"，中间不会被别的输入插进来。
3. **绝对坐标是 0..65535 的归一化值**，按虚拟桌面（多显示器时以 SM_XVIRTUALSCREEN
   为原点）换算，否则副屏上会点到主屏去。
4. **打字走 KEYEVENTF_UNICODE**，直接送码点，不查键盘布局——中文/emoji 都能进，
   也不会因为输入法是中文而打出字母。
5. **按住的键必须能放开**：`release_all()` 记着当前按下的键，"停止"时全部抬起，
   否则用户会得到一个粘住的 Ctrl。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
WHEEL_DELTA = 120

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

ULONG_PTR = ctypes.POINTER(wt.ULONG)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int

# 按键名 → 虚拟键码。只收"玩游戏/点界面真用得上"的那些，不做任意 VK。
VK = {
    "back": 0x08, "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "lwin": 0x5B, "apps": 0x5D,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    VK[_c] = ord(_c.upper())
for _c in "`,.-/=[]\\;'":
    VK[_c] = ord(_c.upper())

_HELD: set[int] = set()


def _arr(*items):
    n = len(items)
    buf = (INPUT * n)()
    for i, it in enumerate(items):
        buf[i] = it
    return buf, n


def _mouse(dx, dy, data, flags):
    mi = MOUSEINPUT(int(dx), int(dy), int(data), int(flags), 0, None)
    return INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=mi))


def _key(vk=0, scan=0, flags=0):
    ki = KEYBDINPUT(int(vk), int(scan), int(flags), 0, None)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=ki))


def _send(*items) -> int:
    buf, n = _arr(*items)
    sent = int(user32.SendInput(n, buf, ctypes.sizeof(INPUT)))
    if sent != n:
        err = ctypes.get_last_error()
        raise RuntimeError(f"SendInput 只送进 {sent}/{n} 个事件（错误码 {err}）；"
                           "常见原因：目标窗口以管理员身份运行（UIPI 拦截）")
    return sent


def _virtual_desk():
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = max(1, user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    h = max(1, user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    return x, y, w, h


def move(x: float, y: float) -> None:
    """把鼠标挪到屏幕坐标（绝对）。"""
    vx, vy, vw, vh = _virtual_desk()
    ax = int(round((float(x) - vx) * 65535.0 / max(1, vw - 1)))
    ay = int(round((float(y) - vy) * 65535.0 / max(1, vh - 1)))
    ax = max(0, min(65535, ax))
    ay = max(0, min(65535, ay))
    _send(_mouse(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK))


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(x=None, y=None, button: str = "left", count: int = 1,
          hold_ms: int = 0, move_first: bool = True) -> None:
    """点一下（或双击/长按）。x、y 是**屏幕绝对坐标**。"""
    down, up = _BUTTONS.get(str(button or "left").lower(), _BUTTONS["left"])
    if x is not None and y is not None and move_first:
        move(x, y)
        time.sleep(0.012)                 # 让目标窗口先收到“鼠标进来了”
    for i in range(max(1, int(count))):
        _send(_mouse(0, 0, 0, down))
        if hold_ms:
            time.sleep(max(1, int(hold_ms)) / 1000.0)
        _send(_mouse(0, 0, 0, up))
        if i + 1 < count:
            time.sleep(0.05)              # 双击间隔要够长，否则被认成两次单击


def scroll(direction: str = "down", clicks: int = 3) -> None:
    sign = -1 if str(direction).lower() in ("down", "往下", "下") else 1
    for _ in range(max(1, int(clicks))):
        _send(_mouse(0, 0, sign * WHEEL_DELTA, MOUSEEVENTF_WHEEL))


def drag(x1, y1, x2, y2, button: str = "left", steps: int = 12, hold_ms: int = 20) -> None:
    """按下 → 分几步移动 → 抬起。分步是必要的：一步到位很多界面不认拖动。"""
    down, up = _BUTTONS.get(str(button or "left").lower(), _BUTTONS["left"])
    move(x1, y1)
    time.sleep(0.02)
    _send(_mouse(0, 0, 0, down))
    time.sleep(max(1, int(hold_ms)) / 1000.0)
    steps = max(2, int(steps))
    for i in range(1, steps + 1):
        move(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
        time.sleep(0.012)
    _send(_mouse(0, 0, 0, up))


def _parse_chord(chord: str):
    parts = [p.strip().lower() for p in str(chord or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError("按键是空的")
    vks = []
    for p in parts:
        if p not in VK:
            raise ValueError(f"不认识的按键「{p}」（支持字母/数字/方向/空格/回车/Esc/Tab/F1-F12 和 ctrl/alt/shift）")
        vks.append(VK[p])
    return vks


def key(chord: str = "space", repeat: int = 1, hold_ms: int = 0, delay_ms: int = 0) -> None:
    """按一个键或组合键，例如 "space" / "ctrl+shift+t"。"""
    vks = _parse_chord(chord)
    for _ in range(max(1, int(repeat))):
        for vk in vks:
            _send(_key(vk=vk, flags=0))
            _HELD.add(vk)
        if hold_ms:
            time.sleep(max(1, int(hold_ms)) / 1000.0)
        for vk in reversed(vks):
            _send(_key(vk=vk, flags=KEYEVENTF_KEYUP))
            _HELD.discard(vk)
        if delay_ms:
            time.sleep(max(0, int(delay_ms)) / 1000.0)


def type_text(text: str, delay_ms: int = 0) -> None:
    """打一段 Unicode 文本（中文也行）。按码点送，不受输入法影响。"""
    for ch in str(text or ""):
        cp = ord(ch)
        if cp > 0xFFFF:                    # 增补平面要拆成代理对
            cp -= 0x10000
            units = [0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF)]
        else:
            units = [cp]
        for u in units:
            _send(_key(scan=u, flags=KEYEVENTF_UNICODE))
            _send(_key(scan=u, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if delay_ms:
            time.sleep(max(0, int(delay_ms)) / 1000.0)


def release_all() -> int:
    """把还按着的键全抬起来（"停止"和异常退出时必做，否则 Ctrl 会粘住）。"""
    n = 0
    for vk in list(_HELD):
        try:
            _send(_key(vk=vk, flags=KEYEVENTF_KEYUP))
            n += 1
        except Exception:  # noqa: BLE001
            pass
    _HELD.clear()
    return n


def held() -> list:
    return sorted(_HELD)
