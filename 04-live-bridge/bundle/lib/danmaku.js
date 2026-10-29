import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'
import { brotliDecompressSync, inflateSync } from 'node:zlib'
import { fetchJson, hhmmss, UA } from './util.js'

const ROOM_INIT = 'https://api.live.bilibili.com/room/v1/Room/room_init'
const DANMU_INFO = 'https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo'
const FINGER_SPI = 'https://api.bilibili.com/x/frontend/finger/spi'
const NAV = 'https://api.bilibili.com/x/web-interface/nav'
const FALLBACK_WS = 'wss://broadcastlv.chat.bilibili.com:443/sub'

const MIXIN_TABLE = [
  46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38,
  41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
  20, 34, 44, 52,
]

function header(total, op, seq = 1) {
  const buf = Buffer.alloc(16)
  buf.writeUInt32BE(total, 0)
  buf.writeUInt16BE(16, 4)
  buf.writeUInt16BE(1, 6)
  buf.writeUInt32BE(op, 8)
  buf.writeUInt32BE(seq, 12)
  return buf
}

function packet(op, body) {
  const payload = Buffer.isBuffer(body) ? body : Buffer.from(String(body), 'utf8')
  return Buffer.concat([header(16 + payload.length, op), payload])
}

/** 解一层包；protover 2/3 里是「又一批完整包」，递归解。 */
function parseBuffer(buf, out) {
  let offset = 0
  while (offset + 16 <= buf.length) {
    const total = buf.readUInt32BE(offset)
    const headLen = buf.readUInt16BE(offset + 4)
    const version = buf.readUInt16BE(offset + 6)
    const op = buf.readUInt32BE(offset + 8)
    if (total < 16 || offset + total > buf.length) break
    const body = buf.subarray(offset + headLen, offset + total)
    offset += total
    if (op !== 5) continue
    try {
      if (version === 2) parseBuffer(inflateSync(body), out)
      else if (version === 3) parseBuffer(brotliDecompressSync(body), out)
      else {
        const text = body.toString('utf8').trim()
        if (!text) continue
        try {
          out.push(JSON.parse(text))
        } catch {
          for (const line of text.split(/\r?\n/)) {
            const piece = line.trim()
            if (!piece) continue
            try {
              out.push(JSON.parse(piece))
            } catch {
              /* 忽略半截 */
            }
          }
        }
      }
    } catch {
      /* 单包解不开不影响其它包 */
    }
  }
}

export class DanmakuClient {
  constructor({ store, logger }) {
    this.store = store
    this.logger = logger
    this.items = []
    // 单调递增序号：给「只读没读过的」做游标用，比时间戳可靠（同毫秒多条也不会漏/重）
    this.seq = 0
    this.running = false
    this.ws = null
    this.heartbeat = null
    this.retryTimer = null
    this.loop = null
    this.buvid = { b3: '', b4: '' }
    this.wbi = { mixin: '', at: 0 }
    this.status = { connected: false, roomId: 0, title: '', live: 0, error: '', since: 0, total: 0 }
  }

  async start(roomId = 0) {
    const want = Number(roomId || this.store.getConfig().roomId || 0)
    if (!want) throw new Error('还没填直播间号')
    if (this.running && this.status.roomId === want && this.status.connected) return this.statusText()
    this.stop()
    this.running = true
    this.status = { ...this.status, roomId: want, error: '', since: Date.now() }
    this.loop = this._loop(want).catch((error) => {
      this.status = { ...this.status, error: error?.message || String(error) }
    })
    for (let i = 0; i < 24 && this.running && !this.status.connected; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    return this.statusText()
  }

  stop() {
    this.running = false
    clearInterval(this.heartbeat)
    clearTimeout(this.retryTimer)
    this.heartbeat = null
    this.retryTimer = null
    try {
      this.ws?.close()
    } catch {
      /* ignore */
    }
    this.ws = null
    this.status = { ...this.status, connected: false }
  }

  statusText() {
    if (!this.status.roomId) return '还没连直播间：先在设置页填直播间号，或者调用 live_danmaku_read 时传 roomId。'
    if (this.status.connected) {
      return `已连接直播间 ${this.status.roomId}${this.status.title ? `（${this.status.title}）` : ''}：本地缓冲 ${this.items.length} 条弹幕。`
    }
    return `正在连直播间 ${this.status.roomId}：${this.status.error || '重连中'}`
  }

  recent(limit = 30, sinceTs = 0) {
    const from = Number(sinceTs || 0)
    const list = this.items.filter((item) => item.ts > from)
    const size = Math.max(1, Math.min(200, Number(limit) || 30))
    return list.slice(-size)
  }

  /** 只取「序号比 from 大」的弹幕，按时间顺序，最多 size 条（老的先给，保证不漏）。 */
  since(from = 0, limit = 30) {
    const start = Number(from || 0)
    const size = Math.max(1, Math.min(200, Number(limit) || 30))
    return this.items.filter((item) => Number(item.seq || 0) > start).slice(0, size)
  }

  /** 当前缓冲里最大的序号；空缓冲返回 0。 */
  headSeq() {
    const last = this.items[this.items.length - 1]
    return last ? Number(last.seq || 0) : 0
  }

  /** 缓冲里还没「读过」的条数（给回执用）。 */
  unreadCount(from = 0) {
    const start = Number(from || 0)
    return this.items.filter((item) => Number(item.seq || 0) > start).length
  }

  async _ensureBuvid() {
    if (this.buvid.b3) return this.buvid
    const cache = path.join(this.store.dataDir, 'buvid.json')
    try {
      const data = JSON.parse(fs.readFileSync(cache, 'utf8'))
      if (data?.b3) {
        this.buvid = { b3: String(data.b3), b4: String(data.b4 || '') }
        return this.buvid
      }
    } catch {
      /* 没缓存 */
    }
    try {
      const res = await fetchJson(FINGER_SPI, { headers: { 'user-agent': UA, referer: 'https://www.bilibili.com/' } }, 10_000)
      const data = res.data?.data || {}
      this.buvid = { b3: String(data.b_3 || ''), b4: String(data.b_4 || '') }
      if (this.buvid.b3) {
        fs.mkdirSync(this.store.dataDir, { recursive: true })
        fs.writeFileSync(cache, JSON.stringify(this.buvid), 'utf8')
      }
    } catch {
      /* 拿不到就空着 */
    }
    return this.buvid
  }

  /** B 站风控：getDanmuInfo 现在要 wbi 签名，-352 就是没签名。 */
  async _mixin() {
    if (this.wbi.mixin && Date.now() - this.wbi.at < 30 * 60_000) return this.wbi.mixin
    const res = await fetchJson(NAV, {
      headers: { 'user-agent': UA, cookie: this.cookieHeader(), referer: 'https://www.bilibili.com/' },
    }, 10_000)
    const img = String(res.data?.data?.wbi_img?.img_url || '')
    const sub = String(res.data?.data?.wbi_img?.sub_url || '')
    const imgKey = img.split('/').pop()?.split('.')[0] || ''
    const subKey = sub.split('/').pop()?.split('.')[0] || ''
    const origin = imgKey + subKey
    const mixin = MIXIN_TABLE.map((index) => origin[index] || '').join('').slice(0, 32)
    this.wbi = { mixin, at: Date.now() }
    return mixin
  }

  async _signed(params) {
    const mixin = await this._mixin()
    const withTs = { ...params, wts: Math.floor(Date.now() / 1000) }
    const query = Object.keys(withTs)
      .sort()
      .map((key) => `${key}=${encodeURIComponent(withTs[key])}`)
      .join('&')
    const wRid = createHash('md5').update(query + mixin).digest('hex')
    return `${query}&w_rid=${wRid}`
  }

  cookieHeader() {
    const parts = []
    const saved = this.store.biliCredential().cookie
    if (saved) parts.push(saved)
    if (this.buvid.b3) parts.push(`buvid3=${this.buvid.b3}`)
    if (this.buvid.b4) parts.push(`buvid4=${this.buvid.b4}`)
    return parts.join('; ')
  }

  async _loop(roomId) {
    let backoff = 3_000
    while (this.running) {
      try {
        await this._connect(roomId)
        backoff = 3_000
      } catch (error) {
        this.status = { ...this.status, connected: false, error: error?.message || String(error) }
        this.logger.warn(`连弹幕失败：${this.status.error}`)
      }
      if (!this.running) break
      await new Promise((resolve) => {
        this.retryTimer = setTimeout(resolve, Math.min(20_000, backoff))
      })
      backoff = Math.min(20_000, Math.round(backoff * 1.6))
    }
  }

  async _connect(roomId) {
    await this._ensureBuvid()
    const cookie = this.cookieHeader()
    const headers = {
      'user-agent': UA,
      cookie,
      referer: `https://live.bilibili.com/${roomId}`,
      origin: 'https://live.bilibili.com',
      accept: 'application/json, text/plain, */*',
    }

    const init = await fetchJson(`${ROOM_INIT}?id=${roomId}`, { headers }, 10_000)
    const info = init.data?.data || {}
    if (init.data?.code !== 0 || !info.room_id) {
      throw new Error(`房间不存在或接口异常：${init.data?.message || init.data?.code || init.status}`)
    }
    const real = Number(info.room_id)
    this.status = { ...this.status, roomId: real, live: Number(info.live_status || 0), title: String(info.title || this.status.title || '') }

    const query = await this._signed({ id: real, type: 0, web_location: '444.8' })
    const tokenRes = await fetchJson(`${DANMU_INFO}?${query}`, { headers }, 12_000)
    const data = tokenRes.data?.data || {}
    const token = data.token
    if (!token) {
      const code = tokenRes.data?.code
      throw new Error(`拿不到弹幕 token：code=${code} ${tokenRes.data?.message || ''}`)
    }
    const host = (data.host_list || [])[0] || {}
    const url = host.host ? `wss://${host.host}:${host.wss_port || 443}/sub` : FALLBACK_WS

    await new Promise((resolve, reject) => {
      const ws = new WebSocket(url)
      ws.binaryType = 'arraybuffer'
      this.ws = ws
      let settled = false
      const fail = (error) => {
        if (!settled) {
          settled = true
          reject(error)
        }
      }
      ws.addEventListener('open', () => {
        const cookies = this.store.cookies()
        const auth = {
          uid: Number(cookies.DedeUserID || 0) || 0,
          roomid: real,
          protover: 3,
          platform: 'web',
          type: 2,
          key: token,
          buvid: this.buvid.b3 || cookies.buvid3 || '',
        }
        ws.send(packet(7, JSON.stringify(auth)))
        if (!this.status.connected) {
          this.status = { ...this.status, connected: true, error: '', since: Date.now() }
          this.logger.info(`直播桥已连上弹幕：房间 ${real}`)
          if (!settled) {
            settled = true
            resolve()
          }
        }
        clearInterval(this.heartbeat)
        this.heartbeat = setInterval(() => {
          try {
            ws.send(packet(2, '[object Object]'))
          } catch {
            /* ignore */
          }
        }, 30_000)
      })
      ws.addEventListener('message', async (event) => {
        const payload = event.data
        let buf
        if (typeof payload === 'string') buf = Buffer.from(payload, 'utf8')
        else if (payload instanceof ArrayBuffer) buf = Buffer.from(payload)
        else if (ArrayBuffer.isView(payload)) buf = Buffer.from(payload.buffer, payload.byteOffset, payload.byteLength)
        else if (typeof payload?.arrayBuffer === 'function') buf = Buffer.from(await payload.arrayBuffer())
        else return
        const boxes = []
        parseBuffer(buf, boxes)
        for (const box of boxes) {
          if (String(box?.cmd || '').startsWith('DANMU_MSG')) this._pushDanmaku(box)
        }
      })
      ws.addEventListener('error', () => fail(new Error('WebSocket 出错')))
      ws.addEventListener('close', () => {
        clearInterval(this.heartbeat)
        this.status = { ...this.status, connected: false }
        fail(new Error('WebSocket 已断开'))
      })
    })
  }

  _pushDanmaku(box) {
    try {
      const info = box.info || []
      const text = String(info[1] || '').trim()
      const user = info[2] || []
      if (!text) return
      const uid = String(user[0] || '')
      const uname = String(user[1] || '观众')
      const ts = Date.now()
      const last = this.items[this.items.length - 1]
      if (last && last.uid === uid && last.text === text && ts - last.ts < 3_000) return
      this.seq += 1
      this.items.push({ seq: this.seq, ts, uid, uname, text, at: hhmmss(ts) })
      const cap = Math.max(50, Math.min(500, Number(this.store.getConfig().bufferSize) || 200))
      if (this.items.length > cap) this.items.splice(0, this.items.length - cap)
      this.status.total = (this.status.total || 0) + 1
    } catch {
      /* 单条坏了不影响 */
    }
  }
}

