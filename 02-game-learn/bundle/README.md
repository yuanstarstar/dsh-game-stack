# 游戏学习（Game Learning）

把之前三个插件合并成一个：**一个侧边栏入口「游戏学习」，四个页签**。

| 原来 | 现在 | 数据文件 |
| --- | --- | --- |
| 回合制游戏教练 `@local/dsh-game-coach-v6` | 页签「游戏与策略」 | `%USERPROFILE%\.dsh\game-coach\library.json` |
| 规则工作台 `@local/dsh-rule-workbench` | 页签「规则与流程」 | `%USERPROFILE%\.dsh\rule-workbench\rules.json` + `sessions.jsonl` + `flow.json` |
| 防幻觉闸门 `@local/dsh-game-guard` | 页签「闸门」 | `%USERPROFILE%\.dsh\game-guard\guard.json` |

## 工具全保留，名字不变

合并后一共 **24 个工具**，AI 侧无需改习惯：

- 知识库：`game_coach_game` / `game_coach_strategy` / `game_coach_research` / `game_coach_turn`
- 决策引擎：`rules_doc` / `rules_list` / `rules_save` / `rules_delete` / `rules_toggle` / `rules_run` / `rules_outcome` / `rules_results` / `rules_replay` / `rules_stats` / `turn_card` / `flow_get` / `flow_learn` / `flow_save`
- 闸门：`guard_doc` / `guard_schema` / `guard_calc` / `guard_check` / `guard_reconcile` / `guard_stats`

三段系统提示（`game-coach` / `rule-workbench` / `game-guard`）也都保留，顺序不冲突。

## 安装

```powershell
# 在 DSH 里：
plugin_manager({ action: 'install_bundle', target: 'D:\插件\game-stack\plugins\dsh-game-learn' })
# 然后重启 DSH（或刷新页面），侧边栏出现「游戏学习」
```

## 目录

```
dsh-game-learn/
├── host.js            # 合并入口：把三段 apply 拼起来，共享默认 scope
├── client.js          # 合并面板：概览 / 游戏与策略 / 规则与流程 / 闸门
├── lib/coach.js       # 原 dsh-game-coach-v6/host.js
├── lib/rules.js       # 原 dsh-rule-workbench/host.js
├── lib/guard.js       # 原 dsh-game-guard/host.js
├── icon.svg
├── cordis.patch.yml
└── package.json
```

三个 host 模块里各自 `export const name`，父入口不转发它们，只调用各自的 `apply(ctx, config)`，所以工具名 / 提示名 / 路由前缀都不会打架：

- `/gc6/api/**`（知识库）
- `/rule-workbench/api/**`（规则引擎）
- `/game-guard/api/**`（闸门）

## 配置

```yaml
- id: game-learn
  name: '@local/dsh-game-learn'
  config:
    scope: default          # 规则与闸门的默认域（两边共享，同一个域=同一个游戏）
```

也可分别覆盖：`config.rules.scope` / `config.guard.scope` / `config.coach.dataFile`。

## 测试

```powershell
node test-learn.mjs        # host：24 工具 / 3 段提示 / 3 条路由 / 默认域共享
node test-ui-learn.mjs     # 面板：四页签渲染、展开策略、+胜 回写、学习流程、闸门契约
```

## 为什么这么合并

三个插件其实是一条流水线的三段：

1. **知识库**记「这个游戏怎么打」（策略、研究、实战）——唯一跨会话资产；
2. **规则引擎**记「这一步怎么算」（规则 + 流程阶段 + 备选 + 预期 + 回填）——确定性、可复跑；
3. **闸门**记「这个数敢不敢信」（契约 + 帧龄 + 对账）——把幻觉拦在决策之前。

合到一起，是为了让它们在**同一个域（scope）**下对话：给 `scope` 填游戏名，规则、闸门、流程就是同一个游戏的。

## 已知限制

- 面板不做「运行结果历史图」，要看历史用 `rules_results` / 台账 JSON。
- `lib/*.js` 是拷贝而非 re-export：Cordis 的 `inject` 在子模块里各自声明，父模块只负责调用，这样升级单个子系统直接替换对应 `lib` 文件即可。
- 三个路由前缀仍沿用旧名（`/gc6` 等），是为了不打断已在跑的会话；改名只需同步改 `client.js` 顶部常量。

## 客户端插件契约（踩过的坑）

client.js 必须 \eturn { name, inject, apply(ctx) { ... } }\——**对象 + apply 方法**。

不能返回 \{ inject, Page }\ 这种"自己发明的形状"：客户端加载器会抛
\Error: invalid plugin, expect function or object with an "apply" method, received object\，
而这一条发生在 renderer boot 阶段，会把**整个 DSH 界面**带崩（不是只坏一个面板），
桌面端随后还会把该 bundle 记进 \desktopDeselectedBundles\ 自动禁用。

注册槽位一律走 \ctx.slots.inject(name, () => ctx.slots.register(options, Component))\，
多语言走 \ctx.locale.register(ns, id, dict)\ + \ctx.locale.bind(ns)\（bind 失败要兜底回内联文案）。