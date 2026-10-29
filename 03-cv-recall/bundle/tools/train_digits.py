# -*- coding: utf-8 -*-
"""训练内置字形识别器（纯 numpy，模型只有几十 KB，随插件走）。

为什么不用现成的 OCR：Windows 自带的 OCR 在《以撒》这种**粗描边像素字**上 20 种预处理
全部返回空串（它的文本行检测把这些实心块当噪声）；Tesseract 要另装二进制 + 训练数据，
而且对同一种字体也未必比专门训的小模型强。

这里的路子是**用字体数据训练一个很小的分类器**：
  · 素材：系统里 80+ 种字体渲染的 0-9，外加"程序化生成的七段块字"（LED/像素字体那一族），
    后者是为了让模型学会《以撒》这种"实心块 + 细缝"的字形；
  · 输入：24x32 的二值字形（和 `pcv/digits.py` 的图集同一个归一化方式）；
  · 模型：768 → 96 (ReLU) → 10 (softmax)，float32 权重大约 300KB；
  · 推理：一次矩阵乘，纯 numpy，跑一次 <1ms。

用法：
    python tools/train_digits.py            # 训练并写出 sidecar/pcv/ocr_digits.npz
    python tools/train_digits.py --eval     # 只在留出字体上评估，不写文件
"""
import argparse
import glob
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sidecar"))
from pcv import feats  # noqa: E402

GW, GH = 24, 32          # 和 digits.py 的图集同尺寸
# 0-9 + A-Z：数字是刚需，大写字母是"能读文字"的最低成本扩展（游戏里几乎只有大写像素字）。
# 推理时可以只选一个子集（HUD 计数只允许数字，避免 0/O、1/I、5/S 打架）。
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
NCLS = len(ALPHABET)
FONTS = r"C:\Windows\Fonts"
SIZES = (30, 34, 40, 46, 54, 62)
SEED = 7
OUT = os.path.join(HERE, "..", "sidecar", "pcv", "ocr_digits.npz")

# 七段显示：a 上 / b 右上 / c 右下 / d 下 / e 左下 / f 左上 / g 中
SEG = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
    "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
}


# ---------------------------------------------------------------- 数据

def _tight(a):
    """裁到墨迹的紧包围盒。"""
    ys, xs = np.nonzero(a)
    if len(ys) == 0:
        return None
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _norm(mask):
    """紧包围盒 → 24x32 二值字形（和 digits._normalize 一致）。"""
    t = _tight(mask.astype(np.float32))
    if t is None:
        return None
    return (feats.fit_to(t, GH, GW) > 0.5).astype(np.float32)


def from_font(path, size, ch):
    try:
        f = ImageFont.truetype(path, size)
    except Exception:
        return None
    im = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(im).text((size // 2, size // 2), ch, font=f, fill=255)
    a = np.asarray(im) > 127
    if a.sum() < 12:
        return None
    return _norm(a)


def _seg_rect(w, h, t, gap):
    """返回七段在 (w,h) 画布上的矩形（左上角坐标, 宽, 高）。"""
    m = max(2, int(round(t * 0.5)))          # 段端留一点缺口
    r = {
        "a": (gap, 0, w - 2 * gap, t),
        "b": (w - t, gap, t, h // 2 - gap - m),
        "c": (w - t, h // 2 + m, t, h // 2 - gap - m),
        "d": (gap, h - t, w - 2 * gap, t),
        "e": (0, h // 2 + m, t, h // 2 - gap - m),
        "f": (0, gap, t, h // 2 - gap - m),
        "g": (gap, h // 2 - t // 2, w - 2 * gap, t),
    }
    return r


def from_segments(ch, rng, style=None):
    """程序化画一个七段块字（模拟 LED / 像素字族：实心段 + 细缝）。"""
    w = int(rng.integers(22, 34))
    h = int(rng.integers(30, 48))
    t = int(rng.integers(4, max(5, min(w, h) // 3)))         # 笔画粗细
    gap = int(rng.integers(0, max(1, t // 2 + 1)))
    canvas = np.zeros((h, w), bool)
    rects = _seg_rect(w, h, t, gap)
    for s in SEG[ch]:
        x, y, sw, sh = rects[s]
        if sw <= 0 or sh <= 0:
            continue
        x0, y0 = max(0, x), max(0, y)
        canvas[y0:y0 + sh, x0:x0 + sw] = True
    if style == "round":                                     # 切掉段角，模拟圆角
        k = max(1, t // 3)
        for yy in (0, h - k):
            canvas[yy:yy + k, 0:k] = False
            canvas[yy:yy + k, w - k:w] = False
    return _norm(canvas)


def augment(bm, rng, n=6):
    """同一字形做轻微几何/像素级扰动：真实截图里有缩放、亮度、抗锯齿、压缩噪声。"""
    out = []
    for _ in range(n):
        a = bm.copy()
        if rng.random() < 0.5:                       # 加粗 / 变细一像素
            d = int(rng.integers(-1, 2))
            for _ in range(abs(d)):
                a = _dilate(a) if d > 0 else _erode(a)
        if rng.random() < 0.5:                       # 随机平移
            dy, dx = int(rng.integers(-2, 3)), int(rng.integers(-2, 3))
            a = np.roll(np.roll(a, dy, axis=0), dx, axis=1)
        if rng.random() < 0.5:                       # 横向拉伸（字体宽高比不一）
            k = float(rng.uniform(0.86, 1.16))
            w2 = max(4, int(round(a.shape[1] * k)))
            a = feats.resize_area(a, w2, a.shape[0]) > 0.5
        if rng.random() < 0.35:                      # 像素噪声（抗锯齿/压缩）
            flip = rng.random(a.shape) < 0.02
            a = np.logical_xor(a, flip)
        out.append(_norm(a.astype(np.float32)))
    return [o for o in out if o is not None]


def _dilate(a):
    a = np.asarray(a) > 0.5
    p = np.pad(a, 1, mode="constant", constant_values=False)
    return (p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:])


def _erode(a):
    a = np.asarray(a) > 0.5
    p = np.pad(a, 1, mode="constant", constant_values=True)
    return (p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:])


def build_dataset(font_paths, rng, norm=None):
    X, y = [], []
    for path in font_paths:
        for size in SIZES:
            for d in range(NCLS):
                bm = from_font(path, size, ALPHABET[d])
                if bm is None:
                    continue
                X.append(bm)
                y.append(d)
                for aug in augment(bm, rng, n=4):
                    X.append(aug)
                    y.append(d)
    # 七段块字：数字才画（字母没有通用的七段写法）每类 260 个随机样式
    for d in range(10):
        for _ in range(260):
            bm = from_segments(str(d), rng, style="round" if rng.random() < 0.5 else None)
            if bm is None:
                continue
            X.append(bm)
            y.append(d)
    X = np.asarray(X, np.float32).reshape(len(X), -1)
    y = np.asarray(y, np.int64)
    if norm is None:
        mu = X.mean(axis=0, keepdims=True)
        sd = X.std(axis=0, keepdims=True) + 1e-6
    else:
        mu, sd = norm
    return (X - mu) / sd, y, mu, sd


# ---------------------------------------------------------------- 模型

def train(X, y, hidden=128, epochs=30, lr=3e-3, bs=256, seed=SEED):
    rng = np.random.default_rng(seed)
    n, d = X.shape
    W1 = rng.normal(0, np.sqrt(2.0 / d), (d, hidden)).astype(np.float32)
    b1 = np.zeros(hidden, np.float32)
    W2 = rng.normal(0, np.sqrt(2.0 / hidden), (hidden, NCLS)).astype(np.float32)
    b2 = np.zeros(NCLS, np.float32)
    P = [W1, b1, W2, b2]
    M = [np.zeros_like(p) for p in P]
    V = [np.zeros_like(p) for p in P]
    b1a, b2a, eps = 0.9, 0.999, 1e-8
    step = 0
    Y = np.eye(NCLS, dtype=np.float32)[y]
    for ep in range(epochs):
        idx = rng.permutation(n)
        tot = 0.0
        for i in range(0, n, bs):
            j = idx[i:i + bs]
            xb, yb = X[j], Y[j]
            h = np.maximum(xb @ W1 + b1, 0)
            z = h @ W2 + b2
            z -= z.max(axis=1, keepdims=True)
            p = np.exp(z)
            p /= p.sum(axis=1, keepdims=True)
            tot += float(-np.log(np.clip((p * yb).sum(axis=1), 1e-9, None)).mean()) * len(j)
            g = (p - yb) / len(j)
            gW2 = h.T @ g
            gb2 = g.sum(axis=0)
            gh = g @ W2.T
            gh[h <= 0] = 0
            gW1 = xb.T @ gh
            gb1 = gh.sum(axis=0)
            step += 1
            for k, gk in enumerate((gW1, gb1, gW2, gb2)):
                M[k] = b1a * M[k] + (1 - b1a) * gk
                V[k] = b2a * V[k] + (1 - b2a) * gk * gk
                mh = M[k] / (1 - b1a ** step)
                vh = V[k] / (1 - b2a ** step)
                P[k] -= lr * mh / (np.sqrt(vh) + eps)
        if ep % 4 == 0 or ep == epochs - 1:
            print(f"  epoch {ep:2d}  loss {tot / n:.4f}  "
                  f"train acc {accuracy(P, X, y):.4f}")
    return P


def forward(P, X):
    W1, b1, W2, b2 = P
    h = np.maximum(X @ W1 + b1, 0)
    z = h @ W2 + b2
    z -= z.max(axis=1, keepdims=True)
    p = np.exp(z)
    p /= p.sum(axis=1, keepdims=True)
    return p


def accuracy(P, X, y):
    return float((forward(P, X).argmax(axis=1) == y).mean())


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", action="store_true", help="只评估，不写模型")
    ap.add_argument("--fonts", type=int, default=84, help="用多少种系统字体训练")
    args = ap.parse_args()
    rng = np.random.default_rng(SEED)

    allf = sorted(glob.glob(os.path.join(FONTS, "*.ttf"))) + \
        sorted(glob.glob(os.path.join(FONTS, "*.ttc")))
    allf = [p for p in allf if os.path.getsize(p) > 20000]
    rng.shuffle(allf)
    hold = allf[:max(6, len(allf) // 12)]                  # 留出字体：只用来评估
    train_f = allf[len(hold):len(hold) + args.fonts]
    print(f"字体：训练 {len(train_f)} 种，留出 {len(hold)} 种用于评估")

    print("生成训练集…")
    X, y, mu, sd = build_dataset(train_f, rng)
    print(f"  样本 {len(X)}，类别 {NCLS}")

    print("训练…")
    P = train(X, y)

    print("评估（留出字体，模型从没见过）…")
    Xv, yv, _, _ = build_dataset(hold, np.random.default_rng(SEED + 1), norm=(mu, sd))
    print(f"  留出字体准确率 {accuracy(P, Xv, yv):.4f}（{len(Xv)} 样本）")
    pv = forward(P, Xv).argmax(axis=1)
    digits_mask = yv < 10
    print(f"    其中数字位 {accuracy(P, Xv[digits_mask], yv[digits_mask]):.4f}"
          f"（{int(digits_mask.sum())} 样本），字母位 "
          f"{accuracy(P, Xv[~digits_mask], yv[~digits_mask]):.4f}")
    seg = []
    for d in range(10):
        for _ in range(60):
            bm = from_segments(str(d), rng, style="round" if rng.random() < 0.5 else None)
            if bm is not None:
                seg.append((bm, d))
    Xs = np.asarray([b for b, _ in seg], np.float32).reshape(len(seg), -1)
    ys = np.asarray([d for _, d in seg])
    Xs = (Xs - mu) / sd
    print(f"  七段块字准确率 {accuracy(P, Xs, ys):.4f}（{len(Xs)} 样本）")

    if not args.eval:
        W1, b1, W2, b2 = P
        np.savez_compressed(OUT, W1=W1.astype(np.float32), b1=b1.astype(np.float32),
                            W2=W2.astype(np.float32), b2=b2.astype(np.float32),
                            mu=mu.astype(np.float32), sd=sd.astype(np.float32),
                            gw=np.int32(GW), gh=np.int32(GH),
                            alphabet=np.array(list(ALPHABET)))
        print(f"写出 {OUT}  ({os.path.getsize(OUT) / 1024:.0f}KB)")


if __name__ == "__main__":
    main()
