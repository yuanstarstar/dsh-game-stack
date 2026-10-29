# -*- coding: utf-8 -*-
"""内置字形识别器：一个 768-128-36 的小 MLP，权重随插件走（380KB，纯 numpy 推理）。

它解决的是**图集没有的字形**：图集只会认编译时学过的字（`value:"00"` 里出现的 0 和 1），
画面冷不丁冒出个 2 就只能输出 `?`。这个模型是在"系统字体 + 程序化七段块字"上训出来的，
所以对没见过的字体也能猜，而且**猜错的时候有得比**：返回 top1/top2 的分数与间隔，
调用方可以要求"要有明显优势才采信"，否则仍然输出 `?`（宁可少读，不要读错）。

实测（`tools/train_digits.py`，留出 30 种训练时没见过的字体）：
  · 全 36 类（0-9A-Z）  76.1%
  · 只允许数字 0-9      86.5%     ← HUD 计数就走这条
  · 七段/LED 块字       99.3%     ← 《以撒》这种粗描边像素字属于这一族

为什么不用现成 OCR：Windows 自带的 OCR 在《以撒》这种实心粗描边字上 20 种预处理全部返回
空串（文本行检测把这堆实心块当噪声），而它读 Arial 一读就中 —— 所以不是桥坏了，是这类字体
根本不在它的训练分布里。Tesseract 要另装二进制 + 训练数据（几十 MB），对同一种字体也未必
比这个 380KB 的小模型强。

用法：
    from . import ocrlib
    r = ocrlib.reader()                    # 第一次调用才加载
    ch, p, margin = r.classify(bm)         # bm: 24x32 的二值/浮点字形
    r.confident(bm)                        # → (char 或 None, p, margin)
    ocrlib.read_text(rgb, alphabet="0-9")  # 一小块画面 → 字符串（自己切字）
"""
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
NPZ = os.path.join(HERE, "ocr_digits.npz")

# 采信门限：top1 概率够高，且比 top2 明显（没优势就交给调用方输出 ?）
MIN_PROB = 0.55
MIN_MARGIN = 0.25

DIGITS = "0123456789"
ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _mask(alphabet, classes):
    """把 `alphabet` 字符串换成允许下标的布尔掩膜（None = 全开）。"""
    if alphabet is None:
        return np.ones(len(classes), bool)
    allow = set(str(alphabet).upper())
    return np.array([c in allow for c in classes])


class Reader:
    """小 MLP 的推理封装。权重只加载一次（`.npz` 十几毫秒）。"""

    def __init__(self, path=NPZ):
        z = np.load(path)
        self.W1, self.b1 = z["W1"], z["b1"]
        self.W2, self.b2 = z["W2"], z["b2"]
        self.mu, self.sd = z["mu"], z["sd"]
        self.gw, self.gh = int(z["gw"]), int(z["gh"])
        self.classes = "".join(np.asarray(z["alphabet"]).tolist()) if "alphabet" in z \
            else ALNUM

    def probs(self, bm, alphabet=None):
        """24x32 字形 → 各类概率（`alphabet` 之外的类别被压到 0 再归一化）。"""
        x = np.asarray(bm, np.float32).reshape(1, -1)
        x = (x - self.mu) / self.sd
        h = np.maximum(x @ self.W1 + self.b1, 0)
        z = h @ self.W2 + self.b2
        z[0, ~_mask(alphabet, self.classes)] = -1e9
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(axis=1, keepdims=True)
        return p[0]

    def classify(self, bm, alphabet=None):
        """→ (字符, top1 概率, 与 top2 的间隔)。低置信度也照实返回，由调用方决定采不采信。"""
        p = self.probs(bm, alphabet)
        order = np.argsort(p)[::-1]
        i = int(order[0])
        margin = float(p[i] - p[int(order[1])])
        return self.classes[i], float(p[i]), margin

    def confident(self, bm, alphabet=None, min_prob=MIN_PROB, min_margin=MIN_MARGIN):
        ch, p, m = self.classify(bm, alphabet)
        return (ch if (p >= min_prob and m >= min_margin) else None), p, m


_READER = None


def reader():
    """进程内单例：模型只加载一次。"""
    global _READER
    if _READER is None:
        _READER = Reader()
    return _READER


def available():
    return os.path.exists(NPZ)


def read_text(rgb, alphabet=ALNUM, min_size=8, max_rel=3.0,
              min_prob=MIN_PROB, min_margin=MIN_MARGIN):
    """读一小块画面上的文字（自己按列投影切字）。

    只认**亮字**（和 `digits.py` 的掩膜同一套：min(R,G,B) + Otsu），所以深色描边的暗字读不出。
    每个字位要么给字符，要么给 `?`（不猜）；返回 `(字符串, 逐字明细)`。
    """
    from . import digits as D          # 这里 import 避免循环依赖（digits 是延迟 import 本模块的）

    if not available():
        return "", []
    r = reader()
    m = D._mask(rgb)
    runs = D._filter_runs(D.segment(rgb))
    if not runs:
        return "", []
    hs = [D._size_of(x)[1] for x in runs]
    hmax = max(hs)
    floor = max(hmax / max_rel, min_size)
    runs = [x for x in runs if D._size_of(x)[1] >= floor]
    out, detail = [], []
    for x0, x1, y0, y1 in runs:
        bm = D._normalize(m, x0, x1, y0, y1)
        ch, p, mg = r.classify(bm, alphabet=alphabet)
        ok = p >= min_prob and mg >= min_margin
        out.append(ch if ok else "?")
        detail.append((ch if ok else "?", round(float(p), 3), x1 - x0, x0))
    return "".join(out), detail

