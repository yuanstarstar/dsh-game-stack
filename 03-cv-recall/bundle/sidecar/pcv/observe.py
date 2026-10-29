"""运行时：一帧画面 + 一个感知包 -> 结构化盘面。零 LLM 调用。

成本目标：10 个实体 < 50ms。
做法：
  - 每个实体**只在自己的搜索窗内**做多尺度 NCC（粗到细，见 feats.locate）
  - 颜色/结构/哈希/边缘 4 个廉价特征做软投票，权重由编译时的可分离度决定
  - NCC 是硬门：形状不对，别的特征再像也不算在（这是压制误报的关键）
  - 场景判定 = 实体存在向量最近邻 + 全局颜色直方图；离得太远直接报 unknown
  - 漂移检测 = 全局特征距离超阈值 -> stale，提示重编，而不是静默错答
"""
from __future__ import annotations

import time

import numpy as np

from . import digits
from . import propose
from . import pack as P
from . import feats

_STATE = {}                     # 进程内上一帧，用于 changed 检测
_BOXES = {}                     # 进程内上一帧各实体的位置（快速路径的 hint）

CONF_PRESENT = 0.55
# 遮挡：整块 NCC 一被挡住就掉，分块投票只掉被挡的那几块 —— 所以「还有几块对得上」
# 是判断"它还在，只是被挡住了"的唯一便宜证据。0.45 = 九块里还有四块严丝合缝。
OCCL_COVER = 0.42
STATIC_COVER = 0.90      # 校准帧覆盖率高于它 = 平时几乎整块都对得上，才谈得上"被遮挡"
OCCL_FULL = 0.78          # 到这个覆盖率就算"基本完整"，不再标遮挡
# 新东西：拿场景参考帧做差分，抹掉已知实体后剩下的连通域就是"没编过的东西"。
NOVEL_DIFF = 0.075        # 逐像素平均绝对差（0..1）
NOVEL_MIN_FRAC = 0.0012   # 至少占画面 0.12% 才算一处（再小就是噪声/动画碎屑）
NOVEL_MAX_FRAC = 0.30     # 单块超过画面的 30% 就不是"一个东西"，多半是整屏变了
NOVEL_MAX = 4
NOVEL_BUSY = 0.25         # 差异像素占比超过这个值 → 整屏都不一样，不逐块报
NOVEL_REF_W = 320
NCC_GATE = 0.45
AR_GATE = 0.25
UNKNOWN_FACTOR = 1.6            # 距离超过 radius 的这么多倍 -> 直接说 unknown
PAR = 4                         # 实体级并行线程数（0/1 = 串行）


def _soft(v, tau, pos):
    """特征值 -> 0..1 置信度；在 tau 处 = 0.5，拉满到 pos 时 ≈ 0.94。"""
    v, tau, pos = float(v), float(tau), float(pos)
    if pos - tau < 1e-6:
        return 1.0 if v >= tau else 0.0
    k = (pos - tau) / 2.7
    z = float(np.clip((v - tau) / k, -30, 30))
    return round(1.0 / (1.0 + float(np.exp(-z))), 4)


def _prep(pk_ent):
    """每个实体只解一次模板（PNG 解码 + 灰度 + 边缘图），之后每帧复用。

    这三样占单次 recall 的相当一部分：PIL 解一张 png ~0.4ms，sobel 一张 256² 模板
    ~0.3ms，11 个实体每帧多花 7~8ms。它们对**同一帧序列是不变的**，没有理由重算。
    缓存挂在实体字典的 `_prep` 上（以 _ 开头，save_pack 会剃掉，不会进 JSON）。
    """
    p = pk_ent.get("_prep")
    if p is not None:
        return p
    from . import pack as P
    tpl = pk_ent.get("template") or {}
    tg = feats.to_gray(P.decode_png(tpl["png"])) if tpl.get("png") else None
    tge = feats.edge_of(tg) if tg is not None else None
    # 归一化模板也一起缓存：`norm_tpl` 每次都要减均值、取范数，11 个实体每帧一次
    # 就是几毫秒，而它只跟模板有关。
    p = {"tg": tg, "tge": tge,
         "gp": (feats.norm_tpl(tg) if tg is not None else None),
         "ep": (feats.norm_tpl(tge) if tge is not None else None)}
    pk_ent["_prep"] = p
    return p


def match_entity(pk_ent, frame, debug=False, hint=None, occl=False):
    """认出单个实体。hint＝上一帧它所在的位置，用来跳过整窗搜索。

    为什么要 hint：UI 里的控件绝大多数帧都在原地，每帧把 1280x380 的搜索窗重新扫
    一遍是白花钱。有 hint 时只在那块小窗里找（一次小 FFT、一两毫秒），找不到（分数
    不够）才退回整窗搜索 —— 退回去只是慢一点，不会漏。
    """
    H, W = frame.shape[:2]
    sx, sy, sw, sh = pk_ent["search"]
    sx, sy = max(0, min(sx, W - 4)), max(0, min(sy, H - 4))
    sw, sh = max(4, min(sw, W - sx)), max(4, min(sh, H - sy))
    _pz = _prep(pk_ent)
    tg, tge = _pz["tg"], _pz["tge"]
    if tg is None:
        return {"id": pk_ent["id"], "present": 0, "conf": 0.0, "box": None, "why": "no-template"}

    # 小窗：以 hint 为中心，留出 12% 的挪动余量
    region = (sx, sy, sw, sh)
    if hint:
        hx, hy, hw, hh = (int(v) for v in hint)
        hp = max(8, int(0.12 * max(hw, hh)))
        x0 = max(sx, hx - hp)
        y0 = max(sy, hy - hp)
        x1 = min(sx + sw, hx + hw + hp)
        y1 = min(sy + sh, hy + hh + hp)
        if x1 - x0 > hw and y1 - y0 > hh:
            region = (x0, y0, x1 - x0, y1 - y0)
    rx, ry, rw, rh = region
    raw_crop = frame[ry:ry + rh, rx:rx + rw]

    scales = pk_ent.get("scales") or [1.0]
    # 在「工作尺度」里匹配：模板保持原生尺寸，把搜索图缩 1/ws（见 compile.compile_entity）
    ws = float(pk_ent.get("ws") or 1.0)
    if ws > 1.02:
        gw = max(12, int(round(rw / ws)))
        gh = max(12, int(round(rh / ws)))
        if gh < tg.shape[0] or gw < tg.shape[1]:
            return {"id": pk_ent["id"], "present": 0, "conf": 0.0, "box": None,
                    "why": "search-window-too-small"}
        # 先转灰度再缩：整窗 resize 三通道比 resize 单通道贵得多（实测 72ms -> 100ms，
        # 因为 reduceat 要按 3 倍数据量跑两遍），灰度化本身只有 3 次乘加。
        work = feats.resize_area(feats.to_gray(raw_crop), gw, gh)
    else:
        work = feats.to_gray(raw_crop)
        ws = 1.0

    score, x, y, sc, _gap, ch = feats.locate(
        work, tg, scales, detail=True, tpl_edge=tge,
        tpl_pack=(_pz["gp"], _pz["ep"]))
    ox, oy = rx + x * ws, ry + y * ws
    tw, th = tg.shape[1] * sc * ws, tg.shape[0] * sc * ws

    box = feats.clamp_box([int(round(ox)), int(round(oy)),
                           max(6, int(round(tw))), max(6, int(round(th)))], W, H)
    crop = frame[box[1]:box[1] + box[3], box[0]:box[0] + box[2]]

    tau = pk_ent["tau"]
    wt = pk_ent["w"]
    diag = pk_ent.get("diag") or {}

    def _p(k, d):
        return (diag.get(k) or {}).get("pos", d)

    # 两路 NCC 分开投票：血条靠边框/刻度线（边缘），面板靠整体明暗（灰度）
    sg, se = ch.get("ncc_g"), ch.get("ncc_e")
    sub = {}
    if "ncc_g" in tau:
        sub["ncc_g"] = _soft(sg if sg is not None else score, tau["ncc_g"], _p("ncc_g", 0.98))
    if "ncc_e" in tau:
        sub["ncc_e"] = _soft(se if se is not None else score, tau["ncc_e"], _p("ncc_e", 0.95))
    if "ncc_t" in tau:
        st = ch.get("ncc_t")
        sub["ncc_t"] = _soft(st if st is not None else score, tau["ncc_t"], _p("ncc_t", 0.92))
    cmp_ = {}
    if crop.size:
        cmp_ = feats.compare(crop, pk_ent["feat"])
        for k, d in (("hsv", 0.95), ("hog", 0.90), ("dh", 0.90)):
            if k in tau:
                sub[k] = _soft(cmp_[k], tau[k], _p(k, d))
        ar_rel = cmp_["ar_rel"]
    else:
        for k in ("hsv", "hog", "dh"):
            if k in tau:
                sub[k] = 0.0
        ar_rel = 1.0

    conf = sum(float(wt.get(k, 0.0)) * sub.get(k, 0.0) for k in wt)
    # 形状这条路单独算一遍：结构（两路 NCC）回答"这个东西在不在"，
    # 外观（hsv/hog/dh）只该"加成"，不该"否决"。血条填充一变、地图光点一换，
    # 外观分会接近 0，但它确实还在那儿 —— 全票加权会让外观一票否决存在性。
    wn = {k: float(wt[k]) for k in wt if k.startswith("ncc") and wt[k] > 0}
    sn = sum(wn.values())
    if sn > 0:
        conf = max(conf, sum(w * sub.get(k, 0.0) for k, w in wn.items()) / sn)
    # 分块投票是"第几条独立证据"：它只看九宫格里还有几块严丝合缝，
    # 对"整体变暗/换文案/换主题"免疫。容器类控件整块 NCC 会崩到 0.3，
    # 但分块还能有 0.85 —— 所以它不该被整块 NCC 平均掉，而该单独成立。
    # 但它**不参与 gate**：14x32 这种小模板只要三两块巧合就能刷到高分，
    # 用它当"在不"的门槛在负样本上会误报（实测 foreign 上把选项光标打了出来）。
    st = ch.get("ncc_t")
    cov = ch.get("ncc_cover")
    # 校准帧上这块模板自己的覆盖率基线（编译时算好）：跟它比才知道"这次是不是缺了"。
    cov0 = float(pk_ent.get("cov0") if pk_ent.get("cov0") is not None else -1.0)
    # 分块投票当"在不在"的证据，门槛比"能不能量一下"更高：40x54 的图标切九块每块才
    # 13x18，换一块纹理就能凑出七八块巧合（实测把金币图标在别处打出了 0.78 覆盖率、
    # 框整体偏 96px）。所以**判定**只在 min边 ≥48 的模板上做；小图标仍可量 cover，只是
    # 不许它单独决定存在性。
    tmin = min(int(pk_ent["template"]["w"]), int(pk_ent["template"]["h"]))
    tile_big = tmin >= 24
    tile_gate = tmin >= 48
    if st is not None and tile_big and st >= feats.TILE_ACCEPT and "ncc_t" in tau:
        conf = max(conf, sub.get("ncc_t", 0.0))
    gate = ar_rel <= AR_GATE
    weak_ncc = max(sg or -1.0, se or -1.0) < NCC_GATE
    if weak_ncc:
        # 大模板的分块投票可以单独当"它还在"的证据：容器整体换内容时两路 NCC 都会
        # 掉到 0.3 左右，而九宫格里通常还有两三块严丝合缝（实测 0.85）。
        # 但**必须同时看 cover**：被盖掉七成时有效块只剩三块且都满分，score 仍是 1.0 ——
        # 光看 score 会得出"完好无损"的假结论。cover 是分母固定的，盖住就算数。
        gate = bool(tile_gate and st is not None and st >= feats.TILE_ACCEPT)
    # ---- 遮挡：一块黑布压掉半边，整块 NCC 会掉到 0.4~0.55，但没被压的块还是 0.95+。
    # 这时"还有多少块对得上"才是真相：cover≥0.45 说明它还在，只是看不全。
    occluded = 0
    via = "-"
    if (tile_gate and cov is not None and cov >= OCCL_COVER
            and ar_rel <= AR_GATE):
        gate = True                      # 还有什么块看得见，就足以说明它还在
        # 覆盖率喂进置信度：只看得见一半 → 0.62 左右，看得见八成 → 0.92 左右
        c_cov = 0.55 + 0.5 * min(1.0, (cov - OCCL_COVER) / max(0.05, OCCL_FULL - OCCL_COVER))
        conf = max(conf, min(0.97, c_cov))
        via = "cover"
    # ---- 遮挡兜底（第二遍）：整块 NCC 在重遮挡下连"在哪"都定不准（黑布把 FFT 峰压到
    # 别的地方去了），这时唯一还站得住的证据是"还有些块严丝合缝"。所以先用分块投票做
    # 一张覆盖地图找到大概位置，再在那个落点上读一次分块读数。只在 `occl=True` 时才做：
    # 这条路要九次小 FFT（约 30~80ms），只该用于"场景已确认、某个已知控件却不见了"。
    if (not gate or conf < CONF_PRESENT) and occl and tile_gate:
        cs = feats.cover_search(work, tg, ds=4)
        if cs is not None and cs["cover"] >= OCCL_COVER:
            px = max(0, min(int(cs["x"]), max(0, work.shape[1] - tg.shape[1])))
            py = max(0, min(int(cs["y"]), max(0, work.shape[0] - tg.shape[0])))
            _t = feats.tile_stats(work[py:py + tg.shape[0], px:px + tg.shape[1]], tg)
            if _t["cover"] >= OCCL_COVER:
                cov = _t["cover"]
                box = feats.clamp_box([int(round(rx + px * ws)), int(round(ry + py * ws)),
                                       max(6, int(round(tg.shape[1] * ws))),
                                       max(6, int(round(tg.shape[0] * ws)))], W, H)
                gate = True
                conf = max(conf, min(0.95, 0.55 + 0.5 * min(1.0, (cov - OCCL_COVER) / max(0.05, OCCL_FULL - OCCL_COVER))))
                occluded = 1 if cov < OCCL_FULL else 0
                via = "occl"
    present = 1 if (conf >= CONF_PRESENT and gate) else 0
    # 「被挡住」= 认得出来，但看得见的比例明显低于它自己的正常水平。
    # 阈值取 max(OCCL_COVER+0.05, cov0*0.8)：正常覆盖 1.0 的模板掉到 0.85 以下才算遮挡；
    # 天然只有 0.6 覆盖的模板（小地图黑底）就别天天报"被遮挡"。
    if (present and cov is not None and cov >= 0 and cov0 >= STATIC_COVER
            and cov < cov0 * 0.8 and (st is None or st >= 0.95)):
        # 只有"平时几乎整块都对得上"的模板（cov0 ≥0.9）才报被遮挡：
        # 内部本来就天天变的容器（对话面板 cov0=0.78）分不清是遮挡还是换了内容，不瞎报。
        occluded = 1
        conf = min(conf, 0.80)           # 只看得到一部分，就别把话说满
    out = {
        "id": pk_ent["id"], "present": present,
        "conf": round(float(conf), 3),
        "box": box if present else None,
        "ncc": round(float(max(sg or -1.0, se or -1.0)), 3),
        "ncc_g": round(float(sg if sg is not None else -1.0), 3),
        "ncc_e": round(float(se if se is not None else -1.0), 3),
        "ncc_t": round(float(ch.get("ncc_t") if ch.get("ncc_t") is not None else -1.0), 3),
        "cover": round(float(cov if cov is not None else -1.0), 3),
        "occluded": int(occluded if present else 0),
        "sub": sub,
        "via": via,
    }
    if debug:
        out["found_box"] = box
        out["gate"] = bool(gate)
        out["ar_rel"] = round(float(ar_rel), 3)
        out["sg"] = None if sg is None else round(float(sg), 3)
        out["se"] = None if se is None else round(float(se), 3)
        out["tscore"] = None if st is None else round(float(st), 3)
        out["cov"] = None if cov is None else round(float(cov), 3)
        out["cmp"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in cmp_.items()}
        out["ws"] = ws
        out["tau"] = tau
        out["w"] = wt
    return out


def _match_one(e, frame, h, fallback, occl=False):
    r = match_entity(e, frame, hint=h, occl=occl)
    if h is not None and r["present"] == 0 and fallback and not occl:
        r = match_entity(e, frame, hint=None, occl=True)   # 快路径只是快，不会漏
    return r


def match_entities(pk, frame, only_presence=False, hints=None, fallback=True,
                   workers=PAR, occl=False):
    """批量认实体。hints={id: box}（上一帧的位置）；命中不了就退回整窗搜索。

    `fallback` 只在有 hint 时有意义：hint 里没认出来就整窗重搜一次。

    实体之间**完全独立**（各自用自己的模板和自己的搜索窗，互不读对方的结果），所以
    可以真的并行跑：numpy 的 FFT 与 ufunc 都放 GIL，4 个线程基本是 2~3 倍加速，
    而这一批正是纯 numpy 的重活。结果按 `pk["entities"]` 的顺序收回，输出不变。
    """
    hints = hints or {}
    ents = pk.get("entities") or []
    if workers > 1 and len(ents) >= 4:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(workers, len(ents))) as tp:
                futs = [tp.submit(_match_one, e, frame, hints.get(e["id"]), fallback, occl)
                        for e in ents]
                return [f.result() for f in futs]
        except Exception:
            pass                                    # 并行只是加速，失败了顺序跑
    return [_match_one(e, frame, hints.get(e["id"]), fallback, occl) for e in ents]


def _global_desc(small_rgb):
    """全局场景签名。传进来的应该是已经缩好的小图（见 observe 里的 shared small）。"""
    g = small_rgb.astype(np.uint8)
    return {"hist": feats.hsv_hist(g, 8, 4, 4), "dh": feats.dhash(feats.to_gray(g), 8)}


def _changed_tiles(pk_id, small_rgb, tile=32):
    """变化格数。同样吃小图 —— 每帧为两件事各缩一次全幅图是纯浪费（实测省 ~25ms）。"""
    prev = _STATE.get(pk_id)
    small = small_rgb
    if prev is None or prev.shape != small.shape:
        _STATE[pk_id] = small
        return -1
    d = np.abs(small - prev).mean(axis=2)
    bh, bw = max(1, 90 // tile), max(1, 160 // tile)
    h2, w2 = (90 // bh) * bh, (160 // bw) * bw
    blk = d[:h2, :w2].reshape(h2 // bh, bh, w2 // bw, bw).mean(axis=(1, 3))
    changed = int((blk > 3.0).sum())
    _STATE[pk_id] = small
    return changed


def _atlas_of(pk):
    """字形图集（解码一次挂在包上，别每帧都解 PNG）。"""
    a = pk.get("_atlas")
    if a is None:
        a = digits.decode(pk.get("glyphs"))
        pk["_atlas"] = a
    return a


def _novel(pk, frame, results, scene, opts):
    """找出「还没编过的东西」：和场景参考帧做差分，抹掉已知实体，剩下的连通域就是它。

    这不是识别，是**发现** —— 它能说的是"这块位置有个约 90x70 的东西，偏红，在画面 62%,48%"，
    说不出"这是史莱姆"。在游戏里这恰恰够用：先知道"多了一个东西"，再决定要不要为它编译一次。

    前提也是诚实的：参考帧是编译这一帧。所以它适合**同一房间/同一界面内**的变化（冒出新敌人、
    掉落物、弹幕、HUD 多一格），换房间/换界面时整屏都不一样，这时只回一句"差别很大"，不逐块报。
    """
    sc = None
    for it in pk.get("scenes") or []:
        if it.get("name") == scene:
            sc = it
            break
    if not sc or not sc.get("ref"):
        return None
    ref = P.decode_png(sc["ref"])
    H, W = frame.shape[:2]
    rh, rw = ref.shape[:2]
    cur = feats.resize_area(frame, rw, rh)
    d = np.abs(cur.astype(np.float32) - ref.astype(np.float32)).mean(axis=2) / 255.0
    mask = d > NOVEL_DIFF
    sx, sy = float(rw) / max(1, W), float(rh) / max(1, H)
    for r in results:                       # 已知且在场的东西不算"新"
        b = r.get("box")
        if not r.get("present") or not b:
            continue
        x, y, w, h = b
        x0, y0 = max(0, int(x * sx) - 3), max(0, int(y * sy) - 3)
        x1, y1 = min(rw, int((x + w) * sx) + 3), min(rh, int((y + h) * sy) + 3)
        mask[y0:y1, x0:x1] = False
    frac = float(mask.mean())
    if frac < NOVEL_MIN_FRAC:
        return {"novel": [], "novel_note": None}
    if frac > NOVEL_BUSY:
        return {"novel": [], "novel_note":
                "整屏和参考帧差别很大（%.0f%% 像素不同）：多半换了房间/界面，不像只是多了个东西"
                % (100 * frac)}
    min_area = max(6, int(mask.size * NOVEL_MIN_FRAC))
    comps = propose._components(mask, min_area=min_area, max_boxes=200)
    items = []
    for (bx, by, bw, bh, area) in comps:
        f = float(bw * bh) / float(mask.size)
        if f > NOVEL_MAX_FRAC:
            continue
        ar = float(bw) / max(1.0, float(bh))
        if ar < 0.12 or ar > 8.0:
            continue
        fx, fy = bx / sx, by / sy
        fw, fh = bw / sx, bh / sy
        patch = cur[max(0, by):by + bh, max(0, bx):bx + bw]
        col = "?"
        if patch.size:
            # rgb_to_hsv 要 0..1 的浮点：直接喂 uint8 会因整数溢出算错色相（曾把黄色报成红色）
            m = feats.to_float(patch.reshape(-1, 3).mean(axis=0).reshape(1, 1, 3))
            h_, s_, v_ = (float(x) for x in feats.rgb_to_hsv(m)[0, 0])
            col = propose._color_name(h_, s_, v_)[0]
        items.append({
            "box": [int(round(fx)), int(round(fy)), max(4, int(round(fw))), max(4, int(round(fh)))],
            "frac": round(f, 4),
            "color": col,
            "at": [int(round(100 * (fx + fw / 2) / W)), int(round(100 * (fy + fh / 2) / H))],
        })
    items.sort(key=lambda it: -it["frac"])
    out = items[:NOVEL_MAX]
    note = None
    if len(items) > NOVEL_MAX:
        note = "另有 %d 处较小的变化没列出" % (len(items) - NOVEL_MAX)
    return {"novel": out, "novel_note": note}


def _values_of(pk, frame, results, opts):
    """读数字 + 算派生数值。

    两条互补的路子，都不需要模型：
      · `digits`：框住一串数字，编译期人报了一次 value 就学成字形图集，运行时只做像素比对；
      · `derived`：**数个数**。3 颗心都在场 → 血量 3。个数不需要识别任何东西，只要子实体
        的 presence 对 —— 而"心在不在"比"心长什么样"稳定得多，这是零成本换来的抗变化。
    读不出（图集缺字形）就给字符串 "0?"，绝不猜一个数字：假数字比没数字更糟。
    """
    ents = {e["id"]: e for e in pk.get("entities", [])}
    present = {r["id"]: r for r in results if r.get("present")}
    frames = [r["id"] for r in results if r.get("present")]
    values, detail = {}, {}

    for eid, e in ents.items():
        is_num = bool(e.get("digits"))
        d = e.get("digits") or e.get("text")
        if not d or eid not in present:
            continue
        box = feats.clamp_box(d.get("box") or [], frame.shape[1], frame.shape[0])
        raw, det = digits.read(feats.crop(frame, box), _atlas_of(pk))
        if not raw:
            continue
        num = int(raw) if (is_num and raw.isdigit()) else None
        values[eid] = num if num is not None else raw
        detail[eid] = {"raw": raw, "digits": det, "label": e.get("label") or eid}

    for dv in pk.get("derived") or []:
        kids = [str(x) for x in (dv.get("from") or [])]
        agg = dv.get("agg") or "count"
        if agg == "count":
            n = sum(1 for k in kids if k in present)
        elif agg == "sum":
            n = sum(values.get(k) for k in kids
                    if isinstance(values.get(k), int))
        else:
            continue
        values[dv.get("id") or "derived"] = n
        detail.setdefault(dv.get("id") or "derived", {})["label"] = dv.get("label") or agg
        detail[dv.get("id") or "derived"]["from"] = kids
        detail[dv.get("id") or "derived"]["agg"] = agg
    return (values, detail) if values else (None, None)


def observe(pk, frame, opts=None):
    opts = opts or {}
    t0 = time.perf_counter()
    H, W = frame.shape[:2]
    expected = pk.get("frame_size") or [W, H]
    note = None
    if abs(H - expected[1]) > max(4, expected[1] * 0.02) or abs(W - expected[0]) > max(4, expected[0] * 0.02):
        frame = feats.resize_area(frame, expected[0], expected[1]).astype(np.uint8)
        note = f"帧尺寸 {W}x{H} != 编译时 {expected[0]}x{expected[1]}，已按比例重采样"

    results = match_entities(pk, frame, hints=_BOXES.get(pk["id"]))
    _BOXES[pk["id"]] = {r["id"]: r["box"] for r in results if r.get("box")}
    presence = [r["present"] for r in results]

    # 全局签名与变化检测共用同一张小图：一次缩放喂两处
    small = feats.resize_area(frame, 160, 90)
    g = _global_desc(small)
    scenes = pk.get("scenes") or []
    radius = float((pk.get("stats") or {}).get("scene_radius", 0.22))
    scene, scene_conf, scene_dist = "unlabeled", 0.0, 9.9
    if scenes:
        best = None
        for s in scenes:
            pv = s.get("presence") or []
            diff = sum(1 for a, b in zip(presence, pv) if a != b) / float(max(len(pv), 1))
            hs = s.get("hist")
            hd = (float(np.abs(g["hist"] - np.asarray(hs, np.float32)).sum()) / 2.0) if hs else 1.0
            d = 0.65 * diff + 0.35 * min(1.0, hd)
            if best is None or d < best[0]:
                best = (d, s)
        scene_dist, sch = best
        scene = sch.get("name", "unknown")
        scene_conf = round(max(0.0, 1.0 - scene_dist / 0.6), 3)
        if scene_dist > radius * UNKNOWN_FACTOR:
            scene, scene_conf = "unknown", 0.0

    stale = bool(scenes) and scene_dist > radius

    # 第二遍（只在必要时）：场景已经确认、包没过期，但某个已知控件被判"不在"。
    # 这时候最可能的不是它消失了，而是被挡住了 —— 花 30~80ms 用分块投票覆盖搜索捞一遍。
    # 放在场景之后是有意的：场景没确认（换房间/别的窗口）时不该为它付这笔钱。
    if scenes and not stale and any(not r["present"] for r in results):
        hints2 = _BOXES.get(pk["id"]) or {}
        for i, r in enumerate(results):
            if r["present"]:
                continue
            ent = pk["entities"][i]
            tpl = ent.get("template") or {}
            if min(int(tpl.get("w") or 0), int(tpl.get("h") or 0)) < 24:
                continue                     # 小图标分块投票没有证据力，不做
            r2 = match_entity(ent, frame, hint=hints2.get(ent["id"]), occl=True)
            if r2["present"]:
                results[i] = r2
        presence = [r["present"] for r in results]

    changed = _changed_tiles(pk["id"], small)
    ms = (time.perf_counter() - t0) * 1000.0

    out = {
        "scene": scene,
        "scene_conf": scene_conf,
        "scene_dist": round(float(scene_dist), 3),
        "entities": presence,
        "conf": [r["conf"] for r in results],
        "changed": changed,
        "stale": stale,
        "ms": round(ms, 1),
    }
    labels = {e["id"]: (e.get("label") or e["id"]) for e in pk.get("entities", [])}
    if opts.get("values") or opts.get("labels"):
        values, vdetail = _values_of(pk, frame, results, opts)
        if values:
            out["values"] = values
            if opts.get("detail"):
                out["value_detail"] = vdetail
        # 派生数值（如「血量（在场红心数）」）也是要给人看的描述，一起进 labels
        for dv in pk.get("derived") or []:
            did = dv.get("id") or "derived"
            if dv.get("label"):
                labels[did] = dv["label"]
        if opts.get("labels") or opts.get("detail"):
            out["labels"] = labels
    if opts.get("detail"):
        out["detail"] = [{"id": r["id"], "label": labels.get(r["id"], r["id"]),
                          "present": r.get("present"), "conf": r.get("conf"),
                          "ncc": r.get("ncc"), "sub": r.get("sub"),
                          "cover": r.get("cover"), "occluded": r.get("occluded"),
                          "via": r.get("via"),
                          "box": r.get("box")} for r in results]
    occ = [r["id"] for r in results if r.get("occluded")]
    if occ:
        out["occluded"] = occ
    if opts.get("novel"):
        # 场景都没认出来时，参考帧根本不适用，别硬做差分
        if scene not in ("unknown", "unlabeled") and not stale:
            nv = _novel(pk, frame, results, scene, opts)
            if nv:
                out.update(nv)
        else:
            out["novel"] = []
            out["novel_note"] = "场景未确认（或包已过期），这次不做新东西检测"
    if opts.get("boxes"):
        out["boxes"] = {r["id"]: r.get("box") for r in results if r.get("box")}
    if note:
        out["scaled"] = note
    if stale:
        out["hint"] = "包可能过期：cv_learn action=add_scene 加一帧，或 action=build 重编"
    return out
