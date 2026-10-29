/**
 * @local/dsh-game-learn —— 游戏学习（三合一）
 *
 * 一个插件、一个侧边栏入口，三条腿各管一段：
 *   - coach  ：知识库（游戏建档 / 策略 / 研究 / 实战记录），唯一跨会话资产
 *   - rules  ：决策引擎（规则 + 回合卡 + 流程状态机 + 备选/预期/回填）
 *   - guard  ：防幻觉闸门（读数契约 + 派生量 + 帧龄 + 对账）
 *
 * 三段代码原样保留在 lib/ 下，工具名不变（game_coach_* / rules_* / guard_* / turn_card / flow_*），
 * 面板合并成一个：侧边栏「游戏学习」。
 */

import { apply as applyCoach } from './lib/coach.js'
import { apply as applyGuard } from './lib/guard.js'
import { apply as applyRules } from './lib/rules.js'

export const name = 'game-learn'
export const inject = ['tools', 'systemPrompt', 'webServer']

/**
 * 每条腿单独兜底：某条腿装配失败（比如工具名被别的插件占用）
 * 只丢它自己，不能让整个插件变砖。
 */
function safe(label, fn) {
  try {
    fn()
  } catch (error) {
    console.warn('[game-learn] ' + label + ' 装配失败：' + (error && error.message ? error.message : error))
  }
}

export function apply(ctx, config) {
  const cfg = config && typeof config === 'object' ? config : {}
  const scope = typeof cfg.scope === 'string' && cfg.scope.trim() ? cfg.scope.trim() : 'default'
  const shared = { scope }

  safe('coach', () => applyCoach(ctx, cfg.coach && typeof cfg.coach === 'object' ? cfg.coach : {}))
  safe('rules', () => applyRules(ctx, cfg.rules && typeof cfg.rules === 'object' ? cfg.rules : shared))
  safe('guard', () => applyGuard(ctx, cfg.guard && typeof cfg.guard === 'object' ? cfg.guard : shared))
}
