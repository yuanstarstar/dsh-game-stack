"""感知包 (.cvpack.json) 的读写与结构定义。

一个包 = 一个界面（通常是一个游戏的一个界面族）的“编译产物”。
它必须能被人类读懂、被 git diff、被版本化管理 —— 所以特征全部存成普通 JSON 数值，
只有模板图用 base64 PNG。
"""
from __future__ import annotations

import base64
import io
import json
import os
import time

import numpy as np

SCHEMA = 1


# ---------------------------------------------------------------- 编码

def encode_png(rgb_u8):
    from PIL import Image
    im = Image.fromarray(np.asarray(rgb_u8, np.uint8))
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def decode_png(b64):
    from PIL import Image
    im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    return np.asarray(im, np.uint8)


def to_list(arr, nd=4):
    return [round(float(v), nd) for v in np.asarray(arr, np.float32).ravel()]


# ---------------------------------------------------------------- 读写

def packs_dir(root):
    d = os.path.join(root, "packs")
    os.makedirs(d, exist_ok=True)
    return d


def pack_path(root, pack_id):
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(pack_id))
    return os.path.join(packs_dir(root), safe + ".cvpack.json")


def _clean(obj):
    """去掉运行期缓存键（以 _ 开头）。运行时会把解码后的模板挂在 dict 上复用，
    那些是 numpy 数组、不该也不能进 JSON —— 存盘前统一剃掉。"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def save_pack(root, pack):
    pack["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p = pack_path(root, pack["id"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_clean(pack), f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)
    return p


def load_pack(root, pack_id):
    p = pack_path(root, pack_id)
    if not os.path.exists(p):
        raise ValueError(f"没有这个感知包：{pack_id}（先 cv_learn action=build 建一个）")
    with open(p, "r", encoding="utf-8") as f:
        pack = json.load(f)
    if pack.get("schema") != SCHEMA:
        raise ValueError(f"感知包 schema 不匹配：{pack.get('schema')} != {SCHEMA}，需要重新编译")
    return pack


def list_packs(root):
    d = packs_dir(root)
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".cvpack.json"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                p = json.load(f)
        except Exception:
            continue
        out.append({
            "id": p.get("id"),
            "game": p.get("game"),
            "entities": len(p.get("entities") or []),
            "scenes": [s.get("name") for s in (p.get("scenes") or [])],
            "frame": p.get("frame_size"),
            "updated_at": p.get("updated_at"),
        })
    return out


def delete_pack(root, pack_id):
    p = pack_path(root, pack_id)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


# ---------------------------------------------------------------- 空包

def new_pack(pack_id, game, frame_size, source_note=""):
    return {
        "schema": SCHEMA,
        "id": pack_id,
        "game": game or pack_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "frame_size": [int(frame_size[0]), int(frame_size[1])],
        "source_note": source_note,
        "entities": [],
        "scenes": [],
        "stats": {},
    }
