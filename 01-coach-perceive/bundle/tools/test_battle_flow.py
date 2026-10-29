# -*- coding: utf-8 -*-
"""战斗四步原语的"接线"验收：不依赖真实画面，只验证
  - select_page 是否按「先点自己骰、再点手牌」的顺序、点到算出来的屏幕坐标；
  - target_enemy 是否点敌方骰；
  - execute 是否按 candidates 依次重试、用 changedTiles 判成功；
  - 手牌没开时是否给出可读错误而不是瞎点。

跑法（在 sidecar 目录下）：
  python tools\test_battle_flow.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "sidecar"))

import coachd as C  # noqa: E402

RESPONSES: list[int] = []
FAIL: list[str] = []
PASS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  " + extra) if extra and not cond else ""))


RECT = [31, 107, 1280, 720]


def fake_state(hand_visible=True, n_hand=3):
    hand = []
    for i in range(n_hand):
        box = [424 + i * 80, 475, 72, 160]
        hand.append({"i": i, "box": box, "screen": C._pt(RECT, [box[0] + 36, box[1] + 80]),
                     "name": None, "cost": None, "dice": [], "playable": None})
    self_dice = [{"i": 0, "box": [300, 330, 44, 44], "color": "gold",
                  "center_local": [322, 352], "screen": C._pt(RECT, [322, 352]),
                  "side": "self", "value": None, "pageAssigned": None}]
    enemy = [{"i": 0, "box": [540, 340, 32, 35], "color": "pink",
              "center_local": [556, 357], "screen": C._pt(RECT, [556, 357]),
              "side": "enemy", "value": None, "pageAssigned": None}]
    return {
        "scene": "battle" if (hand_visible or self_dice or enemy) else None,
        "sub": "battle_assign" if hand_visible else "battle_resolving",
        "handVisible": hand_visible,
        "hand": {"visible": hand_visible, "count": len(hand) if hand_visible else 0,
                 "cards": hand if hand_visible else [], "band": [475, 669],
                 "reason": None if hand_visible else "底部那一带没有成排的亮块"},
        "speedDice": (self_dice if hand_visible else []) + enemy,
        "executeButton": {"found": True, "screen": [672, 167], "how": "plate",
                          "candidates": [[672, 167], [671, 136], [632, 167]]},
    }


def install(states, clicks, responses=None):
    """把 do_state / _click_xy 换成记录器。states 可以是列表（依次返回）。"""
    global RESPONSES
    RESPONSES = list(responses or [])
    seq = list(states)

    def fake_do_state(body=None):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def fake_click(body, xy, why="", **kw):
        clicks.append({"xy": list(xy), "why": why})
        ch = RESPONSES.pop(0) if RESPONSES else 400
        return {"ok": True, "changedTiles": ch, "clicked_screen_xy": list(xy)}

    C.do_state = fake_do_state
    C._click_xy = fake_click


print("[1] select_page：先点自己的骰，再点手牌")
clicks: list = []
install([fake_state()], clicks)
out = C.do_select_page({"die": 0, "hand": 1})
check("返回 ok", out.get("ok") is True)
check("两步", len(clicks) == 2, str(clicks))
if len(clicks) == 2:
    st = fake_state()
    want_die = st["speedDice"][0]["screen"]
    want_card = st["hand"]["cards"][1]["screen"]
    check("第一步点自己的骰", clicks[0]["xy"] == want_die, f"{clicks[0]['xy']} != {want_die}")
    check("第二步点第 2 张手牌", clicks[1]["xy"] == want_card, f"{clicks[1]['xy']} != {want_card}")
    check("回执带 handCount", out.get("handCount") == 3, str(out.get("handCount")))

print("[2] select_page：手牌没开 —— 报错而不是瞎点")
clicks = []
install([fake_state(hand_visible=False)], clicks)
try:
    C.do_select_page({"die": 0, "hand": 0})
    check("抛错", False)
except ValueError as exc:
    check("抛错", True)
    check("错误里说明原因", "手牌" in str(exc), str(exc))
check("没有点任何东西", len(clicks) == 0, str(clicks))

print("[3] target_enemy：点敌方骰")
clicks = []
install([fake_state()], clicks)
out = C.do_target_enemy({"die": 0})
check("点了 1 次", len(clicks) == 1, str(clicks))
if clicks:
    check("点的是敌方骰", clicks[0]["xy"] == fake_state()["speedDice"][1]["screen"], str(clicks[0]["xy"]))
check("回执 ok", out.get("ok") is True, json.dumps(out, ensure_ascii=False)[:160])

print("[4] execute：第一个点没反应就换下一个，第 2 个成功")
clicks = []
install([fake_state()], clicks, responses=[5, 400])
out = C.do_execute({"expectMin": 250, "verifyMs": 10, "dryRun": False})
check("试了 2 个点", len(clicks) == 2, str([c["xy"] for c in clicks]))
check("成功在第 2 次", out.get("attempt") == 2, str(out.get("attempt")))
check("回执说生效了", "生效" in str(out.get("verdict")), str(out.get("verdict")))
check("两次点的坐标不同", len(clicks) == 2 and clicks[0]["xy"] != clicks[1]["xy"])

print("[5] execute：全都没变化 —— 给可读错误，不假装成功")
clicks = []
install([fake_state()], clicks)


def fake_click_flat(body, xy, why="", **kw):
    clicks.append({"xy": list(xy), "why": why})
    return {"ok": True, "changedTiles": 5}


C._click_xy = fake_click_flat
out = C.do_execute({"expectMin": 250, "verifyMs": 10})
check("ok=False", out.get("ok") is False, json.dumps(out, ensure_ascii=False)[:120])
check("错误里提示先看 state", "coach_state_json" in str(out.get("error")), str(out.get("error")))
check("把试过的点都列出来", len(out.get("tried") or []) == 3, str(out.get("tried")))

print()
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print("  ✗", f)
sys.exit(1 if FAIL else 0)
