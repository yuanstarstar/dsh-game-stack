export function clip(value, max = 200) {
  const text = String(value ?? '')
  return text.length > max ? `${text.slice(0, max)}…` : text
}

export function now() {
  return Date.now()
}

export function hhmmss(ts) {
  const d = new Date(Number(ts) || now())
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export async function fetchJson(url, init = {}, timeoutMs = 12_000) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(url, { ...init, signal: controller.signal })
    const raw = await response.text()
    let data = null
    try {
      data = JSON.parse(raw)
    } catch {
      data = { raw }
    }
    return { status: response.status, headers: response.headers, data }
  } finally {
    clearTimeout(timer)
  }
}

export const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
