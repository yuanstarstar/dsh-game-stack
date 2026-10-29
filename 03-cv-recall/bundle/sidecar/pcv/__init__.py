"""纯 numpy 感知编译器（Percept Compiler）。

核心主张：把「看一眼画面并理解」从**每次推理**变成**一次性编译**。
  VLM 只做一件它不可替代的事：认出「画面上有哪些实体、各在哪」。
  剩下的（怎么从像素认出它们）由本地代码编译成 `.cvpack.json`，
  之后每帧只用 numpy 跑匹配，一次 5~20ms、零 token。

    from pcv import feats, propose, pack, compile, observe

    # 1) 学：一帧 + 少量框
    pk = compile.build(root, "stardew", "星露谷", frame, [
        {"id": "hp",  "label": "血条", "box": [24, 18, 160, 14]},
        {"id": "gold","label": "金币", "box": [1180, 20, 90, 28]},
    ], scene_name="farm")

    # 2) 用：每帧只要几毫秒
    out = observe.observe(pk, next_frame)
    # {'scene': 'farm', 'entities': [1, 1], 'stale': False, 'ms': 7.4}

三个让「只标一次」成立的技巧（详细论证见 compile.py 顶部注释）：
  ① 负样本免费：同帧不重叠处天然是负样本，不必第二轮采样
  ② 正样本估计靠抖动：框平移 ±2px 得到真实噪声下的分数下限
  ③ 新场景零成本：位置固定，加场景只需再截一帧，模型不必再看
"""

__version__ = "0.1.0"
__all__ = ["feats", "pack", "propose", "compile", "observe"]
