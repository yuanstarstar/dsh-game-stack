# DSH 打游戏插件合集（2026-10-29）

对应 `docs/DSH通用打游戏-插件经验报告.md` 的七个章节，收成 **5 个可安装 bundle**。
报告里 §2.3/§2.4/§2.5 是三条腿，本次已合并进一个包（`02-game-learn`），侧边栏只算一个入口。

| 报告章节 | 目录 | 包名 | 这一腿负责什么 | 运行时依赖 |
|---|---|---|---|---|
| §2.1 `coach_*` | `01-coach-perceive/` | `@local/dsh-coach-perceive-v3` 0.2.0 | 眼睛和手：抓帧、OCR、点击、按键、驱动推剧情、OBS 锚定 | Python 3.10+ / numpy / Pillow |
| §2.2 `cv_*` | `03-cv-recall/` | `@local/dsh-cv-recall` 0.1.0 | 把画面编译成实体包，之后零视觉 token 读盘面 | Python 3.10+ / numpy / Pillow |
| §2.3+§2.4+§2.5 | `02-game-learn/` | `@local/dsh-game-learn-v2` 0.2.1 | 规则引擎 + 流程状态机 + 四道闸门 + 知识库（合一） | 纯 JS，无依赖 |
| §2.6 `live_*` | `04-live-bridge/` | `@local/dsh-live-bridge` 0.1.0 | 读弹幕、语音说话 | 需自己扫码登录 / 填 TTS key |
| §2.7 `launcher_*` | `05-launcher/` | `@local/dsh-launcher` 3.3.2 | 装游戏、管实例、查版本 | 纯 JS |

体积：**约 2.0 MB / 145 个文件**。安装见 `INSTALL.md`，一键脚本 `install-all.ps1`。

## 工具命名（装好后应该出现这些）

- `coach_*`（约 25 个）：`coach_windows` `coach_anchor` `coach_now` `coach_diff` `coach_snapshot` `coach_frame` `coach_click` `coach_click_text` `coach_key` `coach_key_probe` `coach_drive` `coach_select_page` `coach_target_enemy` `coach_execute` `coach_state_json` `coach_ui_dump` `coach_dismiss` `coach_cancel` `coach_wait_stable` `coach_scene_define` `coach_scene_list` `coach_facts` `coach_click_log`
- `cv_*`：`cv_learn` `cv_recall` `cv_act`
- `game_coach_*`：`game_coach_game` `game_coach_research` `game_coach_strategy` `game_coach_turn`
- `rules_*`：`rules_doc` `rules_list` `rules_save` `rules_run` `rules_replay` `rules_results` `rules_outcome` `rules_stats` `rules_toggle` `rules_delete`
- `flow_*` / `turn_card`：`flow_get` `flow_learn` `flow_save` `turn_card`
- `guard_*`：`guard_doc` `guard_schema` `guard_calc` `guard_check` `guard_reconcile` `guard_stats`
- `live_*`：`live_danmaku_read` `live_speak`
- `launcher_*`：`launcher_status` `launcher_list_games` `launcher_list_versions` `launcher_install_game` `launcher_launch_game` 等

侧边栏入口：`游戏学习`（四页签：概览 / 游戏与策略 / 规则与流程 / 闸门）、`游戏感知`、`视觉实体`、`直播桥`、`启动器`。

## 数据落在哪（升级/重装都不丢）

| 插件 | 目录 |
|---|---|
| 感知输入 | `%USERPROFILE%\.dsh\coach-perceive\`（last-facts.json、input-log.jsonl）+ `%TEMP%\coachd-frames\` |
| 视觉实体 | `%USERPROFILE%\.dsh\cv-recall\`（packs/）+ `%TEMP%\cv-recall-frames\` |
| 游戏学习 | `%USERPROFILE%\.dsh\game-coach\library.json`、`.dsh\game-guard\`、`.dsh\rule-workbench\`（规则/流程/台账/sessions.jsonl） |
| 直播桥 | `<插件目录>\data\config.json`、`bili_credentials.json`、`voice_credentials.json` |
| 启动器 | `%USERPROFILE%\.dsh-launcher\data\`（游戏素材/实例记录，可能 GB 级） |

> 合并版刻意沿用旧目录名（`game-coach` / `game-guard` / `rule-workbench`），**之前积累的策略、规则、台账可直接复用**。

## 包里刻意没有的东西

- ❌ **任何登录凭证与 API key**：B 站 cookie、TTS key、OBS 密码都不进包。直播桥的 `data\` 目录（`bili_credentials.json` / `voice_credentials.json` / `config.json`）**已排除**，首次运行自己生成、自己扫码/填 key。OBS 密码只在工具入参或环境变量 `COACH_OBS_PASSWORD` 里。
- ❌ `node_modules`、`__pycache__`、`.git`、测试截图、`tools/ui/*.png`、`.tmp-*` 调试文件。
- ❌ 启动器的 1.2 GB 游戏素材（`data/game-data`）。
- ⚠️ 各包的 `SOURCE.json` 记录了本机原始路径，分发前可删。

## 已知边界（来自实战报告）

1. `cv_*` 实体包按分辨率写死，换窗口尺寸要重编。
2. `flow_*` 要等场景识别稳定后再 `flow_learn`，否则会反推出一套错的阶段机。
3. `coach_*` 依赖 Python 侧车；缺 numpy/Pillow 会在工具回执里直说。
4. `live_*` 的发弹幕/说话需要真凭据，属"你自己填"的部分。

## 变更记录

### 2026-11-01 · 修「两字以内文本被过滤」

- `01-coach-perceive`：`sidecar/coachd.py` 新增 `ocr_rescue()`（补边距放大 + 横向拼三份投票），
  接进 `/snapshot`、`/ocr`、`/click_text`；抢救结果带 `weak=true`，`coach_snapshot` 会打
  「短文本抢救，置信度低」，`coach_click_text` 会提醒点完复核。
  新增 `tools/test_short_ocr.py`（5 项断言，全部通过）。
- `03-cv-recall`：`sidecar/pcv/text.py::_plausible` 不再一律丢单字行，
  只丢"框很小很扁、像图标"的（高<14 / 宽<10 / 宽高比>4）。
- 已知限制：单个数字、单个拉丁字母仍可能读不出（WinRT 引擎限制），这类 UI 用坐标点击。
