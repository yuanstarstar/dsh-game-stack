# 游戏感知 + 轻操作底座（dsh-coach-perceive v3）

给「游戏教练」装的眼睛和手：一次锚定，之后每一步只花几十毫秒、几百 token 就知道盘面变了什么；
需要推进对话、点菜单时，**一次工具调用顶十几次按键**。

配合 [`dsh-game-coach-v6`](../dsh-game-coach-v6) 的策略库/实战记录一起用；本包只管**看和动**，不碰存档、不改数值。

## 它解决什么

模型自己「截屏 + 看图」每步要 1~2 秒、1~2k 视觉 token，还要多轮工具往返。本包把这一步换成本地进程：

| 手段 | 做什么 | 实测耗时 |
| --- | --- | --- |
| 锚定一次 | OBS 采集源或窗口标题，自动跟随窗口移动，落盘记忆 | 一次性 |
| `coach_diff` | 32px 网格差分，只回变化矩形块，**零视觉 token** | ~40 ms |
| `coach_now` | **一次调用**：场景 + 变化格数/区域 + 下半屏文字行 | 110~200 ms |
| `coach_snapshot{ocr:true}` | 整帧 + 本地 OCR（默认放大 2 倍，小字认得更全） | 帧 ~90 ms + OCR 60~150 ms |
| `coach_drive` | 连按同一个键推进，**遇选项/战斗自动停** | 每步 ~0.5 s，一次调用走十几步 |
| `coach_click_text` | 按 OCR 文字点按钮/选项，坐标自动算 | 帧 + OCR + 点击 ≈ 0.3~1.2 s |
| `coach_frame{attach:true}` | 只有 OCR 看不懂（地图/图标/单位位置）才把图交给模型 | ~85 ms + 视觉 token |
| `coach_facts` | **画面 → 规则引擎能吃的 facts JSON**，落盘 `%USERPROFILE%\.dsh\coach-perceive\last-facts.json` | ~400 ms，零视觉 token |

抓帧走 **PrintWindow**（不是截屏），所以游戏被 DSH 盖住也照抓；OCR 走常驻 PowerShell 进程，冷启一次后每次 5~8 ms。

## 工具

| 工具 | 干什么 | 什么时候用 |
|---|---|---|
| `coach_windows` | 列可见窗口（标题/矩形/hwnd/是否前台） | 第一次接一个游戏 |
| `coach_anchor` | 锚定窗口 / OBS 采集源 / 矩形 | 接游戏、或换了分辨率 |
| `coach_now` | 一次给「场景 + 变化格 + 下半屏文字」 | **每步都先调这个**，最省 |
| `coach_snapshot` | 截一块，可带本地 OCR | 要坐标或要读字 |
| `coach_diff` | 只回变化格 / 等画面静止 | 判断"这一下动了没" |
| `coach_frame` | 存图，`attach` 才真看图 | OCR 读不出时 |
| `coach_drive` | 连按同一个键推进对话，遇选项/战斗自停 | 过剧情最省调用 |
| `coach_key` | 单键（`dryRun` 演练） | 精确一次按键 |
| `coach_click` | 按 `element`/`x,y`/`text` 点 | 点按钮 |
| `coach_click_text` | 按 OCR 文字点 | 文字读得出时最省 |
| `coach_ui_dump` | **把画面拆成可点元素表**（id/type/label/box/enabled） | 不知道点哪儿时 |
| `coach_wait_stable` | 等画面静止（`settleMs/timeoutMs/minTiles`） | 等动画/等过场 |
| `coach_guard` | 问"这个键现在能不能按" | 动手前先问一句 |
| `coach_click_until` | 点直到画面变（`maxTries/expectChange`） | 淡入动画期点不动 |
| `coach_dismiss` | 关浮层：右上× → Esc → 点空白 | 教学窗挡路 |
| `coach_key_probe` | 逐个试键，回报哪些真的有用 | 游戏键位说明不准 |
| `coach_click_log` | 读点击/按键审计日志 | 复盘"我到底点了啥" |
| `coach_scene_define` / `coach_scene_list` | 注册/查看场景模板 | 关键词认不出的界面 |
| `coach_state_json` | **战斗盘面状态源**：场景/子状态、速度骰、手牌、下令按钮、静止与否 | 战斗每幕开头先调一次 |
| `coach_select_page` | 点自己的骰 → 点手牌 | 出牌第 1-2 步 |
| `coach_target_enemy` | 点敌方的骰指敌 | 出牌第 3 步 |
| `coach_cancel` | 右键取消当前选择 | 点错了/要改主意 |
| `coach_execute` | 点「下令战斗」，用大面积变化判成功 | 出牌最后一步 |

## 能不能替玩家操作

能，`inputEnabled` 默认 **true**（v3 改的；v2 是默认关）。四道闸：

1. **只打锚定的那个窗口**：目标不在前台就自己切（SetForegroundWindow → AttachThreadInput → SwitchToThisWindow 三级回退），切不过去才报错；干完把前台还给原来的窗口。
   - 后台 `mode:"post"`（PostMessage）可试，但 Unity 类引擎通常不收，别指望。
2. **白名单按键**：空格/回车/Esc/方向/数字/26 字母；组合键、滚轮、右键、拖拽不开。
3. **全量审计**：每次输入写一行 `%USERPROFILE%\.dsh\coach-perceive\input-log.jsonl`（时间、hwnd、当时前台、动作、返回值）。
4. **停在选择面前**：`coach_drive` 每步先 OCR 判场景，`choice`/`battle` 一律停手，交回模型/玩家决定。

## 遮挡与坐标（v2 起）

- 锚定到窗口后抓帧走 **PrintWindow(PW_RENDERFULLCONTENT)**：别的窗口（包括 DSH 自己）盖在游戏上也拿到游戏画面；抓不到才退回屏幕裁剪。
- OCR 坐标、变化块坐标**一律是屏幕绝对坐标**，可以直接拿去点。`snapshot` 带 `source`（`printwindow`/`screen`）和 `coveredBy`（此刻压着谁）。
- **读画面不受遮挡影响，但鼠标点击会落到最上层窗口**：要动游戏时，插件会先把锚定窗口切到前台再点。
- 锚定落盘 `%USERPROFILE%\.dsh\coach-perceive\anchor.json`：sidecar 重开自动恢复（hwnd 变了按标题/进程名重找）。

已实测（2026-10-27，废墟图书馆 1280x720，DSH 盖住右半屏）：

```text
coach_now    场景=dialog 来源=printwindow 变化格=830 区域=4 条
             [285,743] 喂！别磨叽了，快割！……
coach_drive  按了 40 次，停在 max_steps(dialog)
coach_drive  按了 0 次，停在 scene:choice        ← 识别到选项，一步没按
coach_click_text  按文字点「罗兰」→ 命中 (1175,358)
OCR 2 倍放大   1x 漏掉「罗兰」，2x 认出来了（右栏 7 行 → 9 行）
```

## 安装

```powershell
plugin_manager { action: "install_bundle", target: "D:\插件\game-stack\plugins\dsh-coach-perceive-v3" }
```

已安装一次即可；模型侧会多出 `coach_*` 十个工具。

> 为什么目录/包名带 `-v3`：Cordis 按模块 URL 缓存 ESM，改了 `host.js` 必须换目录换包名重装才会执行新代码（`sidecar/*.py` 不受影响，杀进程即生效）。下次再改 host 就是 `-v4`。

依赖（本机已验证）：

- **Python 3.12 + Pillow + numpy**：优先 DSH 自带运行时 `…\dependencies\python\python.exe`，也可配置 `python` 或 `DSH_COACH_PYTHON`。
- **OCR**：Windows 自带 WinRT OCR，需要中文语言包（`zh-Hans-CN`）。
- **OBS（可选）**：28+ 自带 WebSocket，用来「自动找到游戏窗口 + 免截图锚定」。

## 开启 OBS 锚定（最省事的一路）

1. OBS `工具 → WebSocket 服务器设置`：勾启用，记端口（默认 4455）和密码。
2. OBS 里给游戏加一个**窗口采集**源（一次就够）。
3. 告诉模型密码，或写进配置 `obsPort` / `obsPassword`。

   ```text
   coach_anchor{action:"obs", password:"****"}                       # 用当前采集窗口
   coach_anchor{action:"obs", password:"****", window:"文明6"}        # 顺便把采集源换成游戏
   ```

换窗口是通过 WebSocket 改采集源的 `window` 设置（等于你在属性面板里手选），之后录屏也对着游戏。

## 安全：危险按键默认被拦（v3 起）

战斗界面按空格 = 直接结算，一张牌没出就把整场接待空转掉（这是实机踩过的坑）。所以：

- `space` / `enter` 在 `battle` / `battle_setup` / `result` 场景**默认拒绝**，回执里写清原因，**画面一个键都没收到**；
- 真想按，显式给 `force: true`（回执会标 `forced:true`）；
- `coach_drive` 会**先认场景再按**，`requireScene` 不满足就不动，每步都做变化检测，场景一变成守护场景立刻停并说明第几步；
- `coach_key` / `coach_drive` 支持 `dryRun`：只回报"本来会按什么"，不落键。

## 场景是靠结构认的，不是靠字（v3 起）

废墟图书馆的按钮和数字是描边花字，Windows OCR 基本读不出（实机只读出两个人名）。所以场景判定分两路：

1. **视觉结构**：底部横带按 8px 列统计亮行，能找出"几张等宽并排的手牌"→ 判定 `battle`，置信 0.78，证据写进回执（`底部手牌 4 张（424+64, …）`）；
2. **关键词**：OCR 读到的字对照场景词表；两路冲突时，关键词置信 < 0.7 或 unknown 就让位给视觉。

`coach_now` 现在回 `scene/sceneZh/confidence/evidence/sceneSource`，`textReadable:false` 会明说"这套字体 OCR 读不出字"。

## 元素表：`coach_ui_dump` 怎么找按钮（v3 起）

描述不出名字的按钮，靠三路证据拼：

- **描边矩形**：先找长横线，再找左右两条竖边，拼出框（废墟图书馆那种"深底 + 亮边 + 花纹字"的按钮，按钮面的亮度和背景几乎一样，纯按亮度找会整片漏掉）；
- **亮块**：浅色填充的标签页/槽位；
- **色块**：tile 颜色量化后的连通域（卡牌/面板）。

`enabled` 用"比同类暗一档"判断（同一来源内取 80 分位当基准，低于 0.78/0.72 判置灰），只对 `button`/`close` 有意义。点的时候用 `coach_click{element:"ui_2"}`，别手抄坐标。

## 点完必须能验证（v3 起）

每次点击/按键回执都带：`requested_hwnd`、`actual_foreground_hwnd`、`actual_foreground_title`、`clicked_screen_xy`、`changedTiles`、`changedRegion`。零变化时给 `verdict:"no_change"` 和三种可能（未命中 / 被禁用 / 被遮挡）；前台不是目标给 `verdict:"foreground"`。抢前台失败直接报错，不假装成功——顺序是 `ShowWindow(SW_RESTORE)` → `AttachThreadInput` → `BringWindowToTop` → `SetForegroundWindow` → `SetActiveWindow`。

## 战斗盘面：一个状态源 + 四步原语（v3.1 起）

以前每打一幕都要重做一遍「截全屏 → 猜骰子坐标 → 试点 → 数变化格」，因为没有任何工具一次给出结构化盘面。
现在这条路是：

```
coach_state_json                     # ① 一次拿全：场景/子状态、速度骰、手牌、下令按钮、静止与否
coach_select_page{die:0,hand:0}      # ② 点自己的骰 → 点手牌
coach_target_enemy{die:0}            # ③ 点敌方的骰（画面会多一条弧线）
coach_execute{expectMin:250}         # ④ 点顶部那颗蓝菱形下面的金盘
```

### 坐标一律是屏幕绝对坐标
`coach_state_json` 里所有 `screen` 字段都是**屏幕绝对坐标**，可以直接喂 `coach_click{x,y}`
（`cv_act` 也默认屏幕坐标、可用 `coordSpace:"client"` 切成画面坐标）。这次踩过的坑：
锚定窗口的**客户区**原点才是图像坐标的零点，用窗口边框原点去换算会整体偏 (11,45) 像素——
`(671,162)` 和 `(671,121)` 差的就是这 45 像素，一个点在金盘上、一个点在盘子外面。

### 「下令战斗」按钮在哪儿
蓝菱形 `[626,7,28,45]` 只是指示器，**可点的实体是它下面那块金色光晕圆盘**，
盘心图像坐标 ≈ (640,55)、屏幕 ≈ (671,162)。实测点它 667+ 格变化 = 下令成功；
点菱形正上方（红色箭头轨道）或左右刻度节点只会弹出行动面板（约 135 格）。

**验证窗口要给足**：下令后先是骰子动画，1.2s 时只测到 93 格，2.2s 后才看得到整屏变化。
所以 `coach_execute` 默认 `verifyMs:2200`、`expectMin:250`，一次不中就换 `candidates` 里的下一个点。

### 它认不出来的东西（都写在回执的 `unknown` 里）
- `speedDice[].value`：骰子上的数字是描边花字，Windows OCR 读不出来（这套字体整体读不出字）。
- 自己头顶的速度骰：是**极细的金色六边形描边**，按颜色连通域会被打碎成 26x8 的碎片，
  目前只能按"在画面左半"猜 `side:"self"`，数量也不可靠。敌方的紫/白骰认得比较稳。
- 手牌名字/费用/骰值：同样读不出字。
- 是否已经指派过牌（需要弧线检测，尚未做），所以 `canExecute` 恒为 `null`。

### 场景不再凭"像不像骰子"瞎认
只靠六边形色块会把普通桌面窗口也认成战斗（实测在 DSH 窗口上"找到 2 个骰子"）。
现在 `scene:"battle"` 必须有**战斗界面专属证据**：底部手牌，或顶部那颗蓝色菱形。
都没有就返回 `scene:null` + `unknown` 里写明"画面里像骰子但找不到战斗特征"，绝不给假局面。

### 自测
```
python tools\test_battle_flow.py      # 18 项：四步原语的接线（坐标、顺序、失败路径），不需要游戏在跑
```

## 已知边界

- 独占全屏下 PrintWindow/ffmpeg 都可能采不到；建议游戏用**无边框窗口**。
- OCR 认不出图标、地图、单位位置；这类只能 `coach_frame`。
- 多显示器/DPI 已处理（sidecar 启动即 DPI aware，坐标按物理像素）。
- 会抢焦点（每步约 0.3~0.5 s）：边操作边打字会打架；`coach_drive` 一次走完就还焦点。

## 自测

```powershell
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-plugin-desktop-primary-runtime\dependencies\python\python.exe"
& $py "D:\插件\game-stack\plugins\dsh-coach-perceive-v3\sidecar\coachctl.py" now
& $py "...\coachctl.py" drive --key space --steps 10
& $py "...\coachctl.py" click-text 确定
& $py "...\coachctl.py" snap --ocr --region "[100,200,640,400]" --scale 2
```

`coachctl.py` 是纯 HTTP 客户端，不依赖 DSH，可单独调试 sidecar。

不需要游戏在跑的两套回归：

```powershell
$py   = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-plugin-desktop-primary-runtime\dependencies\python\python.exe"
$node = "C:\Program Files\DSH Desktop\resources\runtime\x64\primary-runtime\dependencies\node\bin\node.exe"
& $py  "D:\插件\game-stack\plugins\dsh-coach-perceive-v3\tools\test_battle_flow.py"   # 18 项：出牌四步的接线
& $node "D:\插件\game-stack\plugins\dsh-coach-perceive-v3\tools\test_render.js"        # 16 项：渲染层不吃异常
```

`test_render.js` 专门防一类回归：**服务端字段形状变了（数组变对象、null、缺字段），渲染不能抛
`xxx.map is not a function`**。之前 `coach_state_json` 就是被 `hp` 从数组改成
`{bars,texts,hint}` 打挂的，现在所有渲染统一过 `arr()/obj()/txt()/px()` 四个小工具。
