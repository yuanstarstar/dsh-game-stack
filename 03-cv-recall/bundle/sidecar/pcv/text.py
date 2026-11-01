# -*- coding: utf-8 -*-
"""数值读取：Windows.Media.Ocr，经常驻 PowerShell 桥。

为什么不用模板匹配数字：游戏字体虽然固定，但换语言/换字体/换分辨率就全废，而且
「0」和「8」、「1」和「7」在 12px 高的时候本来就糊。系统 OCR 一次只花几十毫秒，
比为一个数字建一套字体表划算得多。

为什么是常驻进程：每次冷启动 PowerShell + 初始化 WinRT 要 ~800ms，而真正识别的
只有几十毫秒。常驻后逐行 stdin/stdout 往返，代价就只剩识别本身。
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time

import numpy as np
from PIL import Image

OCR_PS = r"""
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
if ($null -eq $engine) { [Console]::Out.WriteLine('{"error":"no-ocr-engine"}'); exit 0 }

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
    # 每个字的字高离散度：真文字一行里字高几乎一样；把图标当字凑出来的一行，
    # 词高会飞来飞去（PS 5.1 的 Measure-Object 没有 -StandardDeviation，手算）。
    $hs = @($words | ForEach-Object { [double]$_.BoundingRect.Height })
    $hm = 0.0; $sd = 0.0
    if ($hs.Count -gt 0) {
      $hm = ($hs | Measure-Object -Average).Average
      $acc = 0.0
      foreach ($v in $hs) { $acc += [math]::Pow($v - $hm, 2) }
      $sd = [math]::Sqrt($acc / [math]::Max(1, $hs.Count))
    }
    $null = $lines.Add([pscustomobject]@{
      text = [string]$line.Text
      x = [int][math]::Round($minX); y = [int][math]::Round($minY)
      w = [int][math]::Round($maxX - $minX); h = [int][math]::Round($maxY - $minY)
      nw = [int]$words.Count
      hm = [math]::Round($hm, 2)
      hs = [math]::Round($(if ($hm -gt 0) { $sd / $hm } else { 0 }), 3)
    })
  }
  return (@{ lines = @($lines) } | ConvertTo-Json -Depth 4 -Compress)
}

$in = [Console]::In
while ($true) {
  $line = $in.ReadLine()
  if ($null -eq $line) { break }
  $line = $line.Trim()
  if ($line -eq '') { continue }
  if ($line -eq 'QUIT') { break }
  try { $json = Do-Ocr $line }
  catch { $json = (@{ error = [string]$_.Exception.Message } | ConvertTo-Json -Compress) }
  [Console]::Out.WriteLine($json)
  [Console]::Out.Flush()
}
"""

_LOCK = threading.Lock()
_WORKER = None
_DEAD = False
_SCRIPT = None
_STATE = {"ms": 0, "calls": 0}

# 字高离散度门限：见 `_plausible` / `_crop_is_text` 的注释（真文字 ≪0.2，垃圾 ≥0.45）。
HS_LINE = 0.30
HS_CROP = 0.38


def powershell() -> str:
    p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return p if os.path.isfile(p) else "powershell.exe"


def _script() -> str:
    global _SCRIPT
    if _SCRIPT is None:
        fd, path = tempfile.mkstemp(prefix="cvrecall-ocr-", suffix=".ps1")
        os.close(fd)
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write(OCR_PS)
        _SCRIPT = path
    return _SCRIPT


def _worker():
    global _WORKER, _DEAD
    if _DEAD:
        return None
    if _WORKER is not None and _WORKER.poll() is None:
        return _WORKER
    try:
        _WORKER = subprocess.Popen(
            [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _script()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, creationflags=0x08000000,   # CREATE_NO_WINDOW
        )
        return _WORKER
    except Exception:            # noqa: BLE001
        _DEAD = True
        return None


def _readline(worker, timeout: float = 20.0) -> bytes:
    box = {}

    def go():
        try:
            box["line"] = worker.stdout.readline()
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise RuntimeError("OCR 超时")
    if "err" in box:
        raise box["err"]
    return box.get("line") or b""


def available() -> bool:
    """OCR 能不能用（第一次调用会付 ~1s 的 PowerShell/WinRT 启动费）。"""
    try:
        return _worker() is not None
    except Exception:            # noqa: BLE001
        return False


def read_text(rgb, scale: float = 2.0) -> dict:
    """识别一块 RGB 区域里的文字，返回 {"text":..., "lines":[...], "ms":N}。"""
    global _DEAD
    img = Image.fromarray(np.asarray(rgb, np.uint8))
    s = float(scale or 1.0)
    if s != 1.0:
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))),
                         Image.LANCZOS)
    fd, png = tempfile.mkstemp(prefix="cvrecall-ocr-", suffix=".png")
    os.close(fd)
    try:
        img.save(png)
        t0 = time.perf_counter()
        with _LOCK:
            w = _worker()
            if w is None:
                raise RuntimeError("OCR 不可用（找不到 PowerShell 或 WinRT OCR）")
            w.stdin.write((png + "\n").encode("utf-8"))
            w.stdin.flush()
            line = _readline(w)
            if not line:
                _DEAD = True
                raise RuntimeError("OCR 进程没有输出（已标记不可用）")
            data = json.loads(line.decode("utf-8", "replace"))
        if data.get("error"):
            raise RuntimeError("OCR 不可用: " + str(data["error"]))
        lines = data.get("lines") or []
        if isinstance(lines, dict):
            lines = [lines]
        text = " ".join(str(ln.get("text", "")) for ln in lines).strip()
        ms = int((time.perf_counter() - t0) * 1000)
        _STATE["ms"] += ms
        _STATE["calls"] += 1
        return {"text": text, "lines": lines, "ms": ms}
    finally:
        try:
            os.unlink(png)
        except OSError:
            pass


# ---------------------------------------------------------------- 行→块→实体
_CJK = "\\u3400-\\u9fff\\uf900-\\ufaff"
_CJK_GAP = re.compile("(?<=[" + _CJK + r"])\s+(?=[" + _CJK + "])")
_PUNCT_GAP = re.compile(r"\s*([:：/、，,。！!？?])\s*")
_SPACE2 = re.compile(r"[ \t\u3000]{2,}")
_NUM = re.compile(r"\d+(?:[~\-]\d+)?(?:/\d+)?")

# 一行文字被判成什么，用来给下游一个"这块大概是什么"的先验，不追求精确。
_KIND_WORDS = (
    ("button", ("确定", "确认", "取消", "退出", "开始", "继续", "返回", "关闭", "下一步",
                "离开", "放弃", "保存", "购买", "使用", "装备", "结束", "重试", "跳过",
                "ok", "cancel", "back", "start", "continue", "close", "next", "confirm",
                "yes", "no", "quit", "exit", "save", "buy", "use", "retry", "skip")),
    ("stat", ("体力", "生命值", "生命", "攻击", "防御", "速度", "金币", "血量", "魔法",
              "经验", "等级", "力量", "敏捷", "幸运", "护甲",
              "hp", "mp", "damage", "attack", "defense", "speed", "cost", "price",
              "exp", "level", "coin", "gold", "armor", "luck")),
)


def clean_text(text: str) -> str:
    """去掉「汉字-空格-汉字」中间的空格、收掉标点两侧的空白、合并多余空格。

    英文词之间的空格必须保留（`Open World` 不能粘成一个词）。
    """
    s = str(text or "")
    prev = None
    while prev != s:          # 单次 re.sub 会因重叠漏删，收敛到不动为止
        prev = s
        s = _CJK_GAP.sub("", s)
    s = _PUNCT_GAP.sub(r"\1", s)
    s = _SPACE2.sub(" ", s)
    return s.strip()


def _union(boxes):
    xs = [b[0] for b in boxes]
    ys = [b[1] for b in boxes]
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [int(min(xs)), int(min(ys)), int(x1 - min(xs)), int(y1 - min(ys))]


def group_lines(lines, gap: int = 14, max_w=None) -> list:
    """行 → 块。先按"同一行带"聚簇；再按水平间隙 + 总宽上限链式合并。

    三条规则缺一不可：垂直重叠够多（同一行）、水平空隙够小（同一句话）、合并后
    不太宽（不要把并排两栏粘成一行）。只给前两条时，左右两栏只隔十几像素就会被
    粘成一整行，所以 `max_w` 不是可选项，调用方应传「画面宽 × 0.45」左右的值。
    """
    items = []
    for ln in lines or []:
        txt = clean_text(str(ln.get("text", "")))
        if not txt:
            continue
        items.append({"text": txt, "x": int(ln.get("x", 0)), "y": int(ln.get("y", 0)),
                      "w": max(1, int(ln.get("w", 1))), "h": max(1, int(ln.get("h", 1)))})
    if not items:
        return []

    rows = []
    for it in sorted(items, key=lambda a: (a["y"], a["x"])):
        for row in rows:
            ov = min(row["y"] + row["h"], it["y"] + it["h"]) - max(row["y"], it["y"])
            if ov >= 0.5 * min(row["h"], it["h"]):        # same_band：垂直重叠过半
                row["items"].append(it)
                row["box"] = _union([(row["x"], row["y"], row["w"], row["h"]),
                                     (it["x"], it["y"], it["w"], it["h"])])
                row["x"], row["y"], row["w"], row["h"] = row["box"]
                break
        else:
            rows.append({"x": it["x"], "y": it["y"], "w": it["w"], "h": it["h"],
                         "items": [it], "box": [it["x"], it["y"], it["w"], it["h"]]})

    blocks = []
    for row in rows:
        cur = None
        for p in sorted(row["items"], key=lambda a: a["x"]):
            if cur is None:
                cur = {"text": p["text"], "parts": [p], "box": [p["x"], p["y"], p["w"], p["h"]]}
                continue
            cx = cur["box"][0] + cur["box"][2]
            gp = p["x"] - cx                                        # 水平间隙
            add_w = max(cx, p["x"] + p["w"]) - cur["box"][0]         # 合并后的总宽
            if gp <= gap and (max_w is None or add_w <= max_w):      # near + fits
                cur["parts"].append(p)
                cur["text"] = clean_text(cur["text"] + " " + p["text"])
                cur["box"] = _union([cur["box"], [p["x"], p["y"], p["w"], p["h"]]])
            else:
                blocks.append(cur)
                cur = {"text": p["text"], "parts": [p], "box": [p["x"], p["y"], p["w"], p["h"]]}
        if cur:
            blocks.append(cur)
    return [{"text": b["text"], "box": b["box"], "n": len(b["parts"])} for b in blocks]


def _hit(low: str, w: str) -> bool:
    if w.isascii():                      # 英文要按整词匹配，否则 no 会命中 north
        return re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low) is not None
    return w in low


def kind_of(text: str, box, frame_w: int, frame_h: int) -> str:
    low = str(text or "").lower()
    for kind, words in _KIND_WORDS:
        for w in words:
            if _hit(low, w):
                return kind
    if re.fullmatch(r"[\d\s.,:~/+\-]+", low):
        return "number"
    x, y, w, h = box
    if y + h <= frame_h * 0.12 and w >= max(60, int(frame_w * 0.25)):
        return "title"
    return "text"


def value_of(text: str):
    """一块文字里的数值：只认"整块就一个数"的情况，出现多个数字就不猜。"""
    nums = _NUM.findall(str(text or ""))
    return nums[0] if len(nums) == 1 else None


_IDS = {}
_ID_SEQ = [0]


def stable_id(kind: str, text: str, box, bucket: int = 48) -> str:
    """同一个人出现在连续帧里要给同一个 id：用「类型 + 文本 + 位置桶」当身份。"""
    key = "%s|%s|%d,%d" % (kind, re.sub(r"\s+", "", str(text or "")).lower(),
                           int(box[0]) // bucket, int(box[1]) // bucket)
    n = _IDS.get(key)
    if n is None:
        _ID_SEQ[0] += 1
        n = _IDS[key] = _ID_SEQ[0]
    return "t%d" % n


BANDS = {"bottom": (0.50, 1.00), "top": (0.00, 0.45),
         "mid": (0.35, 0.75), "full": (0.00, 1.00)}


def band_box(H: int, W: int, band: str = "bottom"):
    a, b = BANDS.get(str(band or "bottom").lower(), BANDS["bottom"])
    y0 = int(H * a)
    y1 = int(H * b)
    return [0, y0, int(W), max(1, y1 - y0)]


def _same_place(a, b) -> bool:
    """两个框是不是"同一个位置上的东西"（合并/拆行时会一个包含另一个）。"""
    ax, ay, aw, ah = [float(v) for v in a]
    bx, by, bw, bh = [float(v) for v in b]
    ix = min(ax + aw, bx + bw) - max(ax, bx)
    iy = min(ay + ah, by + bh) - max(ay, by)
    if ix > 0 and iy > 0 and ix * iy / max(1.0, aw * ah + bw * bh - ix * iy) >= 0.25:
        return True
    acx, acy = ax + aw / 2, ay + ah / 2
    bcx, bcy = bx + bw / 2, by + bh / 2
    pad = 0.25
    if (bx - bw * pad <= acx <= bx + bw * (1 + pad)
            and by - bh * pad <= acy <= by + bh * (1 + pad)):
        return True
    if (ax - aw * pad <= bcx <= ax + aw * (1 + pad)
            and ay - ah * pad <= bcy <= ay + ah * (1 + pad)):
        return True
    return False


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _readability(text: str) -> float:
    """一段 OCR 文本"像不像真的字"：汉字/字母/数字的占比，孤立的怪符号要扣分。"""
    s = _norm(text)
    if not s:
        return 0.0
    good = len(re.findall(r"[0-9a-z" + _CJK + "]", s))
    bad = len(re.findall(r"[^0-9a-z" + _CJK + r"\s]", s))
    return (good - 0.5 * bad) / max(1, len(s))


def similarity(a: str, b: str) -> float:
    """两段文字的相似度（0~1）。

    除了完全一致，还认"包含"：换缩放后 OCR 可能把两个按钮并成一行，或把一行拆成两行，
    这时短的那段是长的那段的子串，应当算作"读到的是同一句话"。
    """
    a2, b2 = _norm(a), _norm(b)
    if not a2 or not b2:
        return 0.0
    if a2 == b2:
        return 1.0
    if len(a2) >= 2 and len(b2) >= 2 and (a2 in b2 or b2 in a2):
        return 1.0
    ca, cb = set(a2), set(b2)
    return len(ca & cb) / max(1, len(ca | cb))


def _plausible(lines, crop_h: int):
    """把明显不是文字的 OCR 结果剔掉。

    三条：太矮/太高的行不要；一个字母数字都没有的"标点串"不要；**一行里字高忽大忽小
    的不要**（`hs` = 字高标准差/平均值）。第三条是按图标凑出来的假文本的硬特征——
    实测真文字 hs 在 0.02~0.16，把 HUD 图标当字读出来的垃圾行在 0.45~0.67。
    """
    keep = []
    for ln in lines:
        h = int(ln.get("h") or 0)
        if h < 8 or h > max(24, int(crop_h * 0.6)):
            continue
        txt = str(ln.get("text") or "")
        if not re.search(r"[0-9A-Za-z" + _CJK + "]", txt):
            continue
        # 以前这里把"只有一个不同字符"的行一律丢掉（本意是剔掉图标落单），
        # 结果把「是/否/敌/我/1」这类单字按钮全吃了——用户实测「两字内文本被过滤」。
        # 现在只丢确实像图标的：单字 + 框很小或很扁；正常大小的单字放行。
        if len(set(_norm(txt))) < 2:
            w = int(ln.get("w") or 0)
            if h < 14 or w < 10 or w > max(56, h * 4):
                continue
        hs = ln.get("hs")
        if hs is not None and float(hs) > HS_LINE:
            continue
        keep.append(ln)
    return keep


def _crop_is_text(lines) -> bool:
    """整片区域像不像真的文字：一批行的字高离散度中位数。

    单个区域里只要**大多数**行都"字高乱跳"，就判定整片不是文字，一行都不采信——
    宁可不读，也不把一堆胡话拼成一句话交出去。注意这个判定要看**原始**行（先别按
    单行的门限筛），否则那些偶然"看起来整齐"的噪点行会留下来当证据。
    """
    val = [float(ln.get("hs") or 0.0) for ln in lines
           if int(ln.get("h") or 0) >= 8
           and re.search(r"[0-9A-Za-z" + _CJK + "]", str(ln.get("text") or ""))]
    if not val:
        return False
    val.sort()
    med = val[len(val) // 2]
    bad = sum(1 for v in val if v > HS_LINE) / float(len(val))
    return med <= HS_CROP and bad <= 0.5


def _read_once(rgb, box, scale, gap: int, max_w) -> dict:
    arr = np.asarray(rgb, np.uint8)
    H, W = arr.shape[:2]
    if box:
        x, y, w, h = [int(v) for v in box]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + max(1, w)), min(H, y + max(1, h))
    else:
        x0, y0, x1, y1 = 0, 0, W, H
    if x1 - x0 < 4 or y1 - y0 < 4:
        return {"text": "", "lines": [], "blocks": [], "ms": 0, "scale": 1.0,
                "box": [x0, y0, max(1, x1 - x0), max(1, y1 - y0)]}
    crop = np.ascontiguousarray(arr[y0:y1, x0:x1])
    s = float(scale or 1.0)
    res = read_text(crop, scale=s)

    lines = []
    for ln in res.get("lines") or []:
        try:
            dx = int(round(float(ln.get("x", 0)) / s)) + x0
            dy = int(round(float(ln.get("y", 0)) / s)) + y0
            dw = max(1, int(round(float(ln.get("w", 1)) / s)))
            dh = max(1, int(round(float(ln.get("h", 1)) / s)))
        except (TypeError, ValueError):
            continue
        lines.append({"text": str(ln.get("text", "")), "x": dx, "y": dy, "w": dw, "h": dh,
                      "hs": ln.get("hs")})
    raw_n = len(lines)
    if lines and not _crop_is_text(lines):
        lines = []                      # 整片像垃圾就别读，宁可空手
    else:
        lines = _plausible(lines, y1 - y0)

    blocks = group_lines(lines, gap=gap,
                         max_w=max_w if max_w is not None else int(W * 0.45))
    for b in blocks:
        b["kind"] = kind_of(b["text"], b["box"], W, H)
        v = value_of(b["text"])
        if v is not None:
            b["value"] = v
        b["id"] = stable_id(b["kind"], b["text"], b["box"])
    return {"text": clean_text(" ".join(b["text"] for b in blocks)),
            "lines": lines, "blocks": blocks, "ms": int(res.get("ms") or 0),
            "scale": s, "box": [x0, y0, x1 - x0, y1 - y0],
            "raw": raw_n, "kept": len(lines)}


def read_blocks(rgb, box=None, scale=None, gap: int = 14, max_w=None,
                verify: bool = False, min_sim: float = 0.2) -> dict:
    """读一块区域的文字并折成块/实体。坐标一律还原到**原图**坐标系。

    小区域放大再认（小字放大才认得清），大区域原样认（放大只是变慢）。识别出的
    坐标必须除回 scale，否则下游按框点击会偏 scale 倍 —— 这是最容易忘的一步。

    `verify=True` 时**换一个缩放再认一遍，两遍读得差不多的块才算数**。理由：系统 OCR
    遇到像素字/花体字不会报错，而是给出"看着像字"的胡话，而且换个缩放就换一套胡话
    （实测同一帧 1.0/1.5/2.0 三次输出完全不同）；真文字在两三种缩放下基本一致。
    这是目前唯一能自动分辨"读到了"和"读错了"的便宜办法，代价是 OCR 跑两遍。
    """
    arr = np.asarray(rgb, np.uint8)
    H, W = arr.shape[:2]
    if scale is None:
        ch = (int(box[3]) if box else H)
        scale = 2.0 if ch <= 220 else (1.5 if ch <= 460 else 1.0)
    first = _read_once(arr, box, scale, gap, max_w)
    if not verify or not first["blocks"]:
        return first

    # 再换两个缩放各读一遍，同一位置上"三局两胜"才采信。
    # 单个缩放读错字是常事（实测 2.0 把 取消 读成 取氵肖，2.25/2.5/3.0 都对），
    # 而把图标当字读出来的胡话换缩放就换一套，永远凑不齐两票。
    s1 = first["scale"]
    scales = []
    for k in (1.25, 1.5):
        s = round(s1 * k, 2)
        scales.append(s if s <= 4.0 else round(s1 * 0.8, 2))
    passes = [first] + [_read_once(arr, box, s, gap, max_w) for s in scales]

    keep, dropped = [], 0
    for b in first["blocks"]:
        votes = [b["text"]]
        boxes = [b["box"]]
        for other in passes[1:]:
            mates = [c for c in other["blocks"] if _same_place(b["box"], c["box"])]
            if not mates:
                continue
            mate = max(mates, key=lambda c: similarity(b["text"], c["text"]))
            if similarity(b["text"], mate["text"]) >= min_sim:
                votes.append(mate["text"])
                boxes.append(mate["box"])
        if len(votes) < 2:                 # 只有一票 = 没人在别的缩放里看到同样的位置
            dropped += 1
            continue
        # 票数最多的那份文字胜出；平票取更短的那个（OCR 更容易多吐字，而不是少吐）
        best_text = sorted(votes, key=lambda t: (-votes.count(t), len(_norm(t))))[0]
        if _norm(best_text) != _norm(b["text"]):
            b = dict(b, text=best_text, box=boxes[votes.index(best_text)])
            b["kind"] = kind_of(b["text"], b["box"], W, H)
            v = value_of(b["text"])
            if v is not None:
                b["value"] = v
            else:
                b.pop("value", None)
            b["id"] = stable_id(b["kind"], b["text"], b["box"])
        b["agree"] = round(len(votes) / float(len(passes)), 2)
        keep.append(b)

    out = dict(first)
    out["blocks"] = keep
    out["text"] = clean_text(" ".join(b["text"] for b in keep))
    out["ms"] = sum(int(p["ms"]) for p in passes)
    out["scales"] = [round(p["scale"], 2) for p in passes]
    out["dropped"] = dropped
    return out


def stats() -> dict:
    return dict(_STATE)


def stop() -> None:
    global _WORKER
    w = _WORKER
    _WORKER = None
    if w is None:
        return
    try:
        w.stdin.write(b"QUIT\n")
        w.stdin.flush()
    except Exception:            # noqa: BLE001
        pass
    try:
        w.wait(timeout=2)
    except Exception:            # noqa: BLE001
        w.kill()
