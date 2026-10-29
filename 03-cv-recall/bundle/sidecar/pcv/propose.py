"""免费候选框提议器。

目的：让「学一个界面」不必先花一次多模态调用去标框。
做法完全不需要模型：
  1) 边缘密度连通域  —— 抓到有内容的控件（按钮/文字块/图标）
  2) 颜色量化连通域  —— 抓到纯色块（血条/面板/选中高亮）
  3) 合并 + NMS + 尺寸过滤
输出带编号的候选框，模型只需要说「1号是血条、3号是技能」——纯文本，几乎不花 token。
"""
from __future__ import annotations

import numpy as np

from .feats import rgb_to_hsv, resize_area, sobel, to_float


def _components(mask, min_area, max_boxes=64):
    """4-连通域（BFS，纯 numpy + 列表）。返回 [(x,y,w,h,area)]。"""
    H, W = mask.shape
    seen = np.zeros((H, W), np.uint8)
    boxes = []
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return boxes
    flat_mask = mask.ravel()
    for start in range(ys.size):
        sy, sx = int(ys[start]), int(xs[start])
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = 1
        minx = maxx = sx
        miny = maxy = sy
        area = 0
        while stack:
            y, x = stack.pop()
            area += 1
            if x < minx:
                minx = x
            elif x > maxx:
                maxx = x
            if y < miny:
                miny = y
            elif y > maxy:
                maxy = y
            if x > 0 and not seen[y, x - 1] and flat_mask[y * W + x - 1]:
                seen[y, x - 1] = 1
                stack.append((y, x - 1))
            if x + 1 < W and not seen[y, x + 1] and flat_mask[y * W + x + 1]:
                seen[y, x + 1] = 1
                stack.append((y, x + 1))
            if y > 0 and not seen[y - 1, x] and flat_mask[(y - 1) * W + x]:
                seen[y - 1, x] = 1
                stack.append((y - 1, x))
            if y + 1 < H and not seen[y + 1, x] and flat_mask[(y + 1) * W + x]:
                seen[y + 1, x] = 1
                stack.append((y + 1, x))
        if area >= min_area:
            boxes.append((minx, miny, maxx - minx + 1, maxy - miny + 1, area))
            if len(boxes) >= max_boxes:
                break
    return boxes


def _iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / float(a[2] * a[3] + b[2] * b[3] - inter + 1e-9)


def propose(rgb, scale=4, limit=12, min_frac=0.0006, max_frac=0.35):
    """返回 [{'box':[x,y,w,h], 'why':'edge'|'color', 'score':float}]，已按面积从大到小排序。"""
    a = np.asarray(rgb, np.uint8)
    H, W = a.shape[:2]
    small = resize_area(a, max(64, W // scale), max(36, H // scale)).astype(np.uint8)
    sh, sw = small.shape[:2]
    total = float(sh * sw)
    cands = []

    # --- 1) 边缘密度
    g = to_float(small).mean(axis=2) if small.ndim == 3 else small.astype(np.float32) / 255.0
    mag, _ = sobel(g)
    thr = float(np.quantile(mag, 0.88)) if mag.size else 0.2
    thr = max(0.06, thr)
    em = mag > thr
    for (x, y, w, h, area) in _components(em, max(6, int(total * 0.0008))):
        frac = (w * h) / total
        if frac < min_frac or frac > max_frac:
            continue
        cands.append({"box": [x, y, w, h], "why": "edge", "area": area})

    # --- 2) 颜色量化（纯色块：血条 / 面板 / 高亮）
    q = (to_float(small) * 5.999).astype(np.int32)
    key = (q[..., 0] * 6 + q[..., 1]) * 6 + q[..., 2]
    cnt = np.bincount(key.ravel(), minlength=216)
    order = np.argsort(cnt)[::-1]
    used = 0
    for k in order:
        c = int(cnt[k])
        if c / total < 0.003:
            break
        if c / total > 0.28:
            continue
        mask = key == int(k)
        for (x, y, w, h, area) in _components(mask, max(6, int(total * 0.002))):
            frac = (w * h) / total
            if frac < min_frac or frac > max_frac:
                continue
            if area / float(w * h + 1e-9) < 0.55:  # 太散的不是控件
                continue
            cands.append({"box": [x, y, w, h], "why": "color", "area": area})
        used += 1
        if used >= 8:
            break

    # --- 3) 放大回原尺寸 + NMS
    out = []
    for c in sorted(cands, key=lambda d: -d["area"]):
        x, y, w, h = c["box"]
        box = [int(x * scale), int(y * scale), int(w * scale), int(h * scale)]
        box = [max(0, box[0]), max(0, box[1]), max(4, min(box[2], W - max(0, box[0]))),
               max(4, min(box[3], H - max(0, box[1])))]
        if any(_iou(box, o["box"]) > 0.6 for o in out):
            continue
        out.append({"box": box, "why": c["why"], "score": round(c["area"] / total, 4)})
        if len(out) >= limit:
            break
    return out


def describe(rgb, cands):
    """给候选框补一句“它长什么样”，让模型不用看图也能判断谁是谁。"""
    from .feats import extract
    rows = []
    for i, c in enumerate(cands):
        x, y, w, h = c["box"]
        patch = np.asarray(rgb, np.uint8)[y:y + h, x:x + w]
        if patch.size == 0:
            continue
        small = resize_area(patch, 24, 24).astype(np.uint8)
        f = extract(small)
        hsv = rgb_to_hsv(to_float(small))
        hue, sat, val = hsv[..., 0].mean(), hsv[..., 1].mean(), hsv[..., 2].mean()
        name, _ = _color_name(hue, sat, val)
        rows.append({
            "n": i + 1,
            "box": c["box"],
            "why": c["why"],
            "color": name,
            "edge": round(f["edge"], 2),
            "pos": f"{round(x / rgb.shape[1] * 100)}%,{round(y / rgb.shape[0] * 100)}%",
            "size": f"{w}x{h}",
        })
    return rows


def _color_name(h, s, v):
    if v < 0.12:
        return "黑", h
    if s < 0.12:
        return ("白" if v > 0.82 else ("灰" if v > 0.45 else "深灰")), h
    if v < 0.3:
        return "暗色", h
    hh = (h % 1.0) * 360.0
    for lo, hi, nm in ((0, 20, "红"), (20, 45, "橙"), (45, 70, "黄"), (70, 160, "绿"),
                       (160, 200, "青"), (200, 260, "蓝"), (260, 320, "紫"), (320, 360, "洋红")):
        if lo <= hh < hi:
            return nm, h
    return "红", h
