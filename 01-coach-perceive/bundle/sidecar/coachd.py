#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""coachd —— 游戏感知 sidecar（只读，不注入任何输入）。

把「截图 → 读懂盘面」压成纯本地、零 token 的循环：常驻 ffmpeg 视频流读帧，
调用时只做裁剪/差分/OCR，不再每次重启进程。

  GET  /health                      存活与能力探测
  GET  /windows                     可见窗口列表（hwnd / 标题 / 矩形 / 是否前台）
  POST /snapshot  {region, ocr?}    取当前帧（裁剪），返回尺寸/哈希/变化瓦片/可选 OCR
  POST /diff      {region}          只回答「相比上次调用，哪些格子变了」
  POST /ocr       {region|image}    中文 OCR，返回带坐标的文本行
  POST /frame     {region, path?}   区域存 PNG 落盘（供模型偶尔真看图）
  POST /watch     {region, ms}      在 ms 内等盘面「稳定」（不再变化）后返回，省掉轮询

依赖：ffmpeg（本机自带）、Pillow/numpy（本机自带）、Windows.Media.Ocr（经 PowerShell 桥）。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

# ---------------------------------------------------------------- 常量 / 工具

FFMPEG_CANDIDATES = [
    os.path.join(os.environ.get("USERPROFILE", ""), ".dsh", "tools", "ffmpeg", "bin", "ffmpeg.exe"),
    "ffmpeg",
]

OCR_PS = r"""
param([string]$Path)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $type) {
  $t = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  $t.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { Write-Output '{"error":"no-ocr-engine"}'; exit 0 }
$lang = $engine.RecognizerLanguage.LanguageTag

function Do-Ocr([string]$Path) {
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $stream.Dispose()
  $lines = New-Object System.Collections.ArrayList
  foreach ($line in $result.Lines) {
    $words = @($line.Words)
    if ($words.Count -eq 0) { continue }
    $r0 = $words[0].BoundingRect
    $minX = [double]$r0.X; $minY = [double]$r0.Y
    $maxX = $minX + [double]$r0.Width; $maxY = $minY + [double]$r0.Height
    for ($i = 1; $i -lt $words.Count; $i++) {
      $rr = $words[$i].BoundingRect
      $rx = [double]$rr.X; $ry = [double]$rr.Y; $rw = [double]$rr.Width; $rh = [double]$rr.Height
      if ($rx -lt $minX) { $minX = $rx }
      if ($ry -lt $minY) { $minY = $ry }
      if (($rx + $rw) -gt $maxX) { $maxX = $rx + $rw }
      if (($ry + $rh) -gt $maxY) { $maxY = $ry + $rh }
    }
    $null = $lines.Add([pscustomobject]@{
      text = [string]$line.Text
      x = [int][math]::Round($minX); y = [int][math]::Round($minY)
      w = [int][math]::Round($maxX - $minX); h = [int][math]::Round($maxY - $minY)
    })
  }
  return (@{ lang = $lang; lines = @($lines) } | ConvertTo-Json -Depth 4 -Compress)
}

if ($Path) {
  Write-Output (Do-Ocr $Path)
  exit 0
}

# 常驻模式：从 stdin 读一行一个 PNG 路径，写一行 JSON 结果。
$in = [Console]::In
while ($true) {
  $line = $in.ReadLine()
  if ($null -eq $line) { break }
  $line = $line.Trim()
  if ($line -eq '' ) { continue }
  if ($line -eq 'QUIT') { break }
  try {
    $json = Do-Ocr $line
  } catch {
    $json = (@{ error = [string]$_.Exception.Message } | ConvertTo-Json -Compress)
  }
  [Console]::Out.WriteLine($json)
  [Console]::Out.Flush()
}
"""


def find_ffmpeg() -> str:
    for c in FFMPEG_CANDIDATES:
        if c == "ffmpeg" or os.path.isfile(c):
            return c
    return "ffmpeg"


def _powershell() -> str:
    cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(cand):
        return cand
    return "powershell.exe"


FFMPEG = find_ffmpeg()


def enable_dpi_awareness() -> str:
    """不开 DPI 感知时 GetSystemMetrics 返回的是缩放后的逻辑像素（如 1707x1067），
    而 gdigrab 抓到的是物理像素（2560x1600）——帧按逻辑尺寸切会直接错位成花屏。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
        return "shcore/2"
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return "user32"
        except Exception:  # noqa: BLE001
            return "none"


DPI_MODE = enable_dpi_awareness()


class User32:
    def __init__(self) -> None:
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.u.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.u.GetSystemMetrics.restype = ctypes.c_int
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def screen_size(self) -> tuple[int, int]:
        return int(self.u.GetSystemMetrics(0)), int(self.u.GetSystemMetrics(1))

    def _exe_of(self, pid: int) -> str:
        try:
            h = self.k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(1024)
                if self.k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value)
            finally:
                self.k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass
        return ""

    def windows(self) -> list[dict]:
        u = self.u
        rows: list[dict] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        buf = ctypes.create_unicode_buffer(512)
        cls = ctypes.create_unicode_buffer(256)
        fg = int(u.GetForegroundWindow())

        def cb(hwnd, _lparam):
            if not u.IsWindowVisible(hwnd):
                return True
            if u.GetWindowTextW(hwnd, buf, 511) <= 0:
                return True
            rect = wt.RECT()
            if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 80 or h < 80:
                return True
            pid = wt.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            u.GetClassNameW(hwnd, cls, 255)
            rows.append(
                {
                    "hwnd": int(hwnd),
                    "title": buf.value,
                    "cls": cls.value,
                    "exe": self._exe_of(int(pid.value)),
                    "x": rect.left,
                    "y": rect.top,
                    "w": w,
                    "h": h,
                    "pid": int(pid.value),
                    "foreground": int(hwnd) == fg,
                }
            )
            return True

        u.EnumWindows(WNDENUMPROC(cb), 0)
        rows.sort(key=lambda r: (not r["foreground"], -(r["w"] * r["h"])))
        return rows


USER32 = User32()


def parse_region(spec) -> tuple[int, int, int, int] | None:
    """region: [x,y,w,h] / {x,y,w,h} / None(整屏)。"""
    if spec is None:
        return None
    if isinstance(spec, dict):
        vals = [spec.get("x"), spec.get("y"), spec.get("w"), spec.get("h")]
    elif isinstance(spec, (list, tuple)):
        vals = list(spec)
    else:
        raise ValueError("region 需要 [x,y,w,h] 或 {x,y,w,h}")
    if len(vals) != 4 or any(v is None for v in vals):
        raise ValueError("region 需要四个整数")
    x, y, w, h = (int(v) for v in vals)
    if w <= 0 or h <= 0:
        raise ValueError("region 宽高必须为正")
    return (x, y, w, h)


# ---------------------------------------------------------------- 常驻抓帧

class FrameSource:
    """常驻 ffmpeg gdigrab 管道，始终持有最新一帧（BGR ndarray）。"""

    def __init__(self, fps: int = 10) -> None:
        self.fps = fps
        self.width, self.height = USER32.screen_size()
        self.frame: np.ndarray | None = None
        self.seq = 0
        self.ts = 0.0
        self.error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="frames", daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 8
        while time.time() < deadline and self.frame is None and self.error is None:
            time.sleep(0.05)

    def stop(self) -> None:
        self._stop = True
        p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _spawn(self) -> subprocess.Popen:
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "gdigrab", "-framerate", str(self.fps), "-i", "desktop",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
        ]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    def _read_exact(self, out, need: int) -> bytes:
        """管道是 raw IO，read(n) 只返回当前可用字节，必须自己凑满一帧。"""
        chunks: list[bytes] = []
        got = 0
        while got < need:
            chunk = out.read(min(1 << 20, need - got))
            if not chunk:
                raise RuntimeError("ffmpeg 流结束")
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def _run(self) -> None:
        need = self.width * self.height * 3
        while not self._stop:
            proc = None
            try:
                proc = self._spawn()
                self._proc = proc
                out = proc.stdout
                assert out is not None
                self.error = None
                while not self._stop:
                    buf = self._read_exact(out, need)
                    arr = np.frombuffer(buf, dtype=np.uint8).reshape(self.height, self.width, 3)
                    with self._lock:
                        self.frame = arr
                        self.seq += 1
                        self.ts = time.time()
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                time.sleep(0.3)
            finally:
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass

    def latest(self, timeout: float = 6.0) -> tuple[np.ndarray, int, float]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self.frame is not None:
                    return self.frame, self.seq, self.ts
            if self.error:
                raise RuntimeError("抓帧不可用: " + self.error)
            time.sleep(0.02)
        raise RuntimeError("抓帧超时（ffmpeg 未在 %.1fs 内产出帧）" % timeout)


FRAMES = FrameSource()


# ---------------------------------------------------------------- 锚定（OBS / 窗口）
# 「看哪儿」不该每回合由模型猜坐标：先锚定一次，之后所有感知默认看这块区域。

OBS_CONF = {
    "host": os.environ.get("COACH_OBS_HOST", "127.0.0.1"),
    "port": int(os.environ.get("COACH_OBS_PORT", "4455")),
    "password": os.environ.get("COACH_OBS_PASSWORD", ""),
}

ANCHOR: dict | None = None
ANCHOR_LOCK = threading.Lock()
ANCHOR_PATH = os.path.join(os.path.expanduser("~"), ".dsh", "coach-perceive", "anchor.json")


def save_anchor(a: dict | None) -> None:
    """锚定落盘：sidecar 重启（每次 DSH 启动都会重开）后不用重新锚。"""
    try:
        os.makedirs(os.path.dirname(ANCHOR_PATH), exist_ok=True)
        tmp = ANCHOR_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(a, fh, ensure_ascii=False)
        os.replace(tmp, ANCHOR_PATH)
    except Exception:  # noqa: BLE001
        pass


def load_anchor() -> dict | None:
    try:
        with open(ANCHOR_PATH, encoding="utf-8") as fh:
            a = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(a, dict) or not a.get("kind"):
        return None
    if a["kind"] == "rect":
        return a if a.get("rect") else None
    row = None
    try:
        row = find_window(hwnd=a.get("hwnd") or 0)
        if row is None:
            row = find_window(title=a.get("title") or "", exe=a.get("exe") or "")
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        return None
    a.update({
        "hwnd": row["hwnd"], "title": row["title"], "exe": row["exe"],
        "rect": _clip_rect(row["x"], row["y"], row["w"], row["h"]),
        "restored": True,
    })
    return a


def _clip_rect(x: int, y: int, w: int, h: int) -> list[int]:
    sw, sh = USER32.screen_size()
    x = max(0, min(int(x), sw - 1))
    y = max(0, min(int(y), sh - 1))
    w = max(1, min(int(w), sw - x))
    h = max(1, min(int(h), sh - y))
    return [x, y, w, h]


def find_window(title: str = "", hwnd: int | None = None, cls: str = "", exe: str = "") -> dict | None:
    rows = USER32.windows()
    if hwnd:
        for r in rows:
            if r["hwnd"] == int(hwnd):
                return r
        return None
    t = (title or "").strip()
    c = (cls or "").strip()
    e = (exe or "").strip().lower()
    for pred in (
        lambda r: t and r["title"] == t,
        lambda r: t and r["title"].startswith(t),
        lambda r: t and t in r["title"],
        lambda r: c and r["cls"] == c and (not e or r["exe"].lower() == e),
        lambda r: c and r["cls"] == c,
        lambda r: e and r["exe"].lower() == e,
    ):
        for r in rows:
            try:
                if pred(r):
                    return r
            except Exception:  # noqa: BLE001
                continue
    return None


def anchor_from_obs(source: str = "", host: str = "", port: int | None = None, password: str = "", window: str = "") -> dict:
    from obsclient import ObsClient, ObsError, list_capture_inputs, window_spec_of

    cli = ObsClient(host or OBS_CONF["host"], port or OBS_CONF["port"], password or OBS_CONF["password"])
    try:
        cli.connect()
        caps = list_capture_inputs(cli)
        if not caps:
            raise ObsError("OBS 里没有 窗口采集/游戏采集/显示器采集 源：先加一个采集源再锚定")
        picked = None
        if source:
            for c in caps:
                if c["name"] == source:
                    picked = c
                    break
            if picked is None:
                names = "、".join(c["name"] for c in caps)
                raise ObsError(f"OBS 里没有名为「{source}」的采集源。可选：{names}")
        else:
            picked = caps[0]

        retargeted = None
        if window:
            target = find_window(title=window)
            if target is None:
                visible = "、".join(r["title"][:30] for r in USER32.windows()[:10] if r["title"])
                raise ObsError(f"没找到窗口「{window}」（先用 coach_windows 看清单）。当前可见：{visible}")
            spec = f"{target['title']}:{target['cls']}:{target['exe']}"
            cli.request("SetInputSettings", {
                "inputName": picked["name"],
                "inputSettings": {"window": spec, "method": 2},
                "overlay": True,
            })
            time.sleep(0.6)
            retargeted = target
            info = {
                "kind": picked["kind"],
                "windowSpec": spec,
                "title": target["title"],
                "class": target["cls"],
                "exe": target["exe"],
                "monitor": None,
            }
        else:
            info = window_spec_of(cli, picked["name"])
        scene = cli.request("GetSceneList")
        base = {
            "kind": "obs",
            "source": picked["name"],
            "sourceKind": picked["kind"],
            "scene": scene.get("currentProgramSceneName"),
            "windowSpec": info["windowSpec"],
            "retargeted": bool(retargeted),
            "at": time.time(),
        }
        if picked["kind"] in ("monitor_capture", "display_capture") or (not info["title"] and info["monitor"] is not None):
            sw, sh = USER32.screen_size()
            row = None
            return {**base, "mode": "monitor", "title": f"显示器 {info['monitor']}", "rect": [0, 0, sw, sh], "hwnd": row}
        row = find_window(title=info["title"], cls=info["class"], exe=info["exe"])
        if row is None:
            raise ObsError(
                "OBS 采集源对应的窗口没找到（窗口可能最小化/已关闭）。"
                f"OBS 记录的窗口：title={info['title']!r} class={info['class']!r} exe={info['exe']!r}"
            )
        return {
            **base,
            "mode": "window",
            "hwnd": row["hwnd"],
            "title": row["title"],
            "exe": row["exe"],
            "rect": _clip_rect(row["x"], row["y"], row["w"], row["h"]),
        }
    finally:
        cli.close()


def anchor_from_window(title: str = "", hwnd: int | None = None) -> dict:
    row = find_window(title=title, hwnd=hwnd)
    if row is None:
        raise ValueError(f"没找到窗口：{title or hwnd}（先用 coach_windows 看清单）")
    return {
        "kind": "window",
        "mode": "window",
        "hwnd": row["hwnd"],
        "title": row["title"],
        "exe": row["exe"],
        "rect": _clip_rect(row["x"], row["y"], row["w"], row["h"]),
        "at": time.time(),
    }


def current_anchor_region() -> list[int] | None:
    global ANCHOR
    a = ANCHOR
    if not a:
        return None
    if a.get("hwnd"):
        row = find_window(hwnd=a["hwnd"])
        if row is not None:
            rect = _clip_rect(row["x"], row["y"], row["w"], row["h"])
            if rect != a.get("rect"):
                with ANCHOR_LOCK:
                    if ANCHOR is not None:
                        ANCHOR = {**ANCHOR, "rect": rect}
            return rect
    return a.get("rect")


def resolve_region(spec):
    """region 语义：None/'anchor' → 锚定区域；'screen' → 强制全屏；'foreground' → 真正在前台的那块画面。

    'foreground' 以前由 host 端解析成"第一个前台窗口的矩形"，结果抓到过 8x546 的一条边——
    前台窗口不一定是游戏（DSH 自己、Nahimic 浮层都在前台过）。这里改成：
    **锚定过就锚定窗口为准**（那才是"画面"），没锚定才退到真正的前台窗口。
    """
    if spec == "screen":
        return None
    if spec == "foreground":
        a = ANCHOR if isinstance(ANCHOR, dict) else None
        if a and a.get("hwnd") and _WC is not None:
            try:
                cx, cy, cw, ch = _WC.client_rect(int(a["hwnd"]))
                return [int(cx), int(cy), int(cw), int(ch)]
            except Exception:  # noqa: BLE001
                pass
        try:
            for w in USER32.windows():
                if w.get("foreground"):
                    r = w["rect"]
                    return [int(r[0]), int(r[1]), int(r[2]), int(r[3])]
        except Exception:  # noqa: BLE001
            pass
        return None
    if spec in (None, "", "anchor"):
        return current_anchor_region()
    return spec


MIN_REGION_SIDE = 64


def check_region(spec, min_side: int = MIN_REGION_SIDE) -> tuple[int, int, int, int] | None:
    """挡住"8x546 这种细条"：区域小到不像画面就报错，并把候选窗口列出来。"""
    r = parse_region(resolve_region(spec))
    if r is None:
        return None
    if r[2] < min_side or r[3] < min_side:
        try:
            cands = [f'{w["title"][:28]}（{w["w"]}x{w["h"]} hwnd {w["hwnd"]}）' for w in USER32.windows()[:6]]
        except Exception:  # noqa: BLE001
            cands = []
        raise ValueError(
            f"区域太小：{r[2]}x{r[3]}（至少要 {min_side}px 见方）。这通常是 region 指到了某个细条窗口。"
            f"改用 region:\"anchor\"（先 coach_anchor 锚定游戏）或直接给 [x,y,w,h]。候选窗口：{'; '.join(cands)}"
        )
    return r



# ---------------------------------------------------------------- 裁剪 / 差分

def crop(frame: np.ndarray, region) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    h, w = frame.shape[:2]
    r = parse_region(region)
    if r is None:
        return frame, (0, 0, w, h)
    x, y, rw, rh = r
    x = max(0, min(x, w - 1))
    y = max(0, min(y, h - 1))
    rw = max(1, min(rw, w - x))
    rh = max(1, min(rh, h - y))
    return frame[y : y + rh, x : x + rw], (x, y, rw, rh)


def tile_sig(img: np.ndarray, tile: int) -> np.ndarray:
    h, w = img.shape[:2]
    rows = max(1, h // tile)
    cols = max(1, w // tile)
    a = img[: rows * tile, : cols * tile].astype(np.uint16)
    blocks = a.reshape(rows, tile, cols, tile, 3).mean(axis=(1, 3))
    return (blocks / 8).astype(np.uint16)


def to_pil(img: np.ndarray) -> Image.Image:
    return Image.fromarray(img[:, :, ::-1])  # BGR -> RGB


# --------------------------------------------------- 抓帧：窗口锚定优先 PrintWindow
# gdigrab 抓的是屏幕：DSH 一挡在游戏前面，收到的就是 DSH 的界面。
# 锚定到窗口后改用 PrintWindow(PW_RENDERFULLCONTENT)，即使被遮挡也能拿到游戏画面，
# 而且只抓客户区（没有标题栏）。抓不到时自动退回屏幕裁剪。

try:
    import win_capture as _WC
except Exception:  # noqa: BLE001
    _WC = None

try:
    import win_input as INPUT
    INPUT_OK = True
except Exception:  # noqa: BLE001
    INPUT = None
    INPUT_OK = False

INPUT_LOG = os.path.join(os.path.dirname(ANCHOR_PATH), "input-log.jsonl")


def log_input(rec: dict) -> None:
    try:
        os.makedirs(os.path.dirname(INPUT_LOG), exist_ok=True)
        with open(INPUT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass

_WIN_CACHE: dict = {"hwnd": 0, "ts": 0.0, "img": None}


def _win_frame(hwnd: int, ttl: float = 0.05) -> np.ndarray | None:
    if _WC is None:
        return None
    now = time.time()
    cached = _WIN_CACHE
    if cached["hwnd"] == hwnd and cached["img"] is not None and now - cached["ts"] < ttl:
        return cached["img"]
    img = _WC.capture_client(hwnd)
    if img is None or img.size == 0:
        return None
    _WIN_CACHE.update({"hwnd": hwnd, "ts": now, "img": img})
    return img


def grab(spec=None) -> tuple[np.ndarray, tuple[int, int, int, int], int, float, str]:
    """返回 (图, 区域, 帧序号, 帧时间, 来源)。窗口锚定时走 PrintWindow。"""
    a = ANCHOR if isinstance(ANCHOR, dict) else None
    hwnd = int(a.get("hwnd") or 0) if a else 0
    if hwnd and _WC is not None:
        try:
            img = _win_frame(hwnd)
            if img is not None:
                cx, cy, cw, ch = _WC.client_rect(hwnd)
                r = parse_region(resolve_region(spec))
                if r is None:
                    rect = (cx, cy, int(cw), int(ch))
                    return img, rect, FRAMES.seq, time.time(), "printwindow"
                # 区域用屏幕坐标给，内部换算成客户区坐标再裁，回传的仍是屏幕坐标
                x, y, w, h = r
                ix, iy = max(x, cx), max(y, cy)
                ix2, iy2 = min(x + w, cx + cw), min(y + h, cy + ch)
                if ix2 - ix >= 4 and iy2 - iy >= 4:
                    out, _ = crop(img, [ix - cx, iy - cy, ix2 - ix, iy2 - iy])
                    return out, (ix, iy, ix2 - ix, iy2 - iy), FRAMES.seq, time.time(), "printwindow"
        except Exception:  # noqa: BLE001
            pass
    frame, seq, ts = FRAMES.latest()
    img, rect = crop(frame, resolve_region(spec))
    return img, rect, seq, ts, "screen"


class DiffState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last: dict[tuple, np.ndarray] = {}

    def compare(self, key, sig: np.ndarray) -> tuple[bool, np.ndarray | None]:
        with self.lock:
            prev = self.last.get(key)
            self.last[key] = sig
        if prev is None or prev.shape != sig.shape:
            return True, None
        return False, (np.abs(sig.astype(int) - prev.astype(int)).sum(axis=2) > 0)


DIFFS = DiffState()


# ---------------------------------------------------------------- OCR 桥

_OCR_SCRIPT: str | None = None
_OCR_LOCK = threading.Lock()
_OCR_WORKER: subprocess.Popen | None = None
_OCR_WORKER_DEAD = False


def _ocr_script() -> str:
    global _OCR_SCRIPT
    if _OCR_SCRIPT is None:
        fd, path = tempfile.mkstemp(prefix="coachd-ocr-", suffix=".ps1")
        os.close(fd)
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write(OCR_PS)
        _OCR_SCRIPT = path
    return _OCR_SCRIPT


def _ocr_worker() -> subprocess.Popen | None:
    """常驻 PowerShell OCR 进程：每调一次都要付 ~800ms 的 PS 启动 + WinRT 初始化，
    常驻后每次只剩几十毫秒的识别时间——这是「省事件」里很实在的一笔。"""
    global _OCR_WORKER, _OCR_WORKER_DEAD
    if _OCR_WORKER_DEAD:
        return None
    if _OCR_WORKER is not None and _OCR_WORKER.poll() is None:
        return _OCR_WORKER
    try:
        _OCR_WORKER = subprocess.Popen(
            [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _ocr_script()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        return _OCR_WORKER
    except Exception:  # noqa: BLE001
        _OCR_WORKER_DEAD = True
        return None


def _ocr_once(png: str) -> tuple[dict, int]:
    t0 = time.time()
    p = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _ocr_script(), "-Path", png],
        capture_output=True,
        timeout=90,
    )
    ms = int((time.time() - t0) * 1000)
    out = p.stdout.decode("utf-8", "replace").strip()
    try:
        data = json.loads(out)
    except Exception:  # noqa: BLE001
        raise RuntimeError("OCR 桥输出无法解析: " + (out or p.stderr.decode("utf-8", "replace"))[:300])
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError("OCR 不可用: " + str(data["error"]))
    return data, ms


def _read_line_with_timeout(worker: subprocess.Popen, timeout: float = 30.0) -> bytes:
    box: dict = {}

    def read() -> None:
        try:
            box["line"] = worker.stdout.readline()
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise RuntimeError("OCR worker 超时")
    if "err" in box:
        raise box["err"]
    return box.get("line") or b""


def ocr_pil(img: Image.Image, scale: float = 1.0) -> dict:
    """OCR 一张图。scale>1 先放大再识别——游戏 UI 的小字放大 1.5~2 倍认得明显更准。"""
    global _OCR_WORKER_DEAD
    s = float(scale or 1.0)
    if s <= 0:
        s = 1.0
    if s != 1.0:
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    fd, png = tempfile.mkstemp(prefix="coachd-ocr-", suffix=".png")
    os.close(fd)
    try:
        img.save(png)
        out = _ocr_dispatch(png)
        out["scale"] = s
        if s != 1.0:
            lines = out.get("lines") or []
            if isinstance(lines, dict):
                lines = [lines]
            for ln in lines:
                for k in ("x", "y", "w", "h"):
                    if isinstance(ln.get(k), (int, float)):
                        ln[k] = int(round(ln[k] / s))
        return out
    finally:
        try:
            os.unlink(png)
        except OSError:
            pass


def _ocr_dispatch(png: str) -> dict:
    """worker 优先，挂了退回一次性调用。"""
    global _OCR_WORKER_DEAD
    with _OCR_LOCK:
        worker = _ocr_worker()
        if worker is not None:
            try:
                t0 = time.time()
                worker.stdin.write((png + "\n").encode("utf-8"))
                worker.stdin.flush()
                line = _read_line_with_timeout(worker, 30.0)
                if not line:
                    raise RuntimeError("OCR worker 无输出")
                data = json.loads(line.decode("utf-8", "replace"))
                if data.get("error"):
                    raise RuntimeError(str(data["error"]))
                data["ms"] = int((time.time() - t0) * 1000)
                data["engine"] = "worker"
                return data
            except Exception:  # noqa: BLE001
                _OCR_WORKER_DEAD = True
                try:
                    worker.kill()
                except Exception:  # noqa: BLE001
                    pass
        data, ms = _ocr_once(png)
        data["ms"] = ms
        data["engine"] = "oneshot"
        return data


CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def clean_text(text: str) -> str:
    """Windows OCR 会在汉字之间塞空格（"百 度 一 下"），中文里这种空格没有意义。"""
    out = text
    prev = None
    while prev != out:
        prev = out
        out = re.sub(r"(?<=[\u3400-\u9fff\uf900-\ufaff])\s+(?=[\u3400-\u9fff\uf900-\ufaff])", "", out)
    return out.strip()


def normalize_lines(ocr: dict) -> list[dict]:
    lines = ocr.get("lines") or []
    if isinstance(lines, dict):
        lines = [lines]
    out = []
    for ln in lines:
        text = clean_text(str(ln.get("text") or ""))
        if not text:
            continue
        out.append(
            {
                "text": text,
                "x": int(ln.get("x") or 0),
                "y": int(ln.get("y") or 0),
                "w": int(ln.get("w") or 0),
                "h": int(ln.get("h") or 0),
            }
        )
    return out


def _ocr_scale(body: dict) -> float:
    """ocrScale 范围保护，默认 2.0：游戏 UI 的小字放大两倍认得更全（实测多认出被漏掉的「罗兰」）。"""
    try:
        return max(1.0, min(3.0, float(body.get("ocrScale") or 2.0)))
    except (TypeError, ValueError):
        return 2.0


def abs_lines(lines: list[dict], rect) -> list[dict]:
    """OCR 坐标是相对裁切图的，加上区域偏移换成屏幕绝对坐标——这样可以直接照着坐标点。"""
    if not rect:
        return lines
    ox, oy = int(rect[0]), int(rect[1])
    if ox == 0 and oy == 0:
        return lines
    return [{**ln, "x": ln["x"] + ox, "y": ln["y"] + oy} for ln in lines]


def cluster_boxes(changed: np.ndarray, tile: int, limit: int = 24) -> list[list[int]]:
    """把变化的格子并成矩形块（4 邻域），比逐个格子列坐标省很多 token。"""
    h, w = changed.shape
    seen = np.zeros_like(changed, dtype=bool)
    boxes = []
    ys, xs = np.nonzero(changed)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        y0 = y1 = sy
        x0 = x1 = sx
        n = 0
        while stack:
            cy, cx = stack.pop()
            n += 1
            if cy < y0: y0 = cy
            if cy > y1: y1 = cy
            if cx < x0: x0 = cx
            if cx > x1: x1 = cx
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w and changed[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        boxes.append((n, [x0 * tile, y0 * tile, (x1 - x0 + 1) * tile, (y1 - y0 + 1) * tile]))
    boxes.sort(key=lambda b: -b[0])
    return [b[1] for b in boxes[:limit]]


# ------------------------------------------------------- 护栏 / 场景证据 / 稳定 / UI dump
#
# 这一段的每一条都来自事故复盘（废墟图书馆实机）：
#  · 战斗界面按 space = 自动结算，一张牌没出，整场接待被空转掉 —— 所以要护栏，而不是"建议别按"。
#  · 背景是粒子/流水动画，逐格"有变化就算变"会把静止战斗报成每帧 400~850 格 —— 所以要单格色差阈值。
#  · 描边字 OCR 读不出，猜 (x,y) 十次错八次 —— 所以要 ui_dump 给 id，按 id 点。

DANGER_KEYS = {"space", "enter", "return"}
GUARDED_SCENES = {"battle", "battle_setup", "result"}
DANGER_NOTE = {
    "space": "space 在这个场景是「自动推进/自动结算」，会替你把整场打完",
    "enter": "enter 在这个场景是「确认并推进」，可能直接把整轮结算掉",
}
CELL_DIFF = 14.0          # 单格 BGR 绝对差之和超过它才算"这一格变了"（用于点完看有没有反应）
STABLE_DIFF = 60.0        # 判断"静止"时用这个更宽的门：背景粒子/水波每帧都在抖（实测单格最大 29）
STABLE_ANIM_TILES = 24    # 一帧里超过这么多格在变，就认为还在动
UI_DIRTY = 0.72           # 元素亮度低于全屏中位数的这个比例 → 判为置灰
COACH_HOME = os.path.dirname(ANCHOR_PATH)
SCENES_PATH = os.path.join(COACH_HOME, "scenes.json")

SCENE_HINTS = {
    "result": ("结算", "战利品", "接待结果", "获得书页", "战斗胜利", "战斗失败", "重新开始"),
    "battle": ("拼点", "速度", "骰子", "命中", "闪避", "反击", "混乱", "情感值", "情感等级", "行动", "目标", "自动"),
    "battle_setup": ("舞台", "编成", "开启舞台", "司书", "书页", "推荐等级", "出战", "上阵"),
    "invite": ("邀请函", "发送邀请", "今日书籍", "登记", "邀请"),
    "tutorial": ("教学", "操作说明", "如何操作", "点击这里", "试试看"),
    "pause": ("继续游戏", "返回标题画面", "回到标题", "设置"),
    "title": ("废墟图书馆", "开始游戏", "新游戏"),
    "loading": ("加载中", "请稍候", "Loading"),
    "choice": ("确定", "取消", "选择", "选项", "是否"),
    "menu": ("菜单", "载入", "手册"),
    "dialog": ("……", "——"),
}
SCENE_ORDER = ["result", "battle", "battle_setup", "invite", "tutorial", "pause", "title",
               "loading", "choice", "dialog", "menu"]
SCENE_ZH = {"result": "结算", "battle": "战斗", "battle_setup": "舞台编成", "invite": "邀请函",
            "tutorial": "教学浮层", "pause": "暂停菜单", "title": "标题画面", "loading": "加载",
            "choice": "选项", "dialog": "剧情对话", "menu": "菜单", "unknown": "没认出来"}


def _cell_means(img: np.ndarray, tile: int = 32) -> np.ndarray:
    """按格取平均色（float）：差分要能分辨"UI 变了"和"背景粒子抖了一下"。"""
    h, w = img.shape[:2]
    rows, cols = max(1, h // tile), max(1, w // tile)
    a = img[: rows * tile, : cols * tile].astype(np.float32)
    return a.reshape(rows, tile, cols, tile, 3).mean(axis=(1, 3))


def changed_cells(prev: np.ndarray | None, cur: np.ndarray | None, thr: float = CELL_DIFF) -> np.ndarray | None:
    if prev is None or cur is None or prev.shape != cur.shape:
        return None
    return np.abs(cur - prev).sum(axis=2) > float(thr)


def _mask_rect(mask: np.ndarray, rect, tile: int, region) -> np.ndarray:
    """把"永远别算变化"的矩形抹掉（例如自带的时钟/粒子带）。rect 用屏幕坐标。"""
    out = mask.copy()
    rows, cols = out.shape
    ox, oy = int(region[0]), int(region[1])
    x0 = int((rect[0] - ox) // tile)
    y0 = int((rect[1] - oy) // tile)
    x1 = int((rect[0] + rect[2] - ox) // tile) + 1
    y1 = int((rect[1] + rect[3] - oy) // tile) + 1
    out[max(0, y0): max(0, min(rows, y1)), max(0, x0): max(0, min(cols, x1))] = False
    return out


def classify_scene_scored(lines: list[dict]) -> dict:
    """场景 + 置信度 + 证据。没有证据就说 unknown，不硬猜。"""
    if not lines:
        return {"scene": "unknown", "confidence": 0.0, "evidence": [], "hits": {}}
    text = " ".join(str(ln.get("text") or "") for ln in lines)
    hits = {k: [w for w in ws if w in text] for k, ws in SCENE_HINTS.items()}
    counts = {k: len(v) for k, v in hits.items()}
    best_n = max(counts.values()) if counts else 0
    if best_n <= 0:
        return {"scene": "unknown", "confidence": 0.0, "evidence": [], "hits": counts}
    scene = next(s for s in SCENE_ORDER if counts.get(s, 0) == best_n)
    strong = {s for s in SCENE_ORDER if counts.get(s, 0) >= 2}
    # 两条以上证据才有底气；另一条候选也很强时降置信度
    conf = min(0.95, 0.55 + 0.15 * best_n) if best_n >= 2 else 0.40
    if len(strong) > 1:
        conf = min(conf, 0.60)
    return {"scene": scene, "confidence": round(conf, 2), "evidence": hits[scene][:6], "hits": counts}


def classify_scene(lines: list[dict]) -> str:
    """粗分场景（兼容旧调用）：dialog / choice / battle / result / battle_setup / menu / unknown …"""
    return classify_scene_scored(lines)["scene"]


def hand_cards(img: np.ndarray, band: tuple[float, float] = (0.66, 0.93), tile: int = 8,
               min_w: int = 40, max_w: int = 170, min_runs: int = 3) -> dict:
    """底部那一排手牌：OCR 读不出字的时候，这是"这是战斗"最硬的证据。

    废墟图书馆的战斗界面：底部 70%~92% 有一条横带，里面并排列着 3~5 张等宽卡牌。
    实测（1362x776 客户区）：4 张、每张 72px 宽、x 从 424 起、间距 80 —— 肉眼和代码看到的一致。
    """
    h, w = img.shape[:2]
    y0, y1 = int(h * band[0]), int(h * band[1])
    if y1 - y0 < 24:
        return {"runs": [], "count": 0}
    band_img = img[y0:y1, :].astype(np.float32)
    rows, cols = (y1 - y0) // tile, w // tile
    if rows < 3 or cols < 8:
        return {"runs": [], "count": 0}
    lum = band_img[: rows * tile, : cols * tile].reshape(rows, tile, cols, tile, 3).mean(axis=(1, 3)).mean(axis=2)
    thr = float(np.percentile(lum, 80))
    if thr < 60:  # 整条带都黑，别硬找
        return {"runs": [], "count": 0, "thr": round(thr, 1)}
    bright = lum > thr
    colcnt = bright.sum(axis=0)
    need = max(3, int(rows * 0.35))
    runs: list[list[int]] = []
    start = None
    for x in range(cols):
        if colcnt[x] >= need:
            if start is None:
                start = x
        else:
            if start is not None:
                runs.append([start * tile, (x - start) * tile])
                start = None
    if start is not None:
        runs.append([start * tile, (cols - start) * tile])
    raw = len(runs)
    runs = [r for r in runs if min_w <= r[1] <= max_w]
    # 手牌是"等宽并排"：把宽度离群的（背景亮条、右边缘装饰）去掉
    if len(runs) >= 2:
        med = float(np.median([r[1] for r in runs]))
        runs = [r for r in runs if 0.6 * med <= r[1] <= 1.6 * med]
    return {"runs": runs, "count": len(runs), "raw": raw, "band": [y0, y1], "thr": round(thr, 1),
            "ok": len(runs) >= min_runs}


def scene_visual(img: np.ndarray) -> dict | None:
    """不靠文字的视觉场景证据。只有有把握才返回，拿不准就 None（不猜）。"""
    cards = hand_cards(img)
    if cards.get("ok"):
        xs = ", ".join(f"{r[0]}+{r[1]}" for r in cards["runs"][:5])
        return {"scene": "battle", "confidence": 0.78,
                "evidence": [f"底部手牌 {cards['count']} 张（{xs}）"],
                "detail": cards, "suggestStop": True}
    return None


def scene_probe(img: np.ndarray) -> str:
    """场景判定的唯一入口：关键词证据 + 视觉证据，谁有把握听谁的。"""
    scored = {"scene": "unknown", "confidence": 0.0, "evidence": []}
    try:
        pil = to_pil(img)
        w, h = pil.size
        res = ocr_pil(pil.crop((0, int(h * 0.5), w, h)), 1.0)
        scored = classify_scene_scored(normalize_lines(res))
    except Exception:  # noqa: BLE001
        pass
    vis = scene_visual(img)
    if vis and (scored["scene"] == "unknown" or (scored["confidence"] < 0.7 and scored["scene"] not in GUARDED_SCENES)):
        return vis["scene"]
    if scored["scene"] != "unknown":
        return scored["scene"]
    return vis["scene"] if vis else "unknown"


def scene_with_evidence(img: np.ndarray, region, body: dict | None = None) -> dict:
    """抓一帧就给出 {scene, confidence, evidence, animating}（关键词 + 视觉结构两路证据）。"""
    body = body or {}
    pil = to_pil(img)
    w, h = pil.size
    top = int(h * float(body.get("bandFrom") or 0.0))
    res = ocr_pil(pil.crop((0, top, w, h)), _ocr_scale(body))
    lines = normalize_lines(res)
    for ln in lines:
        ln["x"] += int(region[0])
        ln["y"] += int(region[1]) + top
    out = classify_scene_scored(lines)
    vis = scene_visual(img)
    if vis and (out["scene"] == "unknown" or (out["confidence"] < 0.7 and out["scene"] not in GUARDED_SCENES)):
        out = {"scene": vis["scene"], "confidence": vis["confidence"],
               "evidence": vis["evidence"] + list(out["evidence"]),
               "hits": out.get("hits", {}), "sceneSource": "visual", "visual": vis["detail"]}
    out["lines"] = lines[: int(body.get("maxLines") or 12)]
    out["ocrMs"] = res.get("ms")
    out["sceneZh"] = SCENE_ZH.get(out["scene"], out["scene"])
    out["textReadable"] = bool(lines)
    return out


def animating_now(prev_cells: np.ndarray | None, cur_cells: np.ndarray | None) -> bool:
    m = changed_cells(prev_cells, cur_cells, CELL_DIFF)
    if m is None:
        return False
    return int(m.sum()) > STABLE_ANIM_TILES


def guard_key(key: str, scene: str, force: bool = False) -> dict:
    """危险键闸门：返回 {} 表示放行，否则返回拒绝回执。"""
    k = str(key or "").strip().lower()
    if force or k not in DANGER_KEYS or scene not in GUARDED_SCENES:
        return {}
    return {
        "refused": True,
        "guard": "danger_key",
        "key": k,
        "scene": scene,
        "sceneZh": SCENE_ZH.get(scene, scene),
        "reason": f"{SCENE_ZH.get(scene, scene)} 场景里「{k}」是危险键：{DANGER_NOTE.get(k, '会自动推进')}。已阻止，画面没有被按过。",
        "howTo": "确实要按，给 force:true；想精确操作，用 coach_ui_dump 拿 id 再 coach_click{element}。",
    }


def guard_table() -> dict:
    """coach_guard 的回执：哪些键在哪些场景会被拦。"""
    rows = []
    for scene in ["battle", "battle_setup", "result", "invite", "dialog", "choice", "pause", "title"]:
        for key in sorted(DANGER_KEYS):
            rows.append({
                "scene": scene, "sceneZh": SCENE_ZH.get(scene, scene), "key": key,
                "verdict": "refuse" if scene in GUARDED_SCENES else "allow",
                "note": DANGER_NOTE.get(key, "") if scene in GUARDED_SCENES else "这个场景里没有已知的自杀式语义",
            })
    return {"table": rows, "force": "任何一条都能用 force:true 绕过（绕过会写进 input-log）"}


def fg_now() -> dict:
    """真实前台（窗口 + 标题）。回执必须写它，不能只写"我请求的是谁"。"""
    try:
        fg, title = INPUT.foreground_info()
    except Exception:  # noqa: BLE001
        fg, title = 0, ""
    return {"hwnd": fg, "title": title}


def _element_id(i: int) -> str:
    return f"ui_{i}"


def _blockify(lines: list[dict], gap_y: int = 10) -> list[dict]:
    """把 OCR 行并成块：同一行的碎片先横着合，再竖着合相邻行。"""
    if not lines:
        return []
    rows = sorted(lines, key=lambda ln: (ln["y"], ln["x"]))
    merged: list[dict] = []
    for ln in rows:
        placed = False
        for b in merged:
            if abs((ln["y"] + ln["h"] / 2) - (b["y"] + b["h"] / 2)) <= max(6, b["h"] * 0.6):
                x2 = max(b["x"] + b["w"], ln["x"] + ln["w"])
                b["x"] = min(b["x"], ln["x"])
                b["y"] = min(b["y"], ln["y"])
                b["w"] = x2 - b["x"]
                b["h"] = max(b["y"] + b["h"], ln["y"] + ln["h"]) - b["y"]
                b["text"] = (b["text"] + " " + ln["text"]).strip()
                placed = True
                break
        if not placed:
            merged.append(dict(ln))
    # 竖着再合一次：同一块面板里换行写的标题/正文
    merged.sort(key=lambda b: b["y"])
    out: list[dict] = []
    for b in merged:
        if out:
            p = out[-1]
            same_col = abs(b["x"] - p["x"]) <= max(16, p["w"] * 0.2)
            close_y = 0 <= (b["y"] - (p["y"] + p["h"])) <= max(8, gap_y)
            if same_col and close_y and len(p["text"]) < 40:
                p["text"] = (p["text"] + " " + b["text"]).strip()
                p["h"] = b["y"] + b["h"] - p["y"]
                p["w"] = max(p["w"], b["w"])
                continue
        out.append(b)
    return out


_BTN_WORDS = ("确定", "确认", "取消", "继续", "返回", "关闭", "退出", "开始", "进入", "发送",
              "选择", "登记", "装备", "购买", "使用", "跳过", "重试", "下一步", "离开", "开启",
              "返回标题", "舞台", "邀请")
_CLOSE_WORDS = ("关闭", "×", "✕", "X", "退出", "返回")


def _ui_type_of(text: str, box: list[int], frame_w: int, frame_h: int, scene: str) -> str:
    t = (text or "").strip()
    x, y, w, h = box
    if t and any(k in t for k in _CLOSE_WORDS) and len(t) <= 4:
        return "close"
    if t and any(k in t for k in _BTN_WORDS) and len(t) <= 12:
        return "button"
    if t and len(t) >= 14:
        return "panel"
    if t and all(ch.isdigit() or ch in "./:%-+ " for ch in t):
        return "stat"
    if w / max(1, h) > 6 and w > frame_w * 0.5:
        return "panel"
    return "text"


def _bright_blobs(img: np.ndarray, tile: int = 16, limit: int = 16) -> list[list[int]]:
    """没有文字的按钮/卡槽：找"比周围亮、内部又平"的方块。

    只比亮度是不够的——废墟图书馆的背景是石墙/碎砖，纹理丰富，一测就满屏"按钮"。
    所以加两道：内部标准差要小（UI 块是平的，石墙是花的）、亮像素占比要够。
    """
    cells = _cell_means(img, tile)
    if cells.size == 0:
        return []
    lum = cells.mean(axis=2)
    lo, hi = float(np.percentile(lum, 35)), float(np.percentile(lum, 96))
    if hi - lo < 28:
        return []
    thr = lo + (hi - lo) * 0.70
    bright = lum > thr
    if not bright.any():
        return []
    h, w = img.shape[:2]
    cand = cluster_boxes(bright, tile, limit=limit * 3)
    out: list[list[int]] = []
    for b in cand:
        if b[2] < 28 or b[3] < 28:
            continue
        if b[3] > h * 0.55 or b[2] > w * 0.8:
            continue
        patch = img[b[1]: b[1] + b[3], b[0]: b[0] + b[2]].astype(np.float32)
        if patch.size == 0 or float(patch.std()) > 46:
            continue
        fill = float((patch.mean(axis=2) > thr).mean())
        if fill < 0.55:
            continue
        out.append([int(v) for v in b])
    keep: list[list[int]] = []
    for b in sorted(out, key=lambda z: -(z[2] * z[3])):
        if all(_iou(b, k) < 0.4 for k in keep):
            keep.append(b)
    return keep[:limit]


def _runs_of(mask1d: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """一维布尔串里的连续段（允许 2px 断口），只留 >= min_len 的。"""
    idx = np.where(mask1d)[0]
    if idx.size < min_len:
        return []
    out: list[tuple[int, int]] = []
    start = prev = int(idx[0])
    for v in idx[1:]:
        v = int(v)
        if v - prev <= 2:
            prev = v
            continue
        if prev - start + 1 >= min_len:
            out.append((start, prev))
        start = prev = v
    if prev - start + 1 >= min_len:
        out.append((start, prev))
    return out


def _line_rects(img: np.ndarray, min_side: int = 40, max_side: int = 460,
                thr: float = 44.0, limit: int = 12) -> list[tuple[list[int], float]]:
    """从"长直线段"拼出矩形框：专治描边按钮/胶囊（面上没有亮色填充，只有一圈线）。

    为什么需要这一路：废墟图书馆的按钮是"深色底 + 亮描边 + 花纹字"，按钮面的亮度和背景
    几乎一样，按亮度找块会整片漏掉（实测 `/ui_dump` 只吐出 1 个文字块）。而这种按钮的
    边框是画面里最直、最长、最规整的线——先找横线，再找竖直两侧，就能把框拼回来。
    只做直边：圆角/椭圆按钮拼不出来，交给色块那一路兜底。
    """
    lum = np.asarray(img, dtype=np.float32).mean(axis=2)
    gx = np.zeros_like(lum)
    gy = np.zeros_like(lum)
    gx[:, 1:-1] = np.abs(lum[:, 2:] - lum[:, :-2]) / 2.0
    gy[1:-1, :] = np.abs(lum[2:, :] - lum[:-2, :]) / 2.0
    em = np.maximum(gx, gy) > thr
    hs: list[tuple[int, int, int]] = []
    for y in range(em.shape[0]):
        for x0, x1 in _runs_of(em[y], min_side):
            hs.append((y, x0, x1))
    vs: list[tuple[int, int, int]] = []
    for x in range(em.shape[1]):
        for y0, y1 in _runs_of(em[:, x], 12):
            vs.append((x, y0, y1))
    pairs: list[tuple[list[int], float]] = []
    hs.sort()
    for i, (y1, a1, b1) in enumerate(hs):
        for y2, a2, b2 in hs[i + 1:]:
            dy = y2 - y1
            if dy < min_side:
                continue
            if dy > max_side:
                break
            ox = min(b1, b2) - max(a1, a2)
            if ox < 0.75 * max(b1 - a1, b2 - a2):
                continue
            x0, x1 = max(a1, a2), min(b1, b2)
            w = x1 - x0
            if w < min_side or w > max_side:
                continue
            left = any(abs(vx - x0) <= 3 and vy0 <= y1 + 3 and vy1 >= y2 - 3 for vx, vy0, vy1 in vs)
            right = any(abs(vx - x1) <= 3 and vy0 <= y1 + 3 and vy1 >= y2 - 3 for vx, vy0, vy1 in vs)
            if not (left and right):
                continue
            ring = np.concatenate([lum[y1:y1 + 2, x0:x1].ravel(), lum[y2 - 1:y2 + 1, x0:x1].ravel()])
            strength = float(np.abs(ring - float(lum[y1 + dy // 2, max(x0, x1 - 1)])).mean())
            pairs.append(([int(x0), int(y1), int(w), int(dy)], strength))
    keep: list[tuple[list[int], float]] = []
    for b, s in sorted(pairs, key=lambda z: -(z[0][2] * z[0][3])):
        if all(_iou(b, k[0]) < 0.35 for k in keep):
            keep.append((b, s))
        if len(keep) >= limit:
            break
    return keep



def _color_boxes(img: np.ndarray, q: int = 3, tile: int = 16, min_tiles: int = 5,
                 limit: int = 14, contrast: float = 18.0) -> list[tuple[list[int], float]]:
    """按"色块"找控件：tile 颜色量化 → 连通域 → 形状过滤。

    为什么还要这一路：深色主题下的按钮面/胶囊和背景亮度几乎一样，只认亮块会整片漏掉
    （实测废墟图书馆与自造的深色测试窗都是这样）；反过来，浅色胶囊在量化里又常和背景
    混成一类。两路取并集再按 IoU 去重，比任何单独一路都稳。

    返回 [([x,y,w,h], 对比度)]，对比度 = 该块颜色与全画面中位色的 L1 距离，
    给上层用来判断"这个块是不是比别人暗一档"（置灰按钮）。
    """
    h, w = img.shape[:2]
    th, tw = h // tile, w // tile
    if th < 2 or tw < 2:
        return []
    cells = np.asarray(img[: th * tile, : tw * tile], dtype=np.float32).reshape(th, tile, tw, tile, 3).mean(axis=(1, 3))
    step = 256.0 / (2 ** q)
    bk = np.clip(cells / step, 0, 2 ** q - 1).astype(np.int32)
    key = (bk[..., 0] * (2 ** q) + bk[..., 1]) * (2 ** q) + bk[..., 2]
    med = np.median(cells.reshape(-1, 3), axis=0)
    out: list[tuple[list[int], float]] = []
    total = float(th * tw)
    for k in np.unique(key):
        mask = key == k
        n = int(mask.sum())
        if n < min_tiles or n > total * 0.5:
            continue
        col = cells[mask].mean(axis=0)
        d = float(np.abs(col - med).sum())
        if d < contrast:
            continue
        for b in cluster_boxes(mask, tile, limit=limit):
            x, y, bw, bh = [int(v) for v in b]
            if bw < 28 or bh < 24:
                continue
            if bw > w * 0.85 or bh > h * 0.7:
                continue
            sub = mask[max(0, y // tile): max(1, (y + bh) // tile), max(0, x // tile): max(1, (x + bw) // tile)]
            if float(sub.mean() if sub.size else 0.0) < 0.55:
                continue
            out.append(([x, y, bw, bh], d))
    out.sort(key=lambda z: -(z[0][2] * z[0][3]))
    keep: list[tuple[list[int], float]] = []
    for b, d in out:
        if all(_iou(b, k[0]) < 0.35 for k in keep):
            keep.append((b, d))
        if len(keep) >= limit:
            break
    return keep


def _ui_blobs(img: np.ndarray, limit: int = 16) -> list[tuple[list[int], str, float]]:
    """没有文字的控件：描边矩形 + 亮块 + 色块，三路并集。

    三路的强项不一样，缺一路就漏一类：
    - 描边矩形：深色底 + 亮边的按钮/胶囊（废墟图书馆的主力）
    - 亮块：浅色填充的标签页/槽位
    - 色块：靠颜色区分的卡牌/面板
    strength 只在同一路内部可比，用来判"比同类暗一档"（置灰）。
    """
    items: list[tuple[list[int], str, float]] = []
    for b, s in _line_rects(img, limit=limit):
        items.append(([int(v) for v in b], "rect", s))
    for b in _bright_blobs(img):
        patch = np.asarray(img[b[1]:b[1] + b[3], b[0]:b[0] + b[2]], dtype=np.float32)
        items.append(([int(v) for v in b], "bright", float(patch.mean()) if patch.size else 0.0))
    lum = np.asarray(img, dtype=np.float32).mean(axis=2)
    med = float(np.median(lum))
    for b, _d in _color_boxes(img):
        patch = lum[b[1]:b[1] + b[3], b[0]:b[0] + b[2]]
        items.append(([int(v) for v in b], "color", float(np.abs(patch - med).mean()) if patch.size else 0.0))
    items.sort(key=lambda z: -(z[0][2] * z[0][3]))
    keep: list[tuple[list[int], str, float]] = []
    for b, s, v in items:
        if all(_iou(b, k[0]) < 0.35 for k in keep):
            keep.append((b, s, v))
        if len(keep) >= limit:
            break
    return keep


def ui_dump(region=None, ocr_scale: float = 2.0, limit: int = 40, scene: str | None = None) -> dict:
    """把画面拆成可点的元素表：有字用字，没字用亮块，置灰用亮度判。"""
    img, r, seq, ts, src = grab(region)
    pil = to_pil(img)
    res = ocr_pil(pil, ocr_scale)
    lines = abs_lines(normalize_lines(res), r)
    blocks = _blockify(lines)
    elems: list[dict] = []
    frame_w, frame_h = img.shape[1], img.shape[0]
    lum_all = float(img.astype(np.float32).mean())
    for b in blocks:
        box = [int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])]
        kind = _ui_type_of(b["text"], box, frame_w, frame_h, scene or "unknown")
        bx = max(0, box[0] - int(r[0]))
        by = max(0, box[1] - int(r[1]))
        patch = img[by: by + max(4, box[3]), bx: bx + max(4, box[2])]
        lum = float(patch.mean()) if patch.size else lum_all
        # 置灰只对"能点的东西"有意义：文字/面板没有 enabled 这一说
        enabled = True if kind not in ("button", "close") else bool(lum >= lum_all * UI_DIRTY)
        elems.append({
            "type": kind,
            "label": b["text"],
            "box": box,
            "enabled": enabled,
            "selected": False,
            "src": "ocr",
            "_lum": round(lum, 1),
        })
    # 无文字的按钮/卡槽：亮块 + 色块两路，置灰靠"比同类其它块暗/花"判断
    blobs = _ui_blobs(img)
    ref: dict[str, float] = {}
    for _b, s, v in blobs:
        ref.setdefault(s, []).append(v)  # type: ignore[arg-type]
    ref = {k: float(np.percentile(v, 80)) for k, v in ref.items()}  # 80 分位当"正常亮度"
    for braw, bsrc, bval in blobs:
        box = [braw[0] + int(r[0]), braw[1] + int(r[1]), braw[2], braw[3]]
        if any(_iou(box, e["box"]) > 0.5 for e in elems):
            continue
        if box[2] < 24 or box[3] < 24 or box[2] > frame_w * 0.9:
            continue
        ar = box[2] / max(1, box[3])
        kind = "button" if (0.55 <= ar <= 5.0 and box[3] < frame_h * 0.3) else ("slot" if ar <= 1.4 else "card")
        enabled = True
        # 置灰的相对门槛按来源分：描边框的"暗一档"更明显，亮块/色块保守一点
        rel = {"rect": 0.78, "bright": 0.72, "color": 0.72}.get(bsrc, 0.72)
        if kind == "button" and ref.get(bsrc, 0.0) > 0:
            enabled = bool(bval >= ref[bsrc] * rel)
        elems.append({
            "type": kind, "label": "", "box": box,
            "enabled": enabled, "selected": False, "src": bsrc,
            "_c": round(bval, 1),
        })
    elems.sort(key=lambda e: (e["box"][1] // 24, e["box"][0]))
    elems = elems[:limit]
    for i, e in enumerate(elems, 1):
        e["id"] = _element_id(i)
        e.pop("_lum", None)
        e.pop("_c", None)
    LAST_UI.clear()
    for e in elems:
        LAST_UI[e["id"]] = e["box"]
    return {
        "frame": f"seq{seq}",
        "region": list(r),
        "source": src,
        "coord": "screen",
        "scene": scene or "unknown",
        "sceneZh": SCENE_ZH.get(scene or "unknown", scene or "unknown"),
        "elements": elems,
        "ocrMs": res.get("ms"),
        "counts": {
            "all": len(elems),
            "button": sum(1 for e in elems if e["type"] == "button"),
            "slot": sum(1 for e in elems if e["type"] in ("slot", "card")),
        },
    }


def _iou(a: list[int], b: list[int]) -> float:
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    ix = max(0, min(ax2, bx2) - max(a[0], b[0]))
    iy = max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / max(1, union)


LAST_UI: dict = {}
NOW_CELLS: dict = {}


def resolve_click_target(body: dict) -> tuple[list[int] | None, str]:
    """click 的目标：element id → 上次 dump 的框；x/y → 直接用；text → 现读一次 OCR 找。"""
    el = body.get("element") or body.get("elementId")
    if el:
        box = LAST_UI.get(str(el))
        if not box:
            raise ValueError(f"元素 {el} 不在最近一次 ui_dump 里（现有 {sorted(LAST_UI)[:8]}）。先 coach_ui_dump 拿 id。")
        return list(box), str(el)
    if body.get("x") is not None and body.get("y") is not None:
        return [int(body["x"]), int(body["y"]), 1, 1], "xy"
    txt = body.get("text")
    if txt:
        img, r, _s, _t, _src = grab(body.get("region"))
        lines = abs_lines(normalize_lines(ocr_pil(to_pil(img), _ocr_scale(body))), r)
        hit = next((ln for ln in lines if str(txt) in ln["text"]), None) or \
              next((ln for ln in lines if ln["text"] in str(txt)), None)
        if not hit:
            raise ValueError(f"画面里没找到文字「{txt}」（认出 {len(lines)} 行）")
        return [int(hit["x"]), int(hit["y"]), int(hit["w"]), int(hit["h"])], "text:" + hit["text"]
    raise ValueError("要说清点哪儿：element=\"ui_3\" 或 x/y 或 text=\"发送邀请\"")


def do_click(body: dict) -> dict:
    """带护栏、前台复核、事后差分的点击。所有字段都写回执。"""
    box, how = resolve_click_target(body)
    if len(box) == 4 and (box[2] > 1 or box[3] > 1):
        cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
    else:
        cx, cy = box[0], box[1]
    hwnd = int(body.get("hwnd") or (ANCHOR.get("hwnd") if isinstance(ANCHOR, dict) else 0) or 0)
    vx, vy, vw, vh = INPUT.virtual_screen()
    if not (vx <= cx < vx + vw and vy <= cy < vy + vh):
        raise ValueError(
            f"点不了：坐标 ({cx},{cy}) 不在屏幕里（虚拟桌面 {vx},{vy} {vw}x{vh}）。"
            f"给屏幕绝对坐标，或者用 element/text 让工具自己算——这两种都不会点空。"
        )
    move_settle = max(0, min(2000, int(body.get("moveSettleMs") or 250)))
    down_up = max(10, min(2000, int(body.get("downUpMs") or 80)))
    verify_ms = max(0, min(4000, int(body.get("verifyMs") or 450)))
    force = body.get("force") is True

    scene_before = None
    if body.get("guardScene") is not False:
        try:
            scene_before = scene_probe(grab(None)[0])
        except Exception:  # noqa: BLE001
            scene_before = "unknown"
    if scene_before in GUARDED_SCENES and not force and body.get("allowInBattle") is not True:
        # 战斗里点击不是被禁止的（要点卡牌/目标），但要在回执里点明"这里点错代价很大"
        note = f"注意：现在是{SCENE_ZH.get(scene_before)}界面（space/enter 会被拦，点击请确认目标）"
    else:
        note = ""

    if body.get("dryRun") is True:
        return {"ok": True, "dryRun": True, "wouldClick": [cx, cy], "element": how,
                "box": box, "scene": scene_before, "note": note}

    before_img, region, _seq, _ts, _src = grab(None)
    prev_cells = _cell_means(before_img)
    fg_req = hwnd
    claim = {"ok": True, "actual": 0, "actualTitle": "", "tries": 0}
    if hwnd:
        claim = INPUT.force_foreground(hwnd, tries=3)
        if not claim.get("ok"):
            rec = {"at": round(time.time(), 3), "hwnd": hwnd, "x": cx, "y": cy, "ok": False,
                   "code": "NOT_FOREGROUND", "foreground": claim.get("actual"),
                   "foregroundTitle": claim.get("actualTitle"), "via": how}
            log_input(rec)
            raise RuntimeError(
                f"点不了：抢不到前台。请求 {hwnd}，真实前台是 {claim.get('actual')}"
                f"（{claim.get('actualTitle') or '无标题'}）。把游戏点一下再试，或改用 mode=\"post\"。"
            )
    button = str(body.get("button") or "left").lower()
    if button not in ("left", "right"):
        raise ValueError("button 只能是 \"left\" 或 \"right\"")
    sent = INPUT.click(cx, cy, move_settle_ms=move_settle, down_up_ms=down_up, button=button)
    fg_after = fg_now()
    time.sleep(verify_ms / 1000.0)
    after_img, _r2, _s2, _t2, _src2 = grab(None)
    cells = changed_cells(prev_cells, _cell_means(after_img), CELL_DIFF)
    changed = int(cells.sum()) if cells is not None else None
    boxes = cluster_boxes(cells, self_tile()) if cells is not None else []
    ox, oy = int(region[0]), int(region[1])
    out = {
        "ok": True,
        "did": "click",
        "button": button,
        "element": how,
        "box": box,
        "requested_hwnd": fg_req,
        "actual_foreground_hwnd": fg_after["hwnd"],
        "actual_foreground_title": fg_after["title"],
        "foreground_ok": (not fg_req) or fg_after["hwnd"] == fg_req,
        "clicked_screen_xy": [cx, cy],
        "changedTiles": changed,
        "changedRegion": [[b[0] + ox, b[1] + oy, b[2], b[3]] for b in boxes[:6]],
        "scene": scene_before,
        "moveSettleMs": move_settle,
        "downUpMs": down_up,
        "sent": sent,
        "ms": int(verify_ms + move_settle + down_up),
    }
    if changed is not None and changed <= 0:
        out["verdict"] = "no_change"
        out["hint"] = "点了但画面没变：可能未命中（点偏）/ 可能按钮被禁用 / 可能被别的窗口挡住。先 coach_ui_dump 确认框，再看回执里的真实前台。"
    if not out["foreground_ok"]:
        out["verdict"] = out.get("verdict") or "foreground"
        out["hint"] = (
            f"点击后前台不在目标窗口上（现在是「{fg_after['title'] or '无标题'}」"
            f" {fg_after['hwnd']}）：这一下很可能落到了别的窗口。"
            f"先把游戏点一下让它到前台，或改用 mode=\"post\"。"
        )
    if note:
        out["note"] = note
    log_input({"at": round(time.time(), 3), "hwnd": fg_req, "x": cx, "y": cy, "via": how,
               "foreground": fg_after["hwnd"], "foregroundTitle": fg_after["title"],
               "changed": changed, "ok": True})
    return out


def self_tile() -> int:
    return int(getattr(Handler, "tile", 32))


def wait_stable(region=None, settle_ms: int = 400, timeout_ms: int = 8000,
                min_tiles: int = 0, thr: float | None = None, ignore=None,
                interval: float = 0.09) -> dict:
    """等画面静止。用"单格色差"而不是"有没有任何差别"——背景粒子每帧都在抖。"""
    img, r, _s, _t, _src = grab(region)
    prev = _cell_means(img)
    thr_v = STABLE_DIFF if thr is None else float(thr)
    tol = int(min_tiles)
    if tol <= 0:  # 自动容差：约 1.2% 的格子，够挡住背景粒子，挡不住真正的界面切换
        tol = max(2, int(prev.shape[0] * prev.shape[1] * 0.012))
    t0 = time.time()
    last_move = t0
    moved = 0
    frames = 1
    while (time.time() - t0) * 1000.0 < timeout_ms:
        time.sleep(interval)
        img2, _r2, _s2, _t2, _src2 = grab(region)
        cur = _cell_means(img2)
        mask = changed_cells(prev, cur, thr_v)
        prev = cur
        frames += 1
        if mask is None:
            continue
        if ignore:
            for rect in ignore:
                mask = _mask_rect(mask, rect, self_tile(), r)
        n = int(mask.sum())
        if n > tol:
            moved = n
            last_move = time.time()
        elif (time.time() - last_move) * 1000.0 >= settle_ms:
            return {"stable": True, "waitedMs": int((time.time() - t0) * 1000), "region": list(r),
                    "movingTiles": moved, "thr": thr_v, "tol": tol, "frames": frames, "coord": "screen"}
    return {"stable": False, "waitedMs": int((time.time() - t0) * 1000), "region": list(r),
            "movingTiles": moved, "thr": thr_v, "tol": tol, "frames": frames, "coord": "screen"}


def click_until(body: dict) -> dict:
    """点一次没反应就再点：动画结束才生效的按钮靠它救。"""
    tries = max(1, min(12, int(body.get("maxTries") or 3)))
    interval = max(150, min(4000, int(body.get("intervalMs") or 700)))
    expect = max(1, int(body.get("expectChange") or 1))
    attempts = []
    for i in range(tries):
        res = do_click({**body, "verifyMs": body.get("verifyMs") or 450})
        changed = res.get("changedTiles")
        attempts.append({"n": i + 1, "changed": changed, "ok": bool(res.get("ok"))})
        if changed is None or changed >= expect:
            return {"ok": True, "tries": i + 1, "attempts": attempts, "last": res,
                    "stoppedBy": "changed" if changed else "maxTries"}
        time.sleep(interval / 1000.0)
    return {"ok": False, "tries": tries, "attempts": attempts,
            "stoppedBy": "maxTries", "hint": f"点了 {tries} 次画面都没变：可能目标不对/被禁用/被遮挡。看每次回执里的 changedTiles。"}


def dismiss(body: dict) -> dict:
    """通用关闭：先用 dump 找 ×/关闭，再 Esc，再点空白。每一种都实测有没有效果。"""
    img, r, _s, _t, _src = grab(None)
    before = _cell_means(img)
    scene = None
    try:
        scene = scene_probe(img)
    except Exception:  # noqa: BLE001
        pass
    tries = []
    strategies = []
    if body.get("noUiDump") is not True and body.get("prefer") != "esc":
        strategies.append("ui_close")
    strategies.append("esc")
    strategies.append("blank_click")
    if body.get("prefer") == "esc":
        strategies = ["esc"] + [s for s in strategies if s != "esc"]
    hwnd = int(body.get("hwnd") or (ANCHOR.get("hwnd") if isinstance(ANCHOR, dict) else 0) or 0)
    if hwnd:
        INPUT.force_foreground(hwnd, tries=2)
    for s in strategies:
        desc = s
        try:
            if s == "ui_close":
                dump = ui_dump(scene=scene)
                cand = [e for e in dump["elements"] if e["type"] == "close"]
                if not cand:
                    tries.append({"strategy": s, "done": False, "why": "画面上没有 ×/关闭 这类元素"})
                    continue
                bb = cand[0]["box"]
                INPUT.click(bb[0] + bb[2] // 2, bb[1] + bb[3] // 2, move_settle_ms=250, down_up_ms=80)
                desc = f"点了关闭元素 {cand[0]['id']}「{cand[0]['label']}」"
            elif s == "esc":
                INPUT.press("esc", 40)
                desc = "按了 Esc"
            else:
                cx = int(r[0] + r[2] * 0.5)
                cy = int(r[1] + r[3] * 0.72)
                INPUT.click(cx, cy, move_settle_ms=250, down_up_ms=80)
                desc = f"点了空白 ({cx},{cy})"
        except Exception as e:  # noqa: BLE001
            tries.append({"strategy": s, "done": False, "why": f"{type(e).__name__}: {e}"})
            continue
        time.sleep(max(200, int(body.get("waitMs") or 500)) / 1000.0)
        img2, _r2, _s2, _t2, _src2 = grab(None)
        cells = changed_cells(before, _cell_means(img2), CELL_DIFF)
        n = int(cells.sum()) if cells is not None else None
        before = _cell_means(img2)
        tries.append({"strategy": s, "done": True, "what": desc, "changedTiles": n})
        if n:
            return {"ok": True, "closedBy": s, "what": desc, "changedTiles": n, "tries": tries}
    return {"ok": False, "tries": tries,
            "error": "三种方式都试过了，画面没变：" + "；".join(t.get("strategy", "?") for t in tries)}


def key_probe(body: dict) -> dict:
    """逐个按一下键，看画面变没变——游戏写的键位不一定真能按。"""
    keys = [str(k) for k in (body.get("keys") or ["space", "enter", "esc", "q", "e", "1", "2"])][:12]
    hwnd = int(body.get("hwnd") or (ANCHOR.get("hwnd") if isinstance(ANCHOR, dict) else 0) or 0)
    scene = None
    try:
        scene = scene_probe(grab(None)[0])
    except Exception:  # noqa: BLE001
        pass
    if hwnd:
        INPUT.force_foreground(hwnd, tries=2)
    rows = []
    for k in keys:
        g = guard_key(k, scene or "unknown", force=body.get("force") is True)
        if g:
            rows.append({"key": k, "tested": False, "blocked": True, "reason": g["reason"]})
            continue
        before, _r, _s, _t, _src = grab(None)
        prev = _cell_means(before)
        try:
            INPUT.press(k, int(body.get("holdMs") or 40))
        except Exception as e:  # noqa: BLE001
            rows.append({"key": k, "tested": False, "error": str(e)})
            continue
        time.sleep(max(150, int(body.get("waitMs") or 400)) / 1000.0)
        after, _r2, _s2, _t2, _src2 = grab(None)
        cells = changed_cells(prev, _cell_means(after), CELL_DIFF)
        n = int(cells.sum()) if cells is not None else None
        rows.append({"key": k, "tested": True, "changedTiles": n, "works": bool(n)})
    return {"keys": rows, "scene": scene, "sceneZh": SCENE_ZH.get(scene or "unknown", scene or "unknown")}


def read_click_log(limit: int = 20) -> dict:
    rows = []
    try:
        with open(INPUT_LOG, encoding="utf-8") as fh:
            for ln in fh.read().splitlines()[-limit * 2:]:
                try:
                    rows.append(json.loads(ln))
                except Exception:  # noqa: BLE001
                    continue
    except FileNotFoundError:
        return {"rows": [], "note": "还没有点击日志（动手之后才会有）"}
    return {"rows": rows[-limit:], "path": INPUT_LOG}


def load_scenes() -> dict:
    try:
        with open(SCENES_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_scenes(data: dict) -> None:
    os.makedirs(COACH_HOME, exist_ok=True)
    with open(SCENES_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def _thumb(img: np.ndarray, n: int = 32) -> np.ndarray:
    cells = _cell_means(img, max(1, min(img.shape[0], img.shape[1]) // n))
    if cells.shape[0] != n or cells.shape[1] != n:
        return np.zeros((n, n), dtype=np.float32)
    return cells.mean(axis=2)


def _norm_thumb(t: np.ndarray) -> np.ndarray:
    t = t.astype(np.float32)
    t = t - float(t.mean())
    s = float(t.std() or 1.0)
    return t / s


def scene_define(body: dict) -> dict:
    """把一个画面注册成场景模板（截图 + 关键词），之后 scene 判定会先认它。"""
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError('要给个名字：coach_scene_define{name:"邀请函"}')
    img, r, _s, _t, _src = grab(body.get("region"))
    scenes = load_scenes()
    keywords = body.get("keywords")
    if keywords is None:
        try:
            lines = abs_lines(normalize_lines(ocr_pil(to_pil(img), 2.0)), r)
            keywords = [ln["text"] for ln in lines if 2 <= len(ln["text"]) <= 12][:6]
        except Exception:  # noqa: BLE001
            keywords = []
    thumb = _thumb(img)
    scenes[name] = {
        "name": name,
        "kind": str(body.get("kind") or "custom"),
        "keywords": [str(k) for k in keywords][:8],
        "thumb": [round(float(v), 2) for v in thumb.ravel().tolist()],
        "region": list(r),
        "at": round(time.time(), 1),
        "note": str(body.get("note") or ""),
    }
    save_scenes(scenes)
    return {"saved": name, "keywords": scenes[name]["keywords"], "total": len(scenes)}


def scene_list() -> dict:
    scenes = load_scenes()
    return {"count": len(scenes),
            "scenes": [{"name": v["name"], "kind": v.get("kind"), "keywords": v.get("keywords"),
                        "note": v.get("note"), "at": v.get("at")} for v in scenes.values()]}


def scene_match_templates(img: np.ndarray, lines: list[dict], min_corr: float = 0.80) -> dict | None:
    """用户注册过的场景：缩略图相关性 + 关键词命中，两个都不弱才认。"""
    scenes = load_scenes()
    if not scenes:
        return None
    cur = _norm_thumb(_thumb(img))
    text = " ".join(str(ln.get("text") or "") for ln in lines)
    best = None
    for v in scenes.values():
        t = v.get("thumb") or []
        if len(t) != cur.size:
            continue
        ref = _norm_thumb(np.asarray(t, dtype=np.float32).reshape(cur.shape))
        corr = float((cur * ref).mean())
        kw = [k for k in (v.get("keywords") or []) if k and k in text]
        score = corr + 0.10 * len(kw)
        if corr >= min_corr or (corr >= 0.6 and kw):
            if best is None or score > best["score"]:
                best = {"scene": v["name"], "confidence": round(min(0.95, corr), 2),
                        "evidence": kw[:4] + [f"缩略图相关 {corr:.2f}"], "score": score, "source": "template"}
    return best


# ------------------------------------------------------- 战斗盘面（结构化状态）
def _rgb01(img: np.ndarray):
    """BGR → (r,g,b) 三个 0..1 的浮点平面。"""
    f = img[:, :, ::-1].astype(np.float32) / 255.0
    return f[:, :, 0], f[:, :, 1], f[:, :, 2]


def _cc(mask: np.ndarray, min_area: int = 60, limit: int = 12) -> list[list[int]]:
    """四连通域，返回 [[x,y,w,h], ...] 按面积降序。纯 numpy/栈，够用就行。"""
    h, w = mask.shape
    seen = np.zeros_like(mask, bool)
    out: list[tuple[int, list[int]]] = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys, xs):
        if seen[y0, x0]:
            continue
        stack = [(int(y0), int(x0))]
        seen[y0, x0] = True
        n = 0
        xmin = xmax = int(x0)
        ymin = ymax = int(y0)
        while stack:
            y, x = stack.pop()
            n += 1
            if x < xmin:
                xmin = x
            if x > xmax:
                xmax = x
            if y < ymin:
                ymin = y
            if y > ymax:
                ymax = y
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                yy, xx = y + dy, x + dx
                if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True
                    stack.append((yy, xx))
        if n >= min_area:
            out.append((n, [xmin, ymin, xmax - xmin + 1, ymax - ymin + 1]))
    out.sort(key=lambda t: -t[0])
    return [b for _n, b in out[:limit]]


def _pt(rect, local_xy) -> list[int]:
    """图像内坐标 → 屏幕绝对坐标。"""
    return [int(rect[0] + local_xy[0]), int(rect[1] + local_xy[1])]


def dice_hexes(img: np.ndarray, rect) -> dict:
    """头顶的速度骰。

    实测（1302x776 窗口 / 1280x720 客户区）：自己的骰子是**粉紫色六边形描边**，
    均值色 (225,163,201)、外接框约 30x34；敌方骰偏白/灰，靠"亮而中空"判。
    每幕位置都会漂（本轮实测从 (898,512) 漂到 (915,512)），所以一律现算。
    """
    h, w = img.shape[:2]
    r, g, b = _rgb01(img)
    y0, y1 = int(h * 0.12), int(h * 0.82)
    zone = np.zeros((h, w), bool)
    zone[y0:y1] = True

    pink = ((r > 0.50) & (b > 0.48) & ((r - g) > 0.10) & ((b - g) > 0.04)
            & ((r + b - 2 * g) > 0.18) & zone)
    white = ((r > 0.70) & (g > 0.70) & (b > 0.70) & zone)

    out: list[dict] = []
    for box in _cc(pink, min_area=120, limit=8):
        if not (18 <= box[2] <= 70 and 18 <= box[3] <= 70):
            continue
        out.append({"box": box, "color": "pink"})
    for box in _cc(white, min_area=90, limit=8):
        if not (24 <= box[2] <= 70 and 18 <= box[3] <= 70):
            continue
        fill = 0.0
        # 中空的才算骰子（文字、亮块都是实心）
        sub = white[box[1]:box[1] + box[3], box[0]:box[0] + box[2]]
        if sub.size:
            fill = float(sub.mean())
        if not (0.12 <= fill <= 0.55):
            continue
        out.append({"box": box, "color": "white"})

    out.sort(key=lambda d: (d["box"][1], d["box"][0]))
    half = w / 2.0
    for i, d in enumerate(out, 1):
        bx = d["box"]
        d["i"] = i
        d["center_local"] = [bx[0] + bx[2] // 2, bx[1] + bx[3] // 2]
        d["screen"] = _pt(rect, d["center_local"])
        d["side"] = "self" if d["center_local"][0] >= half else "enemy"
        d["value"] = None          # 骰面数字读不出（同一套描边字体）
        d["pageAssigned"] = None   # 弧线要专门检测，先不猜
    return {"dice": out, "count": len(out),
            "note": None if out else "中上部没找到速度骰（可能不是战斗界面，或骰子还没出现）"}


def hand_state(img: np.ndarray, rect) -> dict:
    """底部手牌：可见性 + 每张牌的框（这是"现在能不能出牌"的唯一硬证据）。"""
    h = img.shape[0]
    cards = hand_cards(img)
    band = cards.get("band") or [int(h * 0.66), int(h * 0.93)]
    out_cards: list[dict] = []
    lum = img.astype(np.float32).mean(axis=2) / 255.0
    for i, (x, cw) in enumerate(cards.get("runs") or [], 1):
        col = lum[band[0]:band[1], x:x + cw]
        if col.size:
            rows = col.mean(axis=1)
            thr = max(0.16, float(rows.max()) * 0.55)
            hot = np.nonzero(rows > thr)[0]
        else:
            hot = []
        if len(hot) >= 8:
            top = band[0] + int(hot.min())
            bot = band[0] + int(hot.max())
        else:
            top, bot = band[0], band[1]
        box = [int(x), int(top), int(cw), int(bot - top)]
        out_cards.append({
            "i": i, "box": box,
            "screen": _pt(rect, [box[0] + box[2] // 2, box[1] + box[3] // 2]),
            "name": None, "cost": None, "dice": [], "playable": None,
        })
    visible = bool(cards.get("ok"))
    reason = None
    if not visible:
        raw = cards.get("raw") or 0
        if raw == 0:
            reason = ("底部那一带没有成排的亮块：多半是结算/动画中，或手牌区被台词气泡挡住。"
                      "先 coach_now 看 animating，再 coach_dismiss 关掉气泡/教学浮层。")
        else:
            reason = (f"底部有 {raw} 处亮块但不像手牌（宽度不齐或尺寸不对）：可能是地上的书/杂物反光，"
                      "也可能手牌正被气泡盖住。别硬点，先 coach_dismiss 或者 coach_now 看看。")
    return {"visible": visible, "count": len(out_cards), "band": band,
            "cards": out_cards, "thr": cards.get("thr"), "reason": reason}


def execute_button(img: np.ndarray, rect) -> dict:
    """顶部的「下令战斗」按钮。

    两步定位：① 蓝菱形（全场唯一的蓝，实测外接框 [626,7,28,45]）；
    ② 菱形下方那块金色圆盘——按钮是它，不是菱形本身。实盘心 ≈ (640,55)，
    菱形中心在 (640,29)，所以往下偏 26px 才是真正的可点中心。
    回执同时给 candidates（盘心、菱形中心、盘心左右各 40px），一次点不中就换一个再试。
    """
    h, w = img.shape[:2]
    r, g, b = _rgb01(img)
    top = np.zeros((h, w), bool)
    top[:max(24, int(h * 0.12))] = True
    blue = (b > 0.55) & ((b - r) > 0.18) & (g > 0.32) & (g < b) & top
    boxes = [bb for bb in _cc(blue, min_area=120, limit=6) if bb[2] >= 8]
    if not boxes:
        return {"found": False, "note": "顶部没找到蓝色菱形：可能不在战斗界面，或没到可以下令的阶段。"}
    bb = boxes[0]
    dcx, dcy = bb[0] + bb[2] // 2, bb[1] + bb[3] // 2

    # 暖色光晕（金盘）：只在菱形周围找，避免把别处的火光/血迹算进来
    x0, x1 = max(0, dcx - 90), min(w, dcx + 90)
    y0, y1 = max(0, dcy - 8), min(h, dcy + 60)
    warm = np.zeros((h, w), bool)
    sub = (r > 0.28) & ((r - b) > 0.10) & (g > 0.18)
    warm[y0:y1, x0:x1] = sub[y0:y1, x0:x1]
    ring = _cc(warm, min_area=200, limit=4)
    if ring:
        rb = ring[0]
        pcx, pcy = rb[0] + rb[2] // 2, rb[1] + rb[3] // 2
        plate = rb
        how = "plate"
    else:
        pcx, pcy = dcx, dcy + 26
        plate = [dcx - 40, dcy, 80, 52]
        how = "offset"   # 没找到光晕就按"菱形往下 26px"这个实测偏移兜底

    center = [int(pcx), int(pcy)]
    cands = [[int(pcx), int(pcy)], [dcx, dcy], [pcx - 40, pcy], [pcx + 40, pcy], [pcx, pcy + 18]]
    seen: set[tuple[int, int]] = set()
    candidates = []
    for cx, cy in cands:
        if not (0 <= cx < w and 0 <= cy < h):
            continue
        if (cx, cy) in seen:
            continue
        seen.add((cx, cy))
        candidates.append(_pt(rect, [cx, cy]))

    return {"found": True, "diamond": bb, "plate": plate, "how": how,
            "box": [max(0, plate[0]), max(0, plate[1]),
                    min(w - plate[0], plate[2]), min(h - plate[1], plate[3])],
            "center_local": center, "screen": _pt(rect, center),
            "candidates": candidates,
            "note": "点 screen 那个点（金盘中心）。零变化就换 candidates 里的下一个；"
                    "别点菱形上方、也别点左右两边的刻度节点。"}


def hp_panels(img: np.ndarray, rect, scale: float = 2.5) -> dict:
    """底部两侧的血条 + 名字/数值（OCR 读得出就读，读不出给 null）。"""
    h, w = img.shape[:2]
    r, g, b = _rgb01(img)
    y0 = int(h * 0.68)
    red = (r > 0.60) & ((r - g) > 0.28) & ((r - b) > 0.24)
    grn = (g > 0.55) & ((g - r) > 0.22) & ((g - b) > 0.12)
    bars: list[dict] = []
    for color, mask in (("red", red), ("green", grn)):
        sub = np.zeros_like(mask)
        sub[y0:] = mask[y0:]
        for bb in _cc(sub, min_area=120, limit=10):
            # 血条是"又长又矮"的条：实测 120x10 一带。33x34 的方块是骰子/图标，不是血条
            if not (bb[2] >= 60 and 4 <= bb[3] <= 26 and bb[2] >= 3.5 * bb[3]):
                continue
            bx = bb[0]
            # 条内绿色/红色占比 ≈ 剩余比例（左对齐填充）
            if bx + bb[2] <= w:
                band = (mask if color == "red" else mask)[bb[1]:bb[1] + bb[3], bx:bx + bb[2]]
                frac = round(float(band.mean()), 3) if band.size else None
            else:
                frac = None
            bars.append({"color": color, "box": bb, "frac": frac,
                         "screen": _pt(rect, [bx + bb[2] // 2, bb[1] + bb[3] // 2])})
    bars.sort(key=lambda d: (d["box"][1], d["box"][0]))

    lines: list[dict] = []
    try:
        band_img = img[y0:, :]
        ocr = ocr_pil(to_pil(band_img), scale)
        for ln in (ocr.get("lines") or [])[:24]:
            txt = str(ln.get("text") or "").strip()
            if not txt:
                continue
            lines.append({"text": txt, "box": [int(ln["x"]), int(ln["y"]) + y0,
                                               int(ln["w"]), int(ln["h"])],
                          "screen": _pt(rect, [int(ln["x"]) + int(ln["w"]) // 2,
                                               int(ln["y"]) + y0 + int(ln["h"]) // 2])})
    except Exception:  # noqa: BLE001
        lines = []
    return {"bars": bars[:8], "texts": lines, "ocrHint": None if lines else
            "底部文字没读出来（这套描边字体 OCR 不稳）；名字/数值请看 bars 的位置和 texts 里的原文。"}


def battle_state(img: np.ndarray, rect, body: dict) -> dict:
    """一次给出结构化盘面。字段命名对齐需求清单，认不出的写 null 并记进 unknown。"""
    t0 = time.time()
    dice = dice_hexes(img, rect)
    hand = hand_state(img, rect)
    exe = execute_button(img, rect)

    # 静止判定：再抓一帧比块均值（REQ P1-4：动画期不下结论）
    moving = None
    try:
        img2, _r2, _s2, _t2, _src2 = grab(None)
        if img2.shape == img.shape:
            cells = changed_cells(_cell_means(img), _cell_means(img2), STABLE_DIFF)
            moving = int(cells.sum()) if cells is not None else None
    except Exception:  # noqa: BLE001
        moving = None
    tol = max(2, int(_cell_means(img).size / 3 * 0.012)) if img.size else 8
    animating = (moving is not None and moving > tol)

    # 左上角「自动掷骰：ON」——整屏只有这一处字能被 OCR 稳定读到
    auto_roll = None
    top_lines: list[dict] = []
    try:
        th = max(20, int(img.shape[0] * 0.12))
        tw = max(40, int(img.shape[1] * 0.34))
        ocr = ocr_pil(to_pil(img[:th, :tw]), 2.0)
        for ln in (ocr.get("lines") or [])[:6]:
            txt = str(ln.get("text") or "").strip()
            if txt:
                top_lines.append({"text": txt,
                                  "box": [int(ln["x"]), int(ln["y"]), int(ln["w"]), int(ln["h"])]})
        joined = "".join(t for t in (ln["text"] for ln in top_lines))
        if "ON" in joined.upper() or "开" in joined:
            auto_roll = True
        elif "OFF" in joined.upper() or "关" in joined:
            auto_roll = False
    except Exception:  # noqa: BLE001
        auto_roll = None

    # 认"这是战斗"必须有战斗界面专属的东西：底部手牌，或顶部那颗蓝色菱形（下令按钮）。
    # 只靠"像六边形的色块"会误报——实测在 DSH 桌面窗口上也能"找到 2 个骰子"。
    core = bool(hand["visible"] or exe.get("found"))
    sub = ("battle_assign" if hand["visible"] else
           ("battle_resolving" if (core and dice["count"]) else "unknown"))
    scene = "battle" if core else None

    hp = hp_panels(img, rect) if body.get("hp", True) else {"bars": [], "texts": []}

    unknown: list[str] = []
    if not core and dice["count"]:
        unknown.append("speedDice（画面里像骰子，但找不到顶部下令按钮/底部手牌，多半不是战斗界面）")
    if not hand["visible"]:
        unknown.append("hand（手牌没开）")
    if not exe.get("found"):
        unknown.append("executeButton")
    if hand["visible"] and all(c["name"] is None for c in hand["cards"]):
        unknown.append("hand[].name（卡面文字 OCR 读不出）")
    if hand["visible"] and all(not c["dice"] for c in hand["cards"]):
        unknown.append("hand[].dice（骰面数字读不出，可用 ocrlib 的数字通道再试）")
    if dice["count"] and all(d["value"] is None for d in dice["dice"]):
        unknown.append("speedDice[].value")
    if hp["bars"] and not hp["texts"]:
        unknown.append("hp/hpMax（血条旁的数值没读出）")

    out = {
        "ok": True,
        "scene": scene or "unknown",
        "sub": sub,
        "sceneZh": {"battle_assign": "战斗·选牌", "battle_resolving": "战斗·结算中",
                    "unknown": "没认出来"}.get(sub, sub),
        "animating": animating,
        "movingTiles": moving,
        "autoRoll": auto_roll,
        "handVisible": hand["visible"],
        "hand": hand,
        "speedDice": dice["dice"],
        "speedDiceCount": dice["count"],
        "executeButton": exe,
        "canAssign": bool(hand["visible"] and dice["count"] and not animating),
        "canExecute": None,
        "canExecuteReason": "认不出是否已经指派过（需要检测两骰之间的弧线，v4 再做）",
        "hp": {"bars": hp["bars"], "texts": hp["texts"], "hint": hp.get("ocrHint")},
        "top": top_lines,
        "unknown": unknown,
        "region": list(rect),
        "coordSpace": "screen",
        "ms": int((time.time() - t0) * 1000),
    }
    if hand["visible"]:
        out["hint"] = ("手牌已经打开：用 coach_select_page{die,hand} 选骰选牌，"
                       "coach_target_enemy{die} 指敌，最后 coach_execute 下令。")
    elif not core:
        out["hint"] = ("看起来不在战斗界面（既没手牌、顶部也没有下令按钮）：先用 coach_now "
                       "确认场景，再决定是推剧情、关浮层，还是回邀请函。别在这里试出牌。")
    else:
        out["hint"] = ("在战斗里但手牌没开：先 coach_now 看 animating；有台词气泡就 coach_dismiss；"
                       "结算页 coach_dismiss 之后回邀请函。")
    return out


def do_execute(body: dict) -> dict:
    """点「下令战斗」。

    判定靠"画面是否大面积变化"：文档里的实测是成功 667+ 格、只弹出面板约 135 格。
    所以默认 expectMin=250，且等到 2.2s 再比（下令后先是骰子动画，1.2s 时只测到 93 格）。
    """
    ex: dict = {}
    for attempt in range(3):
        st = do_state({"hp": False})
        ex = st.get("executeButton") or {}
        if ex.get("found"):
            break
        # 结算/动画的中间帧上，蓝菱形与金盘会整块闪没，重抓一帧往往就有了
        time.sleep(0.7)
    if not ex.get("found"):
        raise ValueError("没找到「下令战斗」按钮：" + str(ex.get("note") or "顶部没有蓝色菱形") +
                         "。连抓 3 帧都没有——多半不在战斗界面（还在剧情/结算/主菜单），"
                         "先用 coach_now 或 coach_state_json 确认 scene。")
    expect = int(body.get("expectMin") or 250)
    verify = int(body.get("verifyMs") or 2200)
    cands = ex.get("candidates") or [ex["screen"]]
    tried: list[dict] = []
    for i, xy in enumerate(cands, 1):
        r = _click_xy(body, xy, why="下令战斗", verifyMs=verify, downUpMs=90)
        ch = r.get("changedTiles")
        tried.append({"attempt": i, "at": xy, "ok": r.get("ok"), "changedTiles": ch})
        if not r.get("ok"):
            return {"ok": False, "did": "execute", "tried": tried, "error": r.get("error")}
        if ch is not None and ch >= expect:
            return {"ok": True, "did": "execute", "clicked": xy, "attempt": i, "tried": tried,
                    "expectMin": expect, "verifyMs": verify,
                    "verdict": f"画面变了 {ch} 格（阈值 {expect}）：下令生效了，接着等结算"}
        if body.get("dryRun"):
            break
    best = max([t.get("changedTiles") or 0 for t in tried] or [0])
    return {"ok": False, "did": "execute", "tried": tried, "expectMin": expect,
            "error": (f"试了 {len(tried)} 个点都没有明显变化（最多 {best} 格，阈值 {expect}）。"
                      f"多半是还没指派任何牌、或不在战斗界面。先 coach_state_json 看 "
                      f"handVisible / speedDice，再 coach_select_page + coach_target_enemy。")}


def do_state(body: dict) -> dict:
    img, rect, _seq, _ts, src = grab(None)
    out = battle_state(img, rect, body)
    out["source"] = src
    out["frame"] = _seq
    return out


def _click_xy(body: dict, xy, *, why: str, **kw) -> dict:
    """统一走 do_click（护栏+抢前台+差分回执），但把坐标换成屏幕绝对。"""
    b = {"x": int(xy[0]), "y": int(xy[1]), "dryRun": body.get("dryRun") is True,
         "force": kw.pop("force", body.get("force"))}
    for k in ("verifyMs", "moveSettleMs", "downUpMs", "mode", "button"):
        v = kw.pop(k, None)
        if v is None:
            v = body.get(k)
        if v is not None:
            b[k] = v
    r = do_click(b)
    r["why"] = why
    return r


def do_select_page(body: dict) -> dict:
    """自己头顶骰 → 手牌：模拟真人那两步（点骰只 hover 亮一下，点牌才真正选中）。"""
    st = do_state({"hp": False})
    die_i = int(body.get("die") or 0)
    hand_i = int(body.get("hand") or 0)
    if not st["handVisible"]:
        raise ValueError(f"现在没有手牌可出：{st['hand']['reason']}")
    selfs = [d for d in st["speedDice"] if d["side"] == "self"] or st["speedDice"]
    if not selfs:
        raise ValueError("没找到自己的速度骰：先 coach_state_json 看一眼画面。")
    if not (0 <= die_i < len(selfs)):
        raise ValueError(f"die 越界：自己这边只有 {len(selfs)} 个骰子（0..{len(selfs)-1}）。")
    cards = st["hand"]["cards"]
    if not (0 <= hand_i < len(cards)):
        raise ValueError(f"hand 越界：手牌只有 {len(cards)} 张（0..{len(cards)-1}）。")

    steps = []
    r1 = _click_xy(body, selfs[die_i]["screen"], why=f"选中第 {die_i} 个速度骰",
                   downUpMs=body.get("downUpMs") or 90)
    steps.append({"step": "select-die", "ok": r1.get("ok"), "at": selfs[die_i]["screen"],
                  "changedTiles": r1.get("changedTiles"), "element": r1.get("element")})
    if body.get("dryRun"):
        return {"ok": True, "dryRun": True, "steps": steps, "handCount": len(cards),
                "hand": cards[hand_i]["screen"]}
    if not r1.get("ok"):
        return {"ok": False, "steps": steps, "error": r1.get("error")}

    r2 = _click_xy(body, cards[hand_i]["screen"], why=f"选中第 {hand_i} 张手牌",
                   downUpMs=body.get("downUpMs") or 90)
    steps.append({"step": "select-card", "ok": r2.get("ok"), "at": cards[hand_i]["screen"],
                  "changedTiles": r2.get("changedTiles"), "element": r2.get("element")})
    ok = bool(r1.get("ok") and r2.get("ok"))
    out = {"ok": ok, "did": "select_page", "die": die_i, "hand": hand_i, "steps": steps,
           "handCount": len(cards),
           "verdict": ("已选中骰和牌：接下来 coach_target_enemy{die:N} 指敌，"
                       "再用 coach_click{x,y} 下令" if ok else "中途失败，见 steps"),
           "handCard": cards[hand_i], "dieBox": selfs[die_i]["box"]}
    if not ok:
        out["error"] = r1.get("error") or r2.get("error")
    return out


def do_target_enemy(body: dict) -> dict:
    """把当前选中的牌指向某个敌人的骰子。"""
    st = do_state({"hp": False})
    die_i = int(body.get("die") or 0)
    ens = [d for d in st["speedDice"] if d["side"] == "enemy"]
    if not ens:
        raise ValueError("没找到敌人的速度骰：先 coach_state_json 看 speedDice 里的 side 字段。")
    if not (0 <= die_i < len(ens)):
        raise ValueError(f"die 越界：敌人这边只有 {len(ens)} 个骰子（0..{len(ens)-1}）。")
    r = _click_xy(body, ens[die_i]["screen"], why=f"指定第 {die_i} 个敌方骰",
                  downUpMs=body.get("downUpMs") or 90)
    out = {"ok": bool(r.get("ok")), "did": "target_enemy", "die": die_i,
           "at": ens[die_i]["screen"], "box": ens[die_i]["box"],
           "changedTiles": r.get("changedTiles"), "element": r.get("element"),
           "verdict": ("已点敌方骰。看上一条回执的 changedTiles：只亮一下=指派上了；"
                       "完全没变=没点上（骰子会漂，重新 coach_state_json）")}
    if not r.get("ok"):
        out["error"] = r.get("error")
    return out


def do_cancel(body: dict) -> dict:
    """取消当前选牌（手册里写的是 [RMB] Cancel）。"""
    st = None
    xy = None
    if body.get("x") is not None and body.get("y") is not None:
        xy = [int(body["x"]), int(body["y"])]
    if xy is None:
        try:
            st = do_state({"hp": False})
        except Exception:  # noqa: BLE001
            st = None
        rect = (st or {}).get("region")
        if rect:
            xy = [int(rect[0] + rect[2] / 2), int(rect[1] + rect[3] / 2)]
        else:
            raise ValueError("没有坐标也没锚定窗口：先 coach_anchor 锚定游戏，或直接给 x/y。")
    r = _click_xy(body, xy, why="右键取消当前选牌", button="right")
    out = {"ok": bool(r.get("ok")), "did": "cancel", "at": xy, "button": "right",
           "changedTiles": r.get("changedTiles"),
           "verdict": "已右键取消" if r.get("ok") else "取消失败"}
    if not r.get("ok"):
        out["error"] = r.get("error")
    return out


# ---------------------------------------------------------------- HTTP

def obs_sources(host: str = "", port=None, password: str = "") -> dict:
    from obsclient import ObsClient, list_capture_inputs, window_spec_of

    cli = ObsClient(host or OBS_CONF["host"], port or OBS_CONF["port"], password or OBS_CONF["password"])
    try:
        cli.connect()
        scene = cli.request("GetSceneList")
        rows = []
        for c in list_capture_inputs(cli):
            try:
                info = window_spec_of(cli, c["name"])
            except Exception as exc:  # noqa: BLE001
                rows.append({"name": c["name"], "kind": c["kind"], "error": str(exc)})
                continue
            row = find_window(title=info["title"], cls=info["class"], exe=info["exe"]) if info["title"] else None
            rows.append(
                {
                    "name": c["name"],
                    "kind": c["kind"],
                    "windowSpec": info["windowSpec"],
                    "monitor": info["monitor"],
                    "resolved": None if row is None else {
                        "title": row["title"],
                        "hwnd": row["hwnd"],
                        "rect": _clip_rect(row["x"], row["y"], row["w"], row["h"]),
                    },
                }
            )
        return {
            "scene": scene.get("currentProgramSceneName"),
            "sources": rows,
            "obs": {"host": OBS_CONF["host"], "port": OBS_CONF["port"]},
        }
    finally:
        cli.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "coachd/0.2"
    protocol_version = "HTTP/1.1"
    tile = 32

    def log_message(self, fmt, *args):
        if os.environ.get("COACHD_VERBOSE"):
            sys.stderr.write("[coachd] " + (fmt % args) + "\n")

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if n <= 0:
            return {}
        if n > 4 * 1024 * 1024:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def _ok(self, fn):
        try:
            self._send(200, {"ok": True, **fn()})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"ok": False, "error": str(e), "trace": traceback.format_exc(limit=3)})

    def do_GET(self):
        path = (self.path.split("?")[0].rstrip("/")) or "/"
        if path == "/health":
            return self._ok(lambda: {
                "ffmpeg": FFMPEG if os.path.isfile(FFMPEG) else "missing",
                "python": sys.version.split()[0],
                "screen": list(USER32.screen_size()),
                "dpi": DPI_MODE,
                "fps": FRAMES.fps,
                "frames": FRAMES.seq,
                "frameAgeMs": int((time.time() - FRAMES.ts) * 1000) if FRAMES.ts else None,
                "captureError": FRAMES.error,
                "tile": self.tile,
                "anchor": ANCHOR,
                "anchorRegion": current_anchor_region(),
                "obs": {"host": OBS_CONF["host"], "port": OBS_CONF["port"], "passwordSet": bool(OBS_CONF["password"])},
            })
        if path == "/windows":
            return self._ok(lambda: {"windows": USER32.windows()})
        if path == "/anchor":
            return self._ok(lambda: {"anchor": ANCHOR, "region": current_anchor_region()})
        if path == "/obs/sources":
            return self._ok(lambda: obs_sources())
        return self._send(404, {"ok": False, "error": "未知路径 " + path})

    def do_POST(self):
        path = (self.path.split("?")[0].rstrip("/")) or "/"

        if path == "/anchor":
            def set_anchor():
                global ANCHOR
                body = self._body()
                kind = str(body.get("kind") or "obs").strip().lower()
                if kind in ("clear", "none", "off"):
                    ANCHOR = None
                    save_anchor(None)
                    return {"anchor": None, "region": None}
                if kind == "obs":
                    a = anchor_from_obs(
                        str(body.get("source") or ""),
                        str(body.get("host") or ""),
                        body.get("port"),
                        str(body.get("password") or ""),
                        str(body.get("window") or ""),
                    )
                elif kind == "window":
                    a = anchor_from_window(str(body.get("title") or ""), body.get("hwnd"))
                elif kind == "rect":
                    r = parse_region(body.get("rect"))
                    if r is None:
                        raise ValueError("rect 锚定需要 [x,y,w,h]")
                    a = {"kind": "rect", "mode": "rect", "rect": _clip_rect(*r), "at": time.time()}
                else:
                    raise ValueError("kind 只能是 obs / window / rect / clear")
                ANCHOR = a
                save_anchor(a)
                return {"anchor": a, "region": a["rect"]}
            return self._ok(set_anchor)

        if path == "/snapshot":
            def snap():
                body = self._body()
                img, r, seq, ts, src = grab(body.get("region"))
                sig = tile_sig(img, self.tile)
                first, changed = DIFFS.compare((r, self.tile), sig)
                png = io.BytesIO()
                to_pil(img).save(png, format="PNG")
                meta = {
                    "region": list(r),
                    "source": src,
                    "anchor": {k: (ANCHOR.get(k) if isinstance(ANCHOR, dict) else None) for k in ("kind", "title", "exe")},
                    "coord": "screen",
                    "size": [img.shape[1], img.shape[0]],
                    "sha1": hashlib.sha1(png.getvalue()).hexdigest()[:16],
                    "tiles": [int(sig.shape[0]), int(sig.shape[1])],
                    "tilePx": self.tile,
                    "first": first,
                    "changedTiles": None if changed is None else int(changed.sum()),
                    "frameSeq": seq,
                    "frameAgeMs": int((time.time() - ts) * 1000),
                    "ocr": None,
                }
                if _WC is not None and isinstance(ANCHOR, dict) and ANCHOR.get("hwnd"):
                    try:
                        meta["coveredBy"] = _WC.who_covers(int(ANCHOR["hwnd"]), r)
                    except Exception:  # noqa: BLE001
                        meta["coveredBy"] = None
                if body.get("ocr"):
                    res = ocr_pil(to_pil(img), _ocr_scale(body))
                    meta["ocr"] = abs_lines(normalize_lines(res), r)
                    meta["ocrLang"] = res.get("lang")
                    meta["ocrMs"] = res.get("ms")
                    meta["scene"] = classify_scene(normalize_lines(res))
                return {"snapshot": meta}
            return self._ok(snap)

        if path == "/diff":
            def diff_route():
                body = self._body()
                img, r, seq, ts, src = grab(body.get("region"))
                sig = tile_sig(img, self.tile)
                first, changed = DIFFS.compare(("diff", r, self.tile), sig)
                if first or changed is None:
                    return {"first": True, "region": list(r), "count": 0, "changed": [], "frameSeq": seq, "source": src}
                t = self.tile
                boxes = cluster_boxes(changed, t)
                ox, oy = int(r[0]), int(r[1])
                boxes = [[b[0] + ox, b[1] + oy, b[2], b[3]] for b in boxes]
                return {
                    "first": False,
                    "region": list(r),
                    "source": src,
                    "coord": "screen",
                    "count": int(changed.sum()),
                    "boxes": len(boxes),
                    "changed": boxes,
                    "frameSeq": seq,
                    "frameAgeMs": int((time.time() - ts) * 1000),
                }
            return self._ok(diff_route)

        if path == "/watch":
            def watch():
                body = self._body()
                timeout = min(15.0, max(0.2, float(body.get("ms") or 3000) / 1000.0))
                img, r, _s, _t, _src = grab(body.get("region"))
                prev = tile_sig(img, self.tile)
                t0 = time.time()
                stable_for = 0
                while time.time() - t0 < timeout:
                    time.sleep(0.08)
                    img2, _r2, _s2, _t2, _src2 = grab(body.get("region"))
                    sig = tile_sig(img2, self.tile)
                    changed = int((np.abs(sig.astype(int) - prev.astype(int)).sum(axis=2) > 0).sum())
                    prev = sig
                    if changed == 0:
                        stable_for += 80
                        if stable_for >= 240:
                            return {"stable": True, "waitedMs": int((time.time() - t0) * 1000), "region": list(r)}
                    else:
                        stable_for = 0
                return {"stable": False, "waitedMs": int((time.time() - t0) * 1000), "region": list(r)}
            return self._ok(watch)

        if path == "/ocr":
            def ocr_route():
                body = self._body()
                if body.get("image") == "last" and LAST.get("png"):
                    img = Image.open(io.BytesIO(LAST["png"])).convert("RGB")
                    r = LAST.get("region")
                else:
                    arr, r, _s, _t, _src = grab(body.get("region"))
                    img = to_pil(arr)
                res = ocr_pil(img, _ocr_scale(body))
                return {
                    "region": list(r) if r else None,
                    "coord": "screen",
                    "lang": res.get("lang"),
                    "ms": res.get("ms"),
                    "lines": abs_lines(normalize_lines(res), r),
                }
            return self._ok(ocr_route)

        if path == "/frame":
            def frame_route():
                body = self._body()
                check_region(body.get("region"))
                arr, r, seq, _t, src = grab(body.get("region"))
                img = to_pil(arr)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                png = buf.getvalue()
                LAST["png"] = png
                LAST["region"] = r
                out = body.get("path")
                if not out:
                    d = os.path.join(tempfile.gettempdir(), "coachd-frames")
                    os.makedirs(d, exist_ok=True)
                    out = os.path.join(d, "frame-%d.png" % int(time.time() * 1000))
                with open(out, "wb") as fh:
                    fh.write(png)
                return {
                    "path": out,
                    "region": list(r),
                    "source": src,
                    "size": [img.width, img.height],
                    "bytes": len(png),
                    "frameSeq": seq,
                }
            return self._ok(frame_route)

        if path == "/now":
            def now_route():
                """一次调用看清当前局面：场景 + 证据 + 变化块 + 关键文字（省 token 的看盘口）。"""
                body = self._body()
                check_region(body.get("region"))
                img, r, seq, ts, src = grab(body.get("region"))
                cells = _cell_means(img, self.tile)
                key = ("now", r, self.tile)
                prev = NOW_CELLS.get(key)
                NOW_CELLS[key] = cells
                out = {
                    "region": list(r),
                    "source": src,
                    "sha": hashlib.sha1(img.tobytes()).hexdigest()[:16],
                    "frameSeq": seq,
                    "frameAgeMs": int((time.time() - ts) * 1000),
                }
                mask = changed_cells(prev, cells, CELL_DIFF)
                if mask is None:
                    out["first"] = True
                else:
                    boxes = cluster_boxes(mask, self.tile)
                    ox, oy = int(r[0]), int(r[1])
                    out["changed"] = {
                        "count": int(mask.sum()),
                        "boxes": [[b[0] + ox, b[1] + oy, b[2], b[3]] for b in boxes][: int(body.get("maxBlocks") or 6)],
                        "total": len(boxes),
                    }
                    out["animating"] = bool(int(mask.sum()) > STABLE_ANIM_TILES)
                if body.get("ocr") is not False:
                    pil = to_pil(img)
                    top2 = int(pil.size[1] * float(body.get("bandFrom") or 0.5))
                    res = ocr_pil(pil.crop((0, top2, pil.size[0], pil.size[1])), _ocr_scale(body))
                    lines = normalize_lines(res)
                    for ln in lines:
                        ln["x"] += int(r[0])
                        ln["y"] += int(r[1]) + top2
                    scored = classify_scene_scored(lines)
                    tpl = scene_match_templates(img, lines)
                    vis = scene_visual(img)
                    out["scene"] = scored["scene"]
                    out["sceneZh"] = SCENE_ZH.get(scored["scene"], scored["scene"])
                    out["confidence"] = scored["confidence"]
                    out["evidence"] = scored["evidence"]
                    out["textReadable"] = bool(lines)
                    if vis:
                        out["visual"] = vis["detail"]
                    if vis and (scored["scene"] == "unknown" or
                                (scored["confidence"] < 0.7 and scored["scene"] not in GUARDED_SCENES)):
                        out["scene"] = vis["scene"]
                        out["sceneZh"] = SCENE_ZH.get(vis["scene"], vis["scene"])
                        out["confidence"] = vis["confidence"]
                        out["evidence"] = vis["evidence"] + list(scored["evidence"])
                        out["sceneSource"] = "visual"
                    out["lines"] = lines[: int(body.get("maxLines") or 12)]
                    out["ocrMs"] = res.get("ms")
                    if tpl and out["scene"] in ("unknown",) | set(GUARDED_SCENES):
                        # 用户注册过的场景优先（战斗/未知这种最容易认错的场合）
                        out["scene"] = tpl["scene"]
                        out["sceneZh"] = tpl["scene"]
                        out["confidence"] = tpl["confidence"]
                        out["evidence"] = tpl["evidence"]
                        out["sceneSource"] = "template"
                if body.get("boxes") and out.get("lines"):
                    out["boxes"] = cluster_boxes(out["lines"])
                return out
            return self._ok(now_route)

        if path == "/click_text":
            def click_text_route():
                """按文字点：从 OCR 结果里找含指定文字的整行，点它的中心。菜单/选项/按钮一步到位。"""
                body = self._body()
                if not INPUT_OK:
                    raise RuntimeError("输入模块加载失败")
                needle = str(body.get("text") or "").strip()
                if not needle:
                    raise ValueError("缺少 text：要点的文字")
                img, r, _seq, _ts, _src = grab(None)
                res = ocr_pil(to_pil(img), _ocr_scale(body))
                lines = abs_lines(normalize_lines(res), r)
                cands = [ln for ln in lines if needle in ln["text"]]
                if not cands:
                    cands = [ln for ln in lines if needle.replace(" ", "") in ln["text"].replace(" ", "")]
                if not cands:
                    raise RuntimeError(f"画面里没找到「{needle}」。OCR 到的行：" + " / ".join(ln["text"] for ln in lines[:10]))
                idx = max(0, min(len(cands) - 1, int(body.get("index") or 0)))
                ln = cands[idx]
                x = ln["x"] + ln["w"] // 2
                y = ln["y"] + ln["h"] // 2 + int(body.get("dy") or 0)
                if str(body.get("mode") or "input").lower() == "post":
                    hwnd = int(body.get("hwnd") or (ANCHOR.get("hwnd") if isinstance(ANCHOR, dict) else 0) or 0)
                    sent = INPUT.post_click(hwnd, x, y)
                else:
                    INPUT.ensure_foreground(int(ANCHOR.get("hwnd") or 0), restore=body.get("restore") is not False)
                    sent = INPUT.click(x, y)
                log_input({"at": round(time.time(), 3), "x": x, "y": y, "via": "click_text", "text": ln["text"]})
                return {"clicked": {"x": x, "y": y, "text": ln["text"]}, "sent": sent, "matches": len(cands)}
            return self._ok(click_text_route)

        if path == "/entities":
            def entities_route():
                """画面 → 实体表：编号 + 类型 + 文字 + 数值 + 框内/屏幕坐标（可选 SoM 标号图）。"""
                body = self._body()
                if ENT is None:
                    raise RuntimeError("实体层未安装：sidecar/entities.py 缺失")
                img_path = body.get("imagePath")
                if img_path:
                    pil = Image.open(str(img_path)).convert("RGB")
                    r = (0, 0, pil.width, pil.height)
                    src = "file"
                    lines = normalize_lines(ocr_pil(pil, _ocr_scale(body)))
                else:
                    arr, r, _s, _t, src = grab(body.get("region"))
                    pil = to_pil(arr)
                    lines = abs_lines(normalize_lines(ocr_pil(pil, _ocr_scale(body))), r)
                blocks = ENT.group_lines(lines, int(body.get("gap") or 14))
                akey = "file"
                if not img_path and isinstance(ANCHOR, dict):
                    akey = str(ANCHOR.get("title") or ANCHOR.get("hwnd") or "screen")
                state = ENT_STATE.get(akey) or {}
                ents, st = ENT.build_entities(
                    blocks, pil.width, pil.height, (int(r[0]), int(r[1])), state,
                    int(body.get("maxEntities") or 60),
                )
                ENT_STATE[akey] = st
                try:
                    ENT.save_state(ENT_STATE_PATH, ENT_STATE)
                except OSError:
                    pass
                out = {
                    "scene": classify_scene(lines),
                    "source": src,
                    "region": list(r),
                    "count": len(ents),
                    "entities": ents,
                }
                if body.get("som"):
                    try:
                        over = ENT.som_overlay(pil, ents)
                        path_png = os.path.join(FRAME_DIR, "som-%d.png" % int(time.time() * 1000))
                        os.makedirs(FRAME_DIR, exist_ok=True)
                        over.save(path_png)
                        out["somPath"] = path_png
                    except Exception as exc:  # noqa: BLE001
                        out["somError"] = str(exc)
                prev = [e for e in (LAST.get("entities") or []) if isinstance(e, dict)]
                prev_by = {e["id"]: e for e in prev}
                cur_by = {e["id"]: e for e in ents}
                out["changed"] = {
                    "new": [i for i in cur_by if i not in prev_by][:10],
                    "gone": [i for i in prev_by if i not in cur_by][:10],
                    "valueChanged": [
                        [i, prev_by[i]["value"], cur_by[i]["value"]]
                        for i in cur_by
                        if i in prev_by and prev_by[i].get("value") != cur_by[i].get("value")
                    ][:10],
                }
                LAST["entities"] = ents
                return out
            return self._ok(entities_route)

        if path == "/facts":
            def facts_route():
                """画面 → 规则引擎可直接吃的 facts（scene + ui 文字 + 实体表 + 数值字典）。"""
                body = self._body()
                if ENT is None:
                    raise RuntimeError("实体层未安装：sidecar/entities.py 缺失")
                img_path = body.get("imagePath")
                if img_path:
                    pil = Image.open(str(img_path)).convert("RGB")
                    r = (0, 0, pil.width, pil.height)
                    src = "file"
                    lines = normalize_lines(ocr_pil(pil, _ocr_scale(body)))
                else:
                    arr, r, _s, _t, src = grab(body.get("region"))
                    pil = to_pil(arr)
                    lines = abs_lines(normalize_lines(ocr_pil(pil, _ocr_scale(body))), r)
                blocks = ENT.group_lines(lines, int(body.get("gap") or 14))
                ents, _state = ENT.build_entities(
                    blocks, pil.width, pil.height, (int(r[0]), int(r[1])), {}, int(body.get("maxEntities") or 60)
                )
                values = {}
                out_ents = []
                for e in ents:
                    text = str(e.get("text") or "")
                    label = re.sub(r"[0-9０-９.,:/％%\-]+", "", text).strip()
                    value = e.get("value")
                    sb = e.get("screenBox") or [0, 0, 0, 0]
                    out_ents.append(
                        {
                            "id": "e%d" % int(e.get("id") or 0),
                            "kind": e.get("kind"),
                            "label": label or text,
                            "text": text,
                            "value": value,
                            "x": int(sb[0]),
                            "y": int(sb[1]),
                            "w": int(sb[2]),
                            "h": int(sb[3]),
                        }
                    )
                    if label and value not in (None, "") and label not in values:
                        values[label] = value
                return {
                    "scene": classify_scene(lines),
                    "source": src,
                    "region": list(r),
                    "ageMs": 0,
                    "ui": {"lines": [str(l.get("text")) for l in lines if l.get("text")][:12]},
                    "entities": out_ents,
                    "values": values,
                }
            return self._ok(facts_route)

        if path == "/input/click":
            return self._ok(lambda: do_click(self._body()))

        if path == "/input/key":
            def send_key():
                body = self._body()
                if not INPUT_OK:
                    raise RuntimeError("输入模块加载失败（win_input.py 缺失或被拦截）")
                hwnd = int(body.get("hwnd") or (ANCHOR.get("hwnd") if isinstance(ANCHOR, dict) else 0) or 0)
                restore = body.get("restore") is not False
                post_mode = str(body.get("mode") or "input").lower() == "post"
                key = str(body.get("key") or "")
                # 危险键闸门：战斗/结算界面里 space、enter = 自动结算，先拦。
                scene = str(body.get("scene") or "")
                if not scene or body.get("probeScene") is True:
                    try:
                        scene = scene_probe(grab(None)[0])
                    except Exception:  # noqa: BLE001
                        scene = "unknown"
                g = guard_key(key, scene, force=body.get("force") is True)
                if g:
                    log_input({"at": round(time.time(), 3), "hwnd": hwnd, "key": key,
                               "blocked": True, "scene": scene})
                    return g
                if body.get("dryRun") is True:
                    return {"dryRun": True, "key": key, "scene": scene, "wouldSend": True,
                            "foreground": fg_now()}
                sent = 0
                before = None
                region = None
                if body.get("verify") is not False:
                    before_img, region, _s, _t, _src = grab(None)
                    before = _cell_means(before_img)
                if post_mode:
                    if not hwnd:
                        raise RuntimeError("后台模式需要 hwnd：先 coach_anchor 锚定游戏窗口")
                    sent = INPUT.post_key(hwnd, key)
                else:
                    if hwnd:
                        claim = INPUT.force_foreground(hwnd, tries=3)
                        if not claim.get("ok"):
                            raise RuntimeError(
                                f"按不了：抢不到前台。请求 {hwnd}，真实前台是 {claim.get('actual')}"
                                f"（{claim.get('actualTitle') or '无标题'}）")
                    sent = INPUT.press(key, int(body.get("holdMs") or 30))
                fg = fg_now()
                changed = None
                if before is not None:
                    time.sleep(max(120, int(body.get("verifyMs") or 450)) / 1000.0)
                    after_img, _r2, _s2, _t2, _src2 = grab(None)
                    cells = changed_cells(before, _cell_means(after_img), CELL_DIFF)
                    changed = int(cells.sum()) if cells is not None else None
                out = {"sent": sent, "hwnd": hwnd, "key": key, "mode": "post" if post_mode else "input",
                       "requested_hwnd": hwnd, "actual_foreground_hwnd": fg["hwnd"],
                       "actual_foreground_title": fg["title"],
                       "foreground_ok": (not hwnd) or fg["hwnd"] == hwnd,
                       "scene": scene, "sceneZh": SCENE_ZH.get(scene, scene), "changedTiles": changed}
                if changed is not None and changed <= 0:
                    out["verdict"] = "no_change"
                    out["hint"] = "按键发出去了但画面没变：这个键在当前界面可能无效（游戏写着能按不等于真能按）。"
                log_input({"at": round(time.time(), 3), "hwnd": hwnd, "key": key,
                           "foreground": fg["hwnd"], "foregroundTitle": fg["title"],
                           "changed": changed, "scene": scene, "sent": sent})
                return out
            return self._ok(send_key)

        # ---------------------------------------------------------- 新：护栏 / 稳定 / 元素表
        if path == "/guard":
            def guard_route():
                body = self._body()
                scene = body.get("scene")
                if not scene:
                    try:
                        scene = scene_probe(grab(None)[0])
                    except Exception:  # noqa: BLE001
                        scene = "unknown"
                key = body.get("key")
                if key:
                    g = guard_key(str(key), str(scene), force=body.get("force") is True)
                    return {"scene": scene, "sceneZh": SCENE_ZH.get(str(scene), str(scene)),
                            "key": key, "verdict": "refuse" if g else "allow", **(g or {})}
                return {"scene": scene, "sceneZh": SCENE_ZH.get(str(scene), str(scene)), **guard_table()}
            return self._ok(guard_route)

        if path == "/wait_stable":
            def stable_route():
                body = self._body()
                return wait_stable(
                    body.get("region"),
                    settle_ms=int(body.get("settleMs") or 400),
                    timeout_ms=int(body.get("timeoutMs") or 8000),
                    min_tiles=int(body.get("minTiles") or 0),
                    thr=body.get("cellDiff"),
                    ignore=body.get("ignore"),
                )
            return self._ok(stable_route)

        if path == "/ui_dump":
            def dump_route():
                body = self._body()
                scene = body.get("scene")
                if not scene:
                    try:
                        scene = scene_probe(grab(body.get("region"))[0])
                    except Exception:  # noqa: BLE001
                        scene = "unknown"
                return ui_dump(body.get("region"), ocr_scale=_ocr_scale(body),
                               limit=int(body.get("limit") or 40), scene=scene)
            return self._ok(dump_route)

        if path == "/click":
            return self._ok(lambda: do_click(self._body()))

        if path == "/click_until":
            return self._ok(lambda: click_until(self._body()))

        if path == "/state":
            return self._ok(lambda: do_state(self._body()))

        if path == "/execute":
            return self._ok(lambda: do_execute(self._body()))

        if path == "/select_page":
            return self._ok(lambda: do_select_page(self._body()))

        if path == "/target_enemy":
            return self._ok(lambda: do_target_enemy(self._body()))

        if path == "/cancel_selection":
            return self._ok(lambda: do_cancel(self._body()))

        if path == "/dismiss":
            return self._ok(lambda: dismiss(self._body()))

        if path == "/key_probe":
            return self._ok(lambda: key_probe(self._body()))

        if path == "/click_log":
            def log_route():
                body = self._body()
                return read_click_log(int(body.get("limit") or 20))
            return self._ok(log_route)

        if path == "/scene/define":
            return self._ok(lambda: scene_define(self._body()))

        if path == "/scene/list":
            return self._ok(lambda: scene_list())

        if path == "/drive":
            def drive():
                """一步顶一串：按同一个键推进，直到画面不再变 / 场景变了 / 次数用完。

                护栏放在**按第一次之前**：先认场景，再决定要不要按。
                之前的版本是"先按了再说"——战斗界面按 space 直接自动结算，一场接待白打。
                """
                body = self._body()
                if not INPUT_OK:
                    raise RuntimeError("输入模块加载失败")
                key = str(body.get("key") or "space")
                max_steps = max(1, min(120, int(body.get("maxSteps") or 30)))
                interval = max(0.12, min(3.0, float(body.get("intervalMs") or 400) / 1000.0))
                hwnd = int(body.get("hwnd") or (ANCHOR.get("hwnd") if isinstance(ANCHOR, dict) else 0) or 0)
                post_mode = str(body.get("mode") or "input").lower() == "post"
                force = body.get("force") is True
                dry = body.get("dryRun") is True
                require = body.get("requireScene")
                require = str(require) if require else None
                stop_scenes = body.get("stopScenes") or ["choice", "battle", "battle_setup", "result"]

                img0, r0, _s0, _t0, src0 = grab(None)
                scene0 = scene_probe(img0)
                g = guard_key(key, scene0, force=force)
                if g:
                    log_input({"at": round(time.time(), 3), "hwnd": hwnd, "key": key,
                               "blocked": True, "via": "drive", "scene": scene0})
                    return {"refused": True, "pressed": 0, "scene": scene0,
                            "sceneZh": SCENE_ZH.get(scene0, scene0), **g,
                            "requireScene": require}
                if require and scene0 != require:
                    return {"refused": True, "pressed": 0, "scene": scene0,
                            "sceneZh": SCENE_ZH.get(scene0, scene0),
                            "reason": f"requireScene={require} 不满足（当前是 {SCENE_ZH.get(scene0, scene0)}），一个键都没按。"}
                if dry:
                    return {"dryRun": True, "pressed": 0, "scene": scene0,
                            "sceneZh": SCENE_ZH.get(scene0, scene0),
                            "plan": f"会按 {key} 最多 {max_steps} 次，间隔 {int(interval * 1000)}ms"}

                fg_before = 0
                if not post_mode:
                    fg_before = int(INPUT.user32.GetForegroundWindow() or 0)
                    claim = INPUT.force_foreground(hwnd, tries=3) if hwnd else {"ok": True}
                    if not claim.get("ok"):
                        raise RuntimeError(
                            f"推不动：抢不到前台。请求 {hwnd}，真实前台是 {claim.get('actual')}"
                            f"（{claim.get('actualTitle') or '无标题'}）")
                pressed = 0
                prev = None
                reason = "max_steps"
                scene = scene0
                scene_first = scene0
                steps: list[dict] = []
                for i in range(max_steps):
                    img, _r, _seq, _ts, _src = grab(None)
                    pil = to_pil(img)
                    top = int(pil.size[1] * 0.55)
                    res = ocr_pil(pil.crop((0, top, pil.size[0], pil.size[1])), _ocr_scale(body))
                    band = normalize_lines(res)
                    scored = classify_scene_scored(band)
                    scene = scored["scene"]
                    if scene in stop_scenes:
                        reason = "scene:" + scene
                        steps.append({"n": i + 1, "pressed": False, "scene": scene,
                                      "confidence": scored["confidence"], "evidence": scored["evidence"],
                                      "why": "到了 stopScenes"})
                        break
                    sha = hashlib.sha1(img.tobytes()).hexdigest()[:16]
                    if prev is not None and sha == prev:
                        reason = "no_change"
                        steps.append({"n": i + 1, "pressed": False, "scene": scene,
                                      "why": "和上一帧一模一样，按键没效果"})
                        break
                    prev = sha
                    if post_mode:
                        INPUT.post_key(hwnd, key)
                    else:
                        INPUT.press(key, 30)
                    pressed += 1
                    steps.append({"n": i + 1, "pressed": True, "scene": scene,
                                  "confidence": scored["confidence"]})
                    log_input({"at": round(time.time(), 3), "hwnd": hwnd, "key": key,
                               "via": "drive", "mode": "post" if post_mode else "input",
                               "scene": scene, "foreground": fg_now()["hwnd"]})
                    time.sleep(interval)
                    if scene != scene_first and scene in GUARDED_SCENES:
                        reason = f"scene_changed:{scene_first}->{scene}"
                        steps[-1]["why"] = f"第 {i + 1} 步场景从 {SCENE_ZH.get(scene_first, scene_first)} 变成了 {SCENE_ZH.get(scene, scene)}，停手"
                        break
                img, r2, _seq2, _ts2, src2 = grab(None)
                pil2 = to_pil(img)
                if body.get("fullOcr"):
                    res = ocr_pil(pil2, _ocr_scale(body))
                    lines = normalize_lines(res)
                else:
                    w2, h2 = pil2.size
                    top2 = int(h2 * 0.55)
                    res = ocr_pil(pil2.crop((0, top2, w2, h2)), _ocr_scale(body))
                    lines = normalize_lines(res)
                    for ln in lines:  # 把下半条的坐标还原成整窗坐标
                        ln["y"] += top2
                        ln["x"] += int(r2[0])
                        ln["y"] += int(r2[1])
                final = classify_scene_scored(lines)
                if final["scene"] != "unknown":
                    scene = final["scene"]
                if fg_before and fg_before != hwnd and body.get("restoreFocus") is not False:
                    try:
                        INPUT.user32.ShowWindow(fg_before, 9)  # SW_RESTORE
                        INPUT.SetForeground(fg_before)
                    except Exception:  # noqa: BLE001
                        pass
                out = {
                    "pressed": pressed,
                    "stoppedBy": reason,
                    "scene": scene,
                    "sceneZh": SCENE_ZH.get(scene, scene),
                    "confidence": final["confidence"],
                    "evidence": final["evidence"],
                    "region": list(r2),
                    "source": src2,
                    "sha": hashlib.sha1(img.tobytes()).hexdigest()[:16],
                    "ocrMs": res.get("ms"),
                    "lines": lines[:40],
                    "steps": steps[:12],
                }
                if reason == "no_change":
                    out["why"] = "下一帧和上一帧一模一样：这个键在当前界面按了没反应（界面可能根本不吃这个键）。"
                elif reason.startswith("scene:"):
                    out["why"] = f"第 {pressed + 1} 步认到 {SCENE_ZH.get(scene, scene)}，按约定停手。"
                return out
            return self._ok(drive)

        return self._send(404, {"ok": False, "error": "未知路径 " + path})


LAST: dict = {}

try:  # 实体层（可插拔：没有这个文件也能跑其余工具）
    import entities as ENT  # noqa: N816
except Exception:  # noqa: BLE001
    ENT = None
ESTATE: dict = {}
ENT_STATE_PATH = os.path.join(os.path.expanduser("~"), ".dsh", "coach-perceive", "entities.json")
FRAME_DIR = os.path.join(tempfile.gettempdir(), "coachd-frames")
ENT_STATE: dict = ENT.load_state(ENT_STATE_PATH) if ENT else {}


def main() -> None:
    ap = argparse.ArgumentParser(description="coachd 游戏感知 sidecar（只读）")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--tile", type=int, default=32)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--obs-host", default=os.environ.get("COACH_OBS_HOST", "127.0.0.1"))
    ap.add_argument("--obs-port", type=int, default=int(os.environ.get("COACH_OBS_PORT", "4455")))
    ap.add_argument("--obs-password", default=os.environ.get("COACH_OBS_PASSWORD", ""))
    args = ap.parse_args()

    OBS_CONF["host"] = args.obs_host
    OBS_CONF["port"] = int(args.obs_port)
    OBS_CONF["password"] = args.obs_password or ""
    Handler.tile = max(8, args.tile)
    FRAMES.fps = max(1, args.fps)
    FRAMES.start()

    global ANCHOR
    ANCHOR = load_anchor()
    if ANCHOR:
        sys.stderr.write("恢复上次锚定：%s\n" % json.dumps(
            {k: ANCHOR.get(k) for k in ("kind", "title", "hwnd")}, ensure_ascii=False))
        sys.stderr.flush()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    port = srv.server_address[1]
    sys.stdout.write(json.dumps({"ok": True, "port": port, "pid": os.getpid(), "screen": list(USER32.screen_size())}) + "\n")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        FRAMES.stop()


if __name__ == "__main__":
    main()
