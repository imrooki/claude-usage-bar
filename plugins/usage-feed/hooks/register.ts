// usage-feed: 把订阅账号的限额窗口（5 小时、7 天、spend_limit）合并后写进本地快照文件，
// 供任务栏小窗读取。多个写入方（例如同时打开的多个会话）可以同时写同一个文件，
// 靠下面的合并规则避免用旧数覆盖新数。
//
// 设计约束（只做没风险的事，不能拖垮电脑）。改动前先确认不破坏：
//  - 只用五个引擎调用：$.session.usage()（不带参数）、$.session.id()、$.clock.now()、
//    $.fs.read(path)、$.fs.write(path, text)。fs 只碰两个文件：<dataDir>/<SNAPSHOT_FILE>（用量快照）
//    与 <dataDir>/<EVENTS_FILE>（事件日志）。
//    不碰进程、模型、提示、工具、子代理、命令、界面、store、state、网络，
//    不读写凭证、环境变量、设置文件。
//  - 事件日志只用于排障，每次 hook 被调用记一条，最多保留最近 EVENT_CAP（200）条。每条只含：
//    t（时间，引擎时钟的毫秒数）、ev（事件名）、sid（会话 id）、n 与 kinds（引擎给的限额条数与
//    种类）、kept（保留下来的窗口数）、out 与 why（写 usage.json 的结果 wrote、skipped、failed，
//    以及跳过或失败的原因）、drops（被丢弃窗口的原因计数，没有丢弃时省略）、held（合并时保留了
//    旧值的窗口）、changed（session.measure 事件报告变化的度量项名，session.start 时为空）。
//    不含路径、工作目录、提示词、模型名。取不到有限时间、或写日志失败时，这次调用什么都不记；
//    读不出已有日志时按空日志重新开始（见下面的并发说明）。
//  - 只挂 session.start 与 session.measure 两个 hook。不起定时器，不起后台循环。
//  - 每个 hook 先 await next(e)，自己的逻辑整个包在 try/catch 里，最后原样返回 next 的结果：
//    任何失败都静默，绝不抛出，绝不改变事件结果。
//  - dataDir 安全闸见 resolveTarget：不合格就整个模块不注册任何 hook。
//
// 并发说明：可能同时运行多个会话，它们都会写同一个文件。"读快照、合并、写快照"
// 之间没有锁（设计约束禁止加锁等待），极端时序下会丢一次更新；每次事件带的都是全量窗口、
// 合并只增不减，下一次事件即自愈，所以不为它引入锁。
// 事件日志同样是"读、追加、写回"，没有锁，多个会话并发时可能丢记录；极端时序下读到
// 另一个会话写了一半的日志，或读不出日志，readEvents 都按空日志处理，这次写回就从空日志
// 重新开始，之前保留的记录（最多 EVENT_CAP 条）全部丢失，不止一条。
// 日志只用于排障，不为此引入锁。
// 引擎的写入接口不是原子写（整体覆盖）：读方已能容忍坏文件；写入失败（例如读方此刻
// 正开着文件）静默放弃，下一次事件再写。
import type { EngineInterface, Register } from 'claude-code'

// 输出文件名共两个，与 dataDir 同目录。
const SNAPSHOT_FILE = 'usage.json'
const EVENTS_FILE = 'usage-feed-events.json'
// 只收这三种窗口；写出时也按这个顺序。
const KINDS = ['five_hour', 'seven_day', 'spend_limit'] as const
type Kind = (typeof KINDS)[number]
// resets_at 相差不超过它，视为同一窗口周期。
const SAME_PERIOD_SECONDS = 120
// 外来字符串写进文件前的长度上限，防止单条记录撑大文件。
const CLIP_CHARS = 128
// 事件日志只保留最近这么多条。
const EVENT_CAP = 200
// 日志里 kinds 最多几项。
const MAX_KINDS = 6
// changed 最多几项、每项最长。
const MAX_CHANGED = 5
const CHANGED_CHARS = 16
// 错误名最长。
const ERR_NAME_CHARS = 40

// 窗口被丢弃的原因。写进日志时按这个顺序只列非零项，键序因此固定、便于肉眼对比。
const DROP_REASONS = ['not_object', 'unknown_kind', 'bad_percent', 'no_resets_at', 'bad_resets_at', 'duplicate_kind'] as const
type DropReason = (typeof DROP_REASONS)[number]
type Drops = { [R in DropReason]?: number }

type Fresh = { used_percentage: number; resets_at: number }
type Stored = { used_percentage: number; resets_at: number; observed_at: number; session_id: string | null }
type Windows<T> = { [K in Kind]?: T }

// feed 的结果，交给 logEvent 记成一行日志。nowMs 与 sessionId 让日志复用 feed 已经读到的值。
type Outcome = {
  n: number // 引擎给的限额列表长度；不是数组记 0
  kinds: string[] // kindsOf(list)
  kept: number // fromEngine 留下来的窗口数
  drops: Drops // fromEngine 的丢弃计数，只含非零项
  held: Kind[] // merge 里保留了旧值的 kind，按 KINDS 顺序
  out: 'wrote' | 'skipped' | 'failed'
  why: string // out 为 'wrote' 时是 ''
  nowMs: number | null // feed 已读到的有效时间（有限数）；没读到或无效为 null
  sessionId: string | null | undefined // feed 已读到的会话 id（读失败是 null）；feed 根本没去读为 undefined
}

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

// dataDir 安全闸：返回规范化目录 + '/' + file，不合格返回 null（什么都不写）。
// 快照与事件日志各算一次、用的是同一段检查，所以两个文件同目录；任一不合格就不注册 hook。
// OneDrive 等云同步目录不适合存放每个回合都会改写的快照文件，所以路径里含 onedrive 的一律拒绝；
// 网络路径同理。必须是 Windows 本地绝对路径。
const resolveTarget = (dataDir: unknown, file: string): string | null => {
  if (typeof dataDir !== 'string' || dataDir === '') return null
  if (!/^[A-Za-z]:[\\/]/.test(dataDir)) return null
  if (/^(\\\\|\/\/)/.test(dataDir)) return null
  if (dataDir.toLowerCase().includes('onedrive')) return null
  return dataDir.replace(/\\/g, '/').replace(/\/+$/, '') + '/' + file
}

const noteDrop = (drops: Drops, reason: DropReason): void => {
  drops[reason] = (drops[reason] ?? 0) + 1
}

// 引擎窗口 -> 快照窗口。不满足条件的窗口丢弃并计入 drops；同一 kind 出现多次取第一个有效的。
// 前面被丢弃的无效条目不占 kind，不会挡住后面的有效条目。
const fromEngine = (list: unknown): { windows: Windows<Fresh>; drops: Drops } => {
  const windows: Windows<Fresh> = {}
  const drops: Drops = {}
  if (!Array.isArray(list)) return { windows, drops }
  for (const w of list as unknown[]) {
    if (!isRecord(w)) {
      noteDrop(drops, 'not_object')
      continue
    }
    const kind = w.kind
    if (!isKind(kind)) {
      noteDrop(drops, 'unknown_kind')
      continue
    }
    if (windows[kind] !== undefined) {
      noteDrop(drops, 'duplicate_kind')
      continue
    }
    const used = finite(w.percentUsed)
    if (used === null || used < 0) {
      noteDrop(drops, 'bad_percent')
      continue
    }
    const resetsAt = w.resetsAt
    if (typeof resetsAt !== 'string') {
      noteDrop(drops, 'no_resets_at')
      continue
    }
    const ms = Date.parse(resetsAt)
    if (!Number.isFinite(ms)) {
      noteDrop(drops, 'bad_resets_at')
      continue
    }
    const resets = Math.floor(ms / 1000)
    if (!(resets > 0)) {
      noteDrop(drops, 'bad_resets_at')
      continue
    }
    windows[kind] = { used_percentage: used, resets_at: resets }
  }
  return { windows, drops }
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
// 不能覆盖已确认的新数。held 只收录"本次有新读数、但替换规则留下旧值"的 kind，按 KINDS 顺序。
const merge = (
  old: Windows<Stored>,
  fresh: Windows<Fresh>,
  now: number,
  sessionId: string | null,
): { windows: Windows<Stored>; held: Kind[] } => {
  const merged: Windows<Stored> = {}
  const held: Kind[] = []
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
      held.push(kind)
    }
  }
  return { windows: merged, held }
}

// list 不是数组则为 []；否则取前 MAX_KINDS 个条目，三种 kind 原样保留，其余记 other。
const kindsOf = (list: unknown): string[] => {
  if (!Array.isArray(list)) return []
  const out: string[] = []
  const n = Math.min(list.length, MAX_KINDS)
  for (let i = 0; i < n; i++) {
    const w: unknown = list[i]
    out.push(isRecord(w) && isKind(w.kind) ? w.kind : 'other')
  }
  return out
}

// raw 不是数组则为 []；否则取前 MAX_CHANGED 项，每项截到 CHANGED_CHARS。
const changedOf = (raw: unknown): string[] => {
  if (!Array.isArray(raw)) return []
  const out: string[] = []
  const n = Math.min(raw.length, MAX_CHANGED)
  for (let i = 0; i < n; i++) out.push(String(raw[i]).slice(0, CHANGED_CHARS))
  return out
}

// 日志里只记错误名，不记 message：message 里可能带路径或其它内容。
const errName = (err: unknown): string => {
  const name = isRecord(err) && typeof err.name === 'string' && err.name !== '' ? err.name : 'Error'
  return name.slice(0, ERR_NAME_CHARS)
}

// NaN 经 JSON.stringify 会变成 null，写出去读方会把整个窗口判无效，所以拿不到有限时间就不写。
const readNow = async ($: EngineInterface): Promise<number | null> => {
  try {
    const v = await $.clock.now()
    if (typeof v === 'number' && Number.isFinite(v)) return v
    return null
  } catch {
    return null
  }
}

const readSid = async ($: EngineInterface): Promise<string | null> => {
  try {
    return clip(await $.session.id())
  } catch {
    // 取不到会话 id 不影响用量本身，记 null。
    return null
  }
}

// 宽容解析已有日志：格式不对就当作没有，不因旧文件坏掉而丢掉本次记录的写入机会。
const readEvents = (text: unknown): unknown[] => {
  if (typeof text !== 'string') return []
  let obj: unknown
  try {
    obj = JSON.parse(text)
  } catch {
    return []
  }
  if (!isRecord(obj) || obj.schema !== 1) return []
  const events = obj.events
  if (!Array.isArray(events)) return []
  return events
}

// 采一次：转换、取时间与会话 id、读旧快照、合并、整体写回。
// 返回结果对象供 logEvent 记录。预期内的失败（没有窗口、窗口全被丢、时钟坏、
// 取会话 id 失败、读旧快照失败、写快照失败）不再抛出。
const feed = async ($: EngineInterface, target: string, list: unknown): Promise<Outcome> => {
  const n = Array.isArray(list) ? list.length : 0
  const kinds = kindsOf(list)
  if (n === 0) {
    return { n, kinds, kept: 0, drops: {}, held: [], out: 'skipped', why: 'no_rate_limits', nowMs: null, sessionId: undefined }
  }
  const converted = fromEngine(list)
  const fresh = converted.windows
  const drops = converted.drops
  const kept = Object.keys(fresh).length
  if (kept === 0) {
    return { n, kinds, kept: 0, drops, held: [], out: 'skipped', why: 'all_dropped', nowMs: null, sessionId: undefined }
  }
  const nowMs = await readNow($)
  if (nowMs === null) {
    return { n, kinds, kept, drops, held: [], out: 'skipped', why: 'bad_clock', nowMs: null, sessionId: undefined }
  }
  const now = nowMs / 1000
  const sessionId = await readSid($)
  let existing: unknown = null
  try {
    existing = await $.fs.read(target)
  } catch {
    // 不存在或读不了：当作没有已有快照。
  }
  const merged = merge(readStored(existing), fresh, now, sessionId)
  const windows = merged.windows
  const held = merged.held
  try {
    await $.fs.write(target, JSON.stringify({ schema: 1, written_at: now, windows }, null, 2))
  } catch (err) {
    return { n, kinds, kept, drops, held, out: 'failed', why: 'write_error:' + errName(err), nowMs, sessionId }
  }
  return { n, kinds, kept, drops, held, out: 'wrote', why: '', nowMs, sessionId }
}

// 追加一条事件日志。整个函数永不抛出：日志失败不影响用量快照，也不改变事件结果。
// nowMs / sessionId 已由 feed 读到的就复用，只补读缺的，避免成功路径上时钟和会话 id 各读两次。
const logEvent = async (
  $: EngineInterface,
  file: string,
  ev: 'session.start' | 'session.measure',
  o: Outcome,
  changed: string[],
): Promise<void> => {
  try {
    const t = o.nowMs ?? (await readNow($))
    if (t === null) return
    const sid = o.sessionId !== undefined ? o.sessionId : await readSid($)
    const dropCounts: Drops = {}
    let anyDrop = false
    for (const reason of DROP_REASONS) {
      const count = o.drops[reason]
      if (count === undefined || count === 0) continue
      dropCounts[reason] = count
      anyDrop = true
    }
    const entry = {
      t,
      ev,
      sid,
      n: o.n,
      kinds: o.kinds,
      kept: o.kept,
      out: o.out,
      why: o.why,
      ...(anyDrop ? { drops: dropCounts } : {}),
      held: o.held,
      changed,
    }
    let existing: unknown = null
    try {
      existing = await $.fs.read(file)
    } catch {
      // 不存在或读不了：当作空日志。
    }
    const events = readEvents(existing)
    events.push(entry)
    await $.fs.write(file, JSON.stringify({ schema: 1, events: events.slice(-EVENT_CAP) }, null, 2))
  } catch {
    // 日志失败静默。
  }
}

export const register: Register = (on, options) => {
  // 配置改动会重载模块、重新调用 register，所以安全闸在一次加载内不会变：
  // 永远写不出去的 hook 只是多余的面，不注册它。
  const target = resolveTarget(options?.dataDir, SNAPSHOT_FILE)
  const eventsTarget = resolveTarget(options?.dataDir, EVENTS_FILE)
  if (target === null || eventsTarget === null) return

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    try {
      let list: unknown
      let usageErr: string | null = null
      try {
        const usage: unknown = await $.session.usage()
        list = isRecord(usage) ? usage.rateLimits : undefined
      } catch (err) {
        usageErr = errName(err)
      }
      const outcome: Outcome =
        usageErr === null
          ? await feed($, target, list)
          : {
              n: 0,
              kinds: [],
              kept: 0,
              drops: {},
              held: [],
              out: 'skipped',
              why: 'usage_error:' + usageErr,
              nowMs: null,
              sessionId: undefined,
            }
      await logEvent($, eventsTarget, 'session.start', outcome, [])
    } catch {
      // 失败静默：不抛出，不改变事件结果。
    }
    return r
  })

  on('session.measure', async ($, e, next) => {
    const r = await next(e)
    try {
      // 每个回合后都会触发，数值没变也写一次，用来刷新 observed_at。
      // 空列表交给 feed：它返回 no_rate_limits，不写快照，也不做用量相关的引擎调用。
      const outcome = await feed($, target, e.rateLimits)
      await logEvent($, eventsTarget, 'session.measure', outcome, changedOf(e.changed))
    } catch {
      // 失败静默：不抛出，不改变事件结果。
    }
    return r
  })
}
