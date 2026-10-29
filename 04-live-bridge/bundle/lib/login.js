import path from 'node:path'
import { createRequire } from 'node:module'
import { fetchJson, UA, clip } from './util.js'

const QR_GENERATE = 'https://passport.bilibili.com/x/passport-login/web/qrcode/generate'
const QR_POLL = 'https://passport.bilibili.com/x/passport-login/web/qrcode/poll'
const NAV = 'https://api.bilibili.com/x/web-interface/nav'

const require = createRequire(import.meta.url)

function loadQrCode(root) {
  try {
    return require(path.join(root, 'vendor', 'node_modules', 'qrcode'))
  } catch {
    return null
  }
}

export function createLogin({ store, logger, root }) {
  const QR = loadQrCode(root || path.join(process.cwd()))

  function status() {
    const cred = store.biliCredential()
    return {
      ok: true,
      loggedIn: Boolean(cred.cookie && cred.mid),
      uname: cred.uname,
      mid: cred.mid,
      updated: cred.updated,
    }
  }

  async function start() {
    const res = await fetchJson(QR_GENERATE, { headers: { 'user-agent': UA, referer: 'https://www.bilibili.com/' } }, 12_000)
    const data = res.data?.data || {}
    if (!data.url || !data.qrcode_key) throw new Error(`生成二维码失败：${res.data?.message || res.status}`)
    let svg = ''
    if (QR?.toString) {
      try {
        // node 版 qrcode 的 toString 返回 Promise
        svg = String(await QR.toString(data.url, { type: 'svg', margin: 1, width: 240 }))
      } catch {
        svg = ''
      }
    }
    return { ok: true, key: data.qrcode_key, url: data.url, svg }
  }

  async function poll(key) {
    if (!key) throw new Error('缺二维码 key')
    const res = await fetchJson(`${QR_POLL}?qrcode_key=${encodeURIComponent(key)}`, {
      headers: { 'user-agent': UA, referer: 'https://www.bilibili.com/' },
    }, 12_000)
    const data = res.data?.data || {}
    const code = Number(data.code ?? -1)
    if (code === 86101) return { ok: true, state: 'waiting', message: '还没扫码' }
    if (code === 86090) return { ok: true, state: 'scanned', message: '扫了，等手机上点确认' }
    if (code === 86038) return { ok: true, state: 'expired', message: '二维码过期了，重新点扫码' }
    if (code !== 0) return { ok: false, state: 'error', message: clip(data.message || `状态码 ${code}`, 120) }

    const cookie = cookieFromHeaders(res.headers)
    if (!cookie) throw new Error('登录成功但没拿到 cookie')
    const nav = await fetchJson(NAV, { headers: { 'user-agent': UA, cookie, referer: 'https://www.bilibili.com/' } }, 12_000)
    const info = nav.data?.data || {}
    const saved = store.saveBiliCredential({ cookie, uname: info.uname || '', mid: info.mid || 0 })
    logger.info(`B 站登录成功：${saved.uname || saved.mid || '未知'}（凭证单独存 data/bili_credentials.json）`)
    return { ok: true, state: 'ok', uname: saved.uname, mid: saved.mid, message: `登录成功：${saved.uname || saved.mid}` }
  }

  function logout() {
    store.clearBiliCredential()
    return { ok: true, message: '已退出登录（只删了本插件的 data/bili_credentials.json，不动别的项目）' }
  }

  return { start, poll, status, logout }
}

function cookieFromHeaders(headers) {
  const list = typeof headers.getSetCookie === 'function' ? headers.getSetCookie() : []
  const pairs = new Map()
  for (const line of list) {
    const first = String(line).split(';')[0]
    const index = first.indexOf('=')
    if (index <= 0) continue
    const name = first.slice(0, index).trim()
    if (!['SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5', 'sid', 'buvid3'].includes(name)) continue
    pairs.set(name, first.slice(index + 1).trim())
  }
  return [...pairs.entries()].map(([key, value]) => `${key}=${value}`).join('; ')
}

