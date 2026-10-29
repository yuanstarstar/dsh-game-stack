"""端到端冒烟：起 sidecar → HTTP 走一遍 scan/build/recall/add_scene/list/get。

跟 selftest 的区别：selftest 直接调 pcv 里的函数，这里走**真正要上线的那条路**
（HTTP + JSON），验证 host.js 会看到什么。用合成图，不需要开游戏窗口。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UI = os.path.join(HERE, "ui")
ASSET = os.path.join(ROOT, ".tmp-smoke-root")
PY = sys.executable


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"{path} 失败 HTTP {exc.code}: {detail[:600]}") from None


def main() -> int:
    side = subprocess.Popen([PY, os.path.join(ROOT, "sidecar", "cvcd.py"), "--port", "0",
                             "--root", ASSET],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    line = side.stdout.readline().decode("utf-8", "replace").strip()
    info = json.loads(line)
    base = f"http://127.0.0.1:{info['port']}"
    print(f"sidecar 就绪 port={info['port']} screen={info.get('screen')}")

    def img(name):
        return os.path.join(UI, name)

    fails = []

    def check(name, cond, detail=""):
        print(("  OK  " if cond else "  FAIL") + f" {name}" + (f"  {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    # 1) scan：候选框
    sc = post(base, "/scan", {"path": img("battle_a.png"), "limit": 10})
    check("scan 有候选", len(sc["candidates"]) > 0, f'{len(sc["candidates"])} 个 / {sc["ms"]}ms')
    print("      候选：", ", ".join(f'#{c["n"]}{c["size"]}{c["color"]}' for c in sc["candidates"][:6]))

    # 2) build：用真值框（等价于模型挑对了候选）
    boxes = json.load(open(os.path.join(UI, "boxes.json"), encoding="utf-8"))
    battle_ids = set(boxes["scenes"]["battle"])
    picks = [dict(p) for p in boxes["picks"] if p["id"] in battle_ids]
    bd = post(base, "/build", {"path": img("battle_a.png"), "packId": "smoke", "game": "smoke",
                               "scene": "battle", "picks": picks})
    check("build 出实体", len(bd["entities"]) == len(picks), f'{bd["ms"]}ms')
    risk = {e["id"]: e["risk"] for e in bd["entities"] if e.get("risk")}
    if risk:
        print("      风险提示：", json.dumps(risk, ensure_ascii=False)[:200])

    # 3) recall：同帧必须全在
    rc = post(base, "/recall", {"path": img("battle_a.png"), "packId": "smoke", "boxes": True})
    hit = sum(1 for e in rc["entities"] if e["present"])
    check("recall 同帧全中", hit == len(picks), f'{hit}/{len(picks)}  scene={rc["scene"]} {rc["ms"]}ms')
    check("recall 报耗时", rc["ms"] > 0)
    check("recall 带框", bool(rc.get("boxes")))

    # 4) 变体帧：内容变了也要认得出
    rc2 = post(base, "/recall", {"path": img("battle_b.png"), "packId": "smoke"})
    hit2 = sum(1 for e in rc2["entities"] if e["present"])
    check("recall 变体帧", hit2 >= len(picks) - 1, f'{hit2}/{len(picks)}  changed={rc2["changed"]}')

    # 5) 无关画面：不许幻觉
    fr = post(base, "/recall", {"path": img("foreign.png"), "packId": "smoke"})
    fake = [e["id"] for e in fr["entities"] if e["present"]]
    check("无关画面不幻觉", not fake, f'scene={fr["scene"]} stale={fr["stale"]} 误报={fake}')

    # 6) 换主题：必须 stale
    th = post(base, "/recall", {"path": img("battle_theme.png"), "packId": "smoke"})
    check("换主题报 stale", bool(th["stale"]), f'scene={th["scene"]} dist={th["scene_dist"]}')

    # 7) 第二个场景：append 新实体 + present 校准（真机就是这么用的）
    ids_now = {e["id"] for e in bd["entities"]}
    new_ids = [i for i in boxes["scenes"]["dialog"] if i not in ids_now]
    picks2 = [dict(p) for p in boxes["picks"] if p["id"] in new_ids]
    ad = post(base, "/build", {"path": img("dialog_a.png"), "packId": "smoke", "scene": "dialog",
                               "picks": picks2, "append": True,
                               "present": boxes["scenes"]["dialog"]})
    check("append 加场景", len(ad["entities"]) > len(ids_now), f'{len(ids_now)} -> {len(ad["entities"])} 实体')

    # 7b) add_scene 只标场景（模型不用再看图），并用一帧校准阈值
    sc2 = post(base, "/scene", {"path": img("dialog_b.png"), "packId": "smoke", "scene": "dialog2",
                                "present": boxes["scenes"]["dialog"],
                                "absent": [i for i in ["choice_cursor"] if i in {e["id"] for e in ad["entities"]}]})
    on = [k for k, v in sc2["presence"].items() if v]
    check("add_scene 判在场", set(boxes["scenes"]["dialog"]) <= set(on), f'{on}')

    # 8) list / get
    ls = post(base, "/list", {})
    check("list 有包", any(p["packId"] == "smoke" for p in ls["packs"]))
    gt = post(base, "/get", {"packId": "smoke"})
    check("get 带门限", bool(gt.get("tau")), f'{len(gt["entities"])} 实体')

    side.terminate()
    try:
        side.wait(timeout=5)
    except subprocess.TimeoutExpired:
        side.kill()
    if fails:
        print("\nFAILED:", ", ".join(fails))
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
