# 安装说明（DSH 打游戏插件合集）

## 0. 前置

| 项 | 要求 | 说明 |
|---|---|---|
| DSH Desktop | 已安装并能打开 | 本合集是插件包，不是独立程序 |
| Python | 3.10+，装好 `numpy` / `Pillow` | **只有** `01-coach-perceive`、`03-cv-recall` 需要（侧车是 Python）；缺了会在工具回执里直说缺哪个包 |
| Node.js | 用 DSH 自带运行时即可 | 不用另装 |

```powershell
python -m pip install numpy pillow
```

## 1. 一键安装（推荐）

```powershell
cd D:\插件\game-stack
powershell -ExecutionPolicy Bypass -File .\install-all.ps1
```

脚本做三件事（**无需管理员**）：

1. 在 `%USERPROFILE%\.dsh\profiles\desktop\node_modules\` 下为每个包建目录联结（junction）；
2. 把包名追加进该 profile `package.json` 的 `dsh.profile.bundles`；
3. 改之前备份成 `package.json.bak-<时间戳>`。

先干跑看它要做什么：

```powershell
.\install-all.ps1 -DryRun
```

装完**重启 DSH**（或页面 Ctrl+R）。侧边栏应出现：`游戏学习`、`游戏感知`、`视觉实体`、`直播桥`、`启动器`。

## 2. 验证

```text
coach_windows                      # 有窗口列表 = 感知侧车起来了
game_coach_game {action:"list"}     # 能返回（空也算成功）= 知识库可用
launcher_status                     # 启动器状态
```

再打开侧边栏 `游戏学习` → 四个页签都能点开。

## 3. 手动安装（不跑脚本）

以 `02-game-learn` 为例：

```powershell
$prof = "$env:USERPROFILE\.dsh\profiles\desktop"
cmd /c mklink /J "$prof\node_modules\@local\dsh-game-learn-v2" "D:\插件\game-stack\02-game-learn\bundle"
```

然后编辑 `$prof\package.json`，在 `dsh.profile.bundles` 里加一项 `"@local/dsh-game-learn-v2"`。其余四包同理（包名见 `README.md`）。

## 4. 需要你自己配的东西（包里一律不带）

| 包 | 需要配什么 | 存在哪 |
|---|---|---|
| `01-coach-perceive` | 可选：OBS WebSocket 端口/密码 → `coach_anchor{action:"obs", password:"..."}` 或环境变量 `COACH_OBS_PASSWORD` | 不落盘；纯抓窗口时不需要 |
| `03-cv-recall` | 无 | `%USERPROFILE%\.dsh\cv-recall\` |
| `02-game-learn` | 无 | `%USERPROFILE%\.dsh\{game-coach,game-guard,rule-workbench}\` |
| `04-live-bridge` | **需要**：B 站扫码登录 + 火山 TTS 的 API Key（面板里填） | `<插件目录>\data\bili_credentials.json`、`voice_credentials.json`（首次运行生成） |
| `05-launcher` | 无 | `%USERPROFILE%\.dsh-launcher\data`（面板可改盘符） |

## 5. 卸载

```powershell
.\install-all.ps1 -Uninstall
```

只解链、只删 `bundles` 条目，**不动数据目录**。

## 6. 排障

**整个界面空白/起不来** → 查 DSH 日志 `renderer boot failed (plugins: ...)`，它会点名哪个包。客户端插件必须 `return { name, inject, apply(ctx) {} }`。

**`coach_*` 报 Python 错** → `python -m pip install numpy pillow`；多解释器时用 `COACH_PYTHON` 指路径。

**`coach_frame` 抓到 8px 窄条** → 锚错窗口，先 `coach_windows` 再 `coach_anchor{window:"..."}`。

**OBS 连不上** → OBS 工具 → WebSocket 服务器设置：启用、端口 4455、有密码就带上。纯抓窗口不需要 OBS。

**直播桥读不到弹幕** → 面板看登录状态；cookie 在 `data\bili_credentials.json`，过期重新扫码。

**启动器占盘大** → 数据在 `%USERPROFILE%\.dsh-launcher\data`，面板里可换位置。
