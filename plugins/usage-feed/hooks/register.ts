// usage-feed: 把订阅账号的限额窗口（5 小时、7 天、spend_limit）合并后写进本地快照文件，
// 供任务栏小窗读取。多个写入方（例如同时打开的多个会话）可以同时写同一个文件，
// 靠下面的合并规则避免用旧数覆盖新数。
//
// 任务栏小窗点 Refresh now 时会写请求文件。本插件按 REFRESH_POLL_MS 定时看一眼该文件，
// 发现新请求就向桌面应用要一次账号级用量（不发模型请求），再按合并规则更新快照、写确认。
// 终端会话没有这个服务，调用失败时只记事件日志、不写确认。
//
// 设计约束（只做没风险的事，不能拖垮电脑）。改动前先确认不破坏：
//  - 引擎调用面共九个：$.session.usage()（不带参数）、$.session.id()、$.clock.now()、
//    $.clock.every()、$.clock.after()、$.fs.read(path)、$.fs.write(path, text)、
//    $.fs.exists(path)、$.mcp.call()。$.mcp.call 只在发现新的刷新请求时才调，且只调
//    ccd_session_mgmt（或同一服务的另一种写法 ccd-session-mgmt）的 get_usage，参数为空对象，
//    不发模型请求。两个名字共用一个 APP_CALL_TIMEOUT_MS 的期限（见 callApp），不是各等各的。
//    定时检查（每 REFRESH_POLL_MS 一次）请求文件不存在时每期只做一次 $.fs.exists；
//    小窗点过一次之后请求文件一直在（小窗不删它，本插件也没有删文件的接口），此后每期再多一次
//    $.fs.read（API 没有 stat 或监听，读本身省不掉）；读到的文本与上一次处理完的完全相同就直接返回，
//    不再解析，不同才解析，id 已处理过也返回（读时钟失败的 id 不算处理过，文本也不记，每期再多一次 $.clock.now，
//    直到读到时钟为止）。$.clock.after 只用来给那一次尝试设超时，一次尝试最多布一个。
//    fs 现在碰四个文件，本插件写三个（用量快照、事件日志、确认），读一个（小窗写的刷新请求）：
//    <dataDir>/<SNAPSHOT_FILE>（用量快照）、<dataDir>/<EVENTS_FILE>（事件日志）、
//    <dataDir>/<REQUEST_FILE>（小窗写的刷新请求，只读）、<dataDir>/<ACK_FILE>（确认，本插件写）。
//    不碰进程、模型、提示、工具、子代理、命令、界面、store、state、网络，
//    不读写凭证、环境变量、设置文件。
//    读进来的文本先看长度再 JSON.parse：用量快照超过 MAX_SNAPSHOT_CHARS、事件日志超过 MAX_EVENTS_CHARS、
//    请求与确认超过 MAX_SMALL_CHARS、桌面应用返回的 JSON 文本超过 MAX_PAYLOAD_CHARS，一律当作不可用，
//    与解析失败走同一条路，别的写入方留下的大文件不会被每个 hook、每三秒解析一遍（小窗对它读的两个文件也设了上限）。
//  - 事件日志只用于排障。每次 hook 被调用记一条；处理一个新的刷新请求也记一条
//    （含因确认已存在或限频而跳过的情形，各记一条；请求太旧、来自未来、格式不对、
//    本会话已见过的静默忽略，不记）。最多保留最近 EVENT_CAP（200）条。每条只含：
//    t（时间，引擎时钟的毫秒数）、ev（事件名 session.start、session.measure、refresh.app）、
//    sid（会话 id）、n 与 kinds（引擎给的限额条数与种类）、kept（保留下来的窗口数）、
//    out 与 why（写 usage.json 的结果 wrote、skipped、failed，以及跳过或失败的原因）、
//    drops（被丢弃窗口的原因计数，没有丢弃时省略）、held（合并时保留了旧值的窗口）、
//    changed（session.measure 事件报告变化的度量项名，其余事件为空）。
//    不含路径、工作目录、提示词、模型名。取不到有限时间、或写日志失败时，这次调用什么都不记。
//    日志是 {"schema":1,"events":[ 开头的 JSON：每条紧凑一行、不缩进，最新的在最后（用量快照仍是缩进的）。已有日志非空却读不出（不是合法 JSON、结构不对、
//    超过大小上限）时，第一次遇到只记下这段文本的特征、这一次不写；下一次 hook 读到的仍是同一段才重新开始
//    （两次确认，见 logEvent 和下面的并发说明）。文件不存在、读失败或是空文件则直接从空日志开始。
//    重写时只留下是普通对象、且紧凑后不超过 MAX_EVENT_ENTRY_CHARS 的旧条目，别的写入方留下的臃肿日志不会被永久保留。
//  - 只挂 session.start 与 session.measure 两个 hook，不新增事件；在这两个 hook 里
//    （await next(e) 之后、原有逻辑之后）各检查一次定时检查是否在跑，没起就起，同一时刻只有一个
//    $.clock.every（热重载重新调用 register 时整体重置）；定时检查不重叠，
//    上一次还没结束就跳过这一期。
//  - $.clock.every 与 $.clock.after 都同步返回计时器，被链上的 hook 拒绝时不抛异常：
//    every 的某一期被拒绝，整个间隔静默结束；after 被拒绝，回调永不执行。插件无从直接得知，
//    所以做两道兜底，判断本身都不增加引擎调用：（1）两个 hook 用 feed 已经读到的时间（读不到就跳过）
//    和间隔回调的计数判断间隔是否已死，超过 WATCHDOG_MS 而回调一次都没触发就取消旧句柄重起一个；
//    （2）定时检查连续 BUSY_STUCK_PERIODS 期以上发现上一次处理还没结束，就认为那次卡住
//    （例如 after 被拒绝、调用又一直不返回），强制复位，旧的那次之后即使返回也不会动新一轮的状态。
//    两道兜底都不写事件日志（why 的取值是封闭的清单）。
//  - 定时器不主动收尾，也不挂 session.end。热重载重新调用 register 时，引擎随旧环境丢掉它挂起的计时器
//    （见 API 类型里 clock 的说明），进程退出时计时器随进程消失，而本插件每个模块副本至多一个间隔，所以只靠这两点。
//    session.end 在 /clear 和 resume 时也会触发，但那时进程和模块继续活着（API 文档明说 /clear 之后不再触发 session.start），
//    在那里取消间隔，点击就没人处理，直到下一个回合的 session.measure 才重起；所以不取消。这是有意的假设，别在 session.end 里补。
//  - 每个 hook 先 await next(e)，自己的逻辑整个包在 try/catch 里，最后原样返回 next 的结果：
//    任何失败都静默，绝不抛出，绝不改变事件结果。
//  - dataDir 安全闸见 resolveTarget：不合格就整个模块不注册任何 hook。
//
// 并发说明：可能同时运行多个会话，它们都会写同一个文件。"读快照、合并、写快照"
// 之间没有锁（设计约束禁止加锁等待），极端时序下会丢一次更新；每次事件带的都是全量窗口、
// 合并基本只增不减（同一会话自己的新读数、账号现取的读数是例外，见 merge 与 mergeApp），
// 下一次事件即自愈，所以不为它引入锁。
// 事件日志同样是"读、追加、写回"，没有锁，多个会话并发时可能丢记录；极端时序下读到
// 另一个会话写了一半的日志，readEvents 读不出，这一次不写（丢这一条），下一次读到写完的就接着追加，
// 日志不会因此清空；同一段读不出的文本连续见到两次才认定真坏了，那时才重新开始。
// 日志只用于排障，不为此引入锁。
// 引擎的写入接口不是原子写（整体覆盖）：读方已能容忍坏文件；写入失败（例如读方此刻
// 正开着文件）静默放弃，下一次事件再写。多个会话同时看到同一个刷新请求时可能都去调用一次
// get_usage，重复调用无害（只多一次不发模型请求的读取），靠确认文件里的 id 尽量避免：
// 先看到匹配确认的会话直接跳过。
import type { EngineInterface, Register } from 'claude-code'

// 这里是用量快照与事件日志的文件名；确认与请求的文件名在下面；本插件写三个文件、读一个，都与 dataDir 同目录。
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
// 读进来的文本先看长度再 JSON.parse，超过上限的当作不可用，与解析失败走同一条路。长度按字符数，不是字节数。
// 本插件自己写的文件都远小于这些数；上限防的是别的写入方留下的大文件被每个 hook、每三秒解析一遍。
// 小窗对它读的文件也设了上限（快照 1 MB、确认 4 KB）。
const MAX_SNAPSHOT_CHARS = 64 * 1024
const MAX_EVENTS_CHARS = 256 * 1024
const MAX_SMALL_CHARS = 4 * 1024 // 刷新请求与确认
const MAX_PAYLOAD_CHARS = 64 * 1024 // 桌面应用返回的 JSON 文本
// 重写事件日志时，已有条目紧凑后超过这么长就丢掉；本插件自己写的一条最长不到 1 KB。
const MAX_EVENT_ENTRY_CHARS = 2 * 1024

// 与 dataDir 同目录：小窗写刷新请求，本插件写确认。
const REQUEST_FILE = 'refresh-request.json'
const ACK_FILE = 'refresh-ack.json'
// 定时检查间隔。请求文件不存在时每期只做一次 fs.exists；文件存在（小窗点过之后一直存在）才读它，避免每期空转读整份快照。
const REFRESH_POLL_MS = 3000
// 太旧的请求当作没有，避免启动后重放很久以前点过的刷新。
const REQUEST_MAX_AGE_S = 30
// 容忍请求时间略超本机时钟，吸收小窗与本进程之间的时钟偏差。
const REQUEST_FUTURE_SLOP_S = 5
// 下面这两个常量要与小窗（usage_widget.py）的刷新协议对齐，改其中一边时把另一边一起看。
// 本会话两次调用桌面应用的最小间隔，防止刷新按钮被连点时刷屏。它不能比小窗的重试节奏更长：
// 小窗等不到确认（APP_REFRESH_WAIT_SECONDS，8 秒）之后，再过 APP_REFRESH_RETRY_SECONDS（10 秒）就允许用户重试，
// 那次重试到达时离上一次调用至少已过 10 秒。间隔若更长（例如 30 秒），这次重试会被限频、得不到回应，
// 小窗又显示 no session，尽管桌面会话在、数据也是新的。失败或超时的调用同样算一次调用（见 handleRequest）。
const APP_CALL_GAP_MS = 10000
// 一次尝试等桌面应用的期限，到期就放弃，不拖住定时检查。小窗写出请求后只等 APP_REFRESH_WAIT_SECONDS
// （8 秒）的确认，而定时检查每 REFRESH_POLL_MS（3 秒）才看一次请求文件，最坏要等 3 秒才发现请求，所以尝试最多再等
// 5 秒：3 + 5 = 8，成功的调用最迟约在小窗放弃时写出确认。两个服务器名共用这一个期限（见 callApp）：
// 前一个慢慢失败、后一个再各等 5 秒的话，一次尝试可以拖到 10 秒，超过小窗的 8 秒。上限若更长（例如 15 秒），
// 一次耗时 8 到 15 秒的调用会在插件这边成功（写了 usage.json 和确认），而小窗早已显示 no session。
const APP_CALL_TIMEOUT_MS = 5000
// 同一服务的两种写法，依次尝试；第一个调用没抛异常的就用，不是失败后重试。
const APP_SERVERS = ['ccd_session_mgmt', 'ccd-session-mgmt'] as const
const APP_TOOL = 'get_usage'
// 只看返回窗口列表的前这么多项，防止异常大的返回拖慢合并。
const MAX_APP_WINDOWS = 12
// 定时检查看门狗：已起的间隔在这么久里一次回调都没触发，就当它已被链上的 hook 拒绝而静默结束，重起一个。
const WATCHDOG_MS = 2 * REFRESH_POLL_MS
// 上一次处理连续挡掉超过这么多期（调用超时折成期数，再留两期余量），就当它卡住了，强制复位。
const BUSY_STUCK_PERIODS = Math.ceil(APP_CALL_TIMEOUT_MS / REFRESH_POLL_MS) + 2
// 请求 id 的白名单格式：短、可写进确认文件、不含路径或空白。
const ID_PATTERN = /^[A-Za-z0-9]{1,32}$/

// 窗口被丢弃的原因。写进日志时按这个顺序只列非零项，键序因此固定、便于肉眼对比。
const DROP_REASONS = ['not_object', 'unknown_kind', 'bad_percent', 'no_resets_at', 'bad_resets_at', 'duplicate_kind'] as const
type DropReason = (typeof DROP_REASONS)[number]
type Drops = { [R in DropReason]?: number }

type Fresh = { used_percentage: number; resets_at: number }
type Stored = { used_percentage: number; resets_at: number; observed_at: number; session_id: string | null }
type Windows<T> = { [K in Kind]?: T }
// 两边都有值时的决定：take 用本次读数（会话 id 也换成本次的）；confirm 保留已存的用量与重置时间，只刷新 observed_at 与会话 id；
// hold 保留已存的值并记入 held。
type Decision = 'take' | 'confirm' | 'hold'
// 同一周期（resets_at 相差不超过 SAME_PERIOD_SECONDS）时的决定，merge 与 mergeApp 只在这里不同。
type SamePeriod = (n: Fresh, o: Stored, sessionId: string | null) => Decision
type MergeResult = { windows: Windows<Stored>; held: Kind[] }

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

// 一次结果的默认值只在这里写一次。默认是 skipped：没写快照、没读到时钟、没读到会话 id 是最常见的情形。
// 键的顺序与原先的字面量相同，...o 只覆盖取值、不改顺序。日志的键序由 logEvent 自己排定，与这里无关。
const outcome = (o: Partial<Outcome> = {}): Outcome => ({
  n: 0,
  kinds: [],
  kept: 0,
  drops: {},
  held: [],
  out: 'skipped',
  why: '',
  nowMs: null,
  sessionId: undefined,
  ...o,
})

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

// 一个窗口的用量与重置时间是否可用：可用就返回快照窗口（resets_at 取整到秒），否则返回丢弃原因。
// 引擎窗口与桌面应用窗口共用这一套规则，两边的判断顺序也一样。
const freshFrom = (percentUsed: unknown, resetsAt: unknown): Fresh | DropReason => {
  const used = finite(percentUsed)
  if (used === null || used < 0) return 'bad_percent'
  if (typeof resetsAt !== 'string') return 'no_resets_at'
  const ms = Date.parse(resetsAt)
  if (!Number.isFinite(ms)) return 'bad_resets_at'
  const resets = Math.floor(ms / 1000)
  if (!(resets > 0)) return 'bad_resets_at'
  return { used_percentage: used, resets_at: resets }
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
    const f = freshFrom(w.percentUsed, w.resetsAt)
    if (typeof f === 'string') {
      noteDrop(drops, f)
      continue
    }
    windows[kind] = f
  }
  return { windows, drops }
}

// 读进来的文本先看长度再 JSON.parse。不是字符串、超过 maxChars（按字符数）、解析失败都返回 null（不可用）。
// stripBom 为真时去掉开头的一个 BOM（只去一个：两个就不可用）；别的写入方或编辑器可能留下它。
// 返回 null 也可能是合法 JSON 的 null，调用方一律只看它是不是对象，所以两者不必区分。
const parseJson = (text: unknown, maxChars: number, stripBom = true): unknown => {
  if (typeof text !== 'string' || text.length > maxChars) return null
  try {
    return JSON.parse(stripBom && text.charCodeAt(0) === 0xfeff ? text.slice(1) : text)
  } catch {
    return null
  }
}

// 读已有快照。读不出、超过 MAX_SNAPSHOT_CHARS、非 JSON、schema 不是 1、windows 不是对象都当作没有；
// 单个窗口无效当作该窗口不存在。observed_at 缺失时用 written_at 兜底，再不行记 0
// （表示年龄未知、很旧），不能记成现在，否则会把来历不明的数据伪装成刚确认过的。
const readStored = (text: unknown): Windows<Stored> => {
  const out: Windows<Stored> = {}
  // 已有快照可能带开头的 BOM（别的写入方或编辑器留下的），这里同样容忍。
  const obj = parseJson(text, MAX_SNAPSHOT_CHARS)
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

// 同一周期内 merge 的决定：用量只增不减，更小的是旧数；相等时取新，用来刷新 observed_at。
const mergeSame: SamePeriod = (n, o) => (n.used_percentage >= o.used_percentage ? 'take' : 'hold')

// 逐窗口合并的循环，merge 与 mergeApp 共用，只有两件事由参数决定：同一周期内怎么办（samePeriod），以及
// 会话 id 相同时是否一律取新（sessionWins）。缺席的一侧原样保留或直接取新；两边都有值时：
// 先看会话（sessionWins 为真，且新读数与已存窗口的 session_id 相同、都不是 null），再看周期：
// 同一周期交给 samePeriod；不同周期更晚的取新（百分比小也取新），更早的留旧。
// held 只收录"本次有新读数、但留下了旧值"的 kind，按 KINDS 顺序。
const mergeWindows = (
  old: Windows<Stored>,
  fresh: Windows<Fresh>,
  now: number,
  sessionId: string | null,
  samePeriod: SamePeriod,
  sessionWins: boolean,
): MergeResult => {
  const merged: Windows<Stored> = {}
  const held: Kind[] = []
  for (const kind of KINDS) {
    const o = old[kind]
    const n = fresh[kind]
    if (n === undefined) {
      // 只有已有的：原样保留（含 observed_at、session_id）。
      if (o !== undefined) merged[kind] = o
      continue
    }
    if (o === undefined) {
      merged[kind] = stored(n.used_percentage, n.resets_at, now, sessionId)
      continue
    }
    let decision: Decision
    if (sessionWins && sessionId !== null && o.session_id === sessionId) decision = 'take'
    else if (Math.abs(n.resets_at - o.resets_at) <= SAME_PERIOD_SECONDS) decision = samePeriod(n, o, sessionId)
    else decision = n.resets_at > o.resets_at ? 'take' : 'hold'
    if (decision === 'hold') {
      merged[kind] = o
      held.push(kind)
    } else if (decision === 'confirm') {
      merged[kind] = stored(o.used_percentage, o.resets_at, now, sessionId)
    } else {
      merged[kind] = stored(n.used_percentage, n.resets_at, now, sessionId)
    }
  }
  return { windows: merged, held }
}

// merge 的规则：闲置会话重跑时拿到的是旧数，不能覆盖已确认的新数。
// 例外：新读数的会话 id 与已存窗口的 session_id 相同（都不是 null）时一律取新，不论周期。同一个会话的读数有先后之分，
// 这一条一定比它自己写的上一条新；套餐升级之类让上限变高时，同一周期里百分比会掉下来，
// 不放行的话要等到 resets_at 往后走（seven_day 最长七天）才显示新的数。别的会话写的更高的数仍然挡住更低的读数。
// 两边的 id 只要有一个读不到（null），就不知道是不是同一个会话，不算。
const merge = (old: Windows<Stored>, fresh: Windows<Fresh>, now: number, sessionId: string | null): MergeResult =>
  mergeWindows(old, fresh, now, sessionId, mergeSame, true)

// 把请求或确认文件的文本解析成对象。不是字符串、超过 MAX_SMALL_CHARS、带坏 JSON、或根不是对象（含数组、null）都返回 null。
// 只去掉开头一个 BOM，做法与 readStored 相同：别的写入方或编辑器可能留下它。
const parseObject = (text: unknown): Record<string, unknown> | null => {
  const obj = parseJson(text, MAX_SMALL_CHARS)
  return isRecord(obj) ? obj : null
}

// 小窗写的刷新请求。schema、id 白名单、有限的 requested_at 缺一不可，否则当作没有请求。
export const parseRequest = (text: unknown): { id: string; requestedAt: number } | null => {
  const obj = parseObject(text)
  if (obj === null || obj.schema !== 1) return null
  const id = obj.id
  if (typeof id !== 'string' || !ID_PATTERN.test(id)) return null
  const requestedAt = finite(obj.requested_at)
  if (requestedAt === null) return null
  return { id, requestedAt }
}

// 本插件写的确认。status 只要求是字符串，取值由调用方判断，这里不收窄。
export const parseAck = (text: unknown): { id: string; status: string } | null => {
  const obj = parseObject(text)
  if (obj === null || obj.schema !== 1) return null
  const id = obj.id
  if (typeof id !== 'string' || !ID_PATTERN.test(id)) return null
  const status = obj.status
  if (typeof status !== 'string') return null
  return { id, status }
}

// content 里第一个 type 为 text 且 text 为字符串的块解析出的对象。超过 MAX_PAYLOAD_CHARS、解析失败、
// 根不是对象都是 null，解析失败不拿后面的块补位。
const textPayload = (content: unknown): Record<string, unknown> | null => {
  if (!Array.isArray(content)) return null
  for (const block of content as unknown[]) {
    if (!isRecord(block) || block.type !== 'text' || typeof block.text !== 'string') continue
    // 不去 BOM：这里的文本与上面几个读取不同，原样保留这个差别。
    const obj = parseJson(block.text, MAX_PAYLOAD_CHARS, false)
    return isRecord(obj) ? obj : null
  }
  return null
}

// 从桌面应用一次调用的返回里取出用量对象。structuredContent 带着 plan 就用它，不再解析文本块。
// 它没有 plan 时再看文本块：MCP 约定文本块是同一份结果的 JSON 序列化，两边本应一致；结构化的那份缺了我们要的 plan、
// 文本块里却有，说明结构化的那份是别的形状，以文本块为准，否则会白白报一个 app_unavailable:none，
// 小窗显示 no limits，尽管数据就在文本里。两边都没有 plan 时维持原来的次序（结构化的优先），结果仍是 app_unavailable。
// 不看 isError，那是调用方的事。
export const extractPayload = (res: unknown): Record<string, unknown> | null => {
  if (!isRecord(res)) return null
  const structured = isRecord(res.structuredContent) ? res.structuredContent : null
  if (structured !== null && isRecord(structured.plan)) return structured
  const fromText = textPayload(res.content)
  if (fromText !== null && isRecord(fromText.plan)) return fromText
  return structured ?? fromText
}

// 把桌面应用窗口的 label 映射到账号总用量的 kind。先看 5-hour，再看 weekly：
// 一句标签同时含两者时归 five_hour。Weekly · Fable 和各模型的每周窗口不映射，它们不是账号总用量。
// 不 trim：空白差异说明标签不是预期格式。
const kindOfLabel = (label: string): 'five_hour' | 'seven_day' | null => {
  const text = label.toLowerCase()
  if (text.includes('5-hour')) return 'five_hour'
  if (text.startsWith('weekly') && text.includes('all models')) return 'seven_day'
  return null
}

// 桌面应用的 plan.windows -> 快照用的 Fresh。status 不是 'ok' 时仍照常映射窗口，
// 由调用方根据 status 决定要不要用。未映射上的项（不是对象、label 不是字符串、label 对不上）
// 直接跳过且不计丢弃；同一 kind 先到先得，后面的项即使本身无效也不计丢弃。
// 无效窗口不占 kind。只看前 MAX_APP_WINDOWS 项。
export const windowsFromApp = (
  payload: unknown,
): { status: string | null; fresh: Windows<Fresh>; drops: Drops; count: number } => {
  const plan = isRecord(payload) ? payload.plan : undefined
  if (!isRecord(plan)) return { status: null, fresh: {}, drops: {}, count: 0 }
  const status = clip(plan.status)
  const list = Array.isArray(plan.windows) ? (plan.windows as unknown[]) : []
  const count = Math.min(list.length, MAX_APP_WINDOWS)
  const fresh: Windows<Fresh> = {}
  const drops: Drops = {}
  for (let i = 0; i < count; i++) {
    const w = list[i]
    if (!isRecord(w)) continue
    if (typeof w.label !== 'string') continue
    const kind = kindOfLabel(w.label)
    if (kind === null) continue
    // 先到先得：该 kind 已有有效窗口就整项忽略，后面这项无效也不计丢弃。
    if (fresh[kind] !== undefined) continue
    const f = freshFrom(w.percentUsed, w.resetsAt)
    if (typeof f === 'string') {
      noteDrop(drops, f)
      continue
    }
    fresh[kind] = f
  }
  return { status, fresh, drops, count }
}

// 同一周期内 mergeApp 的决定：账号值低于已存值、且相差不到 1 点，视为同一个读数的取整差，保留已存的用量与重置时间并确认；
// 其余（账号值不低，或低了 1 点以上）取账号值。
const mergeAppSame: SamePeriod = (n, o) =>
  n.used_percentage < o.used_percentage && o.used_percentage - n.used_percentage < 1 ? 'confirm' : 'take'

// 把桌面应用的账号读数并进已有快照。与 merge 共用 mergeWindows，只有同一周期内的决定不同：账号读数是整数、会话读数最多一位小数，
// 规则不同。held 只收录"本次有新读数、但留下了旧值"的 kind，按 KINDS 顺序。
// 同一周期里，账号值不低于已存值就取账号值。否则若两者相差小于 1，视为同一个读数的取整差并确认：
// 朴素的"只增不减"会把刚确认过的整数读数（例如会话读数 66.4、账号读数 66）当成旧数挡掉，
// observed_at 就不刷新，小窗会一直显示旧时间。确认时保留已存的用量值和重置时间，只刷新
// observed_at 与 session_id，不进 held。相差达到 1 时账号值取代已存值：账号值是点击那一刻现取的，
// 比已存值低一个点以上，说明已存的是过时的数。典型的是套餐升级、上限变高，同一周期里百分比掉下来，
// 若还留着旧值，要等 resets_at 往后走（seven_day 最长七天）才显示新的数。held 因此只剩"账号读数属于更早的周期"一种。
// 这里假定 get_usage 返回的是实时数；若它其实缓存了几分钟，一次点击可能把刚写的会话读数换成稍旧的账号值，下一次会话读数会改回来。
export const mergeApp = (
  old: Windows<Stored>,
  fresh: Windows<Fresh>,
  now: number,
  sessionId: string | null,
): MergeResult => mergeWindows(old, fresh, now, sessionId, mergeAppSame, false)

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

// 解析已有日志。读不出（不是字符串、超过 MAX_EVENTS_CHARS、不是合法 JSON、schema 不是 1、events 不是数组）
// 返回 null，与"读出来是空列表"区分开：调用方对前者要多一道确认才重新开始（见 logEvent）。
// 去掉开头一个 BOM，做法与 readStored 相同：别的写入方或编辑器可能留下它，不能因此把整份日志判为坏的。
const readEvents = (text: unknown): unknown[] | null => {
  const obj = parseJson(text, MAX_EVENTS_CHARS)
  if (!isRecord(obj) || obj.schema !== 1) return null
  const events = obj.events
  if (!Array.isArray(events)) return null
  return events
}

// 读不出的日志文本的特征（长度加一个遍历全文的散列），用来认出"又是同一段"。不留整段文本：它可能有几 MB；
// 也不取首尾各一截：只改了中间的两段文本会撞上，截下来的子串还可能连带着让整段大文本一直不被回收。
// 只在日志读不出时才算，最多算两次（第一次记下，第二次认出之后就重新开始）。
const textMark = (text: string): string => {
  let h = 5381
  for (let i = 0; i < text.length; i++) h = (Math.imul(h, 33) + text.charCodeAt(i)) | 0
  return text.length + ':' + h
}

// 已有条目里只留普通对象、且紧凑后不超过 MAX_EVENT_ENTRY_CHARS 的。别的写入方留下的臃肿日志
// （非对象的条目、塞了大块内容的条目）不会因为"只留最近 EVENT_CAP 条"而被永久保留。
const tidyEvents = (list: unknown[]): unknown[] => {
  const out: unknown[] = []
  for (const item of list) {
    if (!isRecord(item)) continue
    if (JSON.stringify(item).length > MAX_EVENT_ENTRY_CHARS) continue
    out.push(item)
  }
  return out
}

// 读旧快照、按 mergeFn 合并、整体写回：feed 与 refreshFromApp 共用，引擎调用的顺序是 读、写。
// 读不出当作没有旧快照；写失败不抛出，由返回值的 failed 告诉调用方。held 在两种情形下都返回。
// 时钟与会话 id 由调用方先读好再传进来，顺序在调用方那边决定。
const writeMerged = async (
  $: EngineInterface,
  target: string,
  fresh: Windows<Fresh>,
  now: number,
  sessionId: string | null,
  mergeFn: typeof merge,
): Promise<{ held: Kind[]; failed: false } | { held: Kind[]; failed: true; err: unknown }> => {
  let existing: unknown = null
  try {
    existing = await $.fs.read(target)
  } catch {
    // 不存在或读不了：当作没有已有快照。
  }
  const merged = mergeFn(readStored(existing), fresh, now, sessionId)
  try {
    await $.fs.write(target, JSON.stringify({ schema: 1, written_at: now, windows: merged.windows }, null, 2))
  } catch (err) {
    return { held: merged.held, failed: true, err }
  }
  return { held: merged.held, failed: false }
}

// 采一次：转换、取时间与会话 id、读旧快照、合并、整体写回。
// 返回结果对象供 logEvent 记录。预期内的失败（没有窗口、窗口全被丢、时钟坏、
// 取会话 id 失败、读旧快照失败、写快照失败）不再抛出。
const feed = async ($: EngineInterface, target: string, list: unknown): Promise<Outcome> => {
  const n = Array.isArray(list) ? list.length : 0
  const kinds = kindsOf(list)
  if (n === 0) return outcome({ n, kinds, why: 'no_rate_limits' })
  const converted = fromEngine(list)
  const fresh = converted.windows
  const drops = converted.drops
  const kept = Object.keys(fresh).length
  if (kept === 0) return outcome({ n, kinds, drops, why: 'all_dropped' })
  const nowMs = await readNow($)
  if (nowMs === null) return outcome({ n, kinds, kept, drops, why: 'bad_clock' })
  const sessionId = await readSid($)
  const written = await writeMerged($, target, fresh, nowMs / 1000, sessionId, merge)
  if (written.failed) {
    return outcome({
      n,
      kinds,
      kept,
      drops,
      held: written.held,
      out: 'failed',
      why: 'write_error:' + errName(written.err),
      nowMs,
      sessionId,
    })
  }
  return outcome({ n, kinds, kept, drops, held: written.held, out: 'wrote', nowMs, sessionId })
}

// 追加一条事件日志。整个函数永不抛出：日志失败不影响用量快照，也不改变事件结果。
// nowMs / sessionId 已由 feed 读到的就复用，只补读缺的，避免成功路径上时钟和会话 id 各读两次。
// 已有日志不存在、读失败或是空文件：从空日志开始。读出来了：先 tidyEvents，再追加，只留最近 EVENT_CAP 条。
// 非空却读不出：多半是另一个会话正好写到一半，所以先不动它。第一次遇到时在 ctx.badEventsMark 里记下这段文本的特征、
// 这一次不写（丢这一条）；下一次 hook 读到的仍是同一段，才认定它真的坏了，从空日志重新开始。
// 中间读到了好的日志（别的会话写完了）就照常追加，记号随下一次成功的写入清掉。
const logEvent = async (
  $: EngineInterface,
  ctx: WatchCtx,
  file: string,
  ev: 'session.start' | 'session.measure' | 'refresh.app',
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
    let events: unknown[] = []
    if (typeof existing === 'string' && existing !== '') {
      const parsed = readEvents(existing)
      if (parsed !== null) {
        events = tidyEvents(parsed)
      } else {
        const mark = textMark(existing)
        if (ctx.badEventsMark !== mark) {
          ctx.badEventsMark = mark
          return
        }
      }
    }
    events.push(entry)
    // 每条紧凑、占一行、不缩进：每个 hook 都要把整份日志读一遍、写一遍，缩进会让文件多出约七成；一行一条便于在普通编辑器里看。
    // 外层不用 JSON.stringify 整体写，因为那样条目之间没有换行。U+2028、U+2029 在 JSON 字符串里可以不转义，
    // 但编辑器和一些工具把它们当作换行，所以也转成转义写法（解析出来的值不变）。
    const lines = events
      .slice(-EVENT_CAP)
      .map((e) => JSON.stringify(e).replace(/[\u2028\u2029]/g, (c) => '\\u' + c.charCodeAt(0).toString(16)))
    await $.fs.write(file, '{"schema":1,"events":[\n' + lines.join(',\n') + '\n]}')
    ctx.badEventsMark = null
  } catch {
    // 日志失败静默。
  }
}

// 同一次模块加载里定时检查的会话内状态，放在 register 闭包里，热重载重新调用 register 时自然重置。
// started 保证同一时刻只有一个定时器，busy 保证检查不重叠，lastSeenId 保证同一个请求不被本会话重复处理，
// lastSeenText 是最近一次处理完的请求文件文本（不超过 MAX_SMALL_CHARS 的才记），下一期读到完全相同的文本就不再解析，
// lastCallAt 用来限频。badEventsMark 是 logEvent 第一次遇到读不出的事件日志时记下的文本特征（两次确认，见 logEvent）。
// 其余字段服务于两道兜底（见文件头）：timer 是当前间隔的句柄；ticks 是间隔回调
// 触发的总次数，只在回调里加一，用来判断间隔是否还活着；lastCheckAt 与 ticksAtCheck 是上一次判断时
// 记下的引擎时间与那一刻的 ticks；busyPeriods 是当前这次处理已经挡掉了几期；runId 标识当前这一次处理。
type WatchCtx = {
  started: boolean
  busy: boolean
  lastSeenId: string | null
  lastSeenText: string | null
  lastCallAt: number | null
  badEventsMark: string | null
  timer: unknown
  ticks: number
  lastCheckAt: number | null
  ticksAtCheck: number
  busyPeriods: number
  runId: number
}

// 超时的标记值：用 Symbol，不会与调用的真实返回值混淆。
const TIMED_OUT = Symbol('app_call_timed_out')

// 取消 $.clock.after 或 $.clock.every 返回的计时器。类型声明里它是带 cancel() 的对象，也容忍返回函数的形态。
// 取消失败静默：after 的计时器多触发一次时 race 早已落定，再 resolve 一次没有影响；
// every 的旧间隔多活一阵时，busy 与 lastSeenId 保证检查不重叠、同一个请求不会被重复处理。
const cancelTimer = (timer: unknown): void => {
  try {
    if (typeof timer === 'function') timer()
    else if (isRecord(timer) && typeof timer.cancel === 'function') timer.cancel()
  } catch {
    // 静默。
  }
}

// 一次尝试的期限：到点时 promise 以 TIMED_OUT 落定、passed() 变真；stop() 取消计时器。
// $.clock.after 被链上的 hook 拒绝时不抛异常、计时器永不触发，这一次尝试就没有期限；调用若又一直不返回，
// 靠 startWatcher 里的 busy 兜底复位（BUSY_STUCK_PERIODS）。
type Deadline = { promise: Promise<typeof TIMED_OUT>; passed: () => boolean; stop: () => void }

const startDeadline = ($: EngineInterface, ms: number): Deadline => {
  let isPassed = false
  let fire: () => void = () => {}
  const promise = new Promise<typeof TIMED_OUT>((resolve) => {
    fire = () => {
      isPassed = true
      resolve(TIMED_OUT)
    }
  })
  const timer: unknown = $.clock.after(ms, () => fire())
  return { promise, passed: () => isPassed, stop: () => cancelTimer(timer) }
}

// callApp 的结果。failed 的 err 是第一个服务器名抛的异常（两个名字都抛了）。
export type AppCall = { kind: 'answered'; res: unknown } | { kind: 'timeout' } | { kind: 'failed'; err: unknown }

// 向桌面应用要一次 get_usage：先 ccd_session_mgmt，抛异常再试 ccd-session-mgmt，两个名字共用同一个
// APP_CALL_TIMEOUT_MS 期限（一个计时器，先设计时器、再发调用）。不是各等各的：前一个慢慢失败、后一个再等满，
// 一次尝试会拖过小窗的 8 秒。期限到了就返回 timeout，哪怕是前一个失败之后才发现已到，也不再发下一个调用。
// 超时取消不了已发出的调用，所以给每个调用本身挂一个空 catch：它若在期限之后才失败，不能变成未处理的 promise 拒绝
// （Promise.race 也会给它挂上处理函数，这里不依赖这个实现细节）；才返回的结果也没人用，调用方已经放弃这一次，
// 什么都不写。结束时一定取消计时器。
// 两个名字都抛异常时 err 是第一个名字的：第一个才是正主，后一个多半只是"没有这个服务"，
// 让它覆盖掉前一个，日志里的 mcp_error:<错误名> 就把真正的原因藏起来了。
// $.clock.after 同步抛异常几乎不会发生；发生时一个调用都没发出，按失败处理（记成 mcp_error:<错误名>），
// 尽管 get_usage 一次都没发出。why 的取值是封闭的清单，所以不为它另设取值，排障时按此理解。
export const callApp = async ($: EngineInterface): Promise<AppCall> => {
  let deadline: Deadline
  try {
    deadline = startDeadline($, APP_CALL_TIMEOUT_MS)
  } catch (err) {
    return { kind: 'failed', err }
  }
  try {
    let firstErr: unknown = null
    let failures = 0
    for (const server of APP_SERVERS) {
      if (deadline.passed()) return { kind: 'timeout' }
      let res: unknown
      try {
        const call = (async () => $.mcp.call(server, APP_TOOL, {}))()
        call.catch(() => {})
        res = await Promise.race([call, deadline.promise])
      } catch (err) {
        if (failures === 0) firstErr = err
        failures += 1
        continue
      }
      return res === TIMED_OUT ? { kind: 'timeout' } : { kind: 'answered', res }
    }
    return { kind: 'failed', err: firstErr }
  } finally {
    deadline.stop()
  }
}

// 发现新的刷新请求之后：向桌面应用要一次账号级用量，合并进 usage.json，再写确认。永不抛出。
// 每个引擎调用单独接住异常，不用包住整个函数的总 catch，避免逼出清单之外的 why。
// usage.json 必须先于确认写：小窗看到确认后会立刻重读 usage.json，那时必须已经是新的。
// 调用抛异常、超时、isError、解析失败、时钟坏都不写确认：另一个会话可能马上成功，
// 失败的会话不能抢先写一个会让小窗误判的确认；小窗靠超时判断没有会话回应。
// unavailable 也要写确认：调用通了但没有可用数据，让小窗立刻显示结果，而不是干等超时。
const refreshFromApp = async (
  $: EngineInterface,
  target: string,
  ackTarget: string,
  requestId: string,
): Promise<Outcome> => {
  const call = await callApp($)
  if (call.kind === 'timeout') return outcome({ why: 'mcp_timeout' })
  if (call.kind === 'failed') return outcome({ why: 'mcp_error:' + errName(call.err) })
  const res = call.res
  if (isRecord(res) && res.isError === true) return outcome({ why: 'app_is_error' })
  const payload = extractPayload(res)
  if (payload === null) return outcome({ why: 'parse_failed' })
  const { status, fresh, drops, count } = windowsFromApp(payload)
  const kinds: string[] = []
  for (const kind of KINDS) {
    if (fresh[kind] !== undefined) kinds.push(kind)
  }
  const kept = kinds.length
  const nowMs = await readNow($)
  if (nowMs === null) return outcome({ n: count, kinds, kept, drops, why: 'bad_clock' })
  const now = nowMs / 1000
  if (status !== 'ok' || kept === 0) {
    const why = status !== 'ok' ? 'app_unavailable:' + (status ?? 'none').slice(0, 24) : 'no_windows'
    try {
      await $.fs.write(
        ackTarget,
        JSON.stringify({ schema: 1, id: requestId, status: 'unavailable', at: now, windows: 0 }, null, 2),
      )
    } catch (err) {
      return outcome({ n: count, kinds, kept, drops, out: 'failed', why: 'ack_write_error:' + errName(err), nowMs })
    }
    return outcome({ n: count, kinds, kept, drops, why, nowMs })
  }
  const sessionId = await readSid($)
  const written = await writeMerged($, target, fresh, now, sessionId, mergeApp)
  if (written.failed) {
    // usage.json 没写上就不写确认，避免小窗读到旧快照却以为已经刷新。
    return outcome({
      n: count,
      kinds,
      kept,
      drops,
      held: written.held,
      out: 'failed',
      why: 'write_error:' + errName(written.err),
      nowMs,
      sessionId,
    })
  }
  // windows 记的是映射成功的账号窗口数（含合并时保留了旧值的），不是 usage.json 里的窗口总数。
  // 小窗只读确认里的 schema、id、status。
  try {
    await $.fs.write(
      ackTarget,
      JSON.stringify({ schema: 1, id: requestId, status: 'ok', at: now, windows: kept }, null, 2),
    )
  } catch (err) {
    return outcome({
      n: count,
      kinds,
      kept,
      drops,
      held: written.held,
      out: 'failed',
      why: 'ack_write_error:' + errName(err),
      nowMs,
      sessionId,
    })
  }
  return outcome({ n: count, kinds, kept, drops, held: written.held, out: 'wrote', nowMs, sessionId })
}

// 记下这段请求文本已经有了定论（无效、已见过、太旧或已处理），下一期读到完全相同的文本就不再解析。
// 超过 MAX_SMALL_CHARS 的不记：它在 parseObject 的长度检查就被挡掉，下一期同样一眼挡掉，不必占内存。
const noteRequestText = (ctx: WatchCtx, text: unknown): void => {
  if (typeof text === 'string' && text.length <= MAX_SMALL_CHARS) ctx.lastSeenText = text
}

// 看一眼请求文件。请求文件不存在时只做一次 exists；小窗点过一次之后文件一直在，每期还会读一次（API 没有 stat 或监听，
// 读本身省不掉），读到的文本与上一次处理完的完全相同就直接返回，不再解析；不同才解析，id 已处理过也返回。
// 永不抛出：任何异常直接结束，不能冒进定时回调。
// 读到时钟之后、判断年龄、确认和限频之前记已见：同一个请求不会被本会话重复处理，哪怕之后被跳过或调用失败。
// 读时钟失败时这个请求连年龄都还没判断过，不算处理过，所以不记已见、文本也不记，下一期再来一遍；否则一次时钟故障就会让
// 这次点击被永久吞掉，小窗对着一个从未尝试过的请求显示 no session。重试受 REQUEST_MAX_AGE_S 约束：时钟恢复得
// 太晚，请求按太旧静默忽略（同时记已见）。时钟一直坏的代价只是每期多一次 $.clock.now，不调用、不写文件、不记日志。
// 太旧或来自未来的请求静默忽略，不记日志：多半是重启后残留的旧文件，记了只是噪声。
// lastCallAt 记在调用之前：这次调用失败也算一次，连点不会把桌面应用打爆。
// 限频用本会话两次 nowMs 之差，与请求年龄同一个引擎时钟，不另取别的时间。
const handleRequest = async (
  $: EngineInterface,
  ctx: WatchCtx,
  requestTarget: string,
  ackTarget: string,
  target: string,
  eventsTarget: string,
): Promise<void> => {
  try {
    if (!(await $.fs.exists(requestTarget))) return
    const text = await $.fs.read(requestTarget)
    if (typeof text === 'string' && text === ctx.lastSeenText) return
    const req = parseRequest(text)
    if (req === null) {
      noteRequestText(ctx, text)
      return
    }
    if (req.id === ctx.lastSeenId) {
      noteRequestText(ctx, text)
      return
    }
    const nowMs = await readNow($)
    // 读不到时钟：什么都不做、不记已见、文本也不记，下一期再试（理由见上面的说明）。
    if (nowMs === null) return
    ctx.lastSeenId = req.id
    noteRequestText(ctx, text)
    // 恰为 REQUEST_MAX_AGE_S 仍处理，只有更旧才忽略；恰为 -REQUEST_FUTURE_SLOP_S 仍处理。
    const age = nowMs / 1000 - req.requestedAt
    if (age > REQUEST_MAX_AGE_S || age < -REQUEST_FUTURE_SLOP_S) return
    let ackText: unknown = null
    try {
      ackText = await $.fs.read(ackTarget)
    } catch {
      // 不存在或读不了：当作没有。不先 exists，少一次调用。
    }
    const ack = parseAck(ackText)
    if (ack !== null && ack.id === req.id) {
      await logEvent($, ctx, eventsTarget, 'refresh.app', outcome({ why: 'duplicate_ack', nowMs }), [])
      return
    }
    // 恰好相差 APP_CALL_GAP_MS 就处理，只有更短才跳过。差为负说明系统时间被往回调过，限频期当作已过，
    // 否则要等时钟重新越过上次调用时刻再加 APP_CALL_GAP_MS，期间每个新请求都被限频、小窗一直显示没有会话。
    if (ctx.lastCallAt !== null) {
      const sinceLast = nowMs - ctx.lastCallAt
      if (sinceLast >= 0 && sinceLast < APP_CALL_GAP_MS) {
        await logEvent($, ctx, eventsTarget, 'refresh.app', outcome({ why: 'throttled', nowMs }), [])
        return
      }
    }
    ctx.lastCallAt = nowMs
    const result = await refreshFromApp($, target, ackTarget, req.id)
    await logEvent($, ctx, eventsTarget, 'refresh.app', result, [])
  } catch {
    // 任何异常直接结束。
  }
}

// 第一次调用布下定时器，之后同一时刻只留一个。$.clock.every 被链上的 hook 拒绝时不抛异常，
// try/catch 发现不了，靠看门狗：调用方带来的引擎时间加上回调计数。nowMs 为 null 就整个跳过，
// 不为判断再读时钟。时间往回走只重记基线，不下结论。误判最多是取消一个还活着的间隔再起一个，
// 重起是幂等的。只有 $.clock.every 本身同步抛异常才把 started 置回 false，让下一个事件再试一次。
// busy 表示上一期还没结束。连续挡掉超过 BUSY_STUCK_PERIODS 期就认为那次卡住，强制复位；
// runId 保证旧的一次之后返回不会清掉新一轮的 busy。回调里的异常不能逃出，也不能变成未处理的拒绝。
const startWatcher = (
  $: EngineInterface,
  ctx: WatchCtx,
  requestTarget: string,
  ackTarget: string,
  target: string,
  eventsTarget: string,
  nowMs: number | null,
): void => {
  if (ctx.started) {
    // 已经起过：只在能确认间隔已死时重起。没有时间就什么都不做，不为此增加引擎调用。
    if (nowMs === null) return
    const gap = ctx.lastCheckAt === null ? null : nowMs - ctx.lastCheckAt
    // 没有基线，或时间往回走（差为负）：只重记基线，不下结论。
    if (gap === null || gap < 0) {
      ctx.lastCheckAt = nowMs
      ctx.ticksAtCheck = ctx.ticks
      return
    }
    // 恰为 WATCHDOG_MS 还不够，要严格超过。
    if (gap <= WATCHDOG_MS) return
    // 回调在走：间隔还活着，只把基线推到现在。
    if (ctx.ticks !== ctx.ticksAtCheck) {
      ctx.lastCheckAt = nowMs
      ctx.ticksAtCheck = ctx.ticks
      return
    }
    // 超过两个周期而回调一次都没触发：取消旧句柄，往下重起。
    cancelTimer(ctx.timer)
    ctx.timer = null
  }
  ctx.started = true
  ctx.lastCheckAt = nowMs
  ctx.ticksAtCheck = ctx.ticks
  const tick = async (): Promise<void> => {
    try {
      if (ctx.busy) {
        ctx.busyPeriods += 1
        // 挡掉的期数还没超过上限：上一次可能只是慢，这一期跳过。
        if (ctx.busyPeriods <= BUSY_STUCK_PERIODS) return
        // 超过上限：认定上一次卡住，落到下面强制复位并处理这一期。
      }
      ctx.busyPeriods = 0
      ctx.busy = true
      ctx.runId += 1
      const run = ctx.runId
      try {
        await handleRequest($, ctx, requestTarget, ackTarget, target, eventsTarget)
      } finally {
        // 被强制复位的旧一次之后才返回时，busy 已经属于新一轮，不能被它清掉。
        if (ctx.runId === run) ctx.busy = false
      }
    } catch {
      // 静默。
    }
  }
  try {
    ctx.timer = $.clock.every(REFRESH_POLL_MS, () => {
      try {
        // 先计数、再看 busy：ticks 表示间隔还活着，与这一期有没有干活无关。
        ctx.ticks += 1
        void tick()
      } catch {
        // 同步异常同样吞掉。
      }
    })
  } catch {
    // $.clock.every 本身同步抛异常（被 hook 拒绝不会走到这里，那种情形由上面的看门狗处理）：
    // started 置回去，让下一个事件再试一次。
    ctx.started = false
    ctx.timer = null
  }
}

export const register: Register = (on, options) => {
  // 配置改动会重载模块、重新调用 register，所以安全闸在一次加载内不会变：
  // 永远写不出去的 hook 只是多余的面，不注册它。
  const target = resolveTarget(options?.dataDir, SNAPSHOT_FILE)
  const eventsTarget = resolveTarget(options?.dataDir, EVENTS_FILE)
  const requestTarget = resolveTarget(options?.dataDir, REQUEST_FILE)
  const ackTarget = resolveTarget(options?.dataDir, ACK_FILE)
  if (target === null || eventsTarget === null || requestTarget === null || ackTarget === null) return

  const ctx: WatchCtx = {
    started: false,
    busy: false,
    lastSeenId: null,
    lastSeenText: null,
    lastCallAt: null,
    badEventsMark: null,
    timer: null,
    ticks: 0,
    lastCheckAt: null,
    ticksAtCheck: 0,
    busyPeriods: 0,
    runId: 0,
  }

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    let nowMs: number | null = null
    try {
      let list: unknown
      let usageErr: string | null = null
      try {
        const usage: unknown = await $.session.usage()
        list = isRecord(usage) ? usage.rateLimits : undefined
      } catch (err) {
        usageErr = errName(err)
      }
      const result: Outcome =
        usageErr === null ? await feed($, target, list) : outcome({ why: 'usage_error:' + usageErr })
      nowMs = result.nowMs
      await logEvent($, ctx, eventsTarget, 'session.start', result, [])
    } catch {
      // 失败静默：不抛出，不改变事件结果。
    }
    try {
      startWatcher($, ctx, requestTarget, ackTarget, target, eventsTarget, nowMs)
    } catch {
      // 失败静默：不抛出，不改变事件结果。
    }
    return r
  })

  on('session.measure', async ($, e, next) => {
    const r = await next(e)
    let nowMs: number | null = null
    try {
      // 每个回合后都会触发，数值没变也写一次，用来刷新 observed_at。
      // 空列表交给 feed：它返回 no_rate_limits，不写快照，也不做用量相关的引擎调用。
      const outcome = await feed($, target, e.rateLimits)
      nowMs = outcome.nowMs
      await logEvent($, ctx, eventsTarget, 'session.measure', outcome, changedOf(e.changed))
    } catch {
      // 失败静默：不抛出，不改变事件结果。
    }
    try {
      startWatcher($, ctx, requestTarget, ackTarget, target, eventsTarget, nowMs)
    } catch {
      // 失败静默：不抛出，不改变事件结果。
    }
    return r
  })
}
