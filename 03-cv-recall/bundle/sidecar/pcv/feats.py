"""
纯 numpy 视觉原语。不依赖 cv2 / scipy / skimage —— 目标环境只有 numpy + Pillow。

设计约束（重要）：
  1. 所有特征必须**可量化、可复现**：同一块像素必须永远产出同一个向量。
  2. 所有特征必须**抗尺寸**：先归一化到 96x96 再抽，尺寸差异交给 ar(宽高比) 单独把关。
  3. 模板匹配必须能在毫秒级跑完：用 FFT 做 NCC，并且**只在实体自己的搜索窗内搜**。
"""
from __future__ import annotations

import numpy as np

EPS = 1e-9
FLAT_STD = 0.016          # 灰度标准差低于这个值(约 4/255)的窗口无法做 NCC，判为"不匹配"
PICK_K = 6                # 细定位时候选峰数（用分块多数票重排）
TILE_W = 0.7              # 分块多数票在候选重排里的权重
TILE_K = 0.34             # 分块打分只取最好的这一比例的块（见 tile_score 的注释）
TILE_MIN_SIDE = 24        # 模板最短边小于这个值就不用分块投票（切太碎会凑出假高分）
TILE_ACCEPT = 0.62        # 分块打分到这个分数就足以证明"位置对"
TILE_EARLY = 0.45         # 第一个候选就到这个分数、且细定位不输粗定位，就不再试别的候选
NORM = 96  # 特征归一化边长
_GRAY_W = np.array([0.299, 0.587, 0.114], np.float32)   # BT.601 灰度权重（to_gray 用）


# ---------------------------------------------------------------- 基础转换

def to_float(rgb_u8):
    return np.asarray(rgb_u8, np.float32) / 255.0


def to_gray(rgb):
    """转灰并归到 0..1。**只做一次全图扫描**。

    原来的实现每次都算 `a.max()`（整图一遍）+ float64 转换，在 1280x380 的搜索窗上
    这一句就吃掉 1~2ms。现在按 dtype 判定：uint8 直接按 /255 处理，float 输入才抽样
    看量级（抽样 1/49 像素足够判断"是不是 0..255 的浮点"）。
    """
    a = np.asarray(rgb)
    if a.ndim == 2:
        if a.dtype == np.uint8:
            return a.astype(np.float32) * np.float32(1.0 / 255.0)
        a = a.astype(np.float32, copy=False)
        if a.size and float(a[::7, ::7].max()) > 1.5:
            a = a * np.float32(1.0 / 255.0)
        return a
    a3 = a[..., :3]
    # 先转成**连续**的 float32 再算：搜索窗是从整帧上切出来的视图（行跨距 3*W），
    # 直接在上面做乘加要跳着走内存，实测比"先拷成连续 1.2MB"慢一倍（4.5ms vs 2.3ms）。
    f = np.ascontiguousarray(a3).astype(np.float32)
    if a.dtype != np.uint8 and f.size and float(f[::7, ::7].max()) > 1.5:
        f = f * np.float32(1.0 / 255.0)
    g = f[..., 0] * np.float32(0.299)
    g += f[..., 1] * np.float32(0.587)
    g += f[..., 2] * np.float32(0.114)
    if a.dtype == np.uint8:
        g *= np.float32(1.0 / 255.0)
    return g


def rgb_to_hsv(rgb):
    """rgb: HxWx3 in [0,1] -> HxWx3 (h in [0,1), s in [0,1], v in [0,1])"""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    d = mx - mn
    safe = np.maximum(d, EPS)
    h = np.zeros_like(mx)
    m = d > EPS
    i = m & (mx == r)
    h[i] = (((g - b)[i] / safe[i]) % 6.0)
    i = m & (mx == g)
    h[i] = (b - r)[i] / safe[i] + 2.0
    i = m & (mx == b)
    h[i] = (r - g)[i] / safe[i] + 4.0
    h = h / 6.0
    s = np.where(mx > EPS, d / np.maximum(mx, EPS), 0.0)
    return np.stack([h, s, mx], axis=-1).astype(np.float32)


# ---------------------------------------------------------------- 缩放

def resize_area(img, ow, oh):
    """块平均缩放。img: HxW 或 HxWxC。纯 numpy，比 PIL 快。"""
    img = np.asarray(img, np.float32)
    h, w = img.shape[:2]
    ow = int(max(1, min(ow, w)))
    oh = int(max(1, min(oh, h)))
    if h == oh and w == ow:
        return img.copy()
    yi = np.floor(np.linspace(0, h, oh + 1)).astype(np.int64)
    xi = np.floor(np.linspace(0, w, ow + 1)).astype(np.int64)
    yi[-1], xi[-1] = h, w
    s = np.add.reduceat(img, yi[:-1], axis=0)
    s = s / np.diff(yi).astype(np.float32).reshape((-1,) + (1,) * (s.ndim - 1))
    s = np.add.reduceat(s, xi[:-1], axis=1)
    s = s / np.diff(xi).astype(np.float32).reshape((1, -1) + (1,) * (s.ndim - 2))
    return s


def fit_to(img, h, w):
    """把图弄成正好 (h, w)：大就块平均缩小，小就边缘复制补齐。

    为什么要补：`resize_area` 只会缩小（块平均），拿一个被画面边界裁小的
    负样本对比完整模板会直接 broadcast 崩掉 —— 真实截图里搜索窗贴到屏幕边缘时
    必然发生。补边等于假设"画面外面和边缘一样"，对负样本来说是安全的保守做法。
    """
    a = np.asarray(img, np.float32)
    ih, iw = a.shape[:2]
    if ih == h and iw == w:
        return a
    if ih < h:
        a = np.pad(a, ((0, h - ih), (0, 0)) + ((0, 0),) * (a.ndim - 2), mode="edge")
    if iw < w:
        a = np.pad(a, ((0, 0), (0, w - iw)) + ((0, 0),) * (a.ndim - 2), mode="edge")
    if a.shape[:2] != (h, w):
        a = resize_area(a, w, h)
    return a


# ---------------------------------------------------------------- 梯度 / 边缘

def grad_mag(gray):
    """只要梯度幅值（不算方向）。边缘图几乎从不关心方向，而 arctan2 比 hypot 贵一个量级。"""
    g = np.pad(np.asarray(gray, np.float32), 1, mode="edge")
    gx = (g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[1:-1, :-2] + g[2:, :-2])
    gy = (g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[:-2, 1:-1] + g[:-2, 2:])
    return np.sqrt(gx * gx + gy * gy) * 0.25


def sobel(gray):
    g = np.pad(np.asarray(gray, np.float32), 1, mode="edge")
    gx = (g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[1:-1, :-2] + g[2:, :-2])
    gy = (g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[:-2, 1:-1] + g[:-2, 2:])
    return np.hypot(gx, gy) / 4.0, np.arctan2(gy, gx)


def edge_density(gray, thr=0.15):
    return float((grad_mag(gray) > thr).mean())


def edge_of(gray):
    """灰度 -> 梯度幅值图，范围 [0,1]。

    为什么模板匹配要掺一半边缘：游戏 UI 的**内容**动（血条掉血、小地图光点、
    数字跳变、特效闪烁），**边框**不动。纯灰度 NCC 会把整块动态内容算进分数，
    掉一格血就从 1.0 掉到 0.5；而梯度幅值只在"有线的地方"有响应，边框、刻度线、
    图标轮廓这些静态骨架照样满分，动态填充只贡献噪声级的差异。
    所以分数取 0.62*灰度NCC + 0.38*边缘NCC：静态骨架兜底，动态内容不至于毁掉整块。
    """
    mag = grad_mag(gray)
    return np.clip(mag, 0.0, 1.0).astype(np.float32)


BLEND = (0.62, 0.38)


def channel_maps(img, tpl, img_edge=None, w=BLEND, tpl_edge=None):
    """返回 (灰度 NCC 图, 边缘 NCC 图)，两张图**分开**给出。

    为什么分开：一个实体到底靠哪一路认出来，是不确定的 —— 血条靠刻度线和边框（边缘），
    对话框面板靠整体明暗（灰度）。把两路固定加权混在一起，等于假设所有实体都长一个样。
    所以每路各自和负样本比、各自定阈值和权重，运行时再按权重投票。
    """
    m1 = ncc_map(img, tpl)
    if w is None or w[1] <= 0:
        return m1, None
    if img_edge is None:
        img_edge = edge_of(img)
    if tpl_edge is None:
        tpl_edge = edge_of(tpl)
    return m1, ncc_map(img_edge, tpl_edge)


def _combine(m1, m2, w=BLEND):
    if m2 is None or w is None or w[1] <= 0:
        return m1
    if m1 is None:
        return m2
    n0, n1 = min(m1.shape[0], m2.shape[0]), min(m1.shape[1], m2.shape[1])
    return np.clip(w[0] * m1[:n0, :n1] + w[1] * m2[:n0, :n1], -1.0, 1.0)


def norm_tpl(tpl):
    """把一个模板变成"零均值、单位范数"，供单点 Pearson 复用。

    单点 Pearson 只需要一次点乘，比开一张 FFT 便宜一个数量级 —— 定位（在整窗里找
    在哪）仍然必须用 FFT，但"在这个落点上到底像不像"完全可以直接算。
    """
    T = np.asarray(tpl, np.float32)
    T0 = T - T.mean()
    n = float(np.sqrt(float((T0 * T0).sum()))) + EPS
    return {"t0": (T0 / n).astype(np.float32), "h": int(T.shape[0]), "w": int(T.shape[1])}


def pearson_at(img, y, x, P):
    """img[y:y+h, x:x+w] 与归一化模板 P 的 Pearson 相关。"""
    h, w = P["h"], P["w"]
    if y < 0 or x < 0 or y + h > img.shape[0] or x + w > img.shape[1]:
        return -1.0
    c = img[y:y + h, x:x + w]
    c0 = c - c.mean()
    den = float(np.sqrt(float((c0 * c0).sum()))) + EPS
    if den < FLAT_STD * float(np.sqrt(h * w)):
        return -1.0                    # 平坦区没法比（同 ncc_map 的处理）
    return float((c0 * P["t0"]).sum() / den)


def best_offset(img, y, x, P, r=1):
    """在 (y,x) 附近 ±r 里找 Pearson 最高的一格，返回 (score, y, x)。

    只用来"把粗定位落到准确的那一两个像素上"：±1 足够，±2 以上成本就翻四倍，
    而换来的精度对 NCC/分块投票毫无意义。
    """
    best = (-2.0, y, x)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            s = pearson_at(img, y + dy, x + dx, P)
            if s > best[0]:
                best = (s, y + dy, x + dx)
    return best


def blend_map(img, tpl, img_edge=None, w=BLEND, tpl_edge=None):
    """灰度 NCC 与边缘 NCC 的加权和。img/tpl 必须是同域（同一工作尺度）。"""
    m1, m2 = channel_maps(img, tpl, img_edge, w, tpl_edge)
    return _combine(m1, m2, w)


# ---------------------------------------------------------------- 直方图 / 描述子

def hsv_hist(rgb, hb=8, sb=4, vb=4):
    """H-S-V 三维直方图，L1 归一化。对主题色/高亮/血条这类最敏感。"""
    hsv = rgb_to_hsv(to_float(rgb) if np.asarray(rgb).max() > 1.5 else np.asarray(rgb, np.float32))
    hi = np.clip((hsv[..., 0] * hb).astype(np.int32), 0, hb - 1)
    si = np.clip((hsv[..., 1] * sb).astype(np.int32), 0, sb - 1)
    vi = np.clip((hsv[..., 2] * vb).astype(np.int32), 0, vb - 1)
    idx = (hi * sb + si) * vb + vi
    cnt = np.bincount(idx.ravel(), minlength=hb * sb * vb).astype(np.float32)
    tot = cnt.sum()
    return cnt / tot if tot > 0 else cnt


def hog_lite(gray, cells=4, bins=8):
    """4x4 cell × 8 bin 无符号梯度方向直方图，L2 归一化。抓“结构”，不要颜色。"""
    mag, ang = sobel(gray)
    o = (ang % np.pi) / np.pi * bins
    oi = np.clip(o.astype(np.int32), 0, bins - 1)
    h, w = gray.shape
    ch, cw = max(1, h // cells), max(1, w // cells)
    ys = np.clip(np.arange(h) // ch, 0, cells - 1)
    xs = np.clip(np.arange(w) // cw, 0, cells - 1)
    flat = (ys[:, None] * cells + xs[None, :]).ravel()
    idx = flat * bins + oi.ravel()
    vals = np.bincount(idx, weights=mag.ravel().astype(np.float64), minlength=cells * cells * bins)
    v = vals.reshape(-1).astype(np.float32)
    n = float(np.sqrt((v * v).sum())) + EPS
    return v / n


def dhash(gray, n=8):
    """64bit 差分哈希。判断“是不是同一块东西”，对亮度整体漂移不敏感。"""
    small = resize_area(gray, n + 1, n)
    bits = (small[:, 1:] > small[:, :-1]).ravel()
    return int.from_bytes(np.packbits(bits).tobytes(), "big")


def hamming(a, b):
    return int(a ^ b).bit_count()


def _peaks(m, k=6, gap=4):
    """响应图上取前 k 个峰（互相至少隔 gap 格），返回 [(y,x)]，第一项就是全局 argmax。"""
    m = np.asarray(m, np.float32)
    out = []
    if m.size == 0:
        return out
    work = m.copy()
    H, W = work.shape
    for _ in range(max(1, k)):
        if work.size == 0:
            break
        i = int(np.argmax(work))
        y, x = divmod(i, W)
        if work[y, x] <= -1.5:
            break
        out.append((y, x))
        work[max(0, y - gap):y + gap + 1, max(0, x - gap):x + gap + 1] = -3.0
    return out


def tile_stats(I, T, grid=3, k=None, k_frac=TILE_K, thr=None):
    """分块投票的完整读数：{"score": 最好 k 块均值, "cover": 可见且对得上的块占比, "valid": 有内容的块数}。

    `cover` 是**遮挡**的度量：分母必须是**全部块**（grid*grid），不能是"有内容的块"。
    被一块不透明色块盖住时，那一块变成纯色 → 方差为 0 → 被跳过；若分母只算剩下的块，
    盖住七成反而算出 cover=1.0（实测过：70% 被盖 > cover 1.00 > 假自信）。分母固定，
    cover 就老老实实等于"这个控件还有多大比例看得见且对得上"。

    `thr` 默认取 `TILE_ACCEPT`：一块算"对得上"的门槛和整体采信门槛同一量级。
    `valid` 少于 2 块时返回 -1（证据不足，不许下结论）。
    """
    if thr is None:
        thr = TILE_ACCEPT
    I = to_gray(I) if np.ndim(I) == 3 else np.asarray(I, np.float32)
    T = to_gray(T) if np.ndim(T) == 3 else np.asarray(T, np.float32)
    I = np.asarray(I, np.float32)
    T = np.asarray(T, np.float32)
    h, w = T.shape
    if I.shape[:2] != (h, w):
        I = fit_to(I, h, w)
    rs = []
    for gy in range(grid):
        for gx in range(grid):
            y0, y1 = gy * h // grid, (gy + 1) * h // grid
            x0, x1 = gx * w // grid, (gx + 1) * w // grid
            if y1 - y0 < 3 or x1 - x0 < 3:
                continue
            a = I[y0:y1, x0:x1].astype(np.float64).ravel()
            b = T[y0:y1, x0:x1].astype(np.float64).ravel()
            if a.std() < FLAT_STD or b.std() < FLAT_STD:
                continue
            a -= a.mean()
            b -= b.mean()
            den = np.sqrt(float((a * a).sum()) * float((b * b).sum()))
            if den < EPS:
                continue
            rs.append(float((a * b).sum()) / den)
    if len(rs) < 2:
        return {"score": -1.0, "cover": -1.0, "valid": len(rs)}
    if k is None:
        k = max(2, int(round(len(rs) * k_frac)))
    k = min(k, len(rs))
    total = grid * grid
    cover = float(sum(1 for v in rs if v >= thr)) / float(total)
    return {"score": float(np.mean(sorted(rs)[-k:])), "cover": cover, "valid": len(rs)}


def tile_score(I, T, grid=3, k=None, k_frac=TILE_K):
    """把模板切成 grid×grid 块，逐块算 Pearson，返回**最好的 k 块均值**。

    为什么需要它：整块 NCC 是"平均分"—— 容器里换掉一半内容就把平均分拉到 0.38，
    哪怕边框、标题栏分毫未动。

    为什么是「最好的 k 块」而不是中位数：实测对话面板 3×3 的九块，在**同一控件但内容
    完全不同**的帧上是 `[0.56,1,1,0.09,-,-,0.18,0.44,0.49]` —— 中位数 0.49（不及
    格），前三块均值 0.85（及格）；而在**根本没有这个控件**的画面上前三块均值只有
    0.23。也就是说"只要还有几块严丝合缝地认得出"就足以说明控件还在，中位数把这个
    信号平均掉了。k 取有效块数的三分之一（至少 2 块），既留出冗余又不至于被单个巧合
    块骗过去。
    """
    I = to_gray(I) if np.ndim(I) == 3 else np.asarray(I, np.float32)
    T = to_gray(T) if np.ndim(T) == 3 else np.asarray(T, np.float32)
    I = np.asarray(I, np.float32)
    T = np.asarray(T, np.float32)
    h, w = T.shape
    if I.shape[:2] != (h, w):
        I = fit_to(I, h, w)
    rs = []
    for gy in range(grid):
        for gx in range(grid):
            y0, y1 = gy * h // grid, (gy + 1) * h // grid
            x0, x1 = gx * w // grid, (gx + 1) * w // grid
            if y1 - y0 < 3 or x1 - x0 < 3:
                continue
            a = I[y0:y1, x0:x1].astype(np.float64).ravel()
            b = T[y0:y1, x0:x1].astype(np.float64).ravel()
            if a.std() < FLAT_STD or b.std() < FLAT_STD:
                continue                      # 纯色块不投票（否则 8 个平坦块压过 1 个有内容块）
            a -= a.mean()
            b -= b.mean()
            den = np.sqrt(float((a * a).sum()) * float((b * b).sum()))
            if den < EPS:
                continue
            rs.append(float((a * b).sum()) / den)
    if len(rs) < 3:
        return -1.0                   # 有内容的块太少，投票结果不可信（小图标常见）
    if k is None:
        k = max(2, int(round(len(rs) * k_frac)))
    k = min(k, len(rs))
    return float(np.mean(sorted(rs)[-k:]))


# ---------------------------------------------------------------- 模板匹配（FFT-NCC）

def _box_sum(x, h, w):
    """每个 h×w 窗口的和，shape (H-h+1, W-w+1)。"""
    x = np.asarray(x, np.float64)
    ii = np.zeros((x.shape[0] + 1, x.shape[1] + 1), np.float64)
    ii[1:, 1:] = x.cumsum(0).cumsum(1)
    return ii[h:, w:] - ii[:-h, w:] - ii[h:, :-w] + ii[:-h, :-w]


def _corr_same(I, T):
    """互相关 c[k,l] = Σ I[k+i, l+j]·T[i,j]，只返回无环绕的合法区域。

    dtype 跟随输入：传 float32 就用 float32/complex64 的 FFT（快一倍、省一半内存）。
    """
    I = np.asarray(I)
    T = np.asarray(T)
    H, W = I.shape
    h, w = T.shape
    if h > H or w > W:
        return None
    FI = np.fft.rfft2(I, (H, W))
    FT = np.fft.rfft2(T, (H, W))
    c = np.fft.irfft2(FI * np.conj(FT), (H, W))
    return c[: H - h + 1, : W - w + 1].astype(np.float32)


def ncc_map(I, T):
    """归一化互相关图。I/T 为灰度 float。返回 [-1,1] 的 score map。

    FFT 走 float32（complex64），积分图仍走 float64：
    pocketfft 对 float32 是 2 倍速、一半内存，而 NCC 的分数精度只需要 1e-3；
    反过来，方差 = E[I²]-E[I]² 是两个大数相减，用 float32 的累计和会把小方差吃成负数。
    """
    I = np.asarray(I, np.float32)
    T = np.asarray(T, np.float32)
    h, w = T.shape
    if float(T.std()) < FLAT_STD:
        # 模板本身就是一片纯色（用户在空白处画了个框）：它的细节全是噪声，放大后比什么
        # 都像。这种实体必须在编译期就报出来，运行时一律不认。
        return None
    T0 = T - T.mean()
    nrm = float(np.sqrt(float((T0 * T0).sum()))) + EPS
    T0 = T0 / nrm
    num = _corr_same(I, T0)
    if num is None:
        return None
    I64 = I.astype(np.float64)
    s1 = _box_sum(I64, h, w)
    s2 = _box_sum(I64 * I64, h, w)
    var = s2 - s1 * s1 / float(h * w)
    den = np.sqrt(np.maximum(var, 0.0)) + EPS
    out = num / den
    # 平坦区域上 NCC 没有意义：搜索块的方差趋零时这是 0/0，浮点噪声会把它推到 ±1，
    # 于是"空背景"被打成完美匹配（实测 foreign.png 上一块渐变色对 14x32 的选项光标
    # 打 1.000 分）。标准差低于 FLAT_STD（约 4/255）的窗口一律判"无法判定"给 -1。
    flat = den < FLAT_STD * float(np.sqrt(h * w))
    out = np.where(flat, -1.0, out)
    return np.clip(out, -1.0, 1.0).astype(np.float32)


_DBG = []


def cover_search(search_gray, tpl, thr=None, ds=6, grid=3):
    """**遮挡兜底**：不用整块 NCC（挡住一半就崩），改用"分块投票"做一张分数地图。

    做法：把搜索图和模板一起缩小 ds 倍，逐一算每块小模板的 NCC 响应图，
    再把"这块对上了"（>= thr）累加成一张覆盖图。峰值所在位置就是"最多块同时对上"的地方 ——
    这正是部分遮挡时唯一还站得住的证据。

    `ds=3` 是速度与精度的折中：2360x1140 的搜索窗缩到 787x380，九块小模板各一次 FFT，
    实测 15~25ms，只在正常路径判"不在"之后才走这条路。
    返回 {"cover","x","y","ds"}；证据不足返回 None。
    """
    thr = TILE_ACCEPT if thr is None else thr
    T = np.asarray(tpl, np.float32)
    th, tw = T.shape[:2]
    if min(th, tw) < TILE_MIN_SIDE:
        return None                     # 小图标切块没有意义（每块太碎，随机内容也能凑分）
    Sh, Sw = np.asarray(search_gray).shape[:2]
    ds = max(1.0, float(ds))
    sw, sh = max(tw + 4, int(round(Sw / ds))), max(th + 4, int(round(Sh / ds)))
    sm = resize_area(search_gray, sw, sh)
    tw2, th2 = max(8, int(round(tw / ds))), max(8, int(round(th / ds)))
    tm = resize_area(T, tw2, th2)
    if th2 > sh or tw2 > sw:
        return None
    acc = np.zeros((sh - th2 + 1, sw - tw2 + 1), np.float32)
    used = 0
    for gy in range(grid):
        for gx in range(grid):
            y0, y1 = gy * th2 // grid, (gy + 1) * th2 // grid
            x0, x1 = gx * tw2 // grid, (gx + 1) * tw2 // grid
            part = tm[y0:y1, x0:x1]
            if min(part.shape) < 3 or float(part.std()) < FLAT_STD:
                continue
            m = ncc_map(sm, part)
            if m is None:
                continue
            hit = (m >= thr).astype(np.float32)
            # 这一块在"整块模板左上角"坐标系里的索引就是 (模板位置 + 块偏移)，所以是
            # acc[p] += hit[p + 偏移]：把块偏移从 hit 里切掉、贴回 acc 的原点。
            hh = min(acc.shape[0], hit.shape[0] - y0)
            ww = min(acc.shape[1], hit.shape[1] - x0)
            if hh <= 0 or ww <= 0:
                continue
            acc[:hh, :ww] += hit[y0:y0 + hh, x0:x0 + ww]
            used += 1
    if used < 2 or float(acc.max()) <= 0:
        return None
    py, px = np.unravel_index(int(np.argmax(acc)), acc.shape)
    return {"cover": float(acc[py, px]) / float(grid * grid),
            "x": int(round(px * ds)), "y": int(round(py * ds)),
            "ds": ds, "n": int(acc[py, px])}


def locate(search_gray, tmpl_gray, scales=(1.0,), coarse=280, min_second_gap=0.0,
           blend=BLEND, refine_max=420, detail=False, tpl_edge=None,
           tpl_pack=None):
    """在 search_gray 里找 tmpl_gray。**先用灰度定位，再在落点小窗里给两路打分。**

    这个拆分是性能的关键（实测 124ms -> 55ms）：
      * 定位只需要**一路**信号。搜索窗大时（选项光标 1060x230、对话面板 1280x380）
        两路都做整窗 FFT，等于把最贵的一步做了两遍，而两路的 argmax 几乎总在同一处。
      * 定位之后，真正要拿去投票的两个分数只在**落点周围一个"模板大小 + 2f"**的小窗里算，
        那个窗只有几十像素见方，边缘图也只需要在小窗里算（省掉整窗 sobel）。
    代价：极少数"只有边缘能拉开差距"的实体，定位可能偏一两像素 —— 位置由灰度定，
    "在不在"仍由两路在小窗上的分数定，语义反而更干净。

    粗到细：搜索窗超过 coarse(=280px) 就先整窗缩 1/f 定位，再回原分辨率小窗细定位。
    小模板不许被缩没：14x32 的光标缩到 6x8 只剩一团色块，argmax 会落到别的选项行上
    （实测偏到 y=533、真值 y=470），所以按模板最短边给 f 兜底，留够 8px。

    返回 (score, x, y, scale, gap)；x,y 是模板左上角在 search 图内的坐标。
    detail=True 时多返回落点上的 {"ncc_g":.., "ncc_e":..}（两个标量）。
    """
    search_gray = np.asarray(search_gray, np.float32)
    tmpl_gray = np.asarray(tmpl_gray, np.float32)
    Sh, Sw = search_gray.shape
    th0, tw0 = tmpl_gray.shape
    use_edge = blend is not None and blend[1] > 0
    f = max(1.0, max(Sh, Sw) / float(coarse))
    mn = min(th0, tw0)
    if mn / f < 8.0:
        f = max(1.0, mn / 8.0)
    best = (-2.0, 0, 0, 1.0)
    aux = (None, None)
    best_aux = None
    best_t = -1.0
    best_cov = -1.0
    last_map = None
    for sc in scales:
        th = max(6, int(round(th0 * sc)))
        tw = max(6, int(round(tw0 * sc)))
        if th > Sh or tw > Sw:
            continue
        tpl = resize_area(tmpl_gray, tw, th) if (tw, th) != (tw0, th0) else tmpl_gray
        # ---- 第一段：灰度定位（粗定位 or 全分辨率）
        if f > 1.2:
            sh, sw = max(8, int(round(Sh / f))), max(8, int(round(Sw / f)))
            sth, stw = max(6, int(round(th / f))), max(6, int(round(tw / f)))
            if sth > sh or stw > sw:
                continue
            m1 = ncc_map(resize_area(search_gray, sw, sh), resize_area(tpl, stw, sth))
            is_coarse = True
        else:
            m1 = ncc_map(search_gray, tpl)
            is_coarse = False
        if m1 is None or m1.size == 0:
            continue
        i = int(np.argmax(m1))
        y, x = divmod(i, m1.shape[1])
        s = float(m1[y, x])
        # ---- 第二段：对**整张**响应图上的前 K 个峰逐个做落点小窗打分，再用
        # 「分块投票」重排。只看 argmax 会在容器内容变化时滑到别处（实测对话面板
        # 下移 86px）；而小窗只有"模板 + 2f"大小，装不下 86px 的跳动，所以候选必须
        # 从整张图上取，不能只在 argmax 附近找。
        # 分块投票只对"够大的模板"有意义：14x32 的图标切九块、每块 4x10 像素，
        # 随机内容也能让其中两块凑出高分（实测把选项光标在无关画面上打了 0.7+）。
        use_tile = min(th, tw) >= TILE_MIN_SIDE
        pad = int(round(f)) + 2
        gapp = max(3, min(m1.shape) // 5)
        cands = _peaks(m1, k=PICK_K, gap=gapp)
        if is_coarse:
            cands = [(int(round(py2 * f)), int(round(px2 * f))) for py2, px2 in cands]
        # 先按「分块投票」把候选排序（只在原分辨率上比较像素，几十微秒），再只对
        # 前两个候选做小窗两路打分。为什么必须先排序：灰度响应图的第一名在容器类
        # 控件上经常是错的（实测对话面板下移 86px），一旦直接采信就再也回不来 ——
        # 而分块投票在"还是它"和"不是它"之间分得很开（0.85 / 0.23）。
        rows = []
        for (cy, cx) in cands:
            cy = min(max(0, int(cy)), max(0, Sh - th))
            cx = min(max(0, int(cx)), max(0, Sw - tw))
            tv = tile_score(search_gray[cy:cy + th, cx:cx + tw], tpl) if use_tile else -1.0
            rows.append((max(tv, 0.0) if use_tile else 0.0, cy, cx, tv))
        rows.sort(key=lambda r: -r[0])
        # 第二段的打分不再开 FFT，改成**在落点上直接算 Pearson**（±1 或 ±2 像素）。
        # 理由：定位（整窗里找在哪）才需要 FFT；"这个落点到底像不像"只是一次点乘，
        # 成本差一个数量级。11 个实体每帧本来要做 66 张 FFT，现在只剩 11 张（灰度定位）
        # + 一次整窗 sobel。
        se_img = edge_of(search_gray) if use_edge else None
        # 归一化模板由调用方缓存（observe._prep 里存了 gp/ep）；单尺度时直接复用，
        # 多尺度或调用方没给才现算 —— 现算一次就是一次全图减均值取范数。
        if tpl_pack is not None and sc == 1.0:
            gp, ep = tpl_pack
            if not use_edge:
                ep = None
        else:
            ge = (tpl_edge if (tpl is tmpl_gray and tpl_edge is not None) else edge_of(tpl))
            gp = norm_tpl(tpl)
            ep = norm_tpl(ge) if (use_edge and se_img is not None) else None
        rf = 0 if not is_coarse else max(1, int(round(f)))
        best_local = None          # (rank, comb, gx, gy, aux, tile)
        tscore = -1.0
        for _tv0, cy, cx, _t0 in rows[:1]:
            bc = (-2.0, cy, cx, -1.0, -1.0)
            for dy in range(-rf, rf + 1):
                for dx in range(-rf, rf + 1):
                    yy, xx = cy + dy, cx + dx
                    sg = pearson_at(search_gray, yy, xx, gp)
                    se = pearson_at(se_img, yy, xx, ep) if ep is not None else sg
                    comb = (blend[0] * sg + blend[1] * se) if ep is not None else sg
                    if comb > bc[0]:
                        bc = (comb, yy, xx, sg, se)
            comb, gy, gx, sg, se = bc
            tscore, tcover = -1.0, -1.0
            if use_tile and gy >= 0 and gx >= 0:
                _ts = tile_stats(search_gray[gy:gy + th, gx:gx + tw], tpl)
                tscore, tcover = _ts["score"], _ts["cover"]
            rank = comb + (TILE_W * max(tscore, 0.0) if use_tile else 0.0)
            if best_local is None or rank > best_local[0]:
                best_local = (rank, comb, gx, gy, (sg, se), tscore, tcover)
            if (use_tile and tscore >= TILE_EARLY) or comb >= s - 0.02:
                break                       # 够确定了：多数块认得出，或细定位不输全局峰
        if best_local is not None:
            _r, comb, gx, gy, agg, tscore, tcover = best_local
            _DBG.append((is_coarse, round(s, 3), round(comb, 3), gx, gy, round(tscore, 2), tcover))
            if comb >= s - 0.45 or tscore >= TILE_ACCEPT:
                s, x, y, aux = comb, gx, gy, agg
        if aux[0] is None:
            aux = (s, s)
        if s > best[0]:
            best = (s, int(x), int(y), float(sc))
            best_aux = aux
            best_t = tscore
            best_cov = tcover
    if best[0] <= -1.5:
        out = (float(best[0]), int(best[1]), int(best[2]), 1.0, 0.0)
        return out + ({"ncc_g": -1.0, "ncc_e": -1.0, "ncc_t": -1.0,
                       "ncc_cover": -1.0},) if detail else out
    aux = best_aux if best_aux is not None else (best[0], best[0])
    gap = 0.0
    if min_second_gap > 0 and last_map is not None and last_map.size:
        s, x, y = best[0], best[1], best[2]
        r = max(6, int(min(last_map.shape) * 0.2))
        r2 = last_map.copy()
        r2[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1] = -2.0
        if r2.size:
            gap = s - float(r2.max())
    out = (float(best[0]), int(best[1]), int(best[2]), float(best[3]), float(gap))
    if detail:
        return out + ({"ncc_g": aux[0], "ncc_e": aux[1], "ncc_t": best_t,
                       "ncc_cover": best_cov},)
    return out


# ---------------------------------------------------------------- 区域工具

def clamp_box(box, W, H, pad=0):
    x, y, w, h = [int(round(v)) for v in box]
    x = max(0, min(x - pad, W - 2))
    y = max(0, min(y - pad, H - 2))
    w = max(1, min(w + 2 * pad, W - x))
    h = max(1, min(h + 2 * pad, H - y))
    return [x, y, w, h]


def crop(rgb, box):
    x, y, w, h = box
    return rgb[y:y + h, x:x + w]


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / float(aw * ah + bw * bh - inter + EPS)


# ---------------------------------------------------------------- 特征包

def extract(rgb_crop):
    """一块画面 -> 可比较的特征向量集合。"""
    a = np.asarray(rgb_crop, np.uint8)
    small = resize_area(a, NORM, NORM)
    g = to_gray(small)
    h, w = a.shape[:2]
    return {
        "hsv": hsv_hist(small, 8, 4, 4),          # 128 维, 颜色
        "hog": hog_lite(g, 4, 8),                 # 128 维, 结构
        "dh": dhash(g, 8),                        # 64 bit, 身份
        "ar": round(float(w) / max(1.0, float(h)), 5),
        "edge": round(edge_density(g), 5),
    }


def compare(probe_crop, ref):
    """
    probe_crop 与已存参考特征的各维度相似度（都归一到 [0,1]，1 最好）。
    返回 dict：ncc 由模板匹配另外算，这里只有 4 个辅助维度。
    """
    f = extract(probe_crop)
    hsv_d = float(np.abs(f["hsv"] - np.asarray(ref["hsv"], np.float32)).sum()) / 2.0
    hog_cos = float(np.dot(f["hog"], np.asarray(ref["hog"], np.float32)))
    dh_d = hamming(f["dh"], int(ref["dh"])) / 64.0
    ar_rel = abs(f["ar"] - float(ref["ar"])) / max(float(ref["ar"]), EPS)
    return {
        "hsv_d": hsv_d,
        "hsv": max(0.0, 1.0 - hsv_d),
        "hog": max(0.0, min(1.0, hog_cos)),
        "dh_d": dh_d,
        # 线性、不设悬崖：老版本 1-dh_d/0.35 会把「变了 40% 位」直接压成 0，
        # 于是"内容变了一点"和"完全不是这个东西"在分数上无法区分（阈值也没法定）。
        "dh": max(0.0, 1.0 - dh_d),
        "ar_rel": ar_rel,
        "ar": f["ar"],
        "edge": f["edge"],
        "edge_d": abs(f["edge"] - float(ref["edge"])),
    }
