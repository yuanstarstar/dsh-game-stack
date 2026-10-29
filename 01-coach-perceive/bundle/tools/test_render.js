// 渲染层回归测试：服务端字段形状变了也不能把工具打成异常。
// 跑法：node tools\test_render.js      （不需要游戏在跑，不需要 sidecar）
import { __render } from '../host.js'

const { renderState } = __render
const fails = []
let pass = 0
function t(name, fn) {
  try {
    const r = fn()
    if (r === true || r === undefined) {
      pass++
      console.log('  ✓ ' + name)
    } else {
      fails.push(name + ' → ' + r)
      console.log('  ✗ ' + name + ' → ' + r)
    }
  } catch (e) {
    fails.push(name + ' → 抛错: ' + e.message)
    console.log('  ✗ ' + name + ' → 抛错: ' + e.message)
  }
}

const noThrow = (d) => {
  const out = renderState(d)
  if (typeof out !== 'string' || !out.length) return '输出不是非空字符串'
  if (/undefined|\[object Object\]|NaN/.test(out)) return '输出里有脏值: ' + out.slice(0, 120)
  return true
}

console.log('[1] 真实形状：hp 是对象（bars/texts）——就是这次报错的那种')
t('不抛异常且带血条', () => {
  const out = renderState({
    scene: 'battle', sub: 'battle_assign', handVisible: true, ms: 900, autoRoll: true,
    hand: { visible: true, count: 2, cards: [
      { i: 0, box: [424, 475, 72, 160], screen: [460, 555], name: null, dice: ['3-5', '1-3'] },
      { i: 1, box: [504, 475, 72, 160], screen: [540, 555], name: null, dice: [] },
    ] },
    speedDice: [{ i: 1, side: 'self', color: 'gold', screen: [322, 352], value: null }],
    hp: { bars: [{ color: 'red', box: [40, 660, 120, 10], frac: 0.43, screen: [100, 665] }],
          texts: [{ text: '罗兰', box: [40, 640, 60, 18], screen: [70, 649] }], hint: null },
    executeButton: { found: true, screen: [672, 167], how: 'plate', candidates: [[672, 167], [671, 136]] },
    unknown: [], hint: '可以出牌',
  })
  if (!out.includes('血条')) return '没有血条段'
  if (!out.includes('43%')) return '血条百分比没算出来'
  if (!out.includes('罗兰')) return '底部文字没渲染'
  if (!out.includes('骰 3-5/1-3')) return '手牌骰值没渲染'
  return true
})

console.log('[2] hp 是数组（老形状）')
t('也能渲染', () => {
  const out = renderState({ hp: [{ color: 'green', box: [1, 2, 30, 8], frac: 0.5 }] })
  return out.includes('50%') || '没有 50%'
})

console.log('[3] 各种退化输入都不抛')
t('hp 缺失', () => noThrow({ scene: 'battle' }))
t('hp 是 null', () => noThrow({ hp: null }))
t('hp 是字符串', () => noThrow({ hp: 'oops' }))
t('全空对象', () => noThrow({}))
t('null', () => noThrow(null))
t('undefined', () => noThrow(undefined))
t('speedDice 是对象', () => noThrow({ speedDice: { 0: { i: 0 } } }))
t('hand 是数组（老形状）', () => noThrow({ handVisible: true, hand: [{ i: 0 }] }))
t('cards 是 null', () => noThrow({ handVisible: true, hand: { cards: null } }))
t('数字是字符串', () => noThrow({ ms: '12', speedDice: [{ i: '1', screen: ['3', '4'] }] }))
t('screen 少了元素', () => noThrow({ speedDice: [{ i: 0, screen: [1] }] }))
t('executeButton 是空对象', () => noThrow({ executeButton: {} }))
t('unknown 是字符串', () => noThrow({ unknown: 'x' }))

console.log('[4] 语义：不在战斗时要说清楚')
t('scene null → 显示未知', () => {
  const out = renderState({ scene: null, sub: 'unknown', handVisible: false, hand: { reason: '底部那一带没有成排的亮块' } })
  if (!out.includes('未知')) return '没显示未知'
  if (!out.includes('没有成排的亮块')) return '没把手牌原因带出来'
  return true
})

console.log(`\n通过 ${pass} 项，失败 ${fails.length} 项`)
if (fails.length) {
  fails.forEach((f) => console.log('  ✗ ' + f))
  process.exit(1)
}
