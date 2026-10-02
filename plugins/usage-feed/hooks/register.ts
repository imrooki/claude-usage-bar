// usage-feed: 把订阅账号的限额窗口（5 小时、7 天、spend_limit）合并后写进本地快照文件，
// 供任务栏小窗读取。多个写入方（例如同时打开的多个会话）可以同时写同一个文件，
// 靠下面的合并规则避免用旧数覆盖新数。
//
// 设计约束（只做没风险的事，不能拖垮电脑）。改动前先确认不破坏：
//  - 只用五个引擎调用：$.session.usage()（不带参数）、$.session.id()、$.clock.now()、
//    $.fs.read(path)、$.fs.write(path, text)。fs 只碰一个文件：<dataDir>/<SNAPSHOT_FILE>。
//    不碰进程、模型、提示、工具、子代理、命令、界面、store、state、网络，
//    不读写凭证、环境变量、设置文件。
//  - 只挂 session.start 与 session.measure 两个 hook。不起定时器，不起后台循环。
//  - 每个 hook 先 await next(e)，自己的逻辑整个包在 try/catch 里，最后原样返回 next 的结果：
//    任何失败都静默，绝不抛出，绝不改变事件结果。
//  - dataDir 安全闸见 resolveTarget：不合格就整个模块不注册任何 hook。
//
// 并发说明：可能同时运行多个会话，它们都会写同一个文件。"读快照、合并、写快照"
// 之间没有锁（设计约束禁止加锁等待），极端时序下会丢一次更新；每次事件带的都是全量窗口、
// 合并只增不减，下一次事件即自愈，所以不为它引入锁。
// 引擎的写入接口不是原子写（整体覆盖）：读方已能容忍坏文件；写入失败（例如读方此刻
// 正开着文件）静默放弃，下一次事件再写。
import type { EngineInterface, Register } from 'claude-code'

// 唯一的输出文件名。
const SNAPSHOT_FILE = 'usage.json'
// 只收这三种窗口；写出时也按这个顺序。
const KINDS = ['five_hour', 'seven_day', 'spend_limit'] as const
type Kind = (typeof KINDS)[number]
// resets_at 相差不超过它，视为同一窗口周期。
const SAME_PERIOD_SECONDS = 120
// 外来字符串写进文件前的长度上限，防止单条记录撑大文件。
const CLIP_CHARS = 128

type Fresh = { used_percentage: number; resets_at: number }
type Stored = { used_percentage: number; resets_at: number; observed_at: number; session_id: string | null }
type Windows<T> = { [K in Kind]?: T }

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

const isKind = (v: unknown): v is Kind => typeof v === 'string' && (KINDS as readonly string[]).includes(v)

// 有限数才返回；布尔、字符串、NaN、Infinity 一律 null。
const finite = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

const clip = (v: unknown): string | null => (typeof v === 'string' ? v.slice(0, CLIP_CHARS) : null)

// 字段顺序（used_percentage、resets_at、observed_at、session_id）是与读方的契约，只在这里构造。
const stored = (used: number, resets: number, observed: number, sessionId: string | null): Stored => ({
  used_percentage: used,
  resets_at: resets,
  observed_at: observed,
  session_id: sessionId,
})

// dataDir 安全闸：返回目标文件路径，不合格返回 null（什么都不写）。
// OneDrive 等云同步目录不适合存放每个回合都会改写的快照文件，所以路径里含 onedrive 的一律拒绝；
// 网络路径同理。必须是 Windows 本地绝对路径。
const resolveTarget = (dataDir: unknown): string | null => {
  if (typeof dataDir !== 'string' || dataDir === '') return null
  if (!/^[A-Za-z]:[\\/]/.test(dataDir)) return null
  if (/^(\\\\|\/\/)/.test(dataDir)) return null
  if (dataDir.toLowerCase().includes('onedrive')) return null
  return dataDir.replace(/\\/g, '/').replace(/\/+$/, '') + '/' + SNAPSHOT_FILE
}

// 引擎窗口 -> 快照窗口。不满足条件的窗口丢弃；同一 kind 出现多次取第一个有效的。
const fromEngine = (list: unknown): Windows<Fresh> => {
  const out: Windows<Fresh> = {}
  if (!Array.isArray(list)) return out
  for (const w of list as unknown[]) {
    if (!isRecord(w)) continue
    const kind = w.kind
    if (!isKind(kind) || out[kind] !== undefined) continue
    const used = finite(w.percentUsed)
    if (used === null || used < 0) continue
    const resetsAt = w.resetsAt
    if (typeof resetsAt !== 'string') continue
    const ms = Date.parse(resetsAt)
    if (!Number.isFinite(ms)) continue
    const resets = Math.floor(ms / 1000)
    if (!(resets > 0)) continue
    out[kind] = { used_percentage: used, resets_at: resets }
  }
  return out
}

// 读已有快照。读不出、非 JSON、schema 不是 1、windows 不是对象都当作没有；
// 单个窗口无效当作该窗口不存在。observed_at 缺失时用 written_at 兜底，再不行记 0
// （表示年龄未知、很旧），不能记成现在，否则会把来历不明的数据伪装成刚确认过的。
const readStored = (text: unknown): Windows<Stored> => {
  const out: Windows<Stored> = {}
  if (typeof text !== 'string') return out
  let obj: unknown
  try {
    // 已有快照可能带开头的 BOM（别的写入方或编辑器留下的），这里同样容忍。
    obj = JSON.parse(text.charCodeAt(0) === 0xfeff ? text.slice(1) : text)
  } catch {
    return out
  }
  if (!isRecord(obj) || obj.schema !== 1) return out
  const windows = obj.windows
  if (!isRecord(windows)) return out
  const fallbackObserved = finite(obj.written_at) ?? 0
  for (const kind of KINDS) {
    const w = windows[kind]
    if (!isRecord(w)) continue
    const used = finite(w.used_percentage)
    const resetsRaw = finite(w.resets_at)
    if (used === null || used < 0 || resetsRaw === null) continue
    // 0 < resets_at < 1 的小数取整后是 0，写回后不再满足 > 0，按无效处理，保持快照自洽。
    const resets = Math.floor(resetsRaw)
    if (!(resets > 0)) continue
    out[kind] = stored(used, resets, finite(w.observed_at) ?? fallbackObserved, clip(w.session_id))
  }
  return out
}

// 两个都有时，本次的数（n）是否取代已有的（o）。
const replaces = (n: Fresh, o: Stored): boolean => {
  // 同一周期内用量只增不减；更小的是旧数。相等时取新，用来刷新 observed_at。
  if (Math.abs(n.resets_at - o.resets_at) <= SAME_PERIOD_SECONDS) return n.used_percentage >= o.used_percentage
  // 更晚的周期是新窗口（百分比小也取新）；更早的周期是旧数。
  return n.resets_at > o.resets_at
}

// 逐窗口合并。闲置会话重跑时拿到的是旧数，
// 不能覆盖已确认的新数。
const merge = (old: Windows<Stored>, fresh: Windows<Fresh>, now: number, sessionId: string | null): Windows<Stored> => {
  const merged: Windows<Stored> = {}
  for (const kind of KINDS) {
    const o = old[kind]
    const n = fresh[kind]
    if (n === undefined) {
      // 只有已有的：原样保留（含 observed_at、session_id）。
      if (o !== undefined) merged[kind] = o
    } else if (o === undefined || replaces(n, o)) {
      merged[kind] = stored(n.used_percentage, n.resets_at, now, sessionId)
    } else {
      merged[kind] = o
    }
  }
  return merged
}

// 采一次：转换、取时间与会话 id、读旧快照、合并、整体写回。
// 任何一步抛出都交给调用方的 try/catch 静默。
const feed = async ($: EngineInterface, target: string, list: unknown): Promise<void> => {
  const fresh = fromEngine(list)
  if (Object.keys(fresh).length === 0) return
  const nowMs = await $.clock.now()
  // NaN 经 JSON.stringify 会变成 null，写出去读方会把整个窗口判无效，所以拿不到有限时间就不写。
  if (typeof nowMs !== 'number' || !Number.isFinite(nowMs)) return
  const now = nowMs / 1000
  let sessionId: string | null = null
  try {
    sessionId = clip(await $.session.id())
  } catch {
    // 取不到会话 id 不影响用量本身，记 null。
  }
  let existing: unknown = null
  try {
    existing = await $.fs.read(target)
  } catch {
    // 不存在或读不了：当作没有已有快照。
  }
  const windows = merge(readStored(existing), fresh, now, sessionId)
  await $.fs.write(target, JSON.stringify({ schema: 1, written_at: now, windows }, null, 2))
}

export const register: Register = (on, options) => {
  // 配置改动会重载模块、重新调用 register，所以安全闸在一次加载内不会变：
  // 永远写不出去的 hook 只是多余的面，不注册它。
  const target = resolveTarget(options?.dataDir)
  if (target === null) return

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    try {
      const usage = await $.session.usage()
      await feed($, target, usage.rateLimits)
    } catch {
      // 失败静默：不抛出，不改变事件结果。
    }
    return r
  })

  on('session.measure', async ($, e, next) => {
    const r = await next(e)
    try {
      // 每个回合后都会触发，数值没变也写一次，用来刷新 observed_at。
      if (Array.isArray(e.rateLimits) && e.rateLimits.length > 0) await feed($, target, e.rateLimits)
    } catch {
      // 失败静默：不抛出，不改变事件结果。
    }
    return r
  })
}
