"""数字读数：字形图集（compile once, read forever）。

为什么不是 OCR：Windows.Media.Ocr 对《以撒的结合》这种"粗描边像素字"完全失效
（试过 raw / 反相 / 二值化 / 3~8 倍放大 / NEAREST / LANCZOS，全部空串）。而这类游戏的
字号是固定的，字形也是固定的 —— 那就不该每帧去猜，应该在编译期把字形**切出来存成模板**，
运行时只做像素比对。零逐帧成本、零外部依赖，和工具其余部分同一个理念。

三个关键约定：
1. **切字形靠列投影**（空白列即分隔），不靠 OCR 的行检测。
2. **每个字归一化到 GWxGH**（按自己的紧包围盒拉伸），大小/位置抖动全部被吸收。
3. **认不出的字返回 `?`**，绝不猜一个相近的数字 —— 假数字比没数字更糟。
   图集里缺的类（比如从没见过 4）会在运行时显示成 `?`，再标一帧补进来即可。
"""

from __future__ import annotations

import numpy as np

from . import feats, pack as P

GW, GH = 24, 32
MATCH_MIN = 0.72          # 单字匹配下限（Pearson），低于它输出 '?'
MIN_RUN = 3               # 少于 3 列 ink 的"字形"是噪点，丢掉
MAX_RUN_FRAC = 0.55       # 单个字形宽度超过整框的 55% ≈ 没切开，直接报错
MIN_H_FRAC = 0.55         # 高度不到最高字形的 55% = 不是数字（下限比附近的图标/高光）


def _gray255(rgb):
    """「白度」而不是亮度：取 min(R,G,B)。

    为什么不用亮度：游戏 HUD 里的数字是纯白带黑描边，而旁边往往有一个**彩色图标**
    （金币是黄色）。黄色 (200,180,60) 的亮度 147，和木地板差不多，用亮度阈值分不开，
    图标边缘会被当成笔画切进字形里；用 min 通道，白字 ≈ 200+，黄色只剩 60，直接消失。
    代价：彩色文字（纯红/纯蓝的数值）读不出来 —— 记在文档里，别硬套。
    """
    a = np.asarray(rgb)
    if a.ndim == 2:
        return a.astype(np.float32)
    return a[:, :, :3].min(axis=2).astype(np.float32)


def _otsu(g):
    """Otsu 阈值：紧框住的文字是"亮字 + 暗底"的双峰分布，Otsu 是最省事的自动切分。

    别用「均值 + kσ」：那块木头地板背景本身很亮（~150），数字（~200）只比它高一点，
    k=2.2 会把整个字切成碎片，列投影只剩一两个残条（实测"2.73"被切成 1 段）。
    """
    hist = np.bincount(np.clip(g, 0, 255).astype(np.uint8).ravel(), minlength=256).astype(np.float64)
    tot = hist.sum()
    if tot <= 0:
        return 128.0
    p = hist / tot
    omega = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    mu_t = mu[-1]
    den = omega * (1.0 - omega)
    num = (mu_t * omega - mu) ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b = np.where(den > 1e-12, num / np.maximum(den, 1e-12), 0.0)
    return float(np.argmax(sigma_b))


def _mask(rgb):
    """字掩码：Otsu 切分后取**亮的那一侧**（游戏里的数字几乎总是比底色亮）。

    夹在 [24, 245]：极端值说明这个框里根本没有对比度（纯色块），那就当没字。
    """
    g = _gray255(rgb)
    thr = float(np.clip(_otsu(g), 24.0, 245.0))
    m = g >= thr
    frac = float(m.mean())
    # 极性问题：亮的那一侧太少（<0.5%，切反了）或太多（>45%，那是背景）就取暗的一侧。
    # 早先写成"亮像素 <8% 就取暗侧"，结果一张 6% 白字的黑底图被整体翻过来，
    # 切成一个横跨全宽的巨块 —— 文字占面积的正常范围其实很宽（2%~45%），按上下限判才稳。
    if frac < 0.005 or frac > 0.45:
        m = g < thr
        if float(m.mean()) < 0.005:
            m = g >= thr
    return m


def segment(rgb):
    """按列投影切成字形，返回 [(x0, x1, y0, y1), ...]（右/下开区间）。"""
    m = _mask(rgb)
    if not m.any():
        return []
    cols = m.any(axis=0)
    runs, x = [], 0
    n = cols.shape[0]
    while x < n:
        if not cols[x]:
            x += 1
            continue
        x0 = x
        while x < n and cols[x]:
            x += 1
        x1 = x
        if x1 - x0 < MIN_RUN:
            continue
        sub = m[:, x0:x1]
        rows = np.flatnonzero(sub.any(axis=1))
        runs.append((x0, x1, int(rows[0]), int(rows[-1]) + 1))
    return _filter_runs(runs)


def _filter_runs(runs):
    """丢掉高度明显矮的段：数字是等高的，矮段是旁边图标的高光/描边反光。
    实测（以撒 HUD）：数字 42px 高，左边缘那块反光只有 12px 高，被误切成第三个"字形"。"""
    if len(runs) < 2:
        return runs
    hmax = max(b - a for _, _, a, b in runs)
    return [r for r in runs if (r[3] - r[2]) >= hmax * MIN_H_FRAC]


def _align_band(runs, uh):
    """把"被上/下方杂白拉高/压低"的字形裁回共同字带。

    实测（以撒炸弹计数）：某个画面里数字正下方冒出一小块白色（拾取物高光），
    列投影把字形高度从 42 拉到 54 —— 归一化后当然认不出（0.47 分）。
    所以拿**图集里学到的标准字形高度 uh** 当基准：最接近 uh 的那段就是干净的字形，
    其余明显偏离的段一律裁进它的字带。数字是等高的，这条约束本来就成立，
    不是启发式 —— 而且只在偏差 >15% 时才动手，正常片段一个像素都不碰。
    """
    if len(runs) < 2 or not uh:
        return runs
    h = [b - a for _, _, a, b in runs]
    best = min(range(len(runs)), key=lambda i: abs(h[i] - uh))
    ay0, ay1 = runs[best][2], runs[best][3]
    out = []
    for (x0, x1, y0, y1), hh in zip(runs, h):
        if abs(hh - uh) > uh * 0.15:
            y0, y1 = ay0, ay1
        out.append((x0, x1, y0, y1))
    return out


def _size_of(r):
    return max(1, r[1] - r[0]), max(1, r[3] - r[2])


def _pick_runs(runs, atlas, want=None):
    """按「学到的字形尺寸」挑出真正的数字，扔掉同样是白色但不是字的东西。

    为什么需要：HUD 里数字左边常有一块**图标反光**（硬币的高光），它是白色、还很"高"，
    高度过滤挡不住；而且它有时会**贴着**第一位数字，列投影把它们连成一段（37px 宽）。
    对策：编译期记下字形的标准尺寸 (uw, uh)，运行时按「宽高比的偏离度」给每段打分，
    只留和标准尺寸同档的，再按 want（= value 的位数）从左取前几个。
    纯尺寸判断，不需要任何语义 —— 这正是"编译一次、运行时只跑像素"的意思。
    """
    if len(runs) < 2:
        return runs
    uw, uh = atlas.get("uw"), atlas.get("uh")
    keep = runs
    # 只在"确实多出段"时才按尺寸筛：字宽可以合法地很不一样（'I' 只有 'M' 的 1/3 宽），
    # 没多段还筛就会把窄字母直接扔掉（实测 'ITEM' 读成 'TEM'）。
    if want and len(keep) > want and uw and uh:
        def cost(r):
            w, h = _size_of(r)
            return abs(float(np.log(w / float(uw)))) + abs(float(np.log(h / float(uh))))
        scored = sorted(((cost(r), r) for r in keep), key=lambda t: t[0])
        keep = [r for c, r in scored if c <= scored[0][0] + 0.55]
    if want and len(keep) > want:
        keep = sorted(keep, key=lambda r: r[0])[:want]
    return sorted(keep, key=lambda r: r[0])


def _normalize(m, x0, x1, y0, y1):
    """把一个字形的紧包围盒缩放到 GWxGH（float 0..1）。"""
    cell = m[y0:y1, x0:x1].astype(np.float32)
    if cell.shape[0] < 1 or cell.shape[1] < 1:
        return np.zeros((GH, GW), np.float32)
    rs = feats.fit_to(cell, GH, GW)
    return np.clip(rs, 0.0, 1.0).astype(np.float32)


def _iou(a, b):
    """纯色块（比如小数点、实心"1"）没有方差，Pearson 无意义（还会返回 -1），
    改比二值重叠率：a 和 b 都归一化到 0..1，>0.5 当 ink。"""
    ia, ib = a > 0.5, b > 0.5
    u = np.logical_or(ia, ib).sum()
    if u <= 0:
        return -1.0
    return float(np.logical_and(ia, ib).sum() / u)


def _pearson(a, b):
    a = a.ravel() - a.mean()
    b = b.ravel() - b.mean()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-5 or nb < 1e-5:
        return -1.0
    return float(a @ b / (na * nb))


def _score(a, b):
    """两个字形模板的相似度：有方差用 Pearson，没方差（纯色块）用 IoU。"""
    am = a - a.mean()
    bm = b - b.mean()
    if np.linalg.norm(am) < 1e-5 or np.linalg.norm(bm) < 1e-5:
        return _iou(a, b)
    return float(am.ravel() @ bm.ravel() / (np.linalg.norm(am) * np.linalg.norm(bm)))


def learn(rgb, value):
    """从「已知数值的一小块画面」里学字形：value='01' → 学到 '0' 和 '1' 两个模板。

    返回的是和 `read()` 同一个形状的图集 `{"w","h","classes"}`（可以直接喂给 read 自检），
    要并进已有图集用 `merge()`。长度对不上直接报错（宁可编译失败，也不要学进错位的模板）。
    """
    runs = segment(rgb)
    value = str(value)
    if not runs:
        raise ValueError("这块区域里没有识别到任何亮色字形（框里没有数字，或对比度太低）")
    if len(runs) > len(value):
        # 框里混进了"白但不是字"的东西（图标高光、反光）——按尺寸先挑一遍
        ws = [_size_of(r)[0] for r in runs]
        hs = [_size_of(r)[1] for r in runs]
        runs = _pick_runs(runs, {"uw": float(np.median(ws)), "uh": float(np.median(hs))},
                          want=len(value))
        runs = _align_band(runs, float(np.median(hs)))
    if len(runs) != len(value):
        raise ValueError(
            f"切出 {len(runs)} 个字形，但 value={value!r} 有 {len(value)} 个字符："
            f"要么 value 写漏了，要么这几个字粘在一起了（字形列区间 {[(a, b) for a, b, _, _ in runs]}）")
    m = _mask(rgb)
    w = max(1, m.shape[1])
    for i, (x0, x1, _y0, _y1) in enumerate(runs):
        if (x1 - x0) / w > MAX_RUN_FRAC:
            raise ValueError(f"第 {i + 1} 个字形占了整框的 {(x1 - x0) / w:.0%}，"
                             f"像是两个数字粘在一起 —— 请把框收紧到数字上")
    classes, sizes = {}, []
    for (x0, x1, y0, y1), ch in zip(runs, value):
        classes.setdefault(ch, []).append(_normalize(m, x0, x1, y0, y1))
        sizes.append(_size_of((x0, x1, y0, y1)))
    uw = float(np.median([s[0] for s in sizes]))
    uh = float(np.median([s[1] for s in sizes]))
    return {"w": GW, "h": GH, "classes": classes, "uw": uw, "uh": uh, "n": len(runs)}


def merge(atlas, new):
    """把新学到的字形并进图集（同一个字可以有多个样本，认的时候取最高分）。
    new 必须是 learn() 的返回形状；传了个扁平 dict 会显式报错，免得静默丢字形。"""
    if atlas is None:
        atlas = {"w": GW, "h": GH, "classes": {}}
    if new is None:
        return atlas
    if "classes" not in new:
        raise ValueError("merge() 只接受 learn()/decode() 形状的图集（要有 classes 键）")
    for ch, bms in (new.get("classes") or {}).items():
        atlas.setdefault("classes", {}).setdefault(ch, []).extend(bms)
    for k in ("uw", "uh"):
        if new.get(k):
            atlas[k] = float(np.median([atlas[k], new[k]])) if atlas.get(k) else float(new[k])
    if new.get("n"):
        atlas["n"] = int(new["n"])
    atlas["w"], atlas["h"] = GW, GH
    return atlas


def encode(atlas):
    """图集 → 可存 JSON 的形态（每个字形一张 24x32 灰度 PNG 的 base64）。"""
    atlas = atlas or {"classes": {}}
    classes = {}
    for ch, bms in (atlas.get("classes") or {}).items():
        classes[ch] = []
        for bm in bms:
            g8 = np.clip(np.asarray(bm, np.float32) * 255.0, 0, 255).astype(np.uint8)
            classes[ch].append(P.encode_png(np.repeat(g8[:, :, None], 3, axis=2)))
    return {"w": int(atlas.get("w") or GW), "h": int(atlas.get("h") or GH),
            "classes": classes, "uw": atlas.get("uw"), "uh": atlas.get("uh"),
            "n": atlas.get("n")}


def decode(d):
    """encode() 的逆：JSON 里的图集 → {char: [GWxGH float 模板, ...]}。"""
    if not d:
        return {"w": GW, "h": GH, "classes": {}}
    out = {}
    for ch, b64s in (d.get("classes") or {}).items():
        out[ch] = []
        for s in b64s:
            rgb = P.decode_png(s)
            out[ch].append(np.clip(feats.to_gray(rgb), 0.0, 1.0).astype(np.float32))
    return {"w": int(d.get("w") or GW), "h": int(d.get("h") or GH), "classes": out,
            "uw": d.get("uw"), "uh": d.get("uh"), "n": d.get("n")}


def read(rgb, atlas, match_min=MATCH_MIN, want=None, ocr=True):
    """读一小块画面上的数字，返回 (字符串, 逐字明细)。

    逐字明细是 [(char, score, best_class, via)]，score 是该位的最好匹配分（-1 = 没匹配上），
    via 是这一位靠什么认出来的：`atlas`（编译时学的字形）或 `ocr`（内置小模型）。
    没有图集 / 没有字形时返回 ("", [])。
    `want` 是这一格应该有几位（= 编译时 value 的长度）；给了就按尺寸挑最像的 `want` 段。

    `ocr=True` 时，图集认不出的字位会交给内置的字形分类器（`pcv/ocrlib.py`）兜一次 ——
    它是在系统字体 + 七段块字上训的，所以"换了个数字但字体没变"这种最常见的情况能直接读出来；
    但它**只在有明显优势时采信**（top1 概率与 top2 间隔都够），否则仍然输出 `?`。
    """
    if not atlas or not (atlas.get("classes") or {}):
        return "", []
    runs = segment(rgb)
    if not runs:
        return "", []
    runs = _pick_runs(runs, atlas, want if want is not None else atlas.get("n"))
    runs = _align_band(runs, atlas.get("uh"))
    m = _mask(rgb)
    out, detail = [], []
    for x0, x1, y0, y1 in runs:
        bm = _normalize(m, x0, x1, y0, y1)
        best, best_s = "?", -1.0
        for ch, bms in atlas["classes"].items():
            for tpl in bms:
                s = _score(bm, tpl)
                if s > best_s:
                    best, best_s = ch, s
        if best_s >= match_min:
            out.append(best)
            detail.append((best, round(float(best_s), 3), best, "atlas"))
            continue
        # 图集里没有这个字形：交给内置小模型兜底（见 ocrlib 的说明与实测数字）
        ch2, p2, m2 = None, 0.0, 0.0
        if ocr:
            try:
                from . import ocrlib
                if ocrlib.available():
                    ch2, p2, m2 = ocrlib.reader().confident(bm, alphabet=ocrlib.DIGITS)
            except Exception:
                ch2 = None
        if ch2:
            out.append(ch2)
            detail.append((ch2, round(float(p2), 3), best, "ocr"))
        else:
            # 认不出就用 '?' 占位：一位数字错了比整串消失更糟，'?' 至少诚实
            out.append("?")
            detail.append(("?", round(float(best_s), 3), best, "-"))
    return "".join(out), detail


def read_int(rgb, atlas, match_min=MATCH_MIN):
    """读数字并转 int（带 '?' 或非数字时返回 None）。"""
    s, detail = read(rgb, atlas, match_min)
    if not s or "?" in s or not s.isdigit():
        return None, s, detail
    return int(s), s, detail
