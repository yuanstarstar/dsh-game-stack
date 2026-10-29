import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import { clip } from './util.js'

const URL = 'https://openspeech.bytedance.com/api/v3/tts/unidirectional'

/** 显示名 → 火山 voice_type（跟 dafeiyu-voice/gen_line.py 对齐）。 */
export const VOICES = {
  撒娇学妹: 'zh_female_sajiaoxuemei_uranus_bigtts',
  活泼刁蛮: 'ICL_uranus_zh_female_huopodiaoman_tob',
  元气甜妹: 'ICL_uranus_zh_female_yuanqitianmei_tob',
  樱桃丸子: 'zh_female_yingtaowanzi_uranus_bigtts',
  萌丫头: 'zh_female_mengyatou_uranus_bigtts',
  调皮公主: 'ICL_uranus_zh_female_tiaopigongzhu_tob',
  可爱女生: 'ICL_uranus_zh_female_keainvsheng_tob',
  俏皮女声: 'zh_female_qiaopinv_uranus_bigtts',
  林潇: 'zh_female_linxiao_uranus_bigtts',
  爽快思思: 'zh_female_shuangkuaisisi_uranus_bigtts',
  Vivi: 'zh_female_vv_uranus_bigtts',
  邻家女孩: 'zh_female_linjianvhai_uranus_bigtts',
  甜美桃子: 'zh_female_tianmeitaozi_uranus_bigtts',
}

export function resolveVoice(name) {
  const key = String(name || '').trim()
  return VOICES[key] || key || VOICES['撒娇学妹']
}

function decodeAudio(raw) {
  const chunks = []
  let code = 0
  let message = ''
  for (const obj of JSONDecoder(raw)) {
    code = Number(obj.code ?? code) || 0
    message = String(obj.message || message || '')
    if (obj.data) chunks.push(Buffer.from(String(obj.data), 'base64'))
  }
  return { code, message, audio: Buffer.concat(chunks) }
}

/** 火山 TTS 返回的是「多段 JSON 拼在一起」的流，按位置逐个解。 */
function* JSONDecoder(raw) {
  let index = 0
  while (index < raw.length) {
    while (index < raw.length && /[\s\r\n]/.test(raw[index])) index += 1
    if (index >= raw.length) return
    let depth = 0
    let inString = false
    let escaped = false
    let start = index
    for (let i = index; i < raw.length; i += 1) {
      const ch = raw[i]
      if (inString) {
        if (escaped) escaped = false
        else if (ch === '\\') escaped = true
        else if (ch === '"') inString = false
        continue
      }
      if (ch === '"') inString = true
      else if (ch === '{') depth += 1
      else if (ch === '}') {
        depth -= 1
        if (depth === 0) {
          const piece = raw.slice(start, i + 1)
          index = i + 1
          try {
            yield JSON.parse(piece)
          } catch {
            /* 跳过坏段 */
          }
          break
        }
      }
    }
    if (index === start) return
  }
}

export class Tts {
  constructor({ store, logger }) {
    this.store = store
    this.logger = logger
    this.queue = Promise.resolve()
  }

  available() {
    return Boolean(this.store.voiceCredential().apiKey)
  }

  async synth(text, options = {}) {
    const apiKey = this.store.voiceCredential().apiKey
    if (!apiKey) throw new Error('还没配语音 key：去设置页「直播桥 → 语音」填火山引擎的 API Key')
    const cfg = this.store.getConfig().voice || {}
    const body = {
      user: { uid: 'dsh-live-bridge' },
      req_params: {
        text: clip(text, Number(options.maxChars || cfg.maxChars || 140)),
        speaker: resolveVoice(options.voice || cfg.voice || '撒娇学妹'),
        audio_params: {
          format: 'mp3',
          sample_rate: 24_000,
          speech_rate: Number(options.speed ?? cfg.speed ?? 0) || 0,
        },
      },
    }
    const pitch = Number(options.pitch ?? cfg.pitch ?? 0) || 0
    if (pitch) body.req_params.post_process = { pitch }
    const response = await fetch(URL, {
      method: 'POST',
      headers: {
        'X-Api-Key': apiKey,
        'X-Api-Resource-Id': String(cfg.resource || 'seed-tts-2.0'),
        'X-Api-Request-Id': randomUUID(),
        'content-type': 'application/json',
      },
      body: JSON.stringify(body),
    })
    const raw = await response.text()
    if (!response.ok) throw new Error(`语音接口 ${response.status}：${clip(raw, 160)}`)
    const { code, message, audio } = decodeAudio(raw)
    if (!audio.length) throw new Error(`语音接口没返回音频：code=${code} ${clip(message, 120)}`)
    return { audio, voice: body.req_params.speaker, text: body.req_params.text }
  }

  /** 念一句：合成 → 本机播放（串行，避免几句叠在一起）。 */
  speak(text, options = {}) {
    this.queue = this.queue.then(() => this._speak(text, options)).catch((error) => {
      this.logger.warn(`说话失败：${error?.message || error}`)
      throw error
    })
    return this.queue
  }

  async _speak(text, options) {
    const clean = clip(String(text || '').trim(), 400)
    if (!clean) throw new Error('要说的话是空的')
    const { audio, voice } = await this.synth(clean, options)
    const file = path.join(os.tmpdir(), `dsh-live-bridge-${Date.now()}.mp3`)
    fs.writeFileSync(file, audio)
    const seconds = Math.max(0.8, Math.min(30, clean.length * 0.26 + 0.6))
    await this._play(file, seconds)
    fs.rm(file, { force: true }, () => {})
    return { ok: true, voice, bytes: audio.length, seconds: Number(seconds.toFixed(1)), text: clean }
  }

  _play(file, seconds) {
    const script = [
      'Add-Type -AssemblyName PresentationCore',
      `$p = New-Object System.Windows.Media.MediaPlayer`,
      `$p.Open([uri]'${file.replace(/'/g, "''")}')`,
      '$p.Play()',
      `Start-Sleep -Milliseconds ${Math.round(seconds * 1000)}`,
      '$p.Close()',
    ].join('; ')
    return new Promise((resolve, reject) => {
      const child = spawn('powershell', ['-NoProfile', '-NonInteractive', '-Command', script], {
        windowsHide: true,
        stdio: 'ignore',
      })
      child.on('error', reject)
      child.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`播放器退出码 ${code}`))))
    })
  }
}
