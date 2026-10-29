"""把「一次标注」编译成「可执行感知包」。

单次校准能成立的关键技巧 —— **负样本是免费的**：
  同一张截图里，凡是跟标注框不重叠的地方，天然就是负样本。
  所以不需要第二轮采样、不需要人工调阈值：每个特征的阈值都由
  「正样本估计」和「负样本分布」自动切开（最大间隔中点 + 逻辑斯蒂软映射）。

第二个关键技巧 —— **正样本估计靠抖动**：
  把框平移 1 个工作像素重新抽特征，得到真实截图噪声下的分数下限，
  避免用过拟合的「和模板完全一样 = 1.0」当正样本而把阈值定高。

第三个关键技巧 —— **新场景零成本**：
  实体框位置在游戏 UI 里固定，所以加新场景只要再截一帧，
  用已编好的匹配器**自动**判断各实体在不在，不需要模型再看一次图。

三个统计上的坑（都踩过，写在这里免得再踩）：
  · **负样本只能取自搜索窗内部**。运行时的「最大分」是在实体自己的搜索窗里取的，
    窗外的相似控件根本搜不到；拿整帧随机块当负样本，会把 NCC 的可分间隔打成 0
    （其它血条就是这种"假硬负样本"），于是权重归零、内容一变就漏报。
  · NCC 是「在整块搜索窗里取最大」，所以它的负样本要用 **max** 而不是 p95
    —— 一个 1060x230 的窗里有十几万个位置，随便哪个撞上高分都会误报。
  · 其它 3 个特征只在**命中点那一处**比较，负样本该用 p95 而不是 max。
  用错一边，不是疯狂误报就是疯狂漏报。

为什么灰度 NCC 和边缘 NCC 要**分开投票**（v2 最重要的改动）：
  血条填充一变，灰度 NCC 掉到 0.5，但它外壳的边框和刻度线（边缘）没变；
  对话面板反过来，里面整段字都换了，灰度整体明暗还在，边缘图却被文字搅乱。
  固定 0.62/0.38 混合这两路，就等于假设"所有实体都同时具备这两种稳定性"——
  一个总要吃亏。分开测、分开定阈值、按可分度分权重，两类实体都能活。
"""
from __future__ import annotations

import numpy as np

from . import digits
from . import feats
from . import pack as P

TEMPLATE_CAP = 256
MARGIN_RATIO = 0.10
MARGIN_MIN = 22
MARGIN_MAX = 90
N_NEG = 36
FEATS = ("ncc_g", "ncc_e", "ncc_t", "hsv", "hog", "dh")
MIN_GAP = 0.02
APPEARANCE_SCALE = 0.4
EDGE_BOOST = 1.6
NCC_SLACK = 0.72
TOL_LOWER = 0.90            # 新场景里"该在的"实测分 -> 门限最多放宽到它的 90%
TOL_UPPER = 1.25            # 新场景里"不该在的"实测分 -> 门限至少要高到它的 125%


def _template_gray(crop_rgb, cap=TEMPLATE_CAP):
    """灰度模板。**按最长边**限制到 cap，而不是按面积：

    320x18 的血条如果缩到 160x9，刻度线会被抹平，模板变成一条纯色带，
    NCC 就会对"任何一条横带"都给 0.9 分 —— 这正是误报的来源。
    """
    g = feats.to_gray(crop_rgb)
    h, w = g.shape
    m = max(h, w)
    if m > cap:
        s = cap / float(m)
        nw, nh = max(6, int(round(w * s))), max(6, int(round(h * s)))
        g = feats.resize_area(g, nw, nh)
    return ((np.clip(g, 0.0, 1.0) * 255.0)).astype(np.uint8)


def _search_box(box, frame_shape, override=None):
    """搜索窗 = 标注框 + 一点余量。

    **会移动的实体必须给 override**（如选项光标、跟随单位的血条）：
    默认余量只有 22~90px，跑出这个范围就找不到它。
    但窗越大越慢、越容易撞上"长得像的东西"，所以只开到必要的大小。
    """
    H, W = frame_shape[:2]
    if override:
        return feats.clamp_box(list(override)[:4], W, H)
    x, y, w, h = box
    pad = int(max(MARGIN_MIN, min(MARGIN_MAX, max(w, h) * MARGIN_RATIO)))
    return feats.clamp_box([x - pad, y - pad, w + 2 * pad, h + 2 * pad], W, H)


def _crop_at(frame, box, dx, dy, ws):
    """把框按 ws 工作像素平移后裁下来（dx,dy 用工作尺度计）。"""
    H, W = frame.shape[:2]
    b = feats.clamp_box([box[0] + dx * ws, box[1] + dy * ws, box[2], box[3]], W, H)
    if b[2] < 6 or b[3] < 6:
        return None
    c = feats.crop(frame, b)
    if c is None or c.size == 0:
        return None
    # 贴到画面边界时 clamp_box 会把框裁小 —— 那种残块跟完整模板不是一回事，
    # 拿去当负样本会污染阈值（而且形状对不上），直接放弃这一个格点。
    if abs(c.shape[0] - box[3]) > 1 or abs(c.shape[1] - box[2]) > 1:
        return None
    return c


def _fit(pos_est, neg_vals, floor=0.0, ceil=0.999, agg="p95", slack=1.0):
    """在「正样本估计」和「负样本最坏情况」之间切开。

    返回 (tau, gap)。gap <= MIN_GAP 表示这个特征分不开正负样本 —— 不可判别。
    agg='max' 用于「在整块窗里取最大」的 NCC；'p95' 用于单点比较的其它特征。

    slack<1 是**内容容差**：正样本估计是「校准那一帧上真位置的分」，可它天然接近 1.0
    （模板就是从这儿抠的），而真实运行时这块区域的内容会变 —— 血条填充从 42% 到 71%，
    NCC 就从 1.0 掉到 0.65。把阈值定在 0.92 等于宣布「内容变一点就当它不存在」，
    这是系统性漏报的根源。所以往下放开 28%（tau = min(中点, pos*slack)），
    用一点误报风险换召回；「这东西在不在」最终还要过形变/比例闸门。
    """
    neg = np.asarray([] if neg_vals is None else
                     [v for v in np.asarray(neg_vals, np.float64).ravel() if v is not None],
                     np.float64)
    pos_est = float(pos_est)
    if neg.size == 0:
        # 连一个负样本都没采到 = 这个特征没有任何"别处"可比，不能给它权重
        return (float(np.clip(pos_est * slack, floor, ceil)), 0.0)
    if agg == "max":
        hi = float(neg.max()) + 0.35 * float(neg.std())
    else:
        hi = float(np.quantile(neg, 0.95))
    gap = pos_est - hi
    if gap <= MIN_GAP:
        return (float(np.clip(pos_est * slack, floor, ceil)), float(gap))
    tau = float(np.clip(min(hi + 0.5 * gap, pos_est * slack), floor, ceil))
    return (tau, float(gap))


def compile_entity(frame, pick, all_boxes, rng):
    ent, glyphs = _compile_entity(frame, pick, all_boxes, rng)
    return ent, glyphs


def _digits_of(frame, pick, box, W, H):
    """数字/文字字段：`pick["digits"]` 或 `pick["text"]` = `{"box":[x,y,w,h], "value":"00"}`。

    两者形状一样，区别只在意图：`digits` 的输出会被当整数（能参与 derived 求和），
    `text` 的输出原样当字符串（商店价签、名字、提示语）。

    value 是**这一帧上真实显示的数字**（人/VLM 看得到，模型顺手抄一遍，零额外成本）。
    它有两个用途：① 编译期切字形存成图集，运行时只做像素比对；② 自检（学完立刻读一遍，
    读不回自己的 value 说明框或字形切分有问题，直接编译失败比运行时瞎读好）。
    """
    from . import digits as D

    key = "digits" if pick.get("digits") else ("text" if pick.get("text") else None)
    if not key:
        return None, None, None
    d = pick.get(key)
    dbox = feats.clamp_box(d.get("box") or [], W, H)
    if dbox[2] < 10 or dbox[3] < 10:
        raise ValueError(f"实体 {pick.get('id')} 的 digits.box 太小（{dbox[2]}x{dbox[3]}）")
    crop = feats.crop(frame, dbox)
    bits = D.learn(crop, str(d.get("value") or ""))
    got = D.read(crop, bits)[0]
    want = str(d.get("value"))
    if got != want:
        raise ValueError(
            f"实体 {pick.get('id')} 的数字字段自检失败：value={want!r}，学完读回是 {got!r}。"
            f"多半是 {key}.box 框歪了（切进别的字或切掉了一位）")
    return {"box": dbox}, bits, key


def _compile_entity(frame, pick, all_boxes, rng):
    H, W = frame.shape[:2]
    box = feats.clamp_box(pick["box"], W, H)
    if box[2] < 8 or box[3] < 8:
        raise ValueError(f"实体 {pick.get('id')} 的框太小（{box[2]}x{box[3]}），至少要 8x8")
    eid = pick.get("id") or f"e{len(all_boxes)}"
    crop_rgb = feats.crop(frame, box)
    ref = feats.extract(crop_rgb)
    tg = _template_gray(crop_rgb)

    # ---- 工作尺度 ws（这个项目最贵的一个教训，别改回去 ----
    # 模板被缩过（dialog_panel 1100x262 -> 256x61）后，如果运行时又把模板**放大**回原尺寸
    # 去匹配，高频细节已经被面积均值抹平了：真实位置只剩 0.6~0.75 分，而编译时拿缩小
    # 模板在原分辨率图上量出的正样本是 0.95 —— 阈值按 0.95 定，运行时系统性漏报。
    # 正确做法：模板永远保持原生尺寸，把**搜索图缩小** 1/ws 去匹配，坐标再乘 ws 映射回去。
    # 好处是双向的：分数一致了，大窗搜索也快了 ws² 倍。
    ws = float(box[2]) / float(tg.shape[1])
    tgf = tg.astype(np.float32) / 255.0
    tsz = (tg.shape[1], tg.shape[0])

    search = _search_box(box, frame.shape, pick.get("search"))
    sg = feats.to_gray(feats.crop(frame, search))
    if sg.shape[0] < 8 or sg.shape[1] < 8:
        raise ValueError(f"实体 {eid} 的搜索窗太小（{search[2]}x{search[3]}），至少 8x8")
    gw = max(tsz[0], int(round(search[2] / ws)))
    gh = max(tsz[1], int(round(search[3] / ws)))
    if (sg.shape[1], sg.shape[0]) != (gw, gh):
        sg = feats.resize_area(sg, gw, gh)
    se = feats.edge_of(sg)
    mg, me = feats.channel_maps(sg, tgf, se)

    # 真位置在「工作尺度搜索图」里的格点（resize 有取整，允许 ±1 的差）
    px = int(round((box[0] - search[0]) / ws))
    py = int(round((box[1] - search[1]) / ws))

    # 负样本必须排除「同一块东西的邻位」：一条 320x18 的血条往右挪 3px，NCC 还是 0.99，
    # 但那是**同一个控件**，不是负样本。按模板尺寸的 1/4 开一个接受邻域，
    # 否则重复性/对称性的控件（血条、面板、进度条）的负样本分布会被自己污染，
    # 可分间隔被打成 0、权重归零，然后一换内容就漏报（v2 之前的真因）。
    ex = max(2, int(round(tsz[0] * 0.25)))
    ey = max(2, int(round(tsz[1] * 0.25)))

    def _posneg(m):
        """在同一张响应图上取「真位置邻域的最大分」和「真正别处位置的分」。"""
        if m is None or m.size == 0:
            return None, None
        yy = min(max(0, py), m.shape[0] - 1)
        xx = min(max(0, px), m.shape[1] - 1)
        win = m[max(0, yy - 1):yy + 2, max(0, xx - 1):xx + 2]
        pos = float(win.max()) if win.size else float(m[yy, xx])
        # 极长条（血条）的 1/4 宽度可能比响应图还宽 —— 留出至少 40% 的格子当负样本
        exx = min(ex, max(2, int(m.shape[1] * 0.4)))
        eyy = min(ey, max(2, int(m.shape[0] * 0.4)))
        keep = np.array(m, copy=True)
        keep[max(0, yy - eyy):yy + eyy + 1, max(0, xx - exx):xx + exx + 1] = -2.0
        rest = keep[keep > -1.5]
        return pos, (rest if rest.size else None)

    pos_g, neg_g = _posneg(mg)
    pos_e, neg_e = _posneg(me)

    tau, diag = {}, {}
    pos_g = 1.0 if pos_g is None else pos_g
    pos_e = 1.0 if pos_e is None else pos_e
    tau["ncc_g"], g = _fit(pos_g, neg_g, floor=-1.0, ceil=0.999, agg="max", slack=NCC_SLACK)
    diag["ncc_g"] = {"pos": round(pos_g, 3), "gap": round(g, 3)}
    tau["ncc_e"], g = _fit(pos_e, neg_e, floor=-1.0, ceil=0.999, agg="max",
                           slack=NCC_SLACK)
    diag["ncc_e"] = {"pos": round(pos_e, 3), "gap": round(g, 3)}

    # --- 其它特征的负样本：定位器**可能落到的**其它格点上的裁图（同样只在窗内）
    offs = []
    if mg is not None and mg.size:
        hh, ww = mg.shape
        for yy in range(hh):
            for xx in range(ww):
                if abs(yy - py) <= ey and abs(xx - px) <= ex:
                    continue        # 同一控件的邻位不算负样本（见 _posneg 的注释）
                offs.append((xx, yy))
    if len(offs) > N_NEG:
        idx = rng.choice(len(offs), N_NEG, replace=False)
        offs = [offs[int(i)] for i in idx]
    neg_crops = [c for c in (_crop_at(frame, box, x_, y_, ws) for (x_, y_) in offs)
                 if c is not None]
    pos_crops = [c for c in (_crop_at(frame, box, dx, dy, ws)
                             for dx in (-1, 0, 1) for dy in (-1, 0, 1)) if c is not None]
    pos_crops = pos_crops or [crop_rgb]

    for key in ("hsv", "hog", "dh"):
        pos_v = float(np.median([feats.compare(pc, ref)[key] for pc in pos_crops]))
        neg_vs = [feats.compare(nc, ref)[key] for nc in neg_crops] or [0.0]
        tau[key], g = _fit(pos_v, neg_vs, agg="p95")
        diag[key] = {"pos": round(pos_v, 3), "gap": round(g, 3)}

    # 分块中位数（结构多数票）：正样本是"真位置那块裁图对模板"，恒为 1.0，
    # 所以门限只能靠负样本来撑 —— 和两路 NCC 一样用 slack 往回收一点。
    pos_t = feats.tile_score(crop_rgb, tg)
    # 平坦裁图给 -1.0 = "判不了"，不是"安全"，不能当负样本进去把分布拖到谷底
    neg_t = [v for v in (feats.tile_score(nc, tg) for nc in neg_crops) if v is not None and v > -1.5]
    tau["ncc_t"], g = _fit(max(pos_t, -1.0), neg_t, floor=-1.0, ceil=0.999,
                           agg="max", slack=NCC_SLACK)
    diag["ncc_t"] = {"pos": round(float(pos_t), 3), "gap": round(g, 3)}

    # --- 权重：分得越开的特征越重要；分不开的直接 0 权重（不参与置信度）
    # 外观类（hsv/hog/dh）整体打五折：它们衡量的是"这块区域现在长什么样"，
    # 而游戏 UI 的内容本来就在变（血条填充、地图光点、面板文字）。
    # 形状/结构（两路 NCC）才是"这个东西在不在这里"的证据，不该被外观分挤掉。
    raw = {}
    for k in FEATS:
        v = (float(np.clip(diag[k]["gap"] / 0.25, 0.15, 1.0)) if diag[k]["gap"] > MIN_GAP else 0.0)
        raw[k] = v * (1.0 if k.startswith("ncc") else APPEARANCE_SCALE)
    # 边缘通道加权：实测血条填充从 42% 变到 71% 时，灰度 NCC 0.94→0.65（-0.29），
    # 边缘 NCC 0.94→0.84（-0.10）。边缘图只认"这里有没有边界"，而边框、刻度线、
    # 按钮轮廓这些正是 UI 里最稳的东西 —— 凡是内部会变的控件，都只能靠它活。
    raw["ncc_e"] = raw.get("ncc_e", 0.0) * EDGE_BOOST
    if sum(raw.values()) <= 0:
        raise ValueError(
            f"实体 {eid} 这块区域没有任何可区分特征（和周围长得太像，或框在纯背景上）。"
            f"请把框收紧到控件本身，或换一块更有辨识度的区域。")
    s = sum(raw.values())
    weights = {k: round(v / s, 4) for k, v in raw.items()}

    ent = {
        "id": eid,
        "label": pick.get("label") or eid,
        "kind": pick.get("kind") or "auto",
        "box": box,
        "search": search,
        "ar": ref["ar"],
        "template": {"png": P.encode_png(np.stack([tg] * 3, axis=-1)),
                     "w": int(tg.shape[1]), "h": int(tg.shape[0])},
        "feat": {
            "hsv": P.to_list(ref["hsv"]),
            "hog": P.to_list(ref["hog"]),
            "dh": int(ref["dh"]),
            "ar": ref["ar"],
            "edge": ref["edge"],
        },
        "tau": {k: round(float(v), 5) for k, v in tau.items()},
        "w": weights,
        "diag": diag,
        "ws": round(float(ws), 5),
        "scales": [1.0],
        # 校准帧上这块模板自己的分块覆盖率：遮挡判定要跟**它自己的正常水平**比，
        # 而不是跟 1.0 比 —— 天然带大片纯色的模板（小地图黑底）永远到不了 1.0。
        "cov0": (round(float(feats.tile_stats(crop_rgb, tg)["cover"]), 3)
                 if min(tg.shape) >= 24 else -1.0),
    }
    ent["risk"] = _risk_of(box, ent, ws)
    meta, glyphs, key = _digits_of(frame, pick, box, W, H)
    if glyphs is not None:
        ent[key] = meta
    return ent, glyphs


def _risk_of(box, ent, ws) -> str:
    """编译期诚实提示：哪些实体「天生不稳」，别等运行时漏报了才去查。

    这不是错误，只是一句提醒 —— 写在包里、`cv_learn get` 能读、`cv_learn build`
    会在结果里重复一遍，让人知道这个实体该怎么补。
    """
    w = ent["w"]
    structural = sum(v for k, v in w.items() if k.startswith("ncc"))
    if ws > 2.0:
        return (f"像是容器类控件（{box[2]}x{box[3]}，模板缩了 {ws:.1f} 倍）：内部内容一换就"
                f"可能漏报。建议改标它的静态子部件（标题栏/边框/按钮），"
                f"或在内容不同的第二个画面上再 add_scene 一次。")
    if structural < 0.55:
        return ("结构特征偏弱（主要靠颜色/纹理认）：换主题、换配色或加了半透明遮罩容易漏。"
                "可以多标一帧，或把框收紧到有硬边界的部分。")
    return ""


def build(root, pack_id, game, frame, picks, scene_name="default", note="", append=False,
          present=None, derived=None):
    """一次标注 → 一个包（或往已有的包里追加实体）。frame: HxWx3 uint8。

    picks: [{id,label,box[,search][,digits][,kind]}]。append=True 时往已有包合并新实体。
    present: 可选，这一帧里"确实在场"的**已有实体** id（不用重标，只当正样本学阈值）。
    derived: 可选，派生数值：`[{"id":"hp","label":"红心数","from":["heart_1","heart_2",
             "heart_3"],"agg":"count"}]`。为什么要有它：一整条血条标成一个实体，运行时
             只能说"在/不在"；拆成 3 颗心之后，**个数本身就是数值** —— 不需要任何额外
             识别，把子实体的 presence 数一数就有了。零成本，而且天然抗内容变化（心在
             不在，比心长什么样稳定得多）。

    为什么要 append：一个屏幕上不可能同时出现所有控件（战斗 HUD 和对话面板就是互斥的），
    所以"一次标完所有实体"这个前提是错的 —— 那样会从空白背景抠出模板，
    运行时对着背景打 0.99 分（这是我第一版误报的直接原因）。
    正确的工作流是：战斗场景标战斗 HUD → 对话场景再标对话面板 → 每次都 append 进同一个包。
    每个场景只花一次标注，包却是完整的。
    """
    if not picks:
        raise ValueError("至少要标 1 个实体：picks=[{id:'hp', box:[x,y,w,h]}]")
    H, W = frame.shape[:2]
    picks = [dict(p) for p in picks]
    for i, p in enumerate(picks):
        if not p.get("id"):
            p["id"] = f"e{i + 1}"
    boxes = [feats.clamp_box(p["box"], W, H) for p in picks]
    rng = np.random.default_rng(20261028)

    if append:
        pk = P.load_pack(root, pack_id)
        fs = tuple(pk.get("frame_size") or (W, H))
        if fs != (W, H):
            raise ValueError(
                f"包 {pack_id} 是在 {fs[0]}x{fs[1]} 上标的，这一帧是 {W}x{H}。"
                f"分辨率/缩放变了旧模板全失效，请用新 id 重建包。")
        have = [e.get("id") for e in pk.setdefault("entities", [])]
        dup = [p["id"] for p in picks if p["id"] in have]
        if dup:
            raise ValueError(f"实体 id 已存在：{dup}。append 只加新实体，删掉重建才能改已有实体")
        if game:
            pk["game"] = game
        if note:
            old = pk.get("note") or ""
            pk["note"] = (old + " | " if old else "") + note
    else:
        pk = P.new_pack(pack_id, game, (W, H), note)
        have = []

    # 追加时，"其它实体框"要含老实体（新实体和老实体也可能长得像，如两条血条）
    all_boxes = list(boxes) + [[int(v) for v in (e.get("box") or [])[:4]]
                               for e in pk.get("entities", []) if len(e.get("box") or []) == 4]
    atlas = None
    for i, pick in enumerate(picks):
        ent, glyphs = compile_entity(frame, {**pick, "box": boxes[i]}, all_boxes, rng)
        pk["entities"].append(ent)
        if glyphs:
            atlas = digits.merge(atlas, glyphs)
    if atlas and (atlas.get("classes") or {}):
        pk["glyphs"] = digits.encode(digits.merge(digits.decode(pk.get("glyphs")), atlas))
    if derived:
        pk["derived"] = [dict(d) for d in derived]

    # 老场景的存在向量是按老实体数记的，追加后要补齐（新实体在那些场景里视为不存在，
    # 这正是"另一个屏幕上的控件"的真实语义）。要覆盖就再对那个场景调一次 add_scene。
    if append and have:
        pad = len(picks)
        for s in pk.get("scenes", []) or []:
            if len(s.get("presence") or []) == len(have):
                s["presence"] = list(s["presence"]) + [0] * pad
                s["conf"] = list(s.get("conf") or []) + [0.0] * pad

    # 这一帧里被标出来的实体无疑是"在场"的 → 顺手当正样本把它们的门限校到真实分布上。
    # 没标的老实体不动（它们在别的场景里可能在场，只是这一帧没标，不能当负样本用）。
    # present 允许额外声明（"这一帧里这些老实体也在场，我看见了但不用重标"）—— 同一个
    # 控件在三个场景里三种内容时，这就是白拿的三次正样本，tau 收敛到真实方差上。
    seen = {str(p["id"]) for p in picks} | ({str(x) for x in present} if present else set())
    add_scene(pk, frame, scene_name, force=True, present=sorted(seen))
    P.save_pack(root, pk)
    return pk


def add_scene(pk, frame, scene_name, force=False, radius=0.22, present=None, absent=None):
    """加一个场景，并顺手把这一次的**正负样本学进去**（这是"内容会变"唯一的解）。

    为什么要学：一次校准只能看到"这个控件此刻的样子"。但同一个控件在不同场景里内容会变
    —— 对话框面板在一段话和一组选项之间切换，灰度 NCC 从 0.91 掉到 0.38；小地图的光点
    换一片，也掉到 0.38。阈值按校准那一帧定死，这些全变漏报。
    而新场景本来就带标签：`present=["portrait","hp_frame",...]` 告诉工具"这些应该在场"，
    于是同一批画面既是**正样本**（该在的分数就是合法下限）又是**负样本**（不该在的分数
    是必须压住的上限）。一次调用、零额外成本，把 tau 调到真实分布上。

    规则（只往一个方向动，防止来回抖）：
      · present 里：tau = min(tau, 实测 * TOL_LOWER) —— 只**放宽**，不收严
      · absent  里：tau = max(tau, 实测 * TOL_UPPER) —— 只**收严**，不放宽
    两个都不给 = 只记场景样本、不学阈值（旧行为）；没被提到的实体不会被动。
    """
    from .observe import match_entity
    results = []
    want = None if present is None else {str(x) for x in present}
    nope = None if absent is None else {str(x) for x in absent}
    for e in pk.get("entities", []):
        r = match_entity(e, frame, debug=True)
        results.append(r)
        if want is None and nope is None:
            continue
        scores = {"ncc_g": r.get("ncc_g"), "ncc_e": r.get("ncc_e")}
        scores.update({k: v for k, v in (r.get("cmp") or {}).items()
                       if k in ("hsv", "hog", "dh")})
        diag = e.setdefault("diag", {})
        if want is not None and e["id"] in want:
            for k, v in scores.items():
                if k not in e.get("tau") or v is None:
                    continue
                new = float(v) * TOL_LOWER
                if new < e["tau"][k]:
                    diag.setdefault(k, {})["lo"] = round(new, 3)
                    e["tau"][k] = round(new, 5)
        elif nope is not None and e["id"] in nope:
            for k, v in scores.items():
                if k not in e.get("tau") or v is None:
                    continue
                new = float(v) * TOL_UPPER
                if new > e["tau"][k]:
                    diag.setdefault(k, {})["hi"] = round(new, 3)
                    e["tau"][k] = round(min(new, 0.995), 5)

    sample = {
        "name": scene_name,
        "presence": [1 if r["present"] else 0 for r in results],
        "conf": [round(r["conf"], 3) for r in results],
    }
    g = feats.resize_area(frame, 64, 36).astype(np.uint8)
    sample["hist"] = P.to_list(feats.hsv_hist(g, 8, 4, 4))
    sample["dh"] = int(feats.dhash(feats.to_gray(g), 8))
    # 参考帧（缩小版）：运行时拿它做差分，就能说出"这些位置有我没编过的东西"。
    # 320 宽足够让一个 60px 的物件留下 7px 的痕迹，同时只有几十 KB。
    H, W = frame.shape[:2]
    rw = 320
    rh = max(24, int(round(320 * H / float(W))))
    sample["ref"] = P.encode_png(feats.resize_area(frame, rw, rh).astype(np.uint8))
    scenes = pk.setdefault("scenes", [])
    for i, s in enumerate(scenes):
        if s.get("name") == scene_name:
            if not force:
                raise ValueError(f"场景 {scene_name} 已存在；要覆盖请加 force=true")
            scenes[i] = sample
            break
    else:
        scenes.append(sample)
    pk.setdefault("stats", {})["scene_radius"] = radius
    return sample
