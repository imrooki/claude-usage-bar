// usage-feed 的行为测试（claude plugin test 运行）。
// 测试环境没有真实的 fs、网络、进程：被测模块的每个引擎调用都落到下面 makeWorld 注册的
// 底层 hook 上，文件在内存里模拟，所以这里任何一次"写"都不会碰磁盘。
// 引擎宿主会把传给 fs 的路径换成反斜杠，所以断言路径时统一用 norm 换回正斜杠；
// 模块自己传出的原始字符串（正斜杠、去末尾斜杠）不在这里断言。
import { test, expect, mock } from 'claude-code/testing'
import type { Engine, MockClock } from 'claude-code/testing'
import type { EngineInterface, On } from 'claude-code'
import { parseRequest, parseAck, extractPayload, windowsFromApp, mergeApp, callApp } from '../hooks/register.ts'

// 假路径，只是内存假文件系统里的键，不对应任何真实目录。
const DATA_DIR = 'D:/usage-feed-test/mem'
const TARGET = DATA_DIR + '/usage.json'
const T0 = 1_800_000_000 // 2027-01-15T08:00:00Z，纪元秒
const NOW_MS = T0 * 1000
const R5 = T0 + 3 * 3600 // 5 小时窗口的重置时刻基准
const R7 = T0 + 5 * 86400 // 7 天窗口的重置时刻基准
const START_MARK = 'engine-echo:'
const CHANGED = ['rateLimits', 'cost']
// 引擎宿主装载模块时产生的噪声事件，不是被测模块发起的调用。
const BOOT_NOISE = new Set(['engine.create', 'plugin.register', 'ui.resolve'])
const FEED_CALLS = ['clock.now', 'session.id', 'fs.read', 'fs.write']

// 引擎宿主会把"在本机上不是绝对路径"的路径放到插件目录之下：Linux 上 D:/... 不是绝对路径，fs 事件里的路径就成了
// <插件目录>/D:/usage-feed-test/mem/...；Windows 上 D:/ 本身是绝对路径，原样传入。所以 norm 还要去掉第一个盘符之前的
// 部分，让两种宿主上的假文件系统用同一个键（否则同一个文件写入和读出的键不同，一半以上的用例读不到自己刚写的文件）。
// 插件自己的路径安全闸（resolveTarget，要求 X:/ 开头）不受影响，也不为迁就测试而放宽。
const norm = (p: string): string => p.replace(/\\/g, '/').replace(/^.*?(?=[A-Za-z]:\/)/, '')
const iso = (sec: number): string => new Date(sec * 1000).toISOString()
const lim = (kind: string, pct: number, resetsSec: number) => ({ kind, percentUsed: pct, resetsAt: iso(resetsSec) })
const ow = (used: unknown, resets: unknown, observed?: unknown, sid?: unknown) => ({
  used_percentage: used,
  resets_at: resets,
  observed_at: observed,
  session_id: sid,
})
// 期望的快照窗口。
const win = (used: number, resets: number, observed: number, sid: string | null) => ({
  used_percentage: used,
  resets_at: resets,
  observed_at: observed,
  session_id: sid,
})

// 两个夹具共用的底层钩子：内存文件、fs.read / fs.write、会话 id 与用量，以及 session.start / session.measure 的回显。
// 各夹具自己的失败注入由 faults 传进来：refuse 决定一次拒绝的形式，其余判断某次调用是否被拒。路径一律先 norm。
type Shared = {
  files: Map<string, string>
  writes: { path: string; text: string }[]
  reads: string[]
  sessionId: unknown
  usageLimits: unknown[]
  lastStart: unknown
  lastMeasure: unknown
}
type Faults = {
  refuse: (what: string) => { deny: string }
  readRefused: (path: string) => boolean
  writeRefused: (path: string) => boolean
  sessionRefused: (what: 'id' | 'usage') => boolean
}
const sharedHooks = (on: On, s: Shared, f: Faults): void => {
  on('session.id', () => (f.sessionRefused('id') ? f.refuse('id') : { value: s.sessionId as string }))
  on('session.usage', () =>
    f.sessionRefused('usage')
      ? f.refuse('usage')
      : { value: { startedAt: 0, context: { window: 200000 }, rateLimits: s.usageLimits as never } },
  )
  on('fs.read', (_$, e) => {
    const p = norm(e.path)
    s.reads.push(p)
    if (f.readRefused(p)) return f.refuse('read')
    const text = s.files.get(p)
    return text === undefined ? { deny: 'ENOENT' } : { value: text }
  })
  on('fs.write', (_$, e) => {
    const p = norm(e.path)
    if (f.writeRefused(p)) return f.refuse('write')
    s.writes.push({ path: p, text: e.text })
    s.files.set(p, e.text)
    return { value: undefined }
  })
  on('session.start', (_$, e) => {
    const value = { cwd: START_MARK + e.cwd }
    s.lastStart = value
    return value
  })
  on('session.measure', (_$, e) => {
    const value = { changed: e.changed }
    s.lastMeasure = value
    return value
  })
}

// 底层 hook：代表引擎，应答被测模块的每个调用，并把"经过的事件"全部记下来。
const makeWorld = (on: On) => {
  const w = {
    nowMs: NOW_MS as unknown,
    sessionId: 'sess-new' as unknown,
    usageLimits: [] as unknown[],
    files: new Map<string, string>(),
    events: [] as string[],
    writes: [] as { path: string; text: string }[],
    timers: [] as number[], // 被测模块每次 $.clock.every 的间隔（毫秒）；不进 events，原因见下面的记录器
    reads: [] as string[], // 每次 fs.read 的 norm 路径，按调用顺序；被拒绝的读也要记
    failReadPaths: new Set<string>(), // 这些路径（norm 形式）的 fs.read 被拒绝
    failWritePaths: new Set<string>(), // 这些路径（norm 形式）的 fs.write 被拒绝
    clockScript: [] as (number | 'fail')[], // 前几次 clock.now：'fail' 拒绝，数字返回该值；用完回到 nowMs / failing
    failing: new Set<string>(),
    failMode: 'deny' as 'deny' | 'throw',
    lastStart: undefined as unknown, // 底层返回的对象，用来按内容核对透传；引擎会在层与层之间复制返回值，所以引用不可比
    lastMeasure: undefined as unknown,
  }
  // 两种失败方式：{ deny } 让调用方的 promise reject；throw 是底层 hook 自己抛错。
  const refuse = (what: string) => {
    if (w.failMode === 'throw') throw new Error('bottom ' + what + ' failed')
    return { deny: 'bottom ' + what + ' failed' }
  }
  // 先注册的在最外层，能看到之后所有事件，包括被测模块发起的引擎调用。
  on('*', async (_$, e, next) => {
    const name = String(next.event)
    // clock.every 单独记：它不属于各组 events 断言所描述的读写流程，而这些断言要保持严格
    // （clock.after 等其它任何新调用仍会进 events 被抓到）。定时器本身另有专门的用例核对。
    if (name === 'clock.every') w.timers.push((e as unknown as { ms: number }).ms)
    else if (!BOOT_NOISE.has(name)) w.events.push(name)
    return next(e)
  })
  on('clock.now', () => {
    const step = w.clockScript.shift()
    if (step === 'fail') return refuse('clock')
    if (step !== undefined) return { value: step }
    return w.failing.has('clock') ? refuse('clock') : { value: w.nowMs as number }
  })
  sharedHooks(on, w, {
    refuse,
    readRefused: (p) => w.failing.has('read') || w.failReadPaths.has(p),
    writeRefused: (p) => w.failing.has('write') || w.failWritePaths.has(p),
    sessionRefused: (what) => w.failing.has(what),
  })
  // 这个夹具没有可推进的时钟：拒绝 clock.every，间隔在第一期就结束，不会空转。每次布下都被拒绝，所以 w.timers 的长度就是布下定时器的次数。
  on('clock.every', () => ({ deny: 'no timers in this fixture' }))
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  return w
}
type World = ReturnType<typeof makeWorld>

// 事件日志与 usage.json 同目录。每次 hook 都会读写它；安全闸拒绝时不注册 hook，这条路径也不会出现。
const EVENTS = DATA_DIR + '/usage-feed-events.json'
const LOG_CALLS = ['fs.read', 'fs.write'] // 事件日志一侧：读已有日志、写回
const MEASURE_EVENTS = ['session.measure', ...FEED_CALLS, ...LOG_CALLS]
const START_EVENTS = ['session.start', 'session.usage', ...FEED_CALLS, ...LOG_CALLS]
const LOG_ONLY_CALLS = ['clock.now', 'session.id', ...LOG_CALLS] // usage.json 一侧零调用的情形
const MEASURE_LOG_ONLY = ['session.measure', ...LOG_ONLY_CALLS]
const START_LOG_ONLY = ['session.start', 'session.usage', ...LOG_ONLY_CALLS]
const writesTo = (w: World, path: string) => w.writes.filter((x) => norm(x.path) === path)
const readsOf = (w: World, path: string) => w.reads.filter((p) => p === path)
type LogEntry = {
  t: number
  ev: string
  sid: string | null
  n: number
  kinds: string[]
  kept: number
  out: string
  why: string
  drops?: Record<string, number>
  held: string[]
  changed: string[]
}
// 文件不存在或结构不对都当作没有日志，避免断言前先被解析错误打断。
const logOf = (w: World): LogEntry[] => {
  const parsed: unknown = JSON.parse(w.files.get(EVENTS) ?? '{"schema":1,"events":[]}')
  if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed) && Array.isArray((parsed as { events?: unknown }).events)) {
    return (parsed as { events: LogEntry[] }).events
  }
  return []
}
const lastLog = (w: World): LogEntry => {
  const events = logOf(w)
  const last = events[events.length - 1]
  if (last === undefined) throw new Error('event log is empty')
  return last
}

const reset = (w: World): void => {
  w.files.clear()
  w.writes.length = 0
  w.events.length = 0
  // failing、failReadPaths、failWritePaths 保持：一组断言之间失败开关不能被清掉。
  w.reads.length = 0
  w.clockScript.length = 0
}
const stage = (w: World, windows: unknown, writtenAt: number = T0 - 1000): void => {
  w.files.set(TARGET, JSON.stringify({ schema: 1, written_at: writtenAt, windows }))
}
const snap = (w: World) => JSON.parse(w.files.get(TARGET) ?? 'null')
const measure = ($: Engine, limits: unknown[]) =>
  $.session.measure({ context: { window: 200000 }, rateLimits: limits as never, changed: CHANGED as never })
const start = ($: Engine) => $.session.start({ cwd: 'D:/proj', surface: 'terminal', isInteractive: true })

// 每个用例一个独立世界；options 缺省时给合格的 dataDir。
const scenario = (
  name: string,
  options: Record<string, string> | undefined,
  body: (w: World, $: Engine) => Promise<void>,
): void => {
  test(name, { options: options ?? { dataDir: DATA_DIR } }, async ($, on) => {
    await body(makeWorld(on), $)
  })
}

// ---------------------------------------------------------------- 合并规则（五种情形）

scenario('merge 1: window only in the old snapshot is kept as is, others are independent', undefined, async (w, $) => {
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a'), seven_day: ow(55.5, R7, T0 - 600, 'old-b') })
  const res = await measure($, [lim('five_hour', 41, R5 + 30)])
  expect(res).toEqual({ changed: CHANGED })
  expect(w.events).toEqual(MEASURE_EVENTS)
  expect(snap(w)).toEqual({
    schema: 1,
    written_at: T0,
    windows: { five_hour: win(41, R5 + 30, T0, 'sess-new'), seven_day: win(55.5, R7, T0 - 600, 'old-b') },
  })
})

scenario('merge 2: window only in the new reading takes now and the session id', undefined, async (w, $) => {
  await measure($, [lim('spend_limit', 105.5, R7), lim('five_hour', 12.5, R5)])
  expect(snap(w)).toEqual({
    schema: 1,
    written_at: T0,
    windows: { five_hour: win(12.5, R5, T0, 'sess-new'), spend_limit: win(105.5, R7, T0, 'sess-new') },
  })
  // 写出顺序固定为 five_hour、seven_day、spend_limit，与读入顺序无关。
  expect(Object.keys(snap(w).windows)).toEqual(['five_hour', 'spend_limit'])
})

scenario('merge 3: same period, new >= old takes new (resets_at from new)', undefined, async (w, $) => {
  const cases: [string, number, number][] = [
    ['equal percentage, same reset', 40, R5],
    ['higher percentage, +120 s is still the same period', 62.5, R5 + 120],
    ['higher percentage, -120 s is still the same period', 70, R5 - 120],
  ]
  for (const [label, pct, resets] of cases) {
    reset(w)
    stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a') })
    await measure($, [lim('five_hour', pct, resets)])
    expect(snap(w).windows.five_hour, label).toEqual(win(pct, resets, T0, 'sess-new'))
  }
})

scenario('merge 3: same period, new < old keeps old untouched', undefined, async (w, $) => {
  const cases: [string, number, number][] = [
    ['slightly lower, same reset', 39.9, R5],
    ['lower, +120 s is still the same period', 10, R5 + 120],
    ['lower, -120 s is still the same period', 10, R5 - 120],
  ]
  for (const [label, pct, resets] of cases) {
    reset(w)
    stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a') })
    await measure($, [lim('five_hour', pct, resets)])
    expect(snap(w).windows.five_hour, label).toEqual(win(40, R5, T0 - 300, 'old-a'))
    expect(snap(w).written_at, label).toBe(T0)
  }
})

scenario('merge 4: new period takes new even when the percentage is smaller', undefined, async (w, $) => {
  const cases: [string, number][] = [
    ['+121 s is a new period', R5 + 121],
    ['the next five-hour window', R5 + 5 * 3600],
  ]
  for (const [label, resets] of cases) {
    reset(w)
    stage(w, { five_hour: ow(90, R5, T0 - 300, 'old-a') })
    await measure($, [lim('five_hour', 3, resets)])
    expect(snap(w).windows.five_hour, label).toEqual(win(3, resets, T0, 'sess-new'))
  }
})

scenario('merge 5: older period keeps old even when the percentage is larger', undefined, async (w, $) => {
  const cases: [string, number][] = [
    ['-121 s is an older period', R5 - 121],
    ['the previous five-hour window', R5 - 5 * 3600],
  ]
  for (const [label, resets] of cases) {
    reset(w)
    stage(w, { five_hour: ow(10, R5, T0 - 300, 'old-a') })
    await measure($, [lim('five_hour', 99, resets)])
    expect(snap(w).windows.five_hour, label).toEqual(win(10, R5, T0 - 300, 'old-a'))
  }
})

// 同一个会话的读数有先后之分，一定比它自己写的上一条新。套餐升级之类让上限变高时，同一周期里百分比会掉下来；
// 不放行的话要等 resets_at 往后走（seven_day 最长七天）才显示新的数。世界里的会话 id 是 'sess-new'。
scenario('merge 6: a reading from the session that wrote the stored value replaces it, also when lower', undefined, async (w, $) => {
  const cases: [string, number, number][] = [
    ['lower, same reset (the limit was raised mid-period)', 12, R5],
    ['lower, +120 s is still the same period', 12, R5 + 120],
    ['lower, -120 s is still the same period', 12, R5 - 120],
    ['lower and from an earlier period: the session reports its own latest', 12, R5 - 5 * 3600],
  ]
  for (const [label, pct, resets] of cases) {
    reset(w)
    stage(w, { five_hour: ow(40, R5, T0 - 300, 'sess-new'), seven_day: ow(55, R7, T0 - 300, 'old-b') })
    await measure($, [lim('five_hour', pct, resets)])
    expect(snap(w).windows.five_hour, label).toEqual(win(pct, resets, T0, 'sess-new'))
    expect(lastLog(w).held, label).toEqual([])
    expect(snap(w).windows.seven_day, label + ': a kind not in the reading is untouched').toEqual(win(55, R7, T0 - 300, 'old-b'))
  }
})

scenario('merge 6: the rule is per window, so another session\'s window in the same reading is still held', undefined, async (w, $) => {
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'sess-new'), seven_day: ow(55, R7, T0 - 300, 'old-b') })
  await measure($, [lim('five_hour', 12, R5), lim('seven_day', 10, R7)])
  expect(snap(w).windows.five_hour).toEqual(win(12, R5, T0, 'sess-new'))
  expect(snap(w).windows.seven_day).toEqual(win(55, R7, T0 - 300, 'old-b'))
  expect(lastLog(w).held).toEqual(['seven_day'])
})

scenario('merge 6: a lower reading from another session, or from a session whose id is unknown, is still held', undefined, async (w, $) => {
  // 别的会话写的：照旧挡住。
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a') })
  await measure($, [lim('five_hour', 12, R5)])
  expect(snap(w).windows.five_hour, 'another session').toEqual(win(40, R5, T0 - 300, 'old-a'))
  expect(lastLog(w).held, 'another session').toEqual(['five_hour'])

  // 本次读不到会话 id（null）：不知道是不是同一个会话，不算；已存的 session_id 是 null 时也一样。
  reset(w)
  w.failing.add('id')
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'sess-new') })
  await measure($, [lim('five_hour', 12, R5)])
  expect(snap(w).windows.five_hour, 'this id unknown').toEqual(win(40, R5, T0 - 300, 'sess-new'))
  expect(lastLog(w).held, 'this id unknown').toEqual(['five_hour'])

  reset(w)
  stage(w, { five_hour: ow(40, R5, T0 - 300, null) })
  await measure($, [lim('five_hour', 12, R5)])
  expect(snap(w).windows.five_hour, 'both ids unknown').toEqual(win(40, R5, T0 - 300, null))
  expect(lastLog(w).held, 'both ids unknown').toEqual(['five_hour'])

  w.failing.delete('id')
  reset(w)
  stage(w, { five_hour: ow(40, R5, T0 - 300, null) })
  await measure($, [lim('five_hour', 12, R5)])
  expect(snap(w).windows.five_hour, 'stored id unknown').toEqual(win(40, R5, T0 - 300, null))
  expect(lastLog(w).held, 'stored id unknown').toEqual(['five_hour'])
})

scenario('merge 6: session.start follows the same rule', undefined, async (w, $) => {
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'sess-new') })
  w.usageLimits = [lim('five_hour', 12, R5)]
  await start($)
  expect(snap(w).windows.five_hour).toEqual(win(12, R5, T0, 'sess-new'))
  expect(lastLog(w).held).toEqual([])
})

// ---------------------------------------------------------------- 窗口转换

scenario('convert: unknown kind, bad percentUsed or bad resetsAt are dropped', undefined, async (w, $) => {
  const good = lim('seven_day', 3, R7)
  const okReset = iso(R5)
  const bad: [string, unknown][] = [
    ['unknown kind', { kind: 'monthly', percentUsed: 1, resetsAt: okReset }],
    ['kind is case sensitive', { kind: 'FIVE_HOUR', percentUsed: 1, resetsAt: okReset }],
    ['kind is not a string', { kind: 5, percentUsed: 1, resetsAt: okReset }],
    ['kind missing', { percentUsed: 1, resetsAt: okReset }],
    ['negative percent', { kind: 'five_hour', percentUsed: -0.1, resetsAt: okReset }],
    ['NaN percent', { kind: 'five_hour', percentUsed: NaN, resetsAt: okReset }],
    ['Infinity percent', { kind: 'five_hour', percentUsed: Infinity, resetsAt: okReset }],
    ['string percent', { kind: 'five_hour', percentUsed: '12', resetsAt: okReset }],
    ['null percent', { kind: 'five_hour', percentUsed: null, resetsAt: okReset }],
    ['boolean percent', { kind: 'five_hour', percentUsed: true, resetsAt: okReset }],
    ['percent missing', { kind: 'five_hour', resetsAt: okReset }],
    ['resetsAt missing', { kind: 'five_hour', percentUsed: 1 }],
    ['resetsAt null', { kind: 'five_hour', percentUsed: 1, resetsAt: null }],
    ['resetsAt unparseable', { kind: 'five_hour', percentUsed: 1, resetsAt: 'garbage' }],
    ['resetsAt empty', { kind: 'five_hour', percentUsed: 1, resetsAt: '' }],
    ['resetsAt impossible date', { kind: 'five_hour', percentUsed: 1, resetsAt: '2027-13-45T99:99:99Z' }],
    ['resetsAt is a number', { kind: 'five_hour', percentUsed: 1, resetsAt: R5 }],
    ['resetsAt at the epoch', { kind: 'five_hour', percentUsed: 1, resetsAt: '1970-01-01T00:00:00Z' }],
    ['resetsAt floors to 0', { kind: 'five_hour', percentUsed: 1, resetsAt: '1970-01-01T00:00:00.999Z' }],
    ['resetsAt before the epoch', { kind: 'five_hour', percentUsed: 1, resetsAt: '1969-12-31T23:59:59Z' }],
  ]
  for (const [label, entry] of bad) {
    reset(w)
    await measure($, [entry, good])
    expect(Object.keys(snap(w).windows), label).toEqual(['seven_day'])
    expect(snap(w).windows.seven_day, label).toEqual(win(3, R7, T0, 'sess-new'))
  }
})

scenario('convert: when every window is dropped usage.json is not touched and only the event log is written', undefined, async (w, $) => {
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a') })
  const before = w.files.get(TARGET)
  const res = await measure($, [{ kind: 'monthly', percentUsed: 1, resetsAt: iso(R5) }, { kind: 'five_hour', percentUsed: -1, resetsAt: iso(R5) }])
  expect(res).toEqual({ changed: CHANGED })
  expect(w.events).toEqual(MEASURE_LOG_ONLY)
  expect(writesTo(w, TARGET)).toHaveLength(0)
  expect(readsOf(w, TARGET)).toHaveLength(0)
  expect(w.files.get(TARGET)).toBe(before)
  expect(writesTo(w, EVENTS)).toHaveLength(1)
})

scenario('convert: zero, over 100 and fractional-second values are accepted and floored', undefined, async (w, $) => {
  await measure($, [
    { kind: 'five_hour', percentUsed: 0, resetsAt: '2027-01-15T11:00:00.999Z' },
    { kind: 'seven_day', percentUsed: 100, resetsAt: iso(R7) },
    { kind: 'spend_limit', percentUsed: 123.4, resetsAt: iso(R7) },
  ])
  expect(snap(w).windows).toEqual({
    five_hour: win(0, T0 + 3 * 3600, T0, 'sess-new'),
    seven_day: win(100, R7, T0, 'sess-new'),
    spend_limit: win(123.4, R7, T0, 'sess-new'),
  })
})

scenario('convert: for a repeated kind the first valid window wins', undefined, async (w, $) => {
  await measure($, [
    lim('five_hour', 10, R5),
    lim('five_hour', 20, R5),
    { kind: 'seven_day', percentUsed: -1, resetsAt: iso(R7) },
    lim('seven_day', 7, R7),
  ])
  expect(snap(w).windows).toEqual({ five_hour: win(10, R5, T0, 'sess-new'), seven_day: win(7, R7, T0, 'sess-new') })
})

// ---------------------------------------------------------------- 已有快照

scenario('existing: unusable snapshots are treated as absent', undefined, async (w, $) => {
  const sd = { seven_day: ow(55, R7, T0 - 600, 'old-b') }
  const good: [string, string][] = [
    ['broken JSON', 'not json{'],
    ['empty text', ''],
    ['JSON null', 'null'],
    ['top level array', '[]'],
    ['top level number', '42'],
    ['schema 2', JSON.stringify({ schema: 2, written_at: T0, windows: sd })],
    ['schema as string', JSON.stringify({ schema: '1', written_at: T0, windows: sd })],
    ['schema as boolean', JSON.stringify({ schema: true, written_at: T0, windows: sd })],
    ['schema missing', JSON.stringify({ written_at: T0, windows: sd })],
    ['windows array', JSON.stringify({ schema: 1, written_at: T0, windows: [sd] })],
    ['windows null', JSON.stringify({ schema: 1, written_at: T0, windows: null })],
    ['windows string', JSON.stringify({ schema: 1, written_at: T0, windows: 'x' })],
    ['windows missing', JSON.stringify({ schema: 1, written_at: T0 })],
  ]
  for (const [label, text] of good) {
    reset(w)
    w.files.set(TARGET, text)
    await measure($, [lim('five_hour', 20, R5)])
    expect(snap(w), label).toEqual({ schema: 1, written_at: T0, windows: { five_hour: win(20, R5, T0, 'sess-new') } })
  }
})

scenario('existing: invalid single windows are treated as absent', undefined, async (w, $) => {
  const bad: [string, unknown][] = [
    ['negative percent', ow(-1, R7, T0 - 1, 'x')],
    ['string percent', ow('55', R7, T0 - 1, 'x')],
    ['boolean percent', ow(true, R7, T0 - 1, 'x')],
    ['null percent', ow(null, R7, T0 - 1, 'x')],
    ['percent missing', { resets_at: R7, observed_at: T0 - 1, session_id: 'x' }],
    ['resets_at zero', ow(55, 0, T0 - 1, 'x')],
    ['resets_at negative', ow(55, -5, T0 - 1, 'x')],
    ['resets_at string', ow(55, String(R7), T0 - 1, 'x')],
    ['resets_at null', ow(55, null, T0 - 1, 'x')],
    ['resets_at missing', { used_percentage: 55, observed_at: T0 - 1, session_id: 'x' }],
    ['resets_at floors to 0', ow(55, 0.5, T0 - 1, 'x')],
    ['window null', null],
    ['window number', 42],
    ['window string', 'x'],
    ['window array', [ow(55, R7, T0 - 1, 'x')]],
  ]
  for (const [label, entry] of bad) {
    reset(w)
    stage(w, { five_hour: entry, seven_day: entry })
    await measure($, [lim('five_hour', 20, R5)])
    expect(snap(w).windows, label).toEqual({ five_hour: win(20, R5, T0, 'sess-new') })
  }
})

scenario('existing: observed_at falls back to written_at, then 0; session_id must be a string', undefined, async (w, $) => {
  const longId = 'x'.repeat(200)
  const cases: [string, unknown, number | undefined, unknown][] = [
    ['no observed_at, written_at present', ow(0, R7), T0 - 7777, win(0, R7, T0 - 7777, null)],
    ['observed_at is a string', ow(12.5, R7 + 0.9, 'x', 7), T0 - 7777, win(12.5, R7, T0 - 7777, null)],
    ['neither observed_at nor written_at', ow(1, R7), undefined, win(1, R7, 0, null)],
    ['observed_at 0 is kept, not replaced', ow(1, R7, 0, 'sid'), T0 - 7777, win(1, R7, 0, 'sid')],
    ['long session_id is clipped to 128', ow(1, R7, T0 - 5, longId), T0 - 7777, win(1, R7, T0 - 5, 'x'.repeat(128))],
    ['fractional resets_at is floored', ow(2, R7 + 0.9, T0 - 5, 's'), T0 - 7777, win(2, R7, T0 - 5, 's')],
  ]
  for (const [label, entry, writtenAt, expected] of cases) {
    reset(w)
    w.files.set(TARGET, JSON.stringify({ schema: 1, ...(writtenAt === undefined ? {} : { written_at: writtenAt }), windows: { seven_day: entry } }))
    await measure($, [lim('five_hour', 20, R5)])
    expect(snap(w).windows.seven_day, label).toEqual(expected)
  }
})

scenario('existing: a leading BOM is tolerated and unknown window names are dropped', undefined, async (w, $) => {
  const text = JSON.stringify({
    schema: 1,
    written_at: T0 - 100,
    windows: { seven_day: ow(55, R7, T0 - 600, 'old-b'), monthly: ow(9, R7, T0 - 600, 'old-m') },
  })
  w.files.set(TARGET, String.fromCharCode(0xfeff) + text)
  await measure($, [lim('five_hour', 20, R5)])
  expect(snap(w).windows).toEqual({ five_hour: win(20, R5, T0, 'sess-new'), seven_day: win(55, R7, T0 - 600, 'old-b') })
})

// 用量快照超过 64 KB（按字符数）当作没有，与解析失败走同一条路：不解析，这次写出的小文件把它换掉。
scenario('existing: a snapshot over 64 KB is treated as absent, one of exactly 64 KB is merged', undefined, async (w, $) => {
  const CAP = 64 * 1024
  const text = JSON.stringify({ schema: 1, written_at: T0 - 100, windows: { seven_day: ow(55, R7, T0 - 600, 'old-b') } })
  const padded = (size: number) => text + ' '.repeat(size - text.length)
  w.files.set(TARGET, padded(CAP))
  await measure($, [lim('five_hour', 20, R5)])
  expect(snap(w).windows, 'exactly 64 KB: the other kind is kept').toEqual({
    five_hour: win(20, R5, T0, 'sess-new'),
    seven_day: win(55, R7, T0 - 600, 'old-b'),
  })

  reset(w)
  w.files.set(TARGET, padded(CAP + 1))
  await measure($, [lim('five_hour', 20, R5)])
  expect(snap(w).windows, 'one over: absent, replaced by the small file').toEqual({ five_hour: win(20, R5, T0, 'sess-new') })

  // 别的写入方留下的几 MB 文件：照样当作没有，被这次写出的小文件换掉。
  reset(w)
  w.files.set(TARGET, 'x'.repeat(3 * 1024 * 1024))
  await measure($, [lim('five_hour', 20, R5)])
  expect(snap(w).windows, 'a multi-MB foreign file').toEqual({ five_hour: win(20, R5, T0, 'sess-new') })
  expect(w.files.get(TARGET)!.length).toBeLessThan(1000)
})

// ---------------------------------------------------------------- dataDir 安全闸

const REFUSED: [string, Record<string, string> | undefined][] = [
  ['unset', {}],
  ['empty string', { dataDir: '' }],
  ['spaces only', { dataDir: '   ' }],
  ['relative path', { dataDir: 'data/dir' }],
  ['dot relative path', { dataDir: './data' }],
  ['drive relative path', { dataDir: 'D:data' }],
  ['unix style absolute path', { dataDir: '/abs/path' }],
  ['UNC with backslashes', { dataDir: '\\\\server\\share' }],
  ['UNC with slashes', { dataDir: '//server/share' }],
  ['extended length prefix', { dataDir: '\\\\?\\D:\\data' }],
  ['OneDrive, mixed case', { dataDir: 'D:/OneDrive - Contoso/data' }],
  ['OneDrive, lower case', { dataDir: 'D:\\data\\onedrive\\x' }],
  ['OneDrive, upper case', { dataDir: 'F:/ONEDRIVE/data' }],
  ['OneDrive in the middle of a segment', { dataDir: 'D:/backup-OneDrive-copy/data' }],
]
for (const [label, options] of REFUSED) {
  scenario('gate refuses: ' + label, options, async (w, $) => {
    const limits = [lim('five_hour', 20, R5)]
    w.usageLimits = limits
    const started = await start($)
    expect(started).toEqual({ cwd: START_MARK + 'D:/proj' })
    const measured = await measure($, limits)
    expect(measured).toEqual({ changed: CHANGED })
    // 除了事件本身，被测模块什么调用都没发起：既不读也不写。
    expect(w.events).toEqual(['session.start', 'session.measure'])
    expect(w.writes).toHaveLength(0)
    expect(w.files.size).toBe(0)
    expect(w.files.has(EVENTS)).toBe(false)
    expect(w.reads).toHaveLength(0)
  })
}

const ACCEPTED: [string, string, string][] = [
  ['backslashes and a trailing backslash', 'D:\\usage-feed-test\\mem\\y\\', 'D:/usage-feed-test/mem/y/usage.json'],
  ['forward slashes', 'D:/usage-feed-test/mem/y', 'D:/usage-feed-test/mem/y/usage.json'],
  ['several trailing slashes', 'D:/usage-feed-test/mem/y//', 'D:/usage-feed-test/mem/y/usage.json'],
  ['mixed separators and a lower case drive', 'd:\\usage-feed-test/mem\\y\\\\', 'd:/usage-feed-test/mem/y/usage.json'],
]
for (const [label, dir, expected] of ACCEPTED) {
  scenario('gate accepts: ' + label, { dataDir: dir }, async (w, $) => {
    await measure($, [lim('five_hour', 20, R5)])
    expect(w.writes).toHaveLength(2)
    expect(norm(w.writes[0]!.path)).toBe(expected)
    expect(norm(w.writes[1]!.path)).toBe(expected.replace(/usage\.json$/, 'usage-feed-events.json'))
    expect(w.events).toEqual(MEASURE_EVENTS)
  })
}

// ---------------------------------------------------------------- session.measure

scenario('measure: empty rateLimits leaves usage.json untouched and only the event log is written', undefined, async (w, $) => {
  const res = await measure($, [])
  expect(res).toEqual({ changed: CHANGED })
  expect(w.events).toEqual(MEASURE_LOG_ONLY)
  expect(writesTo(w, TARGET)).toHaveLength(0)
  expect(readsOf(w, TARGET)).toHaveLength(0)
})

scenario('measure: a non-empty reading writes and refreshes observed_at even when unchanged', undefined, async (w, $) => {
  const limits = [lim('five_hour', 20, R5), lim('seven_day', 8, R7)]
  await measure($, limits)
  expect(snap(w)).toEqual({
    schema: 1,
    written_at: T0,
    windows: { five_hour: win(20, R5, T0, 'sess-new'), seven_day: win(8, R7, T0, 'sess-new') },
  })
  w.nowMs = (T0 + 600) * 1000
  w.sessionId = 'sess-later'
  await measure($, limits)
  expect(writesTo(w, TARGET)).toHaveLength(2)
  expect(snap(w)).toEqual({
    schema: 1,
    written_at: T0 + 600,
    windows: { five_hour: win(20, R5, T0 + 600, 'sess-later'), seven_day: win(8, R7, T0 + 600, 'sess-later') },
  })
})

scenario('measure: written_at and observed_at are float seconds', undefined, async (w, $) => {
  w.nowMs = NOW_MS + 250
  await measure($, [lim('five_hour', 20, R5)])
  expect(snap(w).written_at).toBe(T0 + 0.25)
  expect(snap(w).windows.five_hour.observed_at).toBe(T0 + 0.25)
})

scenario('measure: file layout is 2-space JSON with the contract field order', undefined, async (w, $) => {
  await measure($, [lim('spend_limit', 5, R7), lim('seven_day', 8, R7), lim('five_hour', 20, R5)])
  const text = w.files.get(TARGET)!
  const obj = JSON.parse(text)
  expect(text).toBe(JSON.stringify(obj, null, 2))
  expect(Object.keys(obj)).toEqual(['schema', 'written_at', 'windows'])
  expect(Object.keys(obj.windows)).toEqual(['five_hour', 'seven_day', 'spend_limit'])
  for (const k of Object.keys(obj.windows)) {
    expect(Object.keys(obj.windows[k]), k).toEqual(['used_percentage', 'resets_at', 'observed_at', 'session_id'])
    expect(Number.isInteger(obj.windows[k].resets_at), k).toBe(true)
  }
})

// ---------------------------------------------------------------- session.start

scenario('start: reads the usage through session.usage and writes the snapshot', undefined, async (w, $) => {
  w.usageLimits = [lim('five_hour', 30, R5), lim('seven_day', 8, R7)]
  const res = await start($)
  expect(res).toEqual({ cwd: START_MARK + 'D:/proj' })
  expect(w.events).toEqual(START_EVENTS)
  expect(snap(w)).toEqual({
    schema: 1,
    written_at: T0,
    windows: { five_hour: win(30, R5, T0, 'sess-new'), seven_day: win(8, R7, T0, 'sess-new') },
  })
})

scenario('start: an empty usage reading writes no snapshot and only the event log', undefined, async (w, $) => {
  w.usageLimits = []
  const res = await start($)
  expect(res).toEqual({ cwd: START_MARK + 'D:/proj' })
  expect(w.events).toEqual(START_LOG_ONLY)
  expect(writesTo(w, TARGET)).toHaveLength(0)
  expect(readsOf(w, TARGET)).toHaveLength(0)
})

scenario('start: a usage reading of only invalid windows writes no snapshot and only the event log', undefined, async (w, $) => {
  w.usageLimits = [{ kind: 'five_hour', percentUsed: -3, resetsAt: iso(R5) }]
  await start($)
  expect(w.events).toEqual(START_LOG_ONLY)
  expect(writesTo(w, TARGET)).toHaveLength(0)
  expect(readsOf(w, TARGET)).toHaveLength(0)
})

// ---------------------------------------------------------------- 失败静默与返回值透传

for (const mode of ['deny', 'throw'] as const) {
  scenario('silent failure (' + mode + '): fs.read fails, both hooks still return and the snapshot is written', undefined, async (w, $) => {
    w.failMode = mode
    w.failing.add('read')
    w.usageLimits = [lim('five_hour', 30, R5)]
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(snap(w)).toEqual({ schema: 1, written_at: T0, windows: { five_hour: win(30, R5, T0, 'sess-new') } })
    reset(w)
    expect(await measure($, [lim('five_hour', 31, R5)])).toEqual({ changed: CHANGED })
    expect(snap(w).windows.five_hour).toEqual(win(31, R5, T0, 'sess-new'))
  })

  scenario('silent failure (' + mode + '): fs.write fails, both hooks still return and no file appears', undefined, async (w, $) => {
    w.failMode = mode
    w.failing.add('write')
    w.usageLimits = [lim('five_hour', 30, R5)]
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(w.events).toEqual(START_EVENTS)
    expect(await measure($, [lim('five_hour', 31, R5)])).toEqual({ changed: CHANGED })
    expect(w.files.size).toBe(0)
  })

  scenario('silent failure (' + mode + '): session.usage fails, start returns and writes no snapshot; measure is unaffected', undefined, async (w, $) => {
    w.failMode = mode
    w.failing.add('usage')
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(w.events).toEqual(START_LOG_ONLY)
    expect(w.files.has(TARGET)).toBe(false)
    reset(w)
    expect(await measure($, [lim('five_hour', 31, R5)])).toEqual({ changed: CHANGED })
    expect(w.events).toEqual(MEASURE_EVENTS)
    expect(snap(w).windows.five_hour).toEqual(win(31, R5, T0, 'sess-new'))
  })

  scenario('silent failure (' + mode + '): session.id fails, hooks return and session_id is null', undefined, async (w, $) => {
    w.failMode = mode
    w.failing.add('id')
    w.usageLimits = [lim('five_hour', 30, R5)]
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(snap(w).windows.five_hour).toEqual(win(30, R5, T0, null))
    reset(w)
    expect(await measure($, [lim('five_hour', 31, R5)])).toEqual({ changed: CHANGED })
    expect(snap(w).windows.five_hour).toEqual(win(31, R5, T0, null))
  })

  scenario('silent failure (' + mode + '): clock.now fails, hooks return and nothing is written', undefined, async (w, $) => {
    w.failMode = mode
    w.failing.add('clock')
    w.usageLimits = [lim('five_hour', 30, R5)]
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(await measure($, [lim('five_hour', 31, R5)])).toEqual({ changed: CHANGED })
    expect(w.events).toEqual(['session.start', 'session.usage', 'clock.now', 'clock.now', 'session.measure', 'clock.now', 'clock.now'])
    expect(w.files.size).toBe(0)
  })
}

scenario('silent failure: a non-finite clock or a non-string session id never corrupts the snapshot', undefined, async (w, $) => {
  w.nowMs = NaN
  expect(await measure($, [lim('five_hour', 31, R5)])).toEqual({ changed: CHANGED })
  expect(w.events).toEqual(['session.measure', 'clock.now', 'clock.now'])
  expect(w.writes).toHaveLength(0)
  reset(w)
  w.nowMs = NOW_MS
  w.sessionId = 42
  await measure($, [lim('five_hour', 31, R5)])
  expect(snap(w).windows.five_hour).toEqual(win(31, R5, T0, null))
})

scenario('pass-through: both hooks return exactly what the rest of the chain returned', undefined, async (w, $) => {
  w.usageLimits = [lim('five_hour', 30, R5)]
  expect(await start($)).toStrictEqual({ cwd: START_MARK + 'D:/proj' })
  expect(await measure($, [lim('five_hour', 31, R5)])).toStrictEqual({ changed: CHANGED })
  expect(await measure($, [])).toStrictEqual({ changed: CHANGED })
})

scenario('other events are not hooked: turn.start passes through with no engine call from the module', undefined, async (w, $) => {
  expect(await $.turn.start({ text: 'hello', turnId: 't-1' })).toEqual({ turnId: 't-1' })
  expect(w.events).toEqual(['turn.start'])
  expect(w.writes).toHaveLength(0)
})

// ---------------------------------------------------------------- 契约：交给读方解析

scenario('contract: a representative snapshot is produced and printed', undefined, async (w, $) => {
  stage(w, { seven_day: ow(55.5, R7, T0 - 600, 'old-b') }, T0 - 600)
  w.usageLimits = [lim('five_hour', 33.3, R5), lim('spend_limit', 105.5, R7)]
  await start($)
  const text = w.files.get(TARGET)!
  console.log('CONTRACT_SNAPSHOT_BEGIN\n' + text + '\nCONTRACT_SNAPSHOT_END')
  expect(Object.keys(JSON.parse(text).windows)).toEqual(['five_hour', 'seven_day', 'spend_limit'])
})

// ---------------------------------------------------------------- 事件日志：基本形态

const untouched = (w: World): void => {
  expect(writesTo(w, TARGET)).toHaveLength(0)
  expect(readsOf(w, TARGET)).toHaveLength(0)
}

scenario('log: a normal measure appends one entry describing the write', undefined, async (w, $) => {
  await measure($, [lim('five_hour', 20, R5), lim('seven_day', 8, R7)])
  const first: LogEntry = {
    t: NOW_MS,
    ev: 'session.measure',
    sid: 'sess-new',
    n: 2,
    kinds: ['five_hour', 'seven_day'],
    kept: 2,
    out: 'wrote',
    why: '',
    held: [],
    changed: CHANGED,
  }
  expect(logOf(w)).toEqual([first])
  expect('drops' in lastLog(w)).toBe(false)
  expect(w.events).toEqual(MEASURE_EVENTS)
  expect(Object.keys(snap(w).windows)).toEqual(['five_hour', 'seven_day'])
  expect(w.writes).toHaveLength(2)
  expect(norm(w.writes[0]!.path)).toBe(TARGET)
  expect(norm(w.writes[1]!.path)).toBe(EVENTS)

  w.nowMs = NOW_MS + 5000
  w.sessionId = 'sess-2'
  await measure($, [lim('five_hour', 21, R5)])
  expect(logOf(w)).toEqual([
    first,
    {
      t: NOW_MS + 5000,
      ev: 'session.measure',
      sid: 'sess-2',
      n: 1,
      kinds: ['five_hour'],
      kept: 1,
      out: 'wrote',
      why: '',
      held: [],
      changed: CHANGED,
    },
  ])
})

scenario('log: a normal session.start appends an entry with an empty changed list', undefined, async (w, $) => {
  w.usageLimits = [lim('five_hour', 30, R5), lim('seven_day', 8, R7)]
  expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
  expect(logOf(w)).toEqual([
    {
      t: NOW_MS,
      ev: 'session.start',
      sid: 'sess-new',
      n: 2,
      kinds: ['five_hour', 'seven_day'],
      kept: 2,
      out: 'wrote',
      why: '',
      held: [],
      changed: [],
    },
  ])
  expect(w.events).toEqual(START_EVENTS)
})

scenario('log: an empty rate limit list is recorded as no_rate_limits and usage.json is untouched', undefined, async (w, $) => {
  const skipped = (ev: string, changed: string[]): LogEntry => ({
    t: NOW_MS,
    ev,
    sid: 'sess-new',
    n: 0,
    kinds: [],
    kept: 0,
    out: 'skipped',
    why: 'no_rate_limits',
    held: [],
    changed,
  })
  await measure($, [])
  expect(logOf(w)).toEqual([skipped('session.measure', CHANGED)])
  expect('drops' in lastLog(w)).toBe(false)
  expect(w.events).toEqual(MEASURE_LOG_ONLY)
  untouched(w)

  reset(w)
  w.usageLimits = []
  await start($)
  expect(logOf(w)).toEqual([skipped('session.start', [])])
  expect(w.events).toEqual(START_LOG_ONLY)
  untouched(w)

  for (const bad of [undefined, 'oops', {}]) {
    reset(w)
    await $.session.measure({ context: { window: 200000 }, rateLimits: bad as never, changed: CHANGED as never })
    expect(lastLog(w).n).toBe(0)
    expect(lastLog(w).kinds).toEqual([])
    expect(lastLog(w).why).toBe('no_rate_limits')
    expect(lastLog(w).out).toBe('skipped')
    untouched(w)
  }
})

scenario('log: windows that are all dropped are recorded as all_dropped with counted reasons', undefined, async (w, $) => {
  const okReset = iso(R5)
  const ALL_DROPPED: [string, unknown[], Record<string, number>, string[]][] = [
    ['resetsAt missing', [{ kind: 'five_hour', percentUsed: 1 }], { no_resets_at: 1 }, ['five_hour']],
    ['resetsAt null', [{ kind: 'five_hour', percentUsed: 1, resetsAt: null }], { no_resets_at: 1 }, ['five_hour']],
    ['resetsAt is a number', [{ kind: 'seven_day', percentUsed: 1, resetsAt: R7 }], { no_resets_at: 1 }, ['seven_day']],
    ['negative percent', [{ kind: 'five_hour', percentUsed: -1, resetsAt: okReset }], { bad_percent: 1 }, ['five_hour']],
    ['NaN percent', [{ kind: 'five_hour', percentUsed: NaN, resetsAt: okReset }], { bad_percent: 1 }, ['five_hour']],
    ['string percent', [{ kind: 'spend_limit', percentUsed: '12', resetsAt: okReset }], { bad_percent: 1 }, ['spend_limit']],
    ['percent missing', [{ kind: 'five_hour', resetsAt: okReset }], { bad_percent: 1 }, ['five_hour']],
    ['unknown kind', [{ kind: 'monthly', percentUsed: 1, resetsAt: okReset }], { unknown_kind: 1 }, ['other']],
    ['kind is not a string', [{ kind: 5, percentUsed: 1, resetsAt: okReset }], { unknown_kind: 1 }, ['other']],
    ['kind missing', [{ percentUsed: 1, resetsAt: okReset }], { unknown_kind: 1 }, ['other']],
    ['entries that are not objects', [null, 5, 'x', [1]], { not_object: 4 }, ['other', 'other', 'other', 'other']],
    ['resetsAt unparseable', [{ kind: 'five_hour', percentUsed: 1, resetsAt: 'garbage' }], { bad_resets_at: 1 }, ['five_hour']],
    ['resetsAt empty string', [{ kind: 'five_hour', percentUsed: 1, resetsAt: '' }], { bad_resets_at: 1 }, ['five_hour']],
    ['resetsAt at the epoch', [{ kind: 'five_hour', percentUsed: 1, resetsAt: '1970-01-01T00:00:00Z' }], { bad_resets_at: 1 }, ['five_hour']],
    ['resetsAt floors to 0', [{ kind: 'five_hour', percentUsed: 1, resetsAt: '1970-01-01T00:00:00.999Z' }], { bad_resets_at: 1 }, ['five_hour']],
    ['resetsAt before the epoch', [{ kind: 'five_hour', percentUsed: 1, resetsAt: '1969-12-31T23:59:59Z' }], { bad_resets_at: 1 }, ['five_hour']],
    ['the same reason on two entries adds up', [{ kind: 'five_hour', percentUsed: 1 }, { kind: 'seven_day', percentUsed: 2 }], { no_resets_at: 2 }, ['five_hour', 'seven_day']],
    [
      'five different reasons once each',
      [
        null,
        { kind: 'monthly', percentUsed: 1, resetsAt: okReset },
        { kind: 'five_hour', percentUsed: -1, resetsAt: okReset },
        { kind: 'seven_day', percentUsed: 1 },
        { kind: 'spend_limit', percentUsed: 1, resetsAt: 'garbage' },
      ],
      { not_object: 1, unknown_kind: 1, bad_percent: 1, no_resets_at: 1, bad_resets_at: 1 },
      ['other', 'other', 'five_hour', 'seven_day', 'spend_limit'],
    ],
  ]
  for (const [label, entries, drops, kinds] of ALL_DROPPED) {
    reset(w)
    await measure($, entries)
    expect(logOf(w), label).toEqual([
      {
        t: NOW_MS,
        ev: 'session.measure',
        sid: 'sess-new',
        n: entries.length,
        kinds,
        kept: 0,
        out: 'skipped',
        why: 'all_dropped',
        drops,
        held: [],
        changed: CHANGED,
      },
    ])
    expect(w.events, label).toEqual(MEASURE_LOG_ONLY)
    expect(writesTo(w, TARGET), label).toHaveLength(0)
    expect(readsOf(w, TARGET), label).toHaveLength(0)
  }
  expect(Object.keys(lastLog(w).drops!)).toEqual(['not_object', 'unknown_kind', 'bad_percent', 'no_resets_at', 'bad_resets_at'])

  reset(w)
  w.usageLimits = [{ kind: 'five_hour', percentUsed: 1 }]
  await start($)
  expect(lastLog(w)).toEqual({
    t: NOW_MS,
    ev: 'session.start',
    sid: 'sess-new',
    n: 1,
    kinds: ['five_hour'],
    kept: 0,
    out: 'skipped',
    why: 'all_dropped',
    drops: { no_resets_at: 1 },
    held: [],
    changed: [],
  })
  expect(w.events).toEqual(START_LOG_ONLY)
})

scenario('log: partially dropped lists still write usage.json and record what was dropped', undefined, async (w, $) => {
  const okReset = iso(R5)
  await measure($, [lim('five_hour', 10, R5), { kind: 'monthly', percentUsed: 1, resetsAt: okReset }])
  expect(lastLog(w)).toEqual({
    t: NOW_MS,
    ev: 'session.measure',
    sid: 'sess-new',
    n: 2,
    kinds: ['five_hour', 'other'],
    kept: 1,
    out: 'wrote',
    why: '',
    drops: { unknown_kind: 1 },
    held: [],
    changed: CHANGED,
  })
  expect(Object.keys(snap(w).windows)).toEqual(['five_hour'])

  reset(w)
  await measure($, [
    lim('five_hour', 10, R5),
    lim('five_hour', 20, R5),
    { kind: 'seven_day', percentUsed: -1, resetsAt: iso(R7) },
    lim('seven_day', 7, R7),
  ])
  expect(lastLog(w)).toEqual({
    t: NOW_MS,
    ev: 'session.measure',
    sid: 'sess-new',
    n: 4,
    kinds: ['five_hour', 'five_hour', 'seven_day', 'seven_day'],
    kept: 2,
    out: 'wrote',
    why: '',
    drops: { bad_percent: 1, duplicate_kind: 1 },
    held: [],
    changed: CHANGED,
  })
  expect(Object.keys(snap(w).windows)).toEqual(['five_hour', 'seven_day'])
  expect(snap(w).windows.five_hour.used_percentage).toBe(10)
  expect(snap(w).windows.seven_day.used_percentage).toBe(7)

  reset(w)
  await measure($, [lim('five_hour', 10, R5), { kind: 'five_hour', percentUsed: -1, resetsAt: okReset }])
  expect(lastLog(w).n).toBe(2)
  expect(lastLog(w).kept).toBe(1)
  expect(lastLog(w).drops).toEqual({ duplicate_kind: 1 })
  expect(lastLog(w).out).toBe('wrote')

  reset(w)
  await measure($, [{ kind: 'five_hour', percentUsed: -1, resetsAt: okReset }, lim('five_hour', 10, R5)])
  expect(lastLog(w).n).toBe(2)
  expect(lastLog(w).kept).toBe(1)
  expect(lastLog(w).drops).toEqual({ bad_percent: 1 })
  expect(snap(w).windows.five_hour.used_percentage).toBe(10)
})

scenario('log: kinds keeps the three known kinds, records everything else as other and lists at most six', undefined, async (w, $) => {
  await measure($, [
    lim('five_hour', 1, R5),
    lim('seven_day', 2, R7),
    lim('spend_limit', 3, R7),
    { kind: 'monthly', percentUsed: 1, resetsAt: iso(R5) },
    { kind: 5, percentUsed: 1, resetsAt: iso(R5) },
    null,
    lim('five_hour', 4, R5),
    lim('seven_day', 5, R7),
  ])
  const entry = lastLog(w)
  expect(entry.n).toBe(8)
  expect(entry.kinds).toEqual(['five_hour', 'seven_day', 'spend_limit', 'other', 'other', 'other'])
  expect(entry.kept).toBe(3)
  expect(entry.drops).toEqual({ not_object: 1, unknown_kind: 2, duplicate_kind: 2 })
  expect(entry.out).toBe('wrote')
  expect(Object.keys(snap(w).windows)).toEqual(['five_hour', 'seven_day', 'spend_limit'])
  expect(snap(w).windows.five_hour.used_percentage).toBe(1)
  expect(snap(w).windows.seven_day.used_percentage).toBe(2)
  expect(snap(w).windows.spend_limit.used_percentage).toBe(3)
})

// ---------------------------------------------------------------- 事件日志：changed、held、失败与格式

// 不带 drops 与带 drops 时的键序。多一个键或换序都会让排障记录对不上。
const LOG_KEYS = ['t', 'ev', 'sid', 'n', 'kinds', 'kept', 'out', 'why', 'held', 'changed']
const LOG_KEYS_DROPS = ['t', 'ev', 'sid', 'n', 'kinds', 'kept', 'out', 'why', 'drops', 'held', 'changed']
// deny 与 throw 两种失败都被引擎包成同一种错误名。
const USAGE_WHY = 'usage_error:HooksError'
const WRITE_WHY = 'write_error:HooksError'

const measureChanged = ($: Engine, limits: unknown[], changed: unknown) =>
  $.session.measure({ context: { window: 200000 }, rateLimits: limits as never, changed: changed as never })

scenario('log: changed keeps at most five items, each cut to sixteen characters', undefined, async (w, $) => {
  const limits = [lim('five_hour', 20, R5)]
  await measureChanged($, limits, ['rateLimits', 'cost', 'context', 'a', 'b', 'sixth-item', 'seventh'])
  expect(lastLog(w).changed).toEqual(['rateLimits', 'cost', 'context', 'a', 'b'])
  expect(lastLog(w).out).toBe('wrote')

  reset(w)
  await measureChanged($, limits, ['x'.repeat(40), 7, null])
  expect(lastLog(w).changed).toEqual(['x'.repeat(16), '7', 'null'])
  expect(lastLog(w).out).toBe('wrote')

  reset(w)
  await measureChanged($, limits, [])
  expect(lastLog(w).changed).toEqual([])
  expect(lastLog(w).out).toBe('wrote')
})

scenario('log: held lists the kinds whose new reading lost to the existing value', undefined, async (w, $) => {
  const noDrops = (entry: LogEntry): void => {
    expect(entry.out).toBe('wrote')
    expect('drops' in entry).toBe(false)
  }
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a'), seven_day: ow(10, R7, T0 - 300, 'old-b') })
  await measure($, [lim('five_hour', 39.9, R5), lim('seven_day', 12, R7)])
  noDrops(lastLog(w))
  expect(lastLog(w).held).toEqual(['five_hour'])
  expect(lastLog(w).kept).toBe(2)
  expect(snap(w).windows.five_hour).toEqual(win(40, R5, T0 - 300, 'old-a'))
  expect(snap(w).windows.seven_day).toEqual(win(12, R7, T0, 'sess-new'))

  reset(w)
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a'), seven_day: ow(10, R7, T0 - 300, 'old-b') })
  await measure($, [lim('seven_day', 9, R7), lim('five_hour', 30, R5)])
  noDrops(lastLog(w))
  expect(lastLog(w).held).toEqual(['five_hour', 'seven_day'])
  expect(lastLog(w).kept).toBe(2)
  expect(snap(w).windows.five_hour).toEqual(win(40, R5, T0 - 300, 'old-a'))
  expect(snap(w).windows.seven_day).toEqual(win(10, R7, T0 - 300, 'old-b'))

  reset(w)
  stage(w, { five_hour: ow(10, R5, T0 - 300, 'old-a') })
  await measure($, [lim('five_hour', 99, R5 - 5 * 3600)])
  noDrops(lastLog(w))
  expect(lastLog(w).held).toEqual(['five_hour'])
  expect(lastLog(w).kept).toBe(1)
  expect(snap(w).windows.five_hour).toEqual(win(10, R5, T0 - 300, 'old-a'))

  reset(w)
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a'), seven_day: ow(55, R7, T0 - 600, 'old-b') })
  await measure($, [lim('five_hour', 41, R5)])
  noDrops(lastLog(w))
  expect(lastLog(w).held).toEqual([])
  expect(lastLog(w).kept).toBe(1)
  expect(snap(w).windows.seven_day).toEqual(win(55, R7, T0 - 600, 'old-b'))

  reset(w)
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a') })
  await measure($, [lim('five_hour', 40, R5)])
  noDrops(lastLog(w))
  expect(lastLog(w).held).toEqual([])
  expect(lastLog(w).kept).toBe(1)

  reset(w)
  stage(w, { five_hour: ow(90, R5, T0 - 300, 'old-a') })
  await measure($, [lim('five_hour', 3, R5 + 5 * 3600)])
  noDrops(lastLog(w))
  expect(lastLog(w).held).toEqual([])
  expect(lastLog(w).kept).toBe(1)

  reset(w)
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a') })
  w.usageLimits = [lim('five_hour', 10, R5)]
  await start($)
  noDrops(lastLog(w))
  expect(lastLog(w).ev).toBe('session.start')
  expect(lastLog(w).held).toEqual(['five_hour'])
  expect(lastLog(w).kept).toBe(1)
  expect(snap(w).windows.five_hour).toEqual(win(40, R5, T0 - 300, 'old-a'))
})

for (const mode of ['deny', 'throw'] as const) {
  scenario('log: a failing session.usage in session.start is recorded as usage_error (' + mode + ')', undefined, async (w, $) => {
    w.failMode = mode
    w.failing.add('usage')
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(logOf(w)).toEqual([
      {
        t: NOW_MS,
        ev: 'session.start',
        sid: 'sess-new',
        n: 0,
        kinds: [],
        kept: 0,
        out: 'skipped',
        why: USAGE_WHY,
        held: [],
        changed: [],
      },
    ])
    expect(w.events).toEqual(START_LOG_ONLY)
    expect(w.files.has(TARGET)).toBe(false)
    untouched(w)
    reset(w)
    await measure($, [lim('five_hour', 31, R5)])
    expect(logOf(w)).toHaveLength(1)
    expect(lastLog(w).ev).toBe('session.measure')
    expect(lastLog(w).out).toBe('wrote')
  })
}

// 事件日志每个 hook 都整份读、整份写，所以每条紧凑、占一行（不缩进），最新的在最后；usage.json 仍是两格缩进，小窗两种都读得了。
scenario('log: the events file has one compact entry per line while usage.json stays indented', undefined, async (w, $) => {
  await measure($, [lim('five_hour', 20, R5)])
  await measure($, [lim('five_hour', 21, R5), lim('seven_day', 8, R7)])
  await start($)
  const text = w.files.get(EVENTS)!
  const entries = logOf(w)
  expect(entries).toHaveLength(3)
  expect(text, 'wrapper line, one compact entry per line, newest last').toBe(
    '{"schema":1,"events":[\n' + entries.map((e) => JSON.stringify(e)).join(',\n') + '\n]}',
  )
  expect(text.split('\n'), 'the wrapper opens and closes the list').toHaveLength(entries.length + 2)
  expect(text.startsWith('{"schema":1,"events":[\n{"t":'), 'starts with the wrapper, then a compact entry').toBe(true)
  expect(w.files.get(TARGET), 'usage.json keeps its layout').toBe(JSON.stringify(snap(w), null, 2))
  // 条目里的行分隔符（U+2028、U+2029）不能把一条拆成两行：写成转义形式，读回来的值不变。
  await measureChanged($, [lim('five_hour', 22, R5)], ['a\u2028b', 'c\u2029d'])
  const grown = w.files.get(EVENTS)!
  expect(grown.split('\n'), 'still one line per entry').toHaveLength(logOf(w).length + 2)
  expect(/[\u2028\u2029]/.test(grown), 'no raw line separator in the file, they are escaped').toBe(false)
  expect(logOf(w)[logOf(w).length - 1]!.changed, 'the separators read back unchanged').toEqual(['a\u2028b', 'c\u2029d'])
})

scenario('log: entries have exactly the documented keys, and nothing identifying leaks into the file', undefined, async (w, $) => {
  await measure($, [lim('five_hour', 20, R5)])
  await measure($, [lim('five_hour', 10, R5), { kind: 'monthly', percentUsed: 1, resetsAt: iso(R5) }])
  await measure($, [])
  w.usageLimits = [lim('five_hour', 30, R5)]
  await start($)
  w.failing.add('usage')
  await start($)
  w.failing.delete('usage')
  await measure($, [{ kind: 'five_hour', percentUsed: 1 }])
  const entries = logOf(w)
  const keySets = [LOG_KEYS, LOG_KEYS_DROPS, LOG_KEYS, LOG_KEYS, LOG_KEYS, LOG_KEYS_DROPS]
  expect(entries).toHaveLength(keySets.length)
  for (let i = 0; i < keySets.length; i++) expect(Object.keys(entries[i]!), String(i)).toEqual(keySets[i])

  const text = w.files.get(EVENTS)!
  expect(text, 'one compact entry per line').toBe('{"schema":1,"events":[\n' + entries.map((e) => JSON.stringify(e)).join(',\n') + '\n]}')
  const parsed = JSON.parse(text) as { schema: unknown; events: unknown }
  expect(Object.keys(parsed)).toEqual(['schema', 'events'])
  expect(parsed.schema).toBe(1)
  expect(Array.isArray(parsed.events)).toBe(true)
  for (const secret of ['D:/', 'D:\\', 'usage-feed-test', 'proj', START_MARK, 'cwd', '.json', 'prompt', 'model']) {
    expect(text, secret).not.toContain(secret)
  }

  reset(w)
  w.sessionId = 'x'.repeat(200)
  await measure($, [lim('five_hour', 20, R5)])
  expect(lastLog(w).sid).toBe('x'.repeat(128))
  expect(lastLog(w).sid).toHaveLength(128)

  reset(w)
  w.sessionId = 42
  await measure($, [lim('five_hour', 20, R5)])
  expect(lastLog(w).sid).toBeNull()
  expect(lastLog(w).out).toBe('wrote')

  for (const mode of ['deny', 'throw'] as const) {
    reset(w)
    w.failing.delete('id')
    w.failMode = mode
    w.failing.add('id')
    await measure($, [lim('five_hour', 20, R5)])
    expect(lastLog(w).sid, mode).toBeNull()
    expect(lastLog(w).out, mode).toBe('wrote')
    expect(snap(w).windows.five_hour.session_id, mode).toBeNull()
    w.failing.delete('id')
  }
})

for (const mode of ['deny', 'throw'] as const) {
  scenario('log: a failing usage.json write is recorded as failed with write_error (' + mode + ')', undefined, async (w, $) => {
    w.failMode = mode
    w.failWritePaths.add(TARGET)
    expect(await measure($, [lim('five_hour', 31, R5)])).toEqual({ changed: CHANGED })
    expect(w.events).toEqual(MEASURE_EVENTS)
    expect(w.files.has(TARGET)).toBe(false)
    expect(w.files.has(EVENTS)).toBe(true)
    expect(logOf(w)).toEqual([
      {
        t: NOW_MS,
        ev: 'session.measure',
        sid: 'sess-new',
        n: 1,
        kinds: ['five_hour'],
        kept: 1,
        out: 'failed',
        why: WRITE_WHY,
        held: [],
        changed: CHANGED,
      },
    ])

    reset(w)
    w.usageLimits = [lim('five_hour', 30, R5)]
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(w.events).toEqual(START_EVENTS)
    expect(lastLog(w).ev).toBe('session.start')
    expect(lastLog(w).out).toBe('failed')
    expect(lastLog(w).why).toBe(WRITE_WHY)
    expect(lastLog(w).changed).toEqual([])
    expect(w.files.has(TARGET)).toBe(false)

    reset(w)
    stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a') })
    await measure($, [lim('five_hour', 10, R5)])
    expect(lastLog(w).out).toBe('failed')
    expect(lastLog(w).held).toEqual(['five_hour'])
    expect(lastLog(w).why).toBe(WRITE_WHY)

    w.failWritePaths.delete(TARGET)
    await measure($, [lim('five_hour', 10, R5)])
    expect(logOf(w)).toHaveLength(2)
    expect(logOf(w)[1]!.out).toBe('wrote')
    expect(w.files.has(TARGET)).toBe(true)
    expect(snap(w).written_at).toBe(T0)
  })

  scenario('log: a failing log write leaves usage.json written and the hooks quiet (' + mode + ')', undefined, async (w, $) => {
    w.failMode = mode
    w.failWritePaths.add(EVENTS)
    expect(await measure($, [lim('five_hour', 31, R5)])).toEqual({ changed: CHANGED })
    expect(snap(w)).toEqual({ schema: 1, written_at: T0, windows: { five_hour: win(31, R5, T0, 'sess-new') } })
    expect(w.files.has(EVENTS)).toBe(false)
    expect(w.events).toEqual(MEASURE_EVENTS)

    reset(w)
    w.usageLimits = [lim('five_hour', 30, R5)]
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(snap(w).windows.five_hour).toEqual(win(30, R5, T0, 'sess-new'))
    expect(w.files.has(EVENTS)).toBe(false)

    w.failWritePaths.delete(EVENTS)
    await measure($, [lim('five_hour', 32, R5)])
    expect(logOf(w)).toHaveLength(1)
    expect(lastLog(w).out).toBe('wrote')
    expect(lastLog(w).ev).toBe('session.measure')
  })

  scenario('log: a failing log read is treated as an empty log (' + mode + ')', undefined, async (w, $) => {
    await measure($, [lim('five_hour', 20, R5)])
    await measure($, [lim('five_hour', 21, R5)])
    expect(logOf(w)).toHaveLength(2)
    w.failMode = mode
    w.failReadPaths.add(EVENTS)
    expect(await measure($, [lim('five_hour', 22, R5)])).toEqual({ changed: CHANGED })
    expect(snap(w).windows.five_hour).toEqual(win(22, R5, T0, 'sess-new'))
    expect(logOf(w)).toHaveLength(1)
    expect(lastLog(w).n).toBe(1)
    expect(lastLog(w).kinds).toEqual(['five_hour'])
    expect(lastLog(w).out).toBe('wrote')
  })
}

// ---------------------------------------------------------------- 事件日志：上限、坏日志、时钟失败、透传

scenario('log: only the most recent 200 entries are kept', undefined, async (w, $) => {
  const CAP = 200 // 200 是日志条数上限的约定值
  const seed = (n: number): void => {
    w.files.set(EVENTS, JSON.stringify({ schema: 1, events: Array.from({ length: n }, (_, i) => ({ seq: i })) }))
  }
  const seqAt = (i: number): { seq: number } => logOf(w)[i] as unknown as { seq: number }
  const fresh = (): void => {
    expect(lastLog(w).ev).toBe('session.measure')
    expect(lastLog(w).t).toBe(NOW_MS)
    expect(lastLog(w).out).toBe('wrote')
  }

  seed(CAP)
  await measure($, [lim('five_hour', 20, R5)])
  expect(logOf(w)).toHaveLength(CAP)
  expect(seqAt(0)).toEqual({ seq: 1 })
  expect(seqAt(198)).toEqual({ seq: 199 })
  fresh()

  reset(w)
  seed(CAP - 1)
  await measure($, [lim('five_hour', 20, R5)])
  expect(logOf(w)).toHaveLength(CAP)
  expect(seqAt(0)).toEqual({ seq: 0 })
  fresh()

  reset(w)
  seed(350)
  await measure($, [lim('five_hour', 20, R5)])
  expect(logOf(w)).toHaveLength(CAP)
  expect(seqAt(0)).toEqual({ seq: 151 })
  expect(seqAt(198)).toEqual({ seq: 349 })
  fresh()

  reset(w)
  for (let i = 0; i < 205; i++) {
    w.nowMs = NOW_MS + i * 1000
    await measure($, [lim('five_hour', 20, R5)])
  }
  const kept = logOf(w)
  expect(kept).toHaveLength(CAP)
  expect(kept[0]!.t).toBe(NOW_MS + 5000)
  expect(kept[199]!.t).toBe(NOW_MS + 204000)
  for (let i = 1; i < kept.length; i++) expect(kept[i]!.t - kept[i - 1]!.t).toBe(1000)
})

// 非空却读不出的日志（多半是另一个会话正写到一半）先不动：第一次只记下、这一次不写（丢这一条），
// 下一次 hook 读到的仍是同一段才认定真坏了，从空日志重新开始。文件不存在、读失败或是空文件则直接重新开始。
const UNUSABLE_LOGS: [string, string][] = (() => {
  const sd = [{ seq: 1 }]
  return [
    ['broken JSON', 'not json{'],
    ['whitespace only', '   '],
    ['JSON null', 'null'],
    ['top level array', '[]'],
    ['top level number', '42'],
    ['schema 2', JSON.stringify({ schema: 2, events: sd })],
    ['schema as string', JSON.stringify({ schema: '1', events: sd })],
    ['schema as boolean', JSON.stringify({ schema: true, events: sd })],
    ['schema missing', JSON.stringify({ events: sd })],
    ['events is an object', JSON.stringify({ schema: 1, events: { seq: 1 } })],
    ['events null', JSON.stringify({ schema: 1, events: null })],
    ['events string', JSON.stringify({ schema: 1, events: 'x' })],
    ['events missing', JSON.stringify({ schema: 1 })],
    ['two leading BOMs (only one is stripped)', '﻿﻿' + JSON.stringify({ schema: 1, events: sd })],
  ]
})()

scenario('log: an unusable existing log is left alone once and replaced when the same text is still there at the next entry', undefined, async (w, $) => {
  for (const [label, text] of UNUSABLE_LOGS) {
    reset(w)
    w.files.set(EVENTS, text)
    await measure($, [lim('five_hour', 20, R5)])
    // 第一次：日志原样留着，这一条没记，usage.json 照常写。
    expect(w.files.get(EVENTS), label + ': first entry leaves the log alone').toBe(text)
    expect(writesTo(w, EVENTS), label + ': no log write').toHaveLength(0)
    expect(snap(w).windows.five_hour, label).toEqual(win(20, R5, T0, 'sess-new'))
    // 第二次：仍是同一段，从空日志重新开始，里面只有这一条。
    await measure($, [lim('five_hour', 21, R5)])
    expect(logOf(w), label + ': second entry starts a fresh log').toHaveLength(1)
    expect(lastLog(w).ev, label).toBe('session.measure')
    expect(lastLog(w).out, label).toBe('wrote')
    expect(w.files.get(EVENTS), label).not.toContain('seq')
    expect(snap(w).windows.five_hour, label).toEqual(win(21, R5, T0, 'sess-new'))
  }

  reset(w)
  w.files.set(EVENTS, JSON.stringify({ schema: 1, events: [{ seq: 1 }] }))
  await measure($, [lim('five_hour', 20, R5)])
  expect(logOf(w)).toHaveLength(2)
  expect(logOf(w)[0] as unknown as { seq: number }).toEqual({ seq: 1 })
  expect(lastLog(w).ev).toBe('session.measure')
  expect(lastLog(w).out).toBe('wrote')
})

// 事件日志超过 256 KB（按字符数）与坏文件同一条路：不解析，当作读不出，走两次确认。本插件自己的日志最大约 130 KB。
scenario('log: an events log over 256 KB is unusable, one of exactly 256 KB is read', undefined, async (w, $) => {
  const CAP = 256 * 1024
  const log = JSON.stringify({ schema: 1, events: [{ seq: 1 }] })
  const padded = (size: number) => log + ' '.repeat(size - log.length)
  w.files.set(EVENTS, padded(CAP))
  await measure($, [lim('five_hour', 20, R5)])
  expect(logOf(w), 'exactly 256 KB: read, the old entry is kept').toHaveLength(2)
  expect(logOf(w)[0] as unknown as { seq: number }).toEqual({ seq: 1 })

  // 大字符串用 === 比，不用 toBe：断言失败时 toBe 会去渲染几 MB 的差异，把测试进程拖死。
  reset(w)
  const over = padded(CAP + 1)
  w.files.set(EVENTS, over)
  await measure($, [lim('five_hour', 20, R5)])
  expect(w.files.get(EVENTS) === over, 'one over: left alone the first time').toBe(true)
  await measure($, [lim('five_hour', 21, R5)])
  expect(logOf(w), 'and replaced the second time').toHaveLength(1)

  // 别的写入方留下的几 MB 文件：同样的路，不会被每个 hook 解析一遍，第二次就被小日志换掉。
  reset(w)
  const huge = JSON.stringify({ schema: 1, events: [{ blob: 'x'.repeat(3 * 1024 * 1024) }] })
  w.files.set(EVENTS, huge)
  await measure($, [lim('five_hour', 20, R5)])
  expect(w.files.get(EVENTS) === huge, 'left alone the first time').toBe(true)
  await measure($, [lim('five_hour', 21, R5)])
  expect(logOf(w)).toHaveLength(1)
  expect(w.files.get(EVENTS)!.length).toBeLessThan(1000)
})

scenario('log: a missing, empty or unreadable log starts fresh at once, without a second look', undefined, async (w, $) => {
  await measure($, [lim('five_hour', 20, R5)])
  expect(logOf(w), 'missing').toHaveLength(1)

  reset(w)
  w.files.set(EVENTS, '')
  await measure($, [lim('five_hour', 20, R5)])
  expect(logOf(w), 'empty file').toHaveLength(1)
  expect(lastLog(w).out).toBe('wrote')

  reset(w)
  w.files.set(EVENTS, JSON.stringify({ schema: 1, events: [{ seq: 1 }, { seq: 2 }] }))
  w.failReadPaths.add(EVENTS)
  await measure($, [lim('five_hour', 20, R5)])
  expect(logOf(w), 'unreadable').toHaveLength(1)
  expect(w.files.get(EVENTS)).not.toContain('seq')
})

scenario('log: a half-written log costs one entry, not the whole log', undefined, async (w, $) => {
  // 另一个会话写到一半：这一次读到的是被截断的文本。
  for (let i = 0; i < 3; i++) {
    w.nowMs = NOW_MS + i * 1000
    await measure($, [lim('five_hour', 20 + i, R5)])
  }
  const whole = w.files.get(EVENTS)!
  expect(logOf(w)).toHaveLength(3)
  const cut = whole.slice(0, Math.floor(whole.length / 2))
  w.files.set(EVENTS, cut)
  w.nowMs = NOW_MS + 3000
  await measure($, [lim('five_hour', 23, R5)])
  expect(w.files.get(EVENTS), 'the half-written text is not overwritten').toBe(cut)
  expect(snap(w).windows.five_hour.used_percentage, 'usage.json is unaffected').toBe(23)
  // 对方写完了：它自己的那份完整日志（再加它的一条）还在，我们接着追加，之前的记录一条没丢。
  const finished = JSON.stringify({
    schema: 1,
    events: [...(JSON.parse(whole) as { events: unknown[] }).events, { seq: 'theirs' }],
  })
  w.files.set(EVENTS, finished)
  w.nowMs = NOW_MS + 4000
  await measure($, [lim('five_hour', 24, R5)])
  const entries = logOf(w)
  expect(entries, 'three old + theirs + this one; only the cut-off entry is lost').toHaveLength(5)
  expect(entries.map((e) => e.t).filter((t) => t !== undefined), 'old entries kept in order').toEqual([
    NOW_MS,
    NOW_MS + 1000,
    NOW_MS + 2000,
    NOW_MS + 4000,
  ])
  expect(entries[3] as unknown as { seq: string }).toEqual({ seq: 'theirs' })
})

scenario('log: a different unusable text at the next entry starts the count again', undefined, async (w, $) => {
  w.files.set(EVENTS, 'first bad text{')
  await measure($, [lim('five_hour', 20, R5)])
  w.files.set(EVENTS, 'second bad text{')
  await measure($, [lim('five_hour', 21, R5)])
  expect(w.files.get(EVENTS), 'a different text is a first sighting again').toBe('second bad text{')
  await measure($, [lim('five_hour', 22, R5)])
  expect(logOf(w), 'the second sighting of it resets').toHaveLength(1)
})

scenario('log: two unusable texts that differ only in the middle are told apart', undefined, async (w, $) => {
  // 同样的长度、同样的开头和结尾，只差中间一个字符：记号要看全文，不能只取首尾各一截。
  const bad = (middle: string) => '{' + 'a'.repeat(300) + middle + 'a'.repeat(300) + '{'
  w.files.set(EVENTS, bad('1'))
  await measure($, [lim('five_hour', 20, R5)])
  w.files.set(EVENTS, bad('2'))
  await measure($, [lim('five_hour', 21, R5)])
  expect(w.files.get(EVENTS) === bad('2'), 'the changed text is a first sighting, not a second').toBe(true)
  await measure($, [lim('five_hour', 22, R5)])
  expect(logOf(w), 'and only its own second sighting resets').toHaveLength(1)
})

scenario('log: a log that was repaired in between is appended to, and a later damage counts from the start', undefined, async (w, $) => {
  const bad = 'not json{'
  w.files.set(EVENTS, bad)
  await measure($, [lim('five_hour', 20, R5)])
  expect(w.files.get(EVENTS)).toBe(bad)
  w.files.set(EVENTS, JSON.stringify({ schema: 1, events: [{ seq: 1 }] }))
  await measure($, [lim('five_hour', 21, R5)])
  expect(logOf(w), 'appended, not reset').toHaveLength(2)
  // 同一段坏文本再出现：之前的记号已随成功的写入清掉，这是第一次，不是第二次。
  w.files.set(EVENTS, bad)
  await measure($, [lim('five_hour', 22, R5)])
  expect(w.files.get(EVENTS), 'a first sighting again').toBe(bad)
  await measure($, [lim('five_hour', 23, R5)])
  expect(logOf(w), 'now it resets').toHaveLength(1)
})

scenario('log: a log that cannot be written leaves the count standing so the next entry tries again', undefined, async (w, $) => {
  w.files.set(EVENTS, 'not json{')
  await measure($, [lim('five_hour', 20, R5)])
  w.failWritePaths.add(EVENTS)
  await measure($, [lim('five_hour', 21, R5)])
  expect(w.files.get(EVENTS), 'the reset write was refused').toBe('not json{')
  w.failWritePaths.delete(EVENTS)
  await measure($, [lim('five_hour', 22, R5)])
  expect(logOf(w), 'still the same text, so the next try resets at once').toHaveLength(1)
})

// 带 BOM 的日志（别的写入方或编辑器存过一次）与快照、请求、确认一样能读，不能因此被当成坏的而重置。
scenario('log: an existing log with one leading BOM is read and its entries are kept', undefined, async (w, $) => {
  w.files.set(EVENTS, '﻿' + JSON.stringify({ schema: 1, events: [{ seq: 1 }, { seq: 2 }] }))
  await measure($, [lim('five_hour', 20, R5)])
  const entries = logOf(w)
  expect(entries).toHaveLength(3)
  expect(entries[0] as unknown as { seq: number }).toEqual({ seq: 1 })
  expect(entries[1] as unknown as { seq: number }).toEqual({ seq: 2 })
  expect(lastLog(w).ev).toBe('session.measure')
  expect(w.files.get(EVENTS)!.charCodeAt(0), 'the BOM is not written back').not.toBe(0xfeff)
})

// 重写时只留普通对象、且紧凑后不超过 2 KB 的旧条目，别的写入方留下的臃肿日志不会被永久保留。
scenario('log: foreign entries that are not plain objects or are over 2 KB are dropped when the log is rewritten', undefined, async (w, $) => {
  // JSON.stringify({ blob: 'x'.repeat(n) }) 长 n + 11：2037 个字符正好 2048，2038 个字符是 2049。
  const blob = (n: number) => ({ blob: 'x'.repeat(n) })
  expect(JSON.stringify(blob(2037)).length).toBe(2048)
  const entries = [
    'a string',
    42,
    null,
    true,
    [1, 2],
    { seq: 1 },
    blob(2037),
    blob(2038),
    blob(100_000),
    { seq: 2 },
  ]
  w.files.set(EVENTS, JSON.stringify({ schema: 1, events: entries }))
  await measure($, [lim('five_hour', 20, R5)])
  const kept = logOf(w) as unknown as Record<string, unknown>[]
  expect(kept, 'only plain objects up to 2048 characters survive, in order').toHaveLength(4)
  expect(kept[0]).toEqual({ seq: 1 })
  expect(kept[1]).toEqual(blob(2037))
  expect(kept[2]).toEqual({ seq: 2 })
  expect(kept[3]!.ev).toBe('session.measure')
  expect(w.files.get(EVENTS)!.length, 'the bloated entries are really gone from the file').toBeLessThan(3000)
})

scenario('log: junk is dropped before the newest 200 are kept', undefined, async (w, $) => {
  // 205 条正常的，后面跟 10 条杂物（排在最新的位置）。先过滤再截断：留下最新的 199 条正常的加这一条；
  // 若先截断再过滤，杂物会挤掉正常的条目。
  const valid = Array.from({ length: 205 }, (_, i) => ({ seq: i }))
  const junk = Array.from({ length: 10 }, () => 'junk')
  w.files.set(EVENTS, JSON.stringify({ schema: 1, events: [...valid, ...junk] }))
  await measure($, [lim('five_hour', 20, R5)])
  const kept = logOf(w) as unknown as { seq?: number; ev?: string }[]
  expect(kept).toHaveLength(200)
  expect(kept[0]).toEqual({ seq: 6 })
  expect(kept[198]).toEqual({ seq: 204 })
  expect(kept[199]!.ev).toBe('session.measure')
})

for (const mode of ['deny', 'throw'] as const) {
  scenario('log: a failing clock (' + mode + ')', undefined, async (w, $) => {
    w.failMode = mode
    w.failing.add('clock')
    expect(await measure($, [lim('five_hour', 20, R5)])).toEqual({ changed: CHANGED })
    expect(w.events).toEqual(['session.measure', 'clock.now', 'clock.now'])
    expect(w.files.size).toBe(0)
    expect(w.writes).toHaveLength(0)

    reset(w)
    w.usageLimits = [lim('five_hour', 30, R5)]
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(w.events).toEqual(['session.start', 'session.usage', 'clock.now', 'clock.now'])
    expect(w.files.size).toBe(0)

    reset(w)
    await measure($, [])
    expect(w.events).toEqual(['session.measure', 'clock.now'])
    expect(w.files.size).toBe(0)

    reset(w)
    await measure($, [{ kind: 'monthly', percentUsed: 1, resetsAt: iso(R5) }])
    expect(w.events).toEqual(['session.measure', 'clock.now'])
    expect(w.files.size).toBe(0)

    w.failing.delete('clock')
    reset(w)
    w.clockScript = ['fail']
    await measure($, [lim('five_hour', 20, R5)])
    expect(w.events).toEqual(['session.measure', 'clock.now', 'clock.now', 'session.id', 'fs.read', 'fs.write'])
    untouched(w)
    expect(w.files.has(TARGET)).toBe(false)
    expect(logOf(w)).toEqual([
      {
        t: NOW_MS,
        ev: 'session.measure',
        sid: 'sess-new',
        n: 1,
        kinds: ['five_hour'],
        kept: 1,
        out: 'skipped',
        why: 'bad_clock',
        held: [],
        changed: CHANGED,
      },
    ])

    for (const bad of [NaN, Infinity]) {
      reset(w)
      w.clockScript = [bad]
      await measure($, [lim('five_hour', 20, R5)])
      expect(logOf(w)).toEqual([
        {
          t: NOW_MS,
          ev: 'session.measure',
          sid: 'sess-new',
          n: 1,
          kinds: ['five_hour'],
          kept: 1,
          out: 'skipped',
          why: 'bad_clock',
          held: [],
          changed: CHANGED,
        },
      ])
      untouched(w)
    }

    reset(w)
    w.usageLimits = [lim('five_hour', 30, R5)]
    w.clockScript = ['fail']
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(w.events).toEqual(['session.start', 'session.usage', 'clock.now', 'clock.now', 'session.id', 'fs.read', 'fs.write'])
    expect(lastLog(w).ev).toBe('session.start')
    expect(lastLog(w).why).toBe('bad_clock')
    expect(lastLog(w).out).toBe('skipped')
    expect(lastLog(w).kept).toBe(1)
    expect(lastLog(w).changed).toEqual([])
  })
}

scenario('pass-through: both hooks return what the rest of the chain produced, also when the module fails inside', undefined, async (w, $) => {
  const clearFails = (): void => {
    w.failing.clear()
    w.failWritePaths.clear()
    w.failReadPaths.clear()
  }
  // 引擎在层与层之间复制返回值，引用对不上；这里核对内容与底层返回的对象逐字段相同。
  clearFails()
  expect(await measure($, [lim('five_hour', 20, R5)])).toStrictEqual(w.lastMeasure)

  reset(w)
  clearFails()
  expect(await measure($, [])).toStrictEqual(w.lastMeasure)

  reset(w)
  clearFails()
  expect(await measure($, [{ kind: 'monthly', percentUsed: 1, resetsAt: iso(R5) }])).toStrictEqual(w.lastMeasure)

  reset(w)
  clearFails()
  w.failing.add('write')
  expect(await measure($, [lim('five_hour', 20, R5)])).toStrictEqual(w.lastMeasure)

  reset(w)
  clearFails()
  w.failing.add('clock')
  expect(await measure($, [lim('five_hour', 20, R5)])).toStrictEqual(w.lastMeasure)

  reset(w)
  clearFails()
  w.failWritePaths.add(TARGET)
  expect(await measure($, [lim('five_hour', 20, R5)])).toStrictEqual(w.lastMeasure)

  reset(w)
  clearFails()
  w.usageLimits = [lim('five_hour', 30, R5)]
  expect(await start($)).toStrictEqual(w.lastStart)

  reset(w)
  clearFails()
  w.usageLimits = [lim('five_hour', 30, R5)]
  w.failing.add('usage')
  expect(await start($)).toStrictEqual(w.lastStart)

  reset(w)
  clearFails()
  w.usageLimits = [lim('five_hour', 30, R5)]
  w.failing.add('write')
  expect(await start($)).toStrictEqual(w.lastStart)
})

// ---------------------------------------------------------------- 刷新请求：纯函数

const REQ_ID = '3fa91c07b2de'
const REQ_AT = 1791279531.301
// 协议里 get_usage 的真实形状。\u00b7 是标签里真实出现的中点。
const REAL_PLAN = {
  plan: {
    status: 'ok',
    plan: 'Max',
    windows: [
      { label: '5-hour limit', percentUsed: 67, resetsAt: '2026-10-06T10:49:59.664Z', resetsIn: '1h 10m' },
      { label: 'Weekly \u00b7 all models', percentUsed: 57, resetsAt: '2026-10-11T05:59:59.664Z', resetsIn: '4d 20h' },
      { label: 'Weekly \u00b7 Fable', percentUsed: 0, resetsAt: '2026-10-11T06:00:00.000Z', resetsIn: '4d 20h' },
    ],
    extraUsage: { enabled: false, percentUsed: 0, spent: '0.00', monthlyLimit: '155.00', currency: 'AUD' },
  },
  context: { session: 'self', status: 'ok', tokensUsed: 413648, contextWindow: 1000000, percentUsed: 41 },
}

const reqText = (id: unknown, at: unknown, schema: unknown = 1, extra?: Record<string, unknown>): string =>
  JSON.stringify({ schema, id, requested_at: at, ...extra })

test('app pure: parseRequest accepts the protocol example and ignores extra fields', () => {
  const text = JSON.stringify({ schema: 1, id: REQ_ID, requested_at: REQ_AT })
  const got = parseRequest(text)
  expect(got, 'protocol example').toEqual({ id: REQ_ID, requestedAt: REQ_AT })
  expect(Object.keys(got ?? {}), 'protocol example keys').toEqual(['id', 'requestedAt'])
  const extra = parseRequest(reqText(REQ_ID, REQ_AT, 1, { note: 'x', n: 2 }))
  expect(extra, 'extra fields').toEqual({ id: REQ_ID, requestedAt: REQ_AT })
  expect(Object.keys(extra ?? {}), 'extra fields keys').toEqual(['id', 'requestedAt'])
})

test('app pure: parseRequest id length and charset', () => {
  const rows: { label: string; id: unknown; ok: boolean }[] = [
    { label: 'one char', id: 'a', ok: true },
    { label: '32 chars', id: 'a'.repeat(32), ok: true },
    { label: '33 chars', id: 'a'.repeat(33), ok: false },
    { label: 'mixed case and digits', id: 'AbC09zZ1', ok: true },
    { label: 'empty', id: '', ok: false },
    { label: 'hyphen', id: 'ab-cd', ok: false },
    { label: 'space', id: 'ab cd', ok: false },
    { label: 'cjk', id: 'id中文', ok: false },
    { label: 'trailing newline', id: REQ_ID + '\n', ok: false },
    { label: 'slash', id: 'a/b', ok: false },
    { label: 'backslash', id: 'a\\b', ok: false },
  ]
  for (const row of rows) {
    const got = parseRequest(reqText(row.id, REQ_AT))
    if (row.ok) expect(got, row.label).toEqual({ id: row.id, requestedAt: REQ_AT })
    else expect(got, row.label).toBeNull()
  }
})

test('app pure: parseRequest rejects a non-string or missing id', () => {
  const rows: { label: string; text: string }[] = [
    { label: 'numeric id', text: JSON.stringify({ schema: 1, id: 12, requested_at: REQ_AT }) },
    { label: 'null id', text: JSON.stringify({ schema: 1, id: null, requested_at: REQ_AT }) },
    { label: 'array id', text: JSON.stringify({ schema: 1, id: ['a'], requested_at: REQ_AT }) },
    { label: 'missing id', text: JSON.stringify({ schema: 1, requested_at: REQ_AT }) },
  ]
  for (const row of rows) expect(parseRequest(row.text), row.label).toBeNull()
})

test('app pure: parseRequest schema must be the number 1', () => {
  const rows: { label: string; schema: unknown }[] = [
    { label: 'schema 2', schema: 2 },
    { label: "schema '1'", schema: '1' },
    { label: 'schema true', schema: true },
  ]
  for (const row of rows) expect(parseRequest(reqText(REQ_ID, REQ_AT, row.schema)), row.label).toBeNull()
  expect(parseRequest(JSON.stringify({ id: REQ_ID, requested_at: REQ_AT })), 'schema missing').toBeNull()
})

test('app pure: parseRequest requested_at must be a finite number', () => {
  const bad: { label: string; text: string }[] = [
    { label: 'missing', text: JSON.stringify({ schema: 1, id: REQ_ID }) },
    { label: 'null', text: JSON.stringify({ schema: 1, id: REQ_ID, requested_at: null }) },
    { label: 'string', text: JSON.stringify({ schema: 1, id: REQ_ID, requested_at: '5' }) },
    { label: 'true', text: JSON.stringify({ schema: 1, id: REQ_ID, requested_at: true }) },
    { label: 'infinity', text: '{"schema":1,"id":"' + REQ_ID + '","requested_at":1e999}' },
  ]
  for (const row of bad) expect(parseRequest(row.text), row.label).toBeNull()
  const good: { label: string; at: number }[] = [
    { label: 'zero', at: 0 },
    { label: 'negative', at: -3.5 },
    { label: 'fraction', at: 1.25 },
  ]
  for (const row of good) expect(parseRequest(reqText(REQ_ID, row.at)), row.label).toEqual({ id: REQ_ID, requestedAt: row.at })
})

test('app pure: parseRequest strips one leading BOM and rejects bad roots', () => {
  const text = JSON.stringify({ schema: 1, id: REQ_ID, requested_at: REQ_AT })
  expect(parseRequest('\ufeff' + text), 'one bom').toEqual({ id: REQ_ID, requestedAt: REQ_AT })
  expect(parseRequest('\ufeff\ufeff' + text), 'two boms').toBeNull()
  const bad: { label: string; text: unknown }[] = [
    { label: 'broken json', text: '{schema:1' },
    { label: 'truncated', text: '{"schema":1,"id":"' + REQ_ID + '","requested_a' },
    { label: 'empty', text: '' },
    { label: 'array', text: '[]' },
    { label: 'null', text: 'null' },
    { label: 'number', text: '42' },
    { label: 'string', text: '"x"' },
    { label: 'undefined', text: undefined },
    { label: 'null input', text: null },
    { label: 'number input', text: 5 },
    { label: 'object input', text: { schema: 1, id: REQ_ID, requested_at: REQ_AT } },
  ]
  for (const row of bad) expect(parseRequest(row.text), row.label).toBeNull()
})

test('app pure: parseAck accepts the protocol example and any status string', () => {
  const text = JSON.stringify({ schema: 1, id: REQ_ID, status: 'ok', at: 1791279533.912, windows: 2 })
  const got = parseAck(text)
  expect(got, 'protocol example').toEqual({ id: REQ_ID, status: 'ok' })
  expect(Object.keys(got ?? {}), 'protocol example keys').toEqual(['id', 'status'])
  expect(parseAck(JSON.stringify({ schema: 1, id: REQ_ID, status: 'unavailable' })), 'unavailable').toEqual({
    id: REQ_ID,
    status: 'unavailable',
  })
  expect(parseAck(JSON.stringify({ schema: 1, id: REQ_ID, status: 'zzz' })), 'any string').toEqual({
    id: REQ_ID,
    status: 'zzz',
  })
})

test('app pure: parseAck id follows the same whitelist', () => {
  const rows: { label: string; text: string }[] = [
    { label: '33 chars', text: JSON.stringify({ schema: 1, id: 'a'.repeat(33), status: 'ok' }) },
    { label: 'hyphen', text: JSON.stringify({ schema: 1, id: 'ab-cd', status: 'ok' }) },
    { label: 'numeric id', text: JSON.stringify({ schema: 1, id: 12, status: 'ok' }) },
    { label: 'missing id', text: JSON.stringify({ schema: 1, status: 'ok' }) },
  ]
  for (const row of rows) expect(parseAck(row.text), row.label).toBeNull()
})

test('app pure: parseAck rejects a bad schema, a non-string status, a truncated file and non-string input, and strips one BOM', () => {
  const rows: { label: string; text: unknown }[] = [
    { label: 'schema 2', text: JSON.stringify({ schema: 2, id: REQ_ID, status: 'ok' }) },
    { label: 'status missing', text: JSON.stringify({ schema: 1, id: REQ_ID }) },
    { label: 'status null', text: JSON.stringify({ schema: 1, id: REQ_ID, status: null }) },
    { label: 'status number', text: JSON.stringify({ schema: 1, id: REQ_ID, status: 1 }) },
    { label: 'status object', text: JSON.stringify({ schema: 1, id: REQ_ID, status: { ok: true } }) },
    { label: 'truncated', text: '{"schema":1,"id":"' + REQ_ID + '","sta' },
    { label: 'undefined', text: undefined },
    { label: 'null input', text: null },
    { label: 'number input', text: 5 },
  ]
  for (const row of rows) expect(parseAck(row.text), row.label).toBeNull()
  const text = JSON.stringify({ schema: 1, id: REQ_ID, status: 'ok', at: 1, windows: 0 })
  expect(parseAck('\ufeff' + text), 'one bom').toEqual({ id: REQ_ID, status: 'ok' })
})

test('app pure: extractPayload prefers structuredContent over a text block', () => {
  const onlyStructured = extractPayload({ structuredContent: REAL_PLAN })
  expect(onlyStructured, 'structured only').toEqual(REAL_PLAN)
  const onlyText = extractPayload({ content: [{ type: 'text', text: JSON.stringify(REAL_PLAN) }] })
  expect(onlyText, 'text only').toEqual(REAL_PLAN)
  const structured = { plan: { status: 'ok', from: 'structured' } }
  const both = extractPayload({
    structuredContent: structured,
    content: [{ type: 'text', text: JSON.stringify({ plan: { status: 'ok', from: 'text' } }) }],
  })
  expect(both, 'structured wins').toEqual(structured)
  expect((both as { plan: { from: string } }).plan.from, 'not the text body').toBe('structured')
})

test('app pure: extractPayload falls back when structuredContent is not an object', () => {
  const body = { plan: { status: 'ok' } }
  const rows: { label: string; structuredContent: unknown }[] = [
    { label: 'string', structuredContent: '{"plan":1}' },
    { label: 'array', structuredContent: [{ plan: 1 }] },
    { label: 'null', structuredContent: null },
  ]
  for (const row of rows) {
    const got = extractPayload({
      structuredContent: row.structuredContent,
      content: [{ type: 'text', text: JSON.stringify(body) }],
    })
    expect(got, row.label).toEqual(body)
  }
})

test('app pure: extractPayload returns null for a bad response shape', () => {
  const rows: { label: string; res: unknown }[] = [
    { label: 'null res', res: null },
    { label: 'undefined res', res: undefined },
    { label: 'string res', res: 'x' },
    { label: 'number res', res: 5 },
    { label: 'array res', res: [] },
    { label: 'content string', res: { content: 'nope' } },
    { label: 'content object', res: { content: { type: 'text', text: '{}' } } },
    { label: 'content missing', res: {} },
    { label: 'json array', res: { content: [{ type: 'text', text: '[1,2]' }] } },
    { label: 'json null', res: { content: [{ type: 'text', text: 'null' }] } },
    { label: 'json number', res: { content: [{ type: 'text', text: '7' }] } },
    { label: 'json string', res: { content: [{ type: 'text', text: '"hi"' }] } },
    { label: 'broken json', res: { content: [{ type: 'text', text: '{no' }] } },
    { label: 'empty content', res: { content: [] } },
    { label: 'image only', res: { content: [{ type: 'image' }] } },
    { label: 'text not a string', res: { content: [{ type: 'text', text: 12 }] } },
  ]
  for (const row of rows) expect(extractPayload(row.res), row.label).toBeNull()
})

test('app pure: extractPayload does not fall through after the first text block', () => {
  const good = JSON.stringify({ plan: { status: 'ok' } })
  expect(
    extractPayload({ content: [{ type: 'text', text: '{bad' }, { type: 'text', text: good }] }),
    'first text is broken json',
  ).toBeNull()
  expect(
    extractPayload({ content: [{ type: 'text', text: '[1]' }, { type: 'text', text: good }] }),
    'first text is a json array',
  ).toBeNull()
  expect(
    extractPayload({
      content: [{ type: 'image' }, { type: 'text', text: 1 }, { type: 'text', text: good }],
    }),
    'skips non-text then takes the first string',
  ).toEqual({ plan: { status: 'ok' } })
})

test('app pure: extractPayload does not strip a BOM from a text block, unlike the files, so a BOM-prefixed reply is unusable', () => {
  // 文件读取会去掉开头的一个 BOM；桌面应用返回的文本块原样解析，这个差别保留，并由这个用例钉住。
  const good = JSON.stringify({ plan: { status: 'ok' } })
  expect(extractPayload({ content: [{ type: 'text', text: '\uFEFF' + good }] }), 'one BOM').toBeNull()
  expect(
    extractPayload({ content: [{ type: 'text', text: '\uFEFF' + good }, { type: 'text', text: good }] }),
    'no fall through to the next block',
  ).toBeNull()
})

// 决定：structuredContent 是对象却没有 plan、文本块里有 plan 时，以文本块为准。MCP 约定文本块是同一份结果的 JSON 序列化，
// 两边本应一致；结构化的那份缺了我们要的 plan、文本块里却有，说明结构化的那份是别的形状。照旧让结构化的赢，
// 就会白白报 app_unavailable:none，小窗显示 no limits，尽管数据就在文本里。
test('app pure: extractPayload takes the text block when structuredContent is an object without a plan', () => {
  const text = [{ type: 'text', text: JSON.stringify(REAL_PLAN) }]
  const rows: { label: string; structuredContent: unknown }[] = [
    { label: 'empty object', structuredContent: {} },
    { label: 'another shape', structuredContent: { ok: true, context: { session: 'self' } } },
    { label: 'plan is a number', structuredContent: { plan: 5 } },
    { label: 'plan is an array', structuredContent: { plan: [] } },
    { label: 'plan is null', structuredContent: { plan: null } },
    { label: 'plan is a string', structuredContent: { plan: 'ok' } },
  ]
  for (const row of rows) {
    expect(extractPayload({ structuredContent: row.structuredContent, content: text }), row.label).toEqual(REAL_PLAN)
  }
})

test('app pure: extractPayload still prefers structuredContent when it has a plan, even if the text differs', () => {
  const structured = { plan: { status: 'ok', from: 'structured' } }
  const rows: { label: string; content: unknown }[] = [
    { label: 'text with another plan', content: [{ type: 'text', text: JSON.stringify({ plan: { status: 'ok', from: 'text' } }) }] },
    { label: 'text is not json', content: [{ type: 'text', text: 'not json' }] },
    { label: 'no content', content: undefined },
    { label: 'content is not a list', content: 'nope' },
  ]
  for (const row of rows) {
    const got = extractPayload({ structuredContent: structured, content: row.content })
    expect(got, row.label).toEqual(structured)
    expect((got as { plan: { from: string } }).plan.from, row.label + ' is the structured one').toBe('structured')
  }
})

test('app pure: extractPayload keeps the structured object when the text block has no plan either or is unusable', () => {
  const structured = { ok: true }
  const rows: { label: string; content: unknown }[] = [
    { label: 'text without a plan', content: [{ type: 'text', text: JSON.stringify({ other: 1 }) }] },
    { label: 'text with a plan that is not an object', content: [{ type: 'text', text: JSON.stringify({ plan: 7 }) }] },
    { label: 'broken json', content: [{ type: 'text', text: '{no' }] },
    { label: 'json array', content: [{ type: 'text', text: '[1]' }] },
    { label: 'no text block', content: [{ type: 'image' }] },
    { label: 'second text block has the plan', content: [{ type: 'text', text: '{bad' }, { type: 'text', text: JSON.stringify(REAL_PLAN) }] },
    { label: 'text block over the size cap', content: [{ type: 'text', text: JSON.stringify(REAL_PLAN) + ' '.repeat(64 * 1024) }] },
  ]
  for (const row of rows) {
    expect(extractPayload({ structuredContent: structured, content: row.content }), row.label).toEqual(structured)
  }
  // 结构化的是空对象，文本块也读不出：结果仍是那个空对象（下游据此报 app_unavailable），不是 null。
  expect(extractPayload({ structuredContent: {}, content: [] }), 'empty structured, empty content').toEqual({})
})

test('app pure: extractPayload caps the text block at 64 KB but not a structured object', () => {
  const body = JSON.stringify(REAL_PLAN)
  const padded = (size: number) => body + ' '.repeat(size - body.length)
  const CAP = 64 * 1024
  expect(extractPayload({ content: [{ type: 'text', text: padded(CAP) }] }), 'exactly 64 KB is read').toEqual(REAL_PLAN)
  expect(extractPayload({ content: [{ type: 'text', text: padded(CAP + 1) }] }), 'one over is unusable').toBeNull()
  // 超限算作解析失败：不拿后面的块补位。
  expect(
    extractPayload({ content: [{ type: 'text', text: padded(CAP + 1) }, { type: 'text', text: body }] }),
    'a later block does not stand in',
  ).toBeNull()
  // 结构化内容已经是解析好的对象，没有解析可省，不设上限。
  const big = { plan: { status: 'ok', windows: [] }, blob: 'x'.repeat(200_000) }
  expect(extractPayload({ structuredContent: big }), 'structured is not capped').toBe(big)
})

test('app pure: extractPayload ignores isError and returns the parsed plan', () => {
  const got = extractPayload({
    isError: true,
    content: [{ type: 'text', text: JSON.stringify(REAL_PLAN) }],
  })
  expect(got, 'isError still parsed').toEqual(REAL_PLAN)
  const plan = (got as { plan: { windows: { label: string }[] } }).plan
  expect(plan.status, 'plan status').toBe('ok')
  expect(plan.windows[0].label, 'first label').toBe('5-hour limit')
  expect(plan.windows[1].label, 'weekly all models').toBe('Weekly \u00b7 all models')
  expect(plan.windows[2].label, 'weekly fable').toBe('Weekly \u00b7 Fable')
})

// ---------------------------------------------------------------- 刷新请求：映射与合并

const fr = (used: number, resets: number) => ({ used_percentage: used, resets_at: resets })
const planOf = (windows: unknown, status: unknown = 'ok') => ({ plan: { status, windows } })
const w5 = (percentUsed: unknown, resetsAt: unknown) => ({ label: '5-hour limit', percentUsed, resetsAt })
const w7 = (percentUsed: unknown, resetsAt: unknown) => ({ label: 'Weekly \u00b7 all models', percentUsed, resetsAt })
const EMPTY_APP = { status: null, fresh: {}, drops: {}, count: 0 }
const FABLE = 'Weekly \u00b7 Fable'
const ISO_5H = '2026-10-06T10:49:59.664Z'
const RESET_5H = 1791283799
const ISO_7D = '2026-10-11T05:59:59.664Z'
const RESET_7D = 1791698399

test('app pure: windowsFromApp maps the real plan and drops the per-model weekly window', () => {
  const got = windowsFromApp(REAL_PLAN)
  expect(got, 'real plan').toEqual({
    status: 'ok',
    fresh: {
      five_hour: { used_percentage: 67, resets_at: RESET_5H },
      seven_day: { used_percentage: 57, resets_at: RESET_7D },
    },
    drops: {},
    count: 3,
  })
  expect(Object.keys(got), 'result keys').toEqual(['status', 'fresh', 'drops', 'count'])
  expect(Object.keys(got.fresh), 'no spend_limit').toEqual(['five_hour', 'seven_day'])
  expect('spend_limit' in got.fresh, 'spend_limit absent').toBe(false)
})

test('app pure: windowsFromApp keeps a non-ok status and still maps windows', () => {
  // status 不是 'ok' 时照常映射，要不要用由调用方决定。
  const mapped = windowsFromApp(planOf([w5(67, ISO_5H)], 'not_applicable'))
  expect(mapped, 'not_applicable still maps').toEqual({
    status: 'not_applicable',
    fresh: { five_hour: { used_percentage: 67, resets_at: RESET_5H } },
    drops: {},
    count: 1,
  })
  const rows: { label: string; status: unknown }[] = [
    { label: 'missing', status: undefined },
    { label: 'number', status: 5 },
    { label: 'null', status: null },
    { label: 'object', status: { ok: true } },
  ]
  for (const row of rows) {
    const got = windowsFromApp({ plan: { ...(row.status === undefined ? {} : { status: row.status }), windows: [w5(1, ISO_5H)] } })
    expect(got.status, row.label).toBeNull()
    expect(got.fresh, row.label + ' still mapped').toEqual({ five_hour: { used_percentage: 1, resets_at: RESET_5H } })
  }
  expect(windowsFromApp(planOf([], 'x'.repeat(200))).status, 'clipped to 128').toBe('x'.repeat(128))
})

test('app pure: windowsFromApp returns an empty result when plan is not an object', () => {
  const rows: { label: string; payload: unknown }[] = [
    { label: 'empty object', payload: {} },
    { label: 'null', payload: null },
    { label: 'string', payload: 'x' },
    { label: 'array', payload: [] },
    { label: 'number', payload: 5 },
    { label: 'plan null', payload: { plan: null } },
    { label: 'plan string', payload: { plan: 'x' } },
    { label: 'plan array', payload: { plan: [] } },
    { label: 'plan number', payload: { plan: 5 } },
  ]
  for (const row of rows) expect(windowsFromApp(row.payload), row.label).toEqual(EMPTY_APP)
})

test('app pure: windowsFromApp treats a non-array windows list as empty', () => {
  const rows: { label: string; plan: Record<string, unknown> }[] = [
    { label: 'missing', plan: { status: 'ok' } },
    { label: 'null', plan: { status: 'ok', windows: null } },
    { label: 'string', plan: { status: 'ok', windows: 'x' } },
    { label: 'object', plan: { status: 'ok', windows: {} } },
    { label: 'number', plan: { status: 'ok', windows: 5 } },
  ]
  for (const row of rows) {
    expect(windowsFromApp({ plan: row.plan }), row.label).toEqual({ status: 'ok', fresh: {}, drops: {}, count: 0 })
  }
})

test('app pure: windowsFromApp only looks at the first 12 windows', () => {
  const filler = { label: FABLE, percentUsed: 1, resetsAt: ISO_5H }
  const past = [...Array.from({ length: 12 }, () => filler), w5(9, ISO_5H)]
  expect(windowsFromApp(planOf(past)), 'index 12 ignored').toEqual({ status: 'ok', fresh: {}, drops: {}, count: 12 })
  const at11 = [...Array.from({ length: 11 }, () => filler), w5(9, ISO_5H)]
  expect(windowsFromApp(planOf(at11)), 'index 11 mapped').toEqual({
    status: 'ok',
    fresh: { five_hour: { used_percentage: 9, resets_at: RESET_5H } },
    drops: {},
    count: 12,
  })
  const thirty = [...Array.from({ length: 30 }, () => filler)]
  expect(windowsFromApp(planOf(thirty)).count, '30 clipped to 12').toBe(12)
})

test('app pure: windowsFromApp drops a bad percent and keeps the boundary values', () => {
  const bad: { label: string; percent: unknown }[] = [
    { label: 'negative', percent: -0.1 },
    { label: 'nan', percent: Number.NaN },
    { label: 'infinity', percent: Number.POSITIVE_INFINITY },
    { label: 'string', percent: '67' },
    { label: 'null', percent: null },
    { label: 'true', percent: true },
  ]
  for (const row of bad) {
    const got = windowsFromApp(planOf([w5(row.percent, ISO_5H)]))
    expect(got.fresh, row.label).toEqual({})
    expect(got.drops, row.label).toEqual({ bad_percent: 1 })
  }
  expect(windowsFromApp(planOf([{ label: '5-hour limit', resetsAt: ISO_5H }])).drops, 'missing percent').toEqual({
    bad_percent: 1,
  })
  const good: { label: string; percent: number }[] = [
    { label: 'zero', percent: 0 },
    { label: 'over 100', percent: 100.5 },
    { label: 'fraction', percent: 66.4 },
  ]
  for (const row of good) {
    expect(windowsFromApp(planOf([w5(row.percent, ISO_5H)])).fresh, row.label).toEqual({
      five_hour: { used_percentage: row.percent, resets_at: RESET_5H },
    })
  }
})

test('app pure: windowsFromApp classifies a bad resetsAt and stops at the first reason', () => {
  const missing: { label: string; window: Record<string, unknown> }[] = [
    { label: 'missing', window: { label: '5-hour limit', percentUsed: 1 } },
    { label: 'null', window: w5(1, null) },
    { label: 'number', window: w5(1, 1791283799) },
  ]
  for (const row of missing) {
    expect(windowsFromApp(planOf([row.window])).drops, row.label).toEqual({ no_resets_at: 1 })
  }
  const bad: { label: string; resetsAt: string }[] = [
    { label: 'garbage', resetsAt: 'garbage' },
    { label: 'empty', resetsAt: '' },
    { label: 'impossible date', resetsAt: '2027-13-45T99:99:99Z' },
    { label: 'floors to zero', resetsAt: '1970-01-01T00:00:00.999Z' },
    { label: 'epoch', resetsAt: '1970-01-01T00:00:00Z' },
    { label: 'before epoch', resetsAt: '1969-12-31T23:59:59Z' },
  ]
  for (const row of bad) {
    expect(windowsFromApp(planOf([w5(1, row.resetsAt)])).drops, row.label).toEqual({ bad_resets_at: 1 })
  }
  expect(windowsFromApp(planOf([w5(1, '1970-01-01T00:00:01.000Z')])).fresh, 'one second').toEqual({
    five_hour: { used_percentage: 1, resets_at: 1 },
  })
  expect(windowsFromApp(planOf([w5(-1, 'garbage')])).drops, 'percent checked first').toEqual({ bad_percent: 1 })
})

test('app pure: windowsFromApp keeps the first valid window of a kind', () => {
  const rows: { label: string; windows: unknown[]; fresh: unknown; drops: unknown }[] = [
    {
      label: 'second five_hour ignored',
      windows: [w5(10, ISO_5H), w5(20, ISO_5H)],
      fresh: { five_hour: { used_percentage: 10, resets_at: RESET_5H } },
      drops: {},
    },
    {
      label: 'invalid five_hour does not occupy the kind',
      windows: [w5(-1, ISO_5H), w5(20, ISO_5H)],
      fresh: { five_hour: { used_percentage: 20, resets_at: RESET_5H } },
      drops: { bad_percent: 1 },
    },
    {
      label: 'later invalid five_hour is not counted',
      windows: [w5(10, ISO_5H), w5(-1, 'garbage')],
      fresh: { five_hour: { used_percentage: 10, resets_at: RESET_5H } },
      drops: {},
    },
    {
      label: 'second seven_day ignored',
      windows: [w7(10, ISO_7D), w7(20, ISO_7D)],
      fresh: { seven_day: { used_percentage: 10, resets_at: RESET_7D } },
      drops: {},
    },
    {
      label: 'invalid seven_day does not occupy the kind',
      windows: [w7(-1, ISO_7D), w7(20, ISO_7D)],
      fresh: { seven_day: { used_percentage: 20, resets_at: RESET_7D } },
      drops: { bad_percent: 1 },
    },
    {
      label: 'later invalid seven_day is not counted',
      windows: [w7(10, ISO_7D), w7(-1, 'garbage')],
      fresh: { seven_day: { used_percentage: 10, resets_at: RESET_7D } },
      drops: {},
    },
  ]
  for (const row of rows) {
    const got = windowsFromApp(planOf(row.windows))
    expect(got.fresh, row.label).toEqual(row.fresh)
    expect(got.drops, row.label).toEqual(row.drops)
  }
})

test('app pure: windowsFromApp maps labels without trimming and prefers 5-hour', () => {
  const mapped: { label: string; kind: string }[] = [
    { label: '5-hour limit', kind: 'five_hour' },
    { label: '5-HOUR LIMIT', kind: 'five_hour' },
    { label: 'Weekly \u00b7 all models', kind: 'seven_day' },
    { label: 'WEEKLY \u00b7 ALL MODELS', kind: 'seven_day' },
    { label: 'weekly all models', kind: 'seven_day' },
    { label: 'Weekly 5-hour all models', kind: 'five_hour' },
  ]
  for (const row of mapped) {
    const got = windowsFromApp(planOf([{ label: row.label, percentUsed: 1, resetsAt: ISO_5H }]))
    expect(Object.keys(got.fresh), row.label).toEqual([row.kind])
  }
  const unmapped = [
    'Weekly \u00b7 Fable',
    'Weekly \u00b7 Sonnet only',
    'Fable weekly - all models',
    ' Weekly all models',
    'all models',
    '',
    'Daily',
  ]
  for (const label of unmapped) {
    const got = windowsFromApp(planOf([{ label, percentUsed: 1, resetsAt: ISO_5H }]))
    expect(got.fresh, label === '' ? 'empty label' : label).toEqual({})
    expect(got.drops, label === '' ? 'empty label drops' : label + ' drops').toEqual({})
  }
})

test('app pure: windowsFromApp skips unmapped and non-object items without counting a drop', () => {
  const windows = [null, 5, 'x', [], { label: 5 }, { label: null }, {}, w5(4, ISO_5H)]
  const got = windowsFromApp(planOf(windows))
  expect(got.fresh, 'five_hour mapped').toEqual({ five_hour: { used_percentage: 4, resets_at: RESET_5H } })
  expect(got.drops, 'no drops').toEqual({})
  expect(got.count, 'full length').toBe(windows.length)
  const fable = windowsFromApp(planOf([{ label: FABLE, percentUsed: -1 }, { label: FABLE }, w5(4, ISO_5H)]))
  expect(fable.fresh, 'fable ignored').toEqual({ five_hour: { used_percentage: 4, resets_at: RESET_5H } })
  expect(fable.drops, 'fable drops empty').toEqual({})
})

test('app pure: windowsFromApp accumulates one drop per mapped invalid window', () => {
  const got = windowsFromApp(
    planOf([w5(-1, ISO_5H), { label: 'Weekly \u00b7 all models', percentUsed: 1 }, w7(1, 'garbage')]),
  )
  expect(got.drops, 'three reasons').toEqual({ bad_percent: 1, no_resets_at: 1, bad_resets_at: 1 })
  expect(got.fresh, 'none mapped').toEqual({})
  expect(got.count, 'all three seen').toBe(3)
})

const NOW = T0 + 600
const OLD_AT = T0 - 300
const SID = 'sess-new'

test('app pure: mergeApp keeps stored windows when the app reading has none', () => {
  const old = {
    five_hour: win(40, R5, OLD_AT, 'old-a'),
    seven_day: win(10, R7, OLD_AT, 'old-b'),
    spend_limit: win(1, R7, OLD_AT, 'old-c'),
  }
  const all = mergeApp(old, {}, NOW, SID)
  expect(all.windows, 'all kept').toEqual(old)
  expect(all.held, 'nothing held').toEqual([])
  const one = mergeApp(old, { five_hour: fr(50, R5) }, NOW, SID)
  expect(one.windows.seven_day, 'seven_day untouched').toEqual(old.seven_day)
  expect(one.windows.spend_limit, 'spend_limit untouched').toEqual(old.spend_limit)
})

test('app pure: mergeApp takes an app window when nothing is stored', () => {
  const got = mergeApp({}, { five_hour: fr(12.5, R5) }, NOW, SID)
  expect(got.windows, 'new window').toEqual({ five_hour: win(12.5, R5, NOW, SID) })
  expect(got.held, 'nothing held').toEqual([])
})

test('app pure: mergeApp takes the app value in the same period when it is not lower', () => {
  const old = { five_hour: win(40, R5, OLD_AT, 'old-a') }
  const rows: { label: string; used: number; resets: number }[] = [
    { label: 'equal', used: 40, resets: R5 + 60 },
    { label: 'higher', used: 62.5, resets: R5 + 120 },
    { label: 'minus 120', used: 41, resets: R5 - 120 },
  ]
  for (const row of rows) {
    const got = mergeApp(old, { five_hour: fr(row.used, row.resets) }, NOW, SID)
    expect(got.windows, row.label).toEqual({ five_hour: win(row.used, row.resets, NOW, SID) })
    expect(got.held, row.label).toEqual([])
  }
})

test('app pure: mergeApp confirms a rounding gap under 1 and takes the app value from a gap of 1 up', () => {
  const confirm: { label: string; used: number }[] = [
    { label: 'gap 0.4', used: 66.4 },
    { label: 'gap 0.5', used: 66.5 },
    { label: 'gap 0.99', used: 66.99 },
  ]
  for (const row of confirm) {
    const old = { five_hour: win(row.used, R5, OLD_AT, 'old-a') }
    const got = mergeApp(old, { five_hour: fr(66, R5 + 60) }, NOW, SID)
    expect(got.windows, row.label).toEqual({ five_hour: win(row.used, R5, NOW, SID) })
    expect(got.held, row.label).toEqual([])
  }
  // 账号值是点击那一刻现取的：比已存值低 1 个点以上，说明已存的是过时的数，取账号值（原来是保留旧值并计入 held）。
  const take: { label: string; used: number }[] = [
    { label: 'gap 1', used: 67 },
    { label: 'gap 1.01', used: 67.01 },
    { label: 'gap 4', used: 70 },
  ]
  for (const row of take) {
    const old = { five_hour: win(row.used, R5, OLD_AT, 'old-a') }
    const got = mergeApp(old, { five_hour: fr(66, R5 + 60) }, NOW, SID)
    expect(got.windows, row.label).toEqual({ five_hour: win(66, R5 + 60, NOW, SID) })
    expect(got.held, row.label).toEqual([])
  }
})

test('app pure: mergeApp takes a live value far below the stored one in the same period (a raised limit)', () => {
  // 套餐升级、上限变高：同一周期里账号百分比从 80 掉到 20，不能等到 resets_at 往后走才显示。
  const rows: { label: string; kind: 'five_hour' | 'seven_day'; reset: number; stored: number; live: number; sid: string | null }[] = [
    { label: '5h from another session', kind: 'five_hour', reset: R5, stored: 80, live: 20, sid: 'old-a' },
    { label: '5h from the same session', kind: 'five_hour', reset: R5, stored: 80, live: 20, sid: SID },
    { label: '5h stored without a session id', kind: 'five_hour', reset: R5, stored: 80, live: 20, sid: null },
    { label: '7d, which would otherwise stay stale for days', kind: 'seven_day', reset: R7, stored: 90, live: 30, sid: 'old-b' },
  ]
  const one = <V>(kind: 'five_hour' | 'seven_day', value: V) => (kind === 'five_hour' ? { five_hour: value } : { seven_day: value })
  for (const row of rows) {
    const old = one(row.kind, win(row.stored, row.reset, OLD_AT, row.sid))
    const got = mergeApp(old, one(row.kind, fr(row.live, row.reset + 90)), NOW, SID)
    expect(got.windows, row.label).toEqual(one(row.kind, win(row.live, row.reset + 90, NOW, SID)))
    expect(got.held, row.label).toEqual([])
  }
})

test('app pure: mergeApp still holds a reading from an earlier period, however high', () => {
  const old = { five_hour: win(10, R5, OLD_AT, 'old-a') }
  for (const used of [5, 10, 99]) {
    const got = mergeApp(old, { five_hour: fr(used, R5 - 121) }, NOW, SID)
    expect(got.windows, String(used)).toEqual(old)
    expect(got.held, String(used)).toEqual(['five_hour'])
  }
})

test('app pure: mergeApp follows the later reset across periods', () => {
  const later = mergeApp({ five_hour: win(90, R5, OLD_AT, 'old-a') }, { five_hour: fr(3, R5 + 121) }, NOW, SID)
  expect(later.windows, 'later period').toEqual({ five_hour: win(3, R5 + 121, NOW, SID) })
  expect(later.held, 'later period').toEqual([])
  const earlier = mergeApp({ five_hour: win(10, R5, OLD_AT, 'old-a') }, { five_hour: fr(99, R5 - 121) }, NOW, SID)
  expect(earlier.windows, 'earlier period').toEqual({ five_hour: win(10, R5, OLD_AT, 'old-a') })
  expect(earlier.held, 'earlier period').toEqual(['five_hour'])
})

test('app pure: mergeApp treats 120 seconds as the same period and 121 as a new one', () => {
  // 同一周期里相差不到 1 的是取整差，保留已存的值和重置时间；换了周期则整个取账号的。靠这一点区分边界
  // （同一周期里低得多的账号值现在也会被取走，分不出边界）。
  const old = { five_hour: win(66.4, R5, OLD_AT, 'old-a') }
  const plus120 = mergeApp(old, { five_hour: fr(66, R5 + 120) }, NOW, SID)
  expect(plus120.windows, '+120 is the same period: confirmed').toEqual({ five_hour: win(66.4, R5, NOW, SID) })
  expect(plus120.held, '+120 held').toEqual([])
  const plus121 = mergeApp(old, { five_hour: fr(66, R5 + 121) }, NOW, SID)
  expect(plus121.windows, '+121 is a new period: takes app').toEqual({ five_hour: win(66, R5 + 121, NOW, SID) })
  expect(plus121.held, '+121 held').toEqual([])
  const minus120 = mergeApp(old, { five_hour: fr(66, R5 - 120) }, NOW, SID)
  expect(minus120.windows, '-120 is the same period: confirmed').toEqual({ five_hour: win(66.4, R5, NOW, SID) })
  expect(minus120.held, '-120 held').toEqual([])
  const minus121 = mergeApp(old, { five_hour: fr(99, R5 - 121) }, NOW, SID)
  expect(minus121.windows, '-121 is an earlier period: keeps old').toEqual(old)
  expect(minus121.held, '-121 held').toEqual(['five_hour'])
  // 同一周期的另一侧：差得多的账号值取走，resets_at 用账号的（+120、-120 都在同一周期内）。
  const wide = { five_hour: win(90, R5, OLD_AT, 'old-a') }
  expect(mergeApp(wide, { five_hour: fr(3, R5 + 120) }, NOW, SID).windows, '+120 far below').toEqual({
    five_hour: win(3, R5 + 120, NOW, SID),
  })
  expect(mergeApp(wide, { five_hour: fr(3, R5 - 120) }, NOW, SID).windows, '-120 far below').toEqual({
    five_hour: win(3, R5 - 120, NOW, SID),
  })
})

test('app pure: mergeApp keeps kind order and lists held kinds in kind order', () => {
  const old = {
    five_hour: win(40, R5, OLD_AT, 'old-a'),
    seven_day: win(10, R7, OLD_AT, 'old-b'),
    spend_limit: win(1, R7, OLD_AT, 'old-c'),
  }
  // held 现在只剩"账号读数属于更早的周期"一种，所以用更早的 resets_at 造出被挡住的 kind。
  const fresh = {
    spend_limit: fr(2, R7),
    seven_day: fr(9, R7 - 121),
    five_hour: fr(39.5, R5 + 60),
  }
  const got = mergeApp(old, fresh, NOW, SID)
  expect(Object.keys(got.windows), 'kind order').toEqual(['five_hour', 'seven_day', 'spend_limit'])
  expect(got.windows.five_hour, 'confirmed').toEqual(win(40, R5, NOW, SID))
  expect(got.windows.seven_day, 'held').toEqual(old.seven_day)
  expect(got.windows.spend_limit, 'taken').toEqual(win(2, R7, NOW, SID))
  expect(got.held, 'only seven_day').toEqual(['seven_day'])
  const two = mergeApp(
    old,
    { spend_limit: fr(0, R7 - 121), five_hour: fr(30, R5 - 5 * 3600), seven_day: fr(11, R7) },
    NOW,
    SID,
  )
  expect(two.held, 'two kinds in kind order').toEqual(['five_hour', 'spend_limit'])
  expect(two.windows.seven_day, 'the third is taken').toEqual(win(11, R7, NOW, SID))
})

test('app pure: mergeApp writes a null session id and a fixed field order', () => {
  const taken = mergeApp({}, { five_hour: fr(12, R5) }, NOW, null)
  expect(taken.windows.five_hour, 'null session').toEqual(win(12, R5, NOW, null))
  expect(Object.keys(taken.windows.five_hour), 'taken field order').toEqual([
    'used_percentage',
    'resets_at',
    'observed_at',
    'session_id',
  ])
  const confirmed = mergeApp({ five_hour: win(66.4, R5, OLD_AT, 'old-a') }, { five_hour: fr(66, R5) }, NOW, SID)
  expect(Object.keys(confirmed.windows.five_hour), 'confirmed field order').toEqual([
    'used_percentage',
    'resets_at',
    'observed_at',
    'session_id',
  ])
})

test('app pure: mergeApp does not mutate its inputs', () => {
  const old = { five_hour: win(66.4, R5, OLD_AT, 'old-a'), seven_day: win(10, R7, OLD_AT, 'old-b') }
  const fresh = { five_hour: fr(66, R5 + 60), seven_day: fr(9, R7) }
  const oldText = JSON.stringify(old)
  const freshText = JSON.stringify(fresh)
  mergeApp(old, fresh, NOW, SID)
  expect(JSON.stringify(old), 'old unchanged').toBe(oldText)
  expect(JSON.stringify(fresh), 'fresh unchanged').toBe(freshText)
})

// ---------------------------------------------------------------- 刷新请求：一次尝试（两个服务器名共用一个期限）
// 引擎把 hook 里抛出的任何错误都包成 HooksError，经引擎的用例分不出"第一个名字的错误名"和"第二个的"，
// 所以这一组直接给 callApp 一个手工搭的引擎替身（只有它用到的 clock.after 与 mcp.call）：错误对象原样递到模块手里，
// 期限的到点由测试亲手触发，不依赖 mock 时钟。引擎层面的流程用例见后面（mcp_timeout、mcp_error 等）。

type StubTimer = { ms: number; fn: () => void; cancelled: boolean }
// behave 决定每个服务器名的调用怎么应答：返回 promise（挂着、拒绝、成功都行），或直接抛。
const makeAppStub = (behave: (server: string, nth: number) => Promise<unknown>, afterThrows?: unknown) => {
  const timers: StubTimer[] = []
  const calls: { server: string; tool: string; args: unknown }[] = []
  const $ = {
    clock: {
      after: (ms: number, fn: () => void) => {
        if (afterThrows !== undefined) throw afterThrows
        const timer: StubTimer = { ms, fn, cancelled: false }
        timers.push(timer)
        return { cancel: () => void (timer.cancelled = true) }
      },
    },
    mcp: {
      call: (server: string, tool: string, args: unknown) => {
        calls.push({ server, tool, args })
        return behave(server, calls.length)
      },
    },
  } as unknown as EngineInterface
  return { $, timers, calls }
}
const named = (name: string): Error => Object.assign(new Error('stub ' + name), { name })
// 让出若干个微任务，让挂着的 callApp 走到下一个等待点。
const settle = async (): Promise<void> => {
  for (let i = 0; i < 10; i++) await Promise.resolve()
}

test('app call: both server names share one 5 s timer', async () => {
  const stub = makeAppStub((server) => (server === 'ccd_session_mgmt' ? Promise.reject(named('Gone')) : Promise.resolve(APP_OK)))
  const got = await callApp(stub.$)
  expect(got, 'the second name answered').toEqual({ kind: 'answered', res: APP_OK })
  expect(stub.calls, 'both names, in order, empty arguments').toEqual([
    { server: 'ccd_session_mgmt', tool: 'get_usage', args: {} },
    { server: 'ccd-session-mgmt', tool: 'get_usage', args: {} },
  ])
  expect(stub.timers.map((t) => t.ms), 'one timer for the whole attempt').toEqual([5000])
  expect(stub.timers[0]!.cancelled, 'cancelled at the end').toBe(true)
})

test('app call: an answer from the first name ends the attempt', async () => {
  const stub = makeAppStub(() => Promise.resolve(APP_OK))
  expect(await callApp(stub.$)).toEqual({ kind: 'answered', res: APP_OK })
  expect(stub.calls.map((c) => c.server)).toEqual(['ccd_session_mgmt'])
  expect(stub.timers).toHaveLength(1)
  expect(stub.timers[0]!.cancelled).toBe(true)
  // 空结果、isError 的结果都是"回应了"，怎么处理归调用方。
  for (const res of [null, undefined, { isError: true }]) {
    const one = makeAppStub(() => Promise.resolve(res))
    expect(await callApp(one.$), String(res)).toEqual({ kind: 'answered', res })
    expect(one.calls, String(res)).toHaveLength(1)
  }
})

test('app call: when both names throw it reports the first name\'s error, not the fallback\'s', async () => {
  const first = named('FirstServerGone')
  const second = named('SecondServerGone')
  const stub = makeAppStub((server) => Promise.reject(server === 'ccd_session_mgmt' ? first : second))
  const got = await callApp(stub.$)
  expect(got.kind).toBe('failed')
  if (got.kind !== 'failed') return
  expect(got.err, 'the first error, by identity').toBe(first)
  expect((got.err as Error).name).toBe('FirstServerGone')
  expect(stub.calls, 'both names were really called').toHaveLength(2)
  expect(stub.timers[0]!.cancelled).toBe(true)
})

test('app call: a synchronous throw from mcp.call counts as that name failing', async () => {
  const first = named('SyncFirst')
  const stub = makeAppStub((server) => {
    if (server === 'ccd_session_mgmt') throw first
    return Promise.resolve(APP_OK)
  })
  expect(await callApp(stub.$), 'falls through to the second name').toEqual({ kind: 'answered', res: APP_OK })
  const both = makeAppStub((server) => {
    throw named(server === 'ccd_session_mgmt' ? 'SyncOne' : 'SyncTwo')
  })
  const got = await callApp(both.$)
  expect(got.kind === 'failed' && (got.err as Error).name).toBe('SyncOne')
})

test('app call: the deadline passing while the first name hangs ends the attempt and the second name is never called', async () => {
  const stub = makeAppStub(() => new Promise(() => {}))
  const pending = callApp(stub.$)
  await settle()
  expect(stub.calls, 'waiting on the first name').toHaveLength(1)
  stub.timers[0]!.fn()
  expect(await pending).toEqual({ kind: 'timeout' })
  expect(stub.calls, 'the second name was not tried').toHaveLength(1)
  expect(stub.timers[0]!.cancelled).toBe(true)
})

test('app call: the second name only gets what is left of the one deadline', async () => {
  // 第一个名字失败得慢（期限里的 4 秒），第二个挂住：一次尝试到期限就结束，不是再等一个 5 秒。
  let failFirst: () => void = () => {}
  const stub = makeAppStub((server) =>
    server === 'ccd_session_mgmt'
      ? new Promise((_resolve, reject) => {
          failFirst = () => reject(named('SlowFail'))
        })
      : new Promise(() => {}),
  )
  const pending = callApp(stub.$)
  await settle()
  failFirst()
  await settle()
  expect(stub.calls.map((c) => c.server), 'the second name is now waiting').toEqual(['ccd_session_mgmt', 'ccd-session-mgmt'])
  expect(stub.timers, 'no second timer for the second name').toHaveLength(1)
  stub.timers[0]!.fn()
  expect(await pending).toEqual({ kind: 'timeout' })
})

test('app call: no call is made once the deadline has already passed', async () => {
  // 期限在发第一个调用之前就到了（计时器同步触发）：一个调用都不发，同样是 timeout。
  const stub = makeAppStub(() => Promise.resolve(APP_OK))
  const eager = stub.$ as unknown as { clock: { after: (ms: number, fn: () => void) => unknown } }
  const original = eager.clock.after
  eager.clock.after = (ms, fn) => {
    const timer = original(ms, fn)
    fn()
    return timer
  }
  expect(await callApp(stub.$)).toEqual({ kind: 'timeout' })
  expect(stub.calls, 'nothing was called').toEqual([])
  expect(stub.timers[0]!.cancelled).toBe(true)
})

test('app call: a result that arrives after the deadline is ignored, rejected or not', async () => {
  for (const mode of ['resolve', 'reject'] as const) {
    let settleLate: () => void = () => {}
    const stub = makeAppStub(
      () =>
        new Promise((resolve, reject) => {
          settleLate = () => (mode === 'resolve' ? resolve(APP_OK) : reject(named('TooLate')))
        }),
    )
    const pending = callApp(stub.$)
    await settle()
    stub.timers[0]!.fn()
    expect(await pending, mode).toEqual({ kind: 'timeout' })
    // 期限之后才返回或才失败：结果没人用，也不能变成未处理的 promise 拒绝（那会让这个用例或后面的用例出错）。
    settleLate()
    await settle()
    expect(stub.calls, mode).toHaveLength(1)
  }
})

test('app call: a timer that cannot be set fails the attempt without sending get_usage', async () => {
  // $.clock.after 同步抛异常几乎不会发生（被 hook 拒绝时不抛、只是回调永不执行）；发生时按失败处理，一个调用都不发。
  const cannot = named('NoTimer')
  const stub = makeAppStub(() => Promise.resolve(APP_OK), cannot)
  const got = await callApp(stub.$)
  expect(got).toEqual({ kind: 'failed', err: cannot })
  expect(stub.calls, 'get_usage was never sent').toEqual([])
})

test('app call: the timer is cancelled on every outcome', async () => {
  const outcomes: { label: string; behave: (server: string) => Promise<unknown>; fire: boolean }[] = [
    { label: 'answered', behave: () => Promise.resolve(APP_OK), fire: false },
    { label: 'failed', behave: () => Promise.reject(named('Gone')), fire: false },
    { label: 'timeout', behave: () => new Promise(() => {}), fire: true },
  ]
  for (const row of outcomes) {
    const stub = makeAppStub(row.behave)
    const pending = callApp(stub.$)
    await settle()
    if (row.fire) stub.timers[0]!.fn()
    await pending
    expect(stub.timers, row.label).toHaveLength(1)
    expect(stub.timers[0]!.cancelled, row.label).toBe(true)
  }
})

// ---------------------------------------------------------------- 刷新请求：流程
// 每个用例一个世界：同一个测试里不能再注册同名 hook（clock.now 注册两次会直接拒绝装载）。
// 不给 dataDir 的话安全闸不注册任何 hook，定时检查不会发生。
// mock 时钟只在 advance 越过到期时间时触发；一次 advance(3000) 会把这一期里所有 await 跑完。

const REQUEST = DATA_DIR + '/refresh-request.json'
const ACK = DATA_DIR + '/refresh-ack.json'
const POLL = 3000 // 定时检查间隔；故意写字面量，不从模块里导入，免得常量改坏时测试跟着一起错
const TICK1 = T0 + 3 // 第一期定时检查时的引擎时间（秒）

const makeAppWorld = (on: On) => {
  const a = {
    files: new Map<string, string>(),
    events: [] as string[], // 经过引擎的事件名，按顺序（含 clock.every、clock.after）
    writes: [] as { path: string; text: string }[], // 成功的 fs.write，norm 路径；被拒绝的不记
    exists: [] as string[], // 每次 fs.exists 的 norm 路径
    mcpCalls: [] as { server: string; tool: string; args: unknown }[],
    afterMs: [] as number[], // 每次 clock.after 的等待毫秒
    mcp: {} as Record<string, unknown>, // 服务器名 -> 返回值，或 'throw'、'hang'、'late'、'slowfail'。'late' 的调用各自挂起，由测试用 a.release 按调用顺序放出，放出后调用的返回值是 APP_OK；'slowfail' 的调用各自挂起，由测试用 a.failCall 按调用顺序让它们以异常结束；没配置的服务器等同 'throw'
    failClock: false, // 为真时 clock.now 被拒绝
    clockScript: [] as ('ok' | 'fail' | number)[], // 前几次 clock.now：'fail' 拒绝，'ok' 放行；数字直接作为这次 clock.now 的返回值，用来模拟系统时间被调过（mock 时钟本身只能往前走）；用完后回到 failClock 的取值
    denyEvery: 0, // 接下来这么多次 clock.every 被拒绝（每次拒绝减一）；被拒绝的那一期之后整个间隔静默结束，不抛异常
    denyAfter: false, // 为真时每次 clock.after 都被拒绝：不抛异常，回调永不执行
    release: [] as (() => void)[], // 'late' 模式下各个挂起的调用，按调用顺序；调用 release[i]() 放出第 i 个
    failCall: [] as (() => void)[], // 'slowfail' 模式下各个挂起的调用，按调用顺序；调用 failCall[i]() 让第 i 个以异常结束（失败得慢）
    failExists: false,
    failWritePaths: new Set<string>(), // 这些路径（norm 形式）的 fs.write 被拒绝
    sessionId: 'sess-new' as unknown,
    usageLimits: [] as unknown[], // session.usage 返回的限额列表；默认空，session.start 因此读不到时间
    reads: [] as string[], // 共用钩子记下的 fs.read 路径（norm 形式），这一组不断言它
    lastStart: undefined as unknown, // 同上：共用钩子记下的 session.start 返回值
    lastMeasure: undefined as unknown, // 同上：共用钩子记下的 session.measure 返回值
    clock: undefined as unknown as MockClock,
  }
  // 最先注册，才能看到之后所有事件；clock.now 的故障注入也放在这里，因为不能再注册第二个 clock.now。
  on('*', async (_$, e, next) => {
    const name = String(next.event)
    if (!BOOT_NOISE.has(name)) a.events.push(name)
    if (name === 'clock.after') a.afterMs.push((e as unknown as { ms: number }).ms)
    if (name === 'clock.every' && a.denyEvery > 0) {
      a.denyEvery -= 1
      return { deny: 'every refused' } as never
    }
    if (name === 'clock.after' && a.denyAfter) return { deny: 'after refused' } as never
    if (name === 'clock.now') {
      const step = a.clockScript.shift()
      if (typeof step === 'number') return { value: step } as never
      if (step === 'fail' || (step === undefined && a.failClock)) return { deny: 'clock down' } as never
    }
    return next(e)
  })
  a.clock = mock.clock(on, { now: NOW_MS })
  on('fs.exists', (_$, e) => {
    a.exists.push(norm(e.path))
    if (a.failExists) return { deny: 'exists refused' }
    return { value: a.files.has(norm(e.path)) }
  })
  sharedHooks(on, a, {
    refuse: (what) => ({ deny: what + ' refused' }),
    readRefused: () => false,
    writeRefused: (p) => a.failWritePaths.has(p),
    sessionRefused: () => false,
  })
  on('mcp.call', (_$, e) => {
    a.mcpCalls.push({ server: e.server, tool: e.tool, args: e.args })
    const item = a.mcp[e.server]
    if (item === 'hang') return new Promise(() => {})
    if (item === 'late') {
      return new Promise((resolve) => {
        a.release.push(() => resolve({ value: APP_OK as never }))
      })
    }
    if (item === 'slowfail') {
      return new Promise<never>((_resolve, reject) => {
        a.failCall.push(() => reject(new Error('slow failure')))
      })
    }
    if (item === 'throw' || item === undefined) throw new Error('no such server')
    return { value: item as never }
  })
  on('session.end', (_$, e) => ({ sessionId: e.sessionId }))
  return a
}
type AppWorld = ReturnType<typeof makeAppWorld>

const appScenario = (name: string, body: (a: AppWorld, $: Engine) => Promise<void>): void => {
  test(name, { options: { dataDir: DATA_DIR } }, async ($, on) => {
    await body(makeAppWorld(on), $)
  })
}

// 把启动阶段的动作清掉（含 session.start 记下的事件日志文件），后面只看定时检查引起的动作。
const quiet = (a: AppWorld): void => {
  a.events.length = 0
  a.writes.length = 0
  a.exists.length = 0
  a.mcpCalls.length = 0
  a.afterMs.length = 0
  a.files.delete(EVENTS)
}
// 启动：session.start 布下定时器，然后清记录。
const boot = async (a: AppWorld, $: Engine): Promise<void> => {
  await start($)
  quiet(a)
}
const tick = (a: AppWorld, periods = 1): Promise<void> => a.clock.advance(POLL * periods)
// 请求文件：requested_at 相对下一期的时刻写，ageSeconds 为正表示请求比那一刻早。
const stageRequest = (a: AppWorld, id: unknown, ageSeconds = 1, extra: Record<string, unknown> = {}): void => {
  a.files.set(
    REQUEST,
    JSON.stringify({ schema: 1, id, requested_at: (a.clock.now() + POLL) / 1000 - ageSeconds, ...extra }),
  )
}
const appLog = (a: AppWorld): LogEntry[] => {
  const parsed: unknown = JSON.parse(a.files.get(EVENTS) ?? '{"schema":1,"events":[]}')
  const events = (parsed as { events: LogEntry[] }).events
  return events.filter((e) => e.ev === 'refresh.app')
}
const whys = (a: AppWorld): string[] => appLog(a).map((e) => e.why)
const textResult = (obj: unknown) => ({ isError: false, content: [{ type: 'text', text: JSON.stringify(obj) }] })
// get_usage 的返回：重置时间用已有常量 R5、R7，使合并结果的 resets_at 是整数秒。
const appPayload = (p5 = 67, p7 = 57) => ({
  plan: {
    status: 'ok',
    plan: 'Max',
    windows: [
      { label: '5-hour limit', percentUsed: p5, resetsAt: iso(R5), resetsIn: '1h 10m' },
      { label: 'Weekly \u00b7 all models', percentUsed: p7, resetsAt: iso(R7), resetsIn: '4d 20h' },
      { label: 'Weekly \u00b7 Fable', percentUsed: 0, resetsAt: iso(R7), resetsIn: '4d 20h' },
    ],
  },
  context: { session: 'self', status: 'ok' },
})
const APP_OK = textResult(appPayload())
const okSnap = () => ({
  schema: 1,
  written_at: TICK1,
  windows: {
    five_hour: win(67, R5, TICK1, 'sess-new'),
    seven_day: win(57, R7, TICK1, 'sess-new'),
  },
})
const okAck = (id: string) => ({ schema: 1, id, status: 'ok', at: TICK1, windows: 2 })
const wroteLog = () => ({
  t: TICK1 * 1000,
  ev: 'refresh.app',
  sid: 'sess-new',
  n: 3,
  kinds: ['five_hour', 'seven_day'],
  kept: 2,
  out: 'wrote',
  why: '',
  held: [] as string[],
  changed: [] as string[],
})
const skipLog = (tSec: number, why: string) => ({
  t: tSec * 1000,
  ev: 'refresh.app',
  sid: 'sess-new',
  n: 0,
  kinds: [] as string[],
  kept: 0,
  out: 'skipped',
  why,
  held: [] as string[],
  changed: [] as string[],
})

appScenario('app flow: a period with no request file only checks that the file exists', async (a, $) => {
  await boot(a, $)
  expect(a.events, 'boot cleared').toEqual([])
  await tick(a)
  expect(a.events, 'one period').toEqual(['fs.exists', 'clock.every'])
  expect(a.exists, 'one exists').toEqual([REQUEST])
  expect(a.writes, 'no writes').toEqual([])
  expect(a.mcpCalls, 'no mcp').toEqual([])
  expect(a.events.includes('fs.read'), 'no read').toBe(false)
  await tick(a)
  expect(a.events, 'two periods').toEqual(['fs.exists', 'clock.every', 'fs.exists', 'clock.every'])
  expect(a.exists, 'two exists').toEqual([REQUEST, REQUEST])
})

appScenario('app flow: a new request writes usage.json, then the ack, then one log', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'one get_usage').toEqual([{ server: 'ccd_session_mgmt', tool: 'get_usage', args: {} }])
  expect(a.afterMs, 'timeout armed').toEqual([5000])
  expect(a.writes.map((w) => w.path), 'usage then ack then log').toEqual([TARGET, ACK, EVENTS])
  const snap = okSnap()
  expect(JSON.parse(a.files.get(TARGET) ?? 'null'), 'snapshot').toEqual(snap)
  expect(a.files.get(TARGET), 'snapshot layout').toBe(JSON.stringify(snap, null, 2))
  expect(JSON.stringify(snap).includes('Fable'), 'fable not stored').toBe(false)
  const ack = okAck('req1')
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'ack').toEqual(ack)
  expect(Object.keys(JSON.parse(a.files.get(ACK) ?? 'null')), 'ack keys').toEqual(['schema', 'id', 'status', 'at', 'windows'])
  expect(a.files.get(ACK), 'ack layout').toBe(JSON.stringify(ack, null, 2))
  const logged = appLog(a)
  expect(logged, 'one refresh.app').toEqual([wroteLog()])
  expect(Object.keys(logged[0] ?? {}), 'log keys').toEqual(LOG_KEYS)
  expect(a.events[a.events.length - 1], 'period closed').toBe('clock.every')
  expect(a.events.filter((name) => name === 'mcp.call'), 'mcp once in the trace').toEqual(['mcp.call'])
})

appScenario('app flow: the same request id is not handled on later periods', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  const seen = a.events.length
  await tick(a, 2)
  expect(a.mcpCalls, 'still one call').toHaveLength(1)
  expect(a.writes, 'still three writes').toHaveLength(3)
  expect(appLog(a), 'still one log').toHaveLength(1)
  expect(a.events.slice(seen), 'later periods only reread the request').toEqual([
    'fs.exists',
    'fs.read',
    'clock.every',
    'fs.exists',
    'fs.read',
    'clock.every',
  ])
})

const ageRows: { label: string; age: number; handle: boolean }[] = [
  { label: 'exactly 30s old is handled', age: 30, handle: true },
  { label: '31s old is ignored', age: 31, handle: false },
  { label: 'exactly 5s in the future is handled', age: -5, handle: true },
  { label: '6s in the future is ignored', age: -6, handle: false },
]
for (const row of ageRows) {
  appScenario('app flow: request age ' + row.label, async (a, $) => {
    a.mcp.ccd_session_mgmt = APP_OK
    await boot(a, $)
    stageRequest(a, 'age1', row.age)
    await tick(a)
    if (row.handle) {
      expect(a.mcpCalls, row.label).toHaveLength(1)
      expect(whys(a), row.label).toEqual([''])
    } else {
      expect(a.mcpCalls, row.label).toEqual([])
      expect(a.writes, row.label).toEqual([])
      expect(a.files.has(EVENTS), row.label).toBe(false)
      expect(a.events, row.label).toEqual(['fs.exists', 'fs.read', 'clock.now', 'clock.every'])
    }
  })
}

appScenario('app flow: an ignored request id stays seen even when the file is refreshed', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'old1', 31)
  await tick(a)
  expect(a.mcpCalls, 'old ignored').toEqual([])
  stageRequest(a, 'old1', 1)
  await tick(a)
  expect(a.mcpCalls, 'same id still ignored').toEqual([])
  expect(a.files.has(EVENTS), 'still no log').toBe(false)
  stageRequest(a, 'new1', 1)
  await tick(a)
  expect(a.mcpCalls, 'new id handled').toHaveLength(1)
  expect(whys(a), 'new id logged').toEqual([''])
})

const dupRows = [
  { label: 'status ok', status: 'ok' },
  { label: 'status zzz', status: 'zzz' },
]
for (const row of dupRows) {
  appScenario('app flow: an ack with the same id is a duplicate (' + row.label + ')', async (a, $) => {
    a.mcp.ccd_session_mgmt = APP_OK
    await boot(a, $)
    a.files.set(ACK, JSON.stringify({ schema: 1, id: 'dup1', status: row.status, at: T0, windows: 2 }))
    stageRequest(a, 'dup1')
    await tick(a)
    expect(a.mcpCalls, row.label).toEqual([])
    expect(a.writes.map((w) => w.path), row.label).toEqual([EVENTS])
    const logged = appLog(a)
    expect(logged, row.label).toEqual([skipLog(TICK1, 'duplicate_ack')])
    expect(Object.keys(logged[0] ?? {}), row.label + ' keys').toEqual(LOG_KEYS)
  })
}

const ackRows: { label: string; text: string }[] = [
  { label: 'other id', text: JSON.stringify({ schema: 1, id: 'other9', status: 'ok', at: T0, windows: 2 }) },
  { label: 'truncated', text: '{"schema":1,"id":"' },
  { label: 'empty', text: '' },
  { label: 'schema 2', text: JSON.stringify({ schema: 2, id: 'req2', status: 'ok' }) },
]
for (const row of ackRows) {
  appScenario('app flow: a non-matching ack does not block the request (' + row.label + ')', async (a, $) => {
    a.mcp.ccd_session_mgmt = APP_OK
    await boot(a, $)
    a.files.set(ACK, row.text)
    stageRequest(a, 'req2')
    await tick(a)
    expect(a.mcpCalls, row.label).toHaveLength(1)
    expect(whys(a), row.label).toEqual([''])
    expect(JSON.parse(a.files.get(ACK) ?? 'null').id, row.label).toBe('req2')
  })
}

appScenario('app flow: a second call inside 10s is throttled and the period 12s later is handled', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  // t=3：第一次调用。t=6（3 秒后）与 t=12（9 秒后）距它不到 10 秒，限频；t=15（12 秒后）再处理。
  // 周期是 3 秒，格点上碰不到恰好 10 秒；恰好 10 秒与差 1 毫秒的边界由下面用 clockScript 的两个用例钉住。
  stageRequest(a, 'idA')
  await tick(a)
  stageRequest(a, 'idB')
  await tick(a)
  await tick(a)
  stageRequest(a, 'idC')
  await tick(a)
  stageRequest(a, 'idD')
  await tick(a)
  expect(a.mcpCalls, 'two calls').toHaveLength(2)
  expect(whys(a), 'why sequence').toEqual(['', 'throttled', 'throttled', ''])
  const logged = appLog(a)
  expect(logged[1], 'throttled at t=6').toEqual(skipLog(T0 + 6, 'throttled'))
  expect(logged[2], 'throttled at t=12').toEqual(skipLog(T0 + 12, 'throttled'))
  expect(a.writes.map((w) => w.path), 'throttled periods only write the log').toEqual([
    TARGET,
    ACK,
    EVENTS,
    EVENTS,
    EVENTS,
    TARGET,
    ACK,
    EVENTS,
  ])
})

appScenario('app flow: a request file with a leading BOM is handled', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.files.set(
    REQUEST,
    '\ufeff' + JSON.stringify({ schema: 1, id: 'bom1', requested_at: (a.clock.now() + POLL) / 1000 - 1 }),
  )
  await tick(a)
  expect(a.mcpCalls, 'handled').toHaveLength(1)
  expect(whys(a), 'wrote').toEqual([''])
  expect(JSON.parse(a.files.get(ACK) ?? 'null').id, 'ack id').toBe('bom1')
})

const badRequestRows: { label: string; text: string }[] = [
  { label: 'schema 2', text: JSON.stringify({ schema: 2, id: 'req1', requested_at: TICK1 - 1 }) },
  { label: 'hyphen id', text: JSON.stringify({ schema: 1, id: 'ab-cd', requested_at: TICK1 - 1 }) },
  { label: '33 char id', text: JSON.stringify({ schema: 1, id: 'a'.repeat(33), requested_at: TICK1 - 1 }) },
  { label: 'string time', text: JSON.stringify({ schema: 1, id: 'req1', requested_at: '5' }) },
  { label: 'bad json', text: '{no' },
  { label: 'empty', text: '' },
  { label: 'array', text: '[]' },
]
for (const row of badRequestRows) {
  appScenario('app flow: an invalid request is ignored (' + row.label + ')', async (a, $) => {
    a.mcp.ccd_session_mgmt = APP_OK
    await boot(a, $)
    a.files.set(REQUEST, row.text)
    await tick(a)
    expect(a.mcpCalls, row.label).toEqual([])
    expect(a.writes, row.label).toEqual([])
    expect(a.files.has(EVENTS), row.label).toBe(false)
    expect(a.events, row.label).toEqual(['fs.exists', 'fs.read', 'clock.every'])
  })
}

// 请求与确认文件只有一百来字节，超过 4 KB（按字符数）的当作不可用：不解析，请求当作没有，确认当作没有。
const padTo = (text: string, size: number): string => text + ' '.repeat(size - text.length)

appScenario('caps: a request file over 4 KB is not a request, one of exactly 4 KB is', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  const CAP = 4 * 1024
  const body = (id: string) => JSON.stringify({ schema: 1, id, requested_at: (a.clock.now() + POLL) / 1000 - 1 })
  a.files.set(REQUEST, padTo(body('big1'), CAP + 1))
  await tick(a)
  expect(a.mcpCalls, 'over the cap: no call').toEqual([])
  expect(a.events, 'read and dropped before any clock read').toEqual(['fs.exists', 'fs.read', 'clock.every'])
  expect(a.files.has(EVENTS), 'silent, like any invalid request').toBe(false)
  a.files.set(REQUEST, padTo(body('big2'), CAP))
  await tick(a)
  expect(a.mcpCalls, 'exactly at the cap: handled').toHaveLength(1)
  expect(whys(a)).toEqual([''])
})

const ackCapRows = [
  { label: 'an ack of exactly 4 KB is read, so the request is a duplicate', size: 4096, duplicate: true },
  { label: 'an ack over 4 KB is no ack, so the request is handled', size: 4097, duplicate: false },
]
for (const row of ackCapRows) {
  appScenario('caps: ' + row.label, async (a, $) => {
    a.mcp.ccd_session_mgmt = APP_OK
    await boot(a, $)
    const ack = JSON.stringify({ schema: 1, id: 'ak1', status: 'ok', at: T0, windows: 2 })
    a.files.set(ACK, padTo(ack, row.size))
    stageRequest(a, 'ak1')
    await tick(a)
    if (row.duplicate) {
      expect(a.mcpCalls, row.label).toEqual([])
      expect(whys(a), row.label).toEqual(['duplicate_ack'])
    } else {
      expect(a.mcpCalls, row.label).toHaveLength(1)
      expect(whys(a), row.label).toEqual([''])
    }
  })
}

// 读到的请求文本与上一次处理完的完全相同时不再解析。这在行为上看不出来（同一段文本解析多少次结论都一样），
// 测试环境里模块的状态也不与测试共享、JSON.parse 又是冻结的，没法数解析次数，所以这里只钉住行为不变：
// 引擎调用的序列、无效文本不会粘住、读时钟失败的请求不会被当成处理过（见后面"时钟一直坏"的用例）。
appScenario('app flow: an unparseable request text costs only the reread, and a valid request written later is handled', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.files.set(REQUEST, '{no')
  await tick(a, 4)
  expect(a.events, 'four periods, each one exists and one read').toEqual(
    Array.from({ length: 4 }, () => ['fs.exists', 'fs.read', 'clock.every']).flat(),
  )
  expect(a.mcpCalls).toEqual([])
  a.files.set(REQUEST, '{still no')
  await tick(a, 2)
  expect(a.mcpCalls, 'another bad text is also ignored').toEqual([])
  stageRequest(a, 'ok1')
  await tick(a)
  expect(a.mcpCalls, 'the valid request is handled').toHaveLength(1)
  expect(whys(a)).toEqual([''])
  expect(JSON.parse(a.files.get(ACK) ?? 'null').id).toBe('ok1')
})

appScenario('app flow: a request text that is only rewritten with the same id is not handled again', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'same1')
  await tick(a)
  expect(a.mcpCalls).toHaveLength(1)
  // 同一个 id、别的 requested_at：文本不同所以要解析，id 已见过，仍不处理。
  await tick(a, 5)
  stageRequest(a, 'same1')
  await tick(a, 2)
  expect(a.mcpCalls, 'the same id, whatever the text').toHaveLength(1)
  expect(whys(a)).toEqual([''])
})

appScenario('app flow: a 32 character id is handled', async (a, $) => {
  const id = 'a'.repeat(32)
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, id)
  await tick(a)
  expect(a.mcpCalls, 'handled').toHaveLength(1)
  expect(JSON.parse(a.files.get(ACK) ?? 'null').id, 'ack id').toBe(id)
  expect(whys(a), 'wrote').toEqual([''])
})

appScenario('app flow: a structuredContent payload writes the same snapshot and ack', async (a, $) => {
  a.mcp.ccd_session_mgmt = { isError: false, content: [], structuredContent: appPayload() }
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  const snap = okSnap()
  const ack = okAck('req1')
  expect(JSON.parse(a.files.get(TARGET) ?? 'null'), 'snapshot').toEqual(snap)
  expect(a.files.get(TARGET), 'snapshot layout').toBe(JSON.stringify(snap, null, 2))
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'ack').toEqual(ack)
  expect(a.files.get(ACK), 'ack layout').toBe(JSON.stringify(ack, null, 2))
})

appScenario('app flow: a structuredContent without a plan does not hide the plan in the text block', async (a, $) => {
  a.mcp.ccd_session_mgmt = {
    isError: false,
    content: [{ type: 'text', text: JSON.stringify(appPayload()) }],
    structuredContent: { ok: true },
  }
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(JSON.parse(a.files.get(TARGET) ?? 'null'), 'snapshot from the text block').toEqual(okSnap())
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'a normal ack, not unavailable').toEqual(okAck('req1'))
  expect(appLog(a)).toEqual([wroteLog()])
})

appScenario('app flow: a structuredContent with a plan wins over a text block that says something else', async (a, $) => {
  a.mcp.ccd_session_mgmt = {
    isError: false,
    content: [{ type: 'text', text: JSON.stringify(appPayload(1, 2)) }],
    structuredContent: appPayload(),
  }
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(JSON.parse(a.files.get(TARGET) ?? 'null'), 'the structured numbers').toEqual(okSnap())
})

appScenario('app flow: when neither the structured object nor the text block has a plan the account counts as unavailable', async (a, $) => {
  a.mcp.ccd_session_mgmt = { isError: false, content: [{ type: 'text', text: '{"other":1}' }], structuredContent: { ok: true } }
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expectUnavail(a, 'req1')
  expect(appLog(a)).toEqual([skipLog(TICK1, 'app_unavailable:none')])
})

appScenario('app flow: a stored spend_limit window is kept beside the app windows', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.files.set(
    TARGET,
    JSON.stringify({
      schema: 1,
      written_at: T0 - 100,
      windows: { spend_limit: ow(105.5, R7, T0 - 600, 'old-c') },
    }),
  )
  stageRequest(a, 'req1')
  await tick(a)
  const parsed = JSON.parse(a.files.get(TARGET) ?? 'null') as {
    written_at: number
    windows: Record<string, unknown>
  }
  expect(parsed.written_at, 'rewritten').toBe(TICK1)
  expect(Object.keys(parsed.windows), 'kind order').toEqual(['five_hour', 'seven_day', 'spend_limit'])
  expect(parsed.windows.five_hour, 'five_hour').toEqual(win(67, R5, TICK1, 'sess-new'))
  expect(parsed.windows.seven_day, 'seven_day').toEqual(win(57, R7, TICK1, 'sess-new'))
  expect(parsed.windows.spend_limit, 'spend_limit kept').toEqual(win(105.5, R7, T0 - 600, 'old-c'))
})

// ---------------------------------------------------------------- 刷新请求：失败、超时、不可用、合并

const unavailAck = (id: string) => ({ schema: 1, id, status: 'unavailable', at: TICK1, windows: 0 })
const expectUnavail = (a: AppWorld, id: string): void => {
  const ack = unavailAck(id)
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'unavailable ack').toEqual(ack)
  expect(Object.keys(JSON.parse(a.files.get(ACK) ?? 'null')), 'ack keys').toEqual([
    'schema',
    'id',
    'status',
    'at',
    'windows',
  ])
  expect(a.files.get(ACK), 'ack layout').toBe(JSON.stringify(ack, null, 2))
  expect(a.writes.map((w) => w.path), 'ack then log').toEqual([ACK, EVENTS])
}
const stageStored = (a: AppWorld, windows: unknown): void => {
  a.files.set(TARGET, JSON.stringify({ schema: 1, written_at: T0 - 100, windows }))
}
const readSnap = (a: AppWorld) =>
  JSON.parse(a.files.get(TARGET) ?? 'null') as { written_at: number; windows: Record<string, unknown> }

appScenario('app flow: the dashed server name is used when the first name throws', async (a, $) => {
  a.mcp.ccd_session_mgmt = 'throw'
  a.mcp['ccd-session-mgmt'] = APP_OK
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'both names').toEqual([
    { server: 'ccd_session_mgmt', tool: 'get_usage', args: {} },
    { server: 'ccd-session-mgmt', tool: 'get_usage', args: {} },
  ])
  expect(a.afterMs, 'one timeout shared by both names').toEqual([5000])
  expect(JSON.parse(a.files.get(TARGET) ?? 'null'), 'snapshot').toEqual(okSnap())
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'ack').toEqual(okAck('req1'))
  expect(appLog(a), 'wrote').toEqual([wroteLog()])
})

appScenario('app flow: a successful first server name is not followed by the second', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  a.mcp['ccd-session-mgmt'] = APP_OK
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'first name only').toEqual([{ server: 'ccd_session_mgmt', tool: 'get_usage', args: {} }])
  expect(a.afterMs, 'one timeout').toEqual([5000])
})

appScenario('app flow: both server names throwing writes only the event log', async (a, $) => {
  a.mcp.ccd_session_mgmt = 'throw'
  a.mcp['ccd-session-mgmt'] = 'throw'
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'both tried').toEqual([
    { server: 'ccd_session_mgmt', tool: 'get_usage', args: {} },
    { server: 'ccd-session-mgmt', tool: 'get_usage', args: {} },
  ])
  expect(a.files.has(ACK), 'no ack').toBe(false)
  expect(a.files.has(TARGET), 'no snapshot').toBe(false)
  expect(a.writes.map((w) => w.path), 'log only').toEqual([EVENTS])
  const logged = appLog(a)
  expect(logged, 'mcp error').toEqual([skipLog(TICK1, 'mcp_error:HooksError')])
  expect(Object.keys(logged[0] ?? {}), 'log keys').toEqual(LOG_KEYS)
})

// 每种结果之后，下一次点击都照常调用：超时（挂住不等于没有这个服务）、两个名字都抛异常、服务回应了的各种结果都是如此。
const outcomeRows: { label: string; setup: (a: AppWorld) => void; why: string; settle: number }[] = [
  { label: 'a timeout', setup: (a) => (a.mcp.ccd_session_mgmt = 'hang'), why: 'mcp_timeout', settle: 5000 },
  {
    label: 'the first name throwing and the second hanging',
    setup: (a) => {
      a.mcp.ccd_session_mgmt = 'throw'
      a.mcp['ccd-session-mgmt'] = 'hang'
    },
    why: 'mcp_timeout',
    settle: 5000,
  },
  { label: 'an isError reply', setup: (a) => (a.mcp.ccd_session_mgmt = { isError: true, content: [] }), why: 'app_is_error', settle: 0 },
  { label: 'an unreadable reply', setup: (a) => (a.mcp.ccd_session_mgmt = null), why: 'parse_failed', settle: 0 },
  {
    label: 'an unavailable account',
    setup: (a) => (a.mcp.ccd_session_mgmt = textResult({ plan: { status: 'not_applicable' } })),
    why: 'app_unavailable:not_applicable',
    settle: 0,
  },
  {
    label: 'both server names throwing',
    setup: (a) => {
      a.mcp.ccd_session_mgmt = 'throw'
      a.mcp['ccd-session-mgmt'] = 'throw'
    },
    why: 'mcp_error:HooksError',
    settle: 0,
  },
]
for (const row of outcomeRows) {
  appScenario('app flow: every outcome leaves the next click callable: ' + row.label, async (a, $) => {
    row.setup(a)
    await boot(a, $)
    stageRequest(a, 'n1')
    await tick(a)
    if (row.settle > 0) await a.clock.advance(row.settle)
    expect(whys(a), row.label).toEqual([row.why])
    const callsBefore = a.mcpCalls.length
    a.mcp.ccd_session_mgmt = APP_OK
    a.mcp['ccd-session-mgmt'] = APP_OK
    await a.clock.advance(12000) // 过了 10 秒的限频，下一次点击才会真的调用
    stageRequest(a, 'n2')
    await tick(a)
    expect(a.mcpCalls.length, row.label + ': the next click calls again').toBeGreaterThan(callsBefore)
    expect(whys(a).at(-1), row.label).toBe('')
  })
}

appScenario('app flow: isError skips a usable text body and does not try the second name', async (a, $) => {
  a.mcp.ccd_session_mgmt = { isError: true, content: [{ type: 'text', text: JSON.stringify(appPayload()) }] }
  a.mcp['ccd-session-mgmt'] = APP_OK
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'first name only').toHaveLength(1)
  expect(a.files.has(ACK), 'no ack').toBe(false)
  expect(a.files.has(TARGET), 'no snapshot').toBe(false)
  expect(a.writes.map((w) => w.path), 'log only').toEqual([EVENTS])
  expect(appLog(a), 'is error').toEqual([skipLog(TICK1, 'app_is_error')])
})

const parseFailRows: { label: string; res: unknown }[] = [
  { label: 'not json', res: { isError: false, content: [{ type: 'text', text: 'not json' }] } },
  { label: 'json array', res: { isError: false, content: [{ type: 'text', text: '[1]' }] } },
  { label: 'image only', res: { isError: false, content: [{ type: 'image' }] } },
  { label: 'empty content', res: { isError: false, content: [] } },
  { label: 'null result', res: null },
  { label: 'string result', res: 'x' },
]
for (const row of parseFailRows) {
  appScenario('app flow: an unreadable app result is parse_failed (' + row.label + ')', async (a, $) => {
    a.mcp.ccd_session_mgmt = row.res
    await boot(a, $)
    stageRequest(a, 'req1')
    await tick(a)
    expect(appLog(a), row.label).toEqual([skipLog(TICK1, 'parse_failed')])
    expect(a.files.has(ACK), row.label).toBe(false)
    expect(a.files.has(TARGET), row.label).toBe(false)
    expect(a.writes.map((w) => w.path), row.label).toEqual([EVENTS])
  })
}

const payloadCapRows = [
  { label: 'a text block of exactly 64 KB is read', size: 64 * 1024, ok: true },
  { label: 'a text block over 64 KB is parse_failed', size: 64 * 1024 + 1, ok: false },
]
for (const row of payloadCapRows) {
  appScenario('caps: ' + row.label, async (a, $) => {
    a.mcp.ccd_session_mgmt = {
      isError: false,
      content: [{ type: 'text', text: padTo(JSON.stringify(appPayload()), row.size) }],
    }
    await boot(a, $)
    stageRequest(a, 'cap1')
    await tick(a)
    if (row.ok) {
      expect(appLog(a), row.label).toEqual([wroteLog()])
      expect(JSON.parse(a.files.get(TARGET) ?? 'null'), row.label).toEqual(okSnap())
    } else {
      expect(appLog(a), row.label).toEqual([skipLog(TICK1, 'parse_failed')])
      expect(a.files.has(ACK), row.label).toBe(false)
      expect(a.files.has(TARGET), row.label).toBe(false)
    }
  })
}

appScenario('app flow: a hanging call times out once and later periods do not overlap', async (a, $) => {
  a.mcp.ccd_session_mgmt = 'hang'
  a.mcp['ccd-session-mgmt'] = APP_OK
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'one call at t=3').toHaveLength(1)
  expect(a.afterMs, 'timeout armed').toEqual([5000])
  expect(a.writes, 'still waiting').toEqual([])
  expect(a.files.has(EVENTS), 'no log yet').toBe(false)
  // 超时在 t=3 + 5 = 8 秒到期：差 1 毫秒还在等，到点放弃。
  await a.clock.advance(4999)
  expect(a.files.has(EVENTS), '1 ms short of the timeout, still no log').toBe(false)
  expect(a.mcpCalls, 'still one call').toHaveLength(1)
  expect(a.exists, 'no extra exists while busy').toEqual([REQUEST])
  await a.clock.advance(1)
  expect(whys(a), 'timed out').toEqual(['mcp_timeout'])
  expect(appLog(a), 'timeout log').toEqual([skipLog(T0 + 8, 'mcp_timeout')])
  expect(a.mcpCalls, 'second name not tried').toHaveLength(1)
  expect(a.files.has(ACK), 'no ack').toBe(false)
  expect(a.files.has(TARGET), 'no snapshot').toBe(false)
  await tick(a, 2)
  expect(a.mcpCalls, 'same id not retried').toHaveLength(1)
  expect(appLog(a), 'still one log').toHaveLength(1)
})

// 两个服务器名共用一个 5 秒期限。第一个名字失败得慢（4 秒后才失败），第二个挂住：整次尝试在第 5 秒结束
// （t=3 发出，t=8 放弃），不是第二个再等满 5 秒（t=12）。小窗只等 8 秒，晚于它写出的确认没有意义。
appScenario('app flow: a slow failure of the first name leaves the second name only the rest of the same 5 s', async (a, $) => {
  a.mcp.ccd_session_mgmt = 'slowfail'
  a.mcp['ccd-session-mgmt'] = 'hang'
  await boot(a, $)
  stageRequest(a, 'dl1')
  await tick(a) // t=3：第一个名字发出
  expect(a.mcpCalls.map((c) => c.server)).toEqual(['ccd_session_mgmt'])
  expect(a.afterMs, 'one timer armed').toEqual([5000])
  await a.clock.advance(4000) // t=7：第一个名字这时才失败
  a.failCall[0]?.()
  await a.clock.settle()
  expect(a.mcpCalls.map((c) => c.server), 'now the second name').toEqual(['ccd_session_mgmt', 'ccd-session-mgmt'])
  expect(a.afterMs, 'no second timer for the second name').toEqual([5000])
  await a.clock.advance(999) // t=7.999：离期限还差 1 毫秒
  expect(a.files.has(EVENTS), '1 ms short of the shared deadline').toBe(false)
  await a.clock.advance(1) // t=8
  expect(appLog(a), 'given up at 3 + 5 s, not 7 + 5 s').toEqual([skipLog(T0 + 8, 'mcp_timeout')])
  expect(a.writes.map((w) => w.path)).toEqual([EVENTS])
  expect(a.files.has(ACK), 'no ack').toBe(false)
})

appScenario('app flow: an answer that arrives after the shared deadline writes nothing', async (a, $) => {
  a.mcp.ccd_session_mgmt = 'slowfail'
  a.mcp['ccd-session-mgmt'] = 'late'
  await boot(a, $)
  stageRequest(a, 'dl2')
  await tick(a) // t=3
  await a.clock.advance(4000) // t=7：第一个名字失败，第二个发出
  a.failCall[0]?.()
  await a.clock.settle()
  expect(a.release, 'the second name is waiting').toHaveLength(1)
  await a.clock.advance(1000) // t=8：共用的期限到了
  expect(whys(a)).toEqual(['mcp_timeout'])
  await a.clock.advance(500) // t=8.5：若第二个名字有自己的 5 秒（到 t=12），这时放出的答案会被收下
  a.release[0]?.()
  await a.clock.settle()
  expect(a.files.has(ACK), 'no ack').toBe(false)
  expect(a.files.has(TARGET), 'no snapshot').toBe(false)
  expect(a.writes.map((w) => w.path), 'only the timeout log').toEqual([EVENTS])
  expect(appLog(a)).toEqual([skipLog(T0 + 8, 'mcp_timeout')])
})

appScenario('app flow: an answer that arrives after the deadline of a single call writes nothing either', async (a, $) => {
  a.mcp.ccd_session_mgmt = 'late'
  await boot(a, $)
  stageRequest(a, 'dl3')
  await tick(a)
  await a.clock.advance(5000)
  expect(whys(a), 'timed out').toEqual(['mcp_timeout'])
  expect(a.release).toHaveLength(1)
  a.release[0]?.()
  await a.clock.settle()
  expect(a.files.has(ACK), 'no ack').toBe(false)
  expect(a.files.has(TARGET), 'no snapshot').toBe(false)
  expect(a.writes.map((w) => w.path)).toEqual([EVENTS])
  await tick(a, 2)
  expect(a.mcpCalls, 'the same id is not retried').toHaveLength(1)
  expect(appLog(a)).toHaveLength(1)
})

const unavailableRows: { label: string; payload: unknown; log: Record<string, unknown>; keys: string[] }[] = [
  {
    label: 'not_applicable without windows',
    payload: { plan: { status: 'not_applicable' } },
    log: skipLog(TICK1, 'app_unavailable:not_applicable'),
    keys: LOG_KEYS,
  },
  {
    label: 'not_applicable with usable windows',
    payload: { plan: { status: 'not_applicable', windows: appPayload().plan.windows } },
    log: { ...skipLog(TICK1, 'app_unavailable:not_applicable'), n: 3, kinds: ['five_hour', 'seven_day'], kept: 2 },
    keys: LOG_KEYS,
  },
  {
    label: 'no plan',
    payload: {},
    log: skipLog(TICK1, 'app_unavailable:none'),
    keys: LOG_KEYS,
  },
  {
    label: 'numeric status',
    payload: { plan: { status: 5, windows: [] } },
    log: skipLog(TICK1, 'app_unavailable:none'),
    keys: LOG_KEYS,
  },
  {
    label: 'long status',
    payload: { plan: { status: 'x'.repeat(100) } },
    log: skipLog(TICK1, 'app_unavailable:' + 'x'.repeat(24)),
    keys: LOG_KEYS,
  },
  {
    label: 'only fable',
    payload: {
      plan: {
        status: 'ok',
        windows: [{ label: 'Weekly \u00b7 Fable', percentUsed: 0, resetsAt: iso(R7) }],
      },
    },
    log: { ...skipLog(TICK1, 'no_windows'), n: 1 },
    keys: LOG_KEYS,
  },
  {
    label: 'empty windows',
    payload: { plan: { status: 'ok', windows: [] } },
    log: skipLog(TICK1, 'no_windows'),
    keys: LOG_KEYS,
  },
  {
    label: 'bad percent',
    payload: { plan: { status: 'ok', windows: [{ label: '5-hour limit', percentUsed: -1, resetsAt: iso(R5) }] } },
    log: { ...skipLog(TICK1, 'no_windows'), n: 1, drops: { bad_percent: 1 } },
    keys: LOG_KEYS_DROPS,
  },
]
for (const row of unavailableRows) {
  appScenario('app flow: unavailable data writes an ack and leaves usage.json (' + row.label + ')', async (a, $) => {
    a.mcp.ccd_session_mgmt = textResult(row.payload)
    await boot(a, $)
    stageRequest(a, 'req1')
    await tick(a)
    expectUnavail(a, 'req1')
    const logged = appLog(a)
    expect(logged, row.label).toEqual([row.log])
    expect(Object.keys(logged[0] ?? {}), row.label + ' keys').toEqual(row.keys)
    if (row.label === 'not_applicable with usable windows') expect(a.files.has(TARGET), row.label).toBe(false)
  })
}

appScenario('app flow: an unavailable reply does not rewrite an existing usage.json', async (a, $) => {
  const prior = JSON.stringify({ schema: 1, written_at: T0 - 100, windows: { five_hour: ow(40, R5, T0 - 300, 'old-a') } })
  a.mcp.ccd_session_mgmt = textResult({ plan: { status: 'not_applicable' } })
  await boot(a, $)
  a.files.set(TARGET, prior)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.files.get(TARGET), 'byte for byte').toBe(prior)
  expectUnavail(a, 'req1')
})

appScenario('app flow: a gap under 1 confirms the stored percent and refreshes the observation', async (a, $) => {
  a.mcp.ccd_session_mgmt = textResult(appPayload(66, 57))
  await boot(a, $)
  stageStored(a, { five_hour: ow(66.4, R5, T0 - 100, 'old-a') })
  stageRequest(a, 'req1')
  await tick(a)
  const snap = readSnap(a)
  expect(snap.windows.five_hour, 'confirmed').toEqual(win(66.4, R5, TICK1, 'sess-new'))
  expect(snap.windows.seven_day, 'app value').toEqual(win(57, R7, TICK1, 'sess-new'))
  expect(appLog(a)[0], 'not held').toMatchObject({ held: [], kept: 2, out: 'wrote' })
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'ack').toEqual(okAck('req1'))
})

appScenario('app flow: a higher app percent in the same period replaces the stored window', async (a, $) => {
  a.mcp.ccd_session_mgmt = textResult(appPayload(67, 57))
  await boot(a, $)
  stageStored(a, { five_hour: ow(66, R5, T0 - 100, 'old-a') })
  stageRequest(a, 'req1')
  await tick(a)
  expect(readSnap(a).windows.five_hour, 'replaced').toEqual(win(67, R5, TICK1, 'sess-new'))
})

// 账号读数是点击那一刻现取的：同一周期里比已存值低 1 个点以上，说明已存的是过时的数（套餐升级、上限变高），取账号值。
appScenario('app flow: a live value 3 points below the stored one replaces it and is not held', async (a, $) => {
  a.mcp.ccd_session_mgmt = textResult(appPayload(67, 57))
  await boot(a, $)
  stageStored(a, { five_hour: ow(70, R5, T0 - 100, 'old-a') })
  stageRequest(a, 'req1')
  await tick(a)
  const snap = readSnap(a)
  expect(snap.written_at, 'rewritten').toBe(TICK1)
  expect(snap.windows.five_hour, 'taken from the app').toEqual(win(67, R5, TICK1, 'sess-new'))
  expect(snap.windows.seven_day, 'new').toEqual(win(57, R7, TICK1, 'sess-new'))
  expect(appLog(a)[0]?.held, 'not held').toEqual([])
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'ack ok').toEqual(okAck('req1'))
})

appScenario('app flow: after a plan upgrade the live percentages replace the stored ones at once', async (a, $) => {
  // 上限变高：账号百分比同一周期里从 80/90 掉到 20/30。5 小时的和 7 天的（原来要等 resets_at 往后走，最长七天）都立刻跟上。
  a.mcp.ccd_session_mgmt = textResult(appPayload(20, 30))
  await boot(a, $)
  stageStored(a, { five_hour: ow(80, R5, T0 - 100, 'old-a'), seven_day: ow(90, R7, T0 - 100, 'old-b') })
  stageRequest(a, 'upg1')
  await tick(a)
  const snap = readSnap(a)
  expect(snap.windows.five_hour).toEqual(win(20, R5, TICK1, 'sess-new'))
  expect(snap.windows.seven_day).toEqual(win(30, R7, TICK1, 'sess-new'))
  expect(appLog(a)[0]).toEqual(wroteLog())
  expect(JSON.parse(a.files.get(ACK) ?? 'null')).toEqual(okAck('upg1'))
})

appScenario('app flow: a gap of exactly 1 is taken and a gap of 0.01 is confirmed', async (a, $) => {
  a.mcp.ccd_session_mgmt = textResult(appPayload(66, 57))
  await boot(a, $)
  stageStored(a, { five_hour: ow(67, R5, T0 - 100, 'old-a') })
  stageRequest(a, 'gap1')
  await tick(a)
  expect(readSnap(a).windows.five_hour, 'gap 1 taken').toEqual(win(66, R5, TICK1, 'sess-new'))
  expect(appLog(a)[0]?.held, 'gap 1 not held').toEqual([])
  // 同一个 id 不会再处理，换一个 id 才能测差 0.01。限频要等满 10 秒：第一次调用在 t=3，新请求在 t=15 被读到，已隔 12 秒。
  await tick(a, 3)
  stageStored(a, { five_hour: ow(66.01, R5, T0 - 100, 'old-a') })
  stageRequest(a, 'gap001')
  await tick(a)
  const second = readSnap(a)
  expect(second.windows.five_hour, 'gap 0.01 confirmed').toEqual(win(66.01, R5, T0 + 15, 'sess-new'))
  expect(appLog(a)[1]?.held, 'gap 0.01 not held').toEqual([])
})

appScenario('app flow: a later app period replaces the stored window even when the percent is lower', async (a, $) => {
  a.mcp.ccd_session_mgmt = textResult(appPayload(3, 57))
  await boot(a, $)
  stageStored(a, { five_hour: ow(90, R5 - 5 * 3600, T0 - 100, 'old-a') })
  stageRequest(a, 'req1')
  await tick(a)
  expect(readSnap(a).windows.five_hour, 'new period').toEqual(win(3, R5, TICK1, 'sess-new'))
  expect(appLog(a)[0]?.held, 'not held').toEqual([])
})

appScenario('app flow: an earlier app period keeps the stored window', async (a, $) => {
  a.mcp.ccd_session_mgmt = textResult(appPayload(99, 57))
  await boot(a, $)
  stageStored(a, { five_hour: ow(10, R5 + 5 * 3600, T0 - 100, 'old-a') })
  stageRequest(a, 'req1')
  await tick(a)
  expect(readSnap(a).windows.five_hour, 'kept').toEqual(win(10, R5 + 5 * 3600, T0 - 100, 'old-a'))
  expect(appLog(a)[0]?.held, 'held').toEqual(['five_hour'])
})

appScenario('app flow: an unreadable stored snapshot is treated as absent', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.files.set(TARGET, 'not json{')
  stageRequest(a, 'req1')
  await tick(a)
  expect(JSON.parse(a.files.get(TARGET) ?? 'null'), 'fresh snapshot').toEqual(okSnap())
})

appScenario('app flow: a stored snapshot with a leading BOM is read and its other kinds are kept', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.files.set(
    TARGET,
    '\ufeff' +
      JSON.stringify({
        schema: 1,
        written_at: T0 - 100,
        windows: { spend_limit: ow(105.5, R7, T0 - 600, 'old-c') },
      }),
  )
  stageRequest(a, 'req1')
  await tick(a)
  const snap = readSnap(a)
  expect(Object.keys(snap.windows), 'kind order').toEqual(['five_hour', 'seven_day', 'spend_limit'])
  expect(snap.windows.spend_limit, 'kept').toEqual(win(105.5, R7, T0 - 600, 'old-c'))
  expect(snap.windows.five_hour, 'new').toEqual(win(67, R5, TICK1, 'sess-new'))
})

// ---------------------------------------------------------------- 刷新请求：写失败、时钟与 fs 故障、卫生、启动

appScenario('app flow: a refused usage.json write logs the error and writes no ack', async (a, $) => {
  a.failWritePaths.add(TARGET)
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.files.has(TARGET), 'no snapshot').toBe(false)
  expect(a.files.has(ACK), 'no ack').toBe(false)
  expect(a.writes.map((w) => w.path), 'log only').toEqual([EVENTS])
  expect(appLog(a), 'write error').toEqual([{ ...wroteLog(), out: 'failed', why: 'write_error:HooksError' }])
})

appScenario('app flow: a refused ack write keeps the snapshot and logs the ack error', async (a, $) => {
  a.failWritePaths.add(ACK)
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(JSON.parse(a.files.get(TARGET) ?? 'null'), 'snapshot written').toEqual(okSnap())
  expect(a.files.has(ACK), 'no ack').toBe(false)
  expect(a.writes.map((w) => w.path), 'snapshot then log').toEqual([TARGET, EVENTS])
  expect(appLog(a), 'ack error').toEqual([{ ...wroteLog(), out: 'failed', why: 'ack_write_error:HooksError' }])
})

appScenario('app flow: a refused unavailable ack writes only the event log', async (a, $) => {
  a.mcp.ccd_session_mgmt = textResult({ plan: { status: 'not_applicable' } })
  a.failWritePaths.add(ACK)
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.files.has(TARGET), 'no snapshot').toBe(false)
  expect(a.files.has(ACK), 'no ack').toBe(false)
  expect(a.writes.map((w) => w.path), 'log only').toEqual([EVENTS])
  expect(appLog(a), 'ack error').toEqual([{ ...skipLog(TICK1, 'ack_write_error:HooksError'), out: 'failed' }])
})

appScenario('app flow: a refused event log does not block the snapshot or the next request', async (a, $) => {
  a.failWritePaths.add(EVENTS)
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'idA')
  await tick(a)
  expect(a.writes.map((w) => w.path), 'snapshot and ack').toEqual([TARGET, ACK])
  expect(a.files.has(EVENTS), 'no log').toBe(false)
  a.failWritePaths.delete(EVENTS)
  // 等过 10 秒的限频：idA 在 t=3，idB 在 t=15 被读到。
  await tick(a, 3)
  stageRequest(a, 'idB')
  await tick(a)
  expect(a.mcpCalls, 'both calls').toHaveLength(2)
  expect(JSON.parse(a.files.get(ACK) ?? 'null').id, 'ack is idB').toBe('idB')
  expect(appLog(a), 'only idB was logged').toHaveLength(1)
  expect(appLog(a)[0]?.why, 'idB wrote').toBe('')
})

appScenario('app flow: a non-string session id is stored and logged as null', async (a, $) => {
  a.sessionId = 42
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  const snap = readSnap(a)
  expect(snap.windows.five_hour, 'five_hour sid').toEqual(win(67, R5, TICK1, null))
  expect(snap.windows.seven_day, 'seven_day sid').toEqual(win(57, R7, TICK1, null))
  expect(appLog(a), 'sid null').toEqual([{ ...wroteLog(), sid: null }])
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'ack').toEqual(okAck('req1'))
})

appScenario('app flow: a clock that stays down costs one clock.now per period, then the request is handled', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.failClock = true
  stageRequest(a, 'clk1')
  await tick(a)
  expect(a.mcpCalls, 'not called').toEqual([])
  expect(a.writes, 'nothing written').toEqual([])
  expect(a.files.has(EVENTS), 'no log').toBe(false)
  expect(a.events, 'stopped after the clock').toEqual(['fs.exists', 'fs.read', 'clock.now', 'clock.every'])
  // 时钟一直坏：请求没有被处理过，不记已见，每期只多试一次 clock.now，除此之外什么都不做。
  const seen = a.events.length
  await tick(a, 2)
  expect(a.events.slice(seen), 'two more periods, one clock.now each').toEqual([
    'fs.exists',
    'fs.read',
    'clock.now',
    'clock.every',
    'fs.exists',
    'fs.read',
    'clock.now',
    'clock.every',
  ])
  expect(a.mcpCalls, 'still not called').toEqual([])
  expect(a.writes, 'still nothing written').toEqual([])
  expect(a.files.has(EVENTS), 'still no log').toBe(false)
  // 时钟恢复：同一个请求仍年轻（t=12 时 10 秒），这一期就处理。
  a.failClock = false
  await tick(a)
  expect(a.mcpCalls, 'handled once the clock is back').toHaveLength(1)
  expect(whys(a), 'wrote').toEqual([''])
  expect(JSON.parse(a.files.get(ACK) ?? 'null').id, 'ack id').toBe('clk1')
  await tick(a, 2)
  expect(a.mcpCalls, 'now seen, not handled again').toHaveLength(1)
})

appScenario('app flow: one failed clock read does not swallow the request and the next period handles it', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.clockScript = ['fail']
  stageRequest(a, 'clk1')
  await tick(a)
  expect(a.events, 'first period stops after the clock').toEqual(['fs.exists', 'fs.read', 'clock.now', 'clock.every'])
  expect(a.mcpCalls, 'nothing attempted yet').toEqual([])
  expect(a.writes, 'nothing written').toEqual([])
  expect(a.files.has(EVENTS), 'no log').toBe(false)
  await tick(a)
  expect(a.mcpCalls, 'exactly one call on the second period').toEqual([
    { server: 'ccd_session_mgmt', tool: 'get_usage', args: {} },
  ])
  expect(a.writes.map((w) => w.path), 'usage then ack then log').toEqual([TARGET, ACK, EVENTS])
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'ack').toEqual({ ...okAck('clk1'), at: T0 + 6 })
  expect(appLog(a), 'one refresh.app').toEqual([{ ...wroteLog(), t: (T0 + 6) * 1000 }])
  await tick(a, 2)
  expect(a.mcpCalls, 'handled once, later periods only reread the request').toHaveLength(1)
})

appScenario('app flow: a request that outlives a long clock outage is ignored once the clock is back', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.failClock = true
  stageRequest(a, 'clk1')
  // 重试受 REQUEST_MAX_AGE_S 约束：请求写于 t=2，时钟坏到 t=33，期间每期只试一次 clock.now，除此之外什么都不做。
  await tick(a, 11)
  const period = ['fs.exists', 'fs.read', 'clock.now', 'clock.every']
  expect(a.events, 'every period of the outage costs one clock.now and nothing else').toEqual(
    Array.from({ length: 11 }, () => period).flat(),
  )
  expect(a.mcpCalls, 'no call during the outage').toEqual([])
  expect(a.writes, 'nothing written during the outage').toEqual([])
  expect(a.files.has(EVENTS), 'no log during the outage').toBe(false)
  a.failClock = false
  await tick(a)
  expect(a.mcpCalls, 't=36 the request is 34s old, not handled').toEqual([])
  expect(a.writes, 'ignored silently').toEqual([])
  expect(a.files.has(EVENTS), 'no log for a stale request').toBe(false)
  // 读到时钟之后它就记为已见，不会再有 clock.now。
  const seen = a.events.length
  await tick(a)
  expect(a.events.slice(seen), 'seen now, only the reread').toEqual(['fs.exists', 'fs.read', 'clock.every'])
})

appScenario('app flow: a clock failure after the app call skips the write and logs bad_clock', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.clockScript = ['ok', 'fail']
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'called once').toHaveLength(1)
  expect(a.files.has(ACK), 'no ack').toBe(false)
  expect(a.files.has(TARGET), 'no snapshot').toBe(false)
  expect(a.writes.map((w) => w.path), 'log only').toEqual([EVENTS])
  expect(appLog(a), 'bad clock').toEqual([
    { ...skipLog(TICK1, 'bad_clock'), n: 3, kinds: ['five_hour', 'seven_day'], kept: 2 },
  ])
})

appScenario('app flow: a refused exists check does nothing and the next period handles the request', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.failExists = true
  stageRequest(a, 'ex1')
  await tick(a)
  expect(a.mcpCalls, 'not called').toEqual([])
  expect(a.writes, 'nothing written').toEqual([])
  expect(a.events, 'exists then the next period').toEqual(['fs.exists', 'clock.every'])
  a.failExists = false
  await tick(a)
  expect(a.mcpCalls, 'handled next period').toHaveLength(1)
  expect(whys(a), 'wrote').toEqual([''])
  expect(JSON.parse(a.files.get(ACK) ?? 'null').id, 'ack id').toBe('ex1')
})

appScenario('app flow: after a hang times out a later request is handled', async (a, $) => {
  a.mcp.ccd_session_mgmt = 'hang'
  await boot(a, $)
  stageRequest(a, 'h1')
  await tick(a)
  await a.clock.advance(5000)
  expect(whys(a), 'timed out').toEqual(['mcp_timeout'])
  a.mcp.ccd_session_mgmt = APP_OK
  // 超时的调用也算一次调用，h2 要隔满 10 秒：h1 在 t=3 被读到，h2 在 t=15 被读到，隔 12 秒。
  await a.clock.advance(4000)
  stageRequest(a, 'h2')
  await tick(a)
  expect(a.mcpCalls, 'hang plus the later call').toHaveLength(2)
  expect(whys(a), 'timeout then wrote').toEqual(['mcp_timeout', ''])
  expect(JSON.parse(a.files.get(ACK) ?? 'null').id, 'ack id').toBe('h2')
  const snap = readSnap(a)
  expect(snap.written_at, 'written at t=15').toBe(T0 + 15)
  expect(snap.windows.five_hour, 'five_hour').toEqual(win(67, R5, T0 + 15, 'sess-new'))
  expect(snap.windows.seven_day, 'seven_day').toEqual(win(57, R7, T0 + 15, 'sess-new'))
})

// 下面两个用例钉住 usage_widget.py 的刷新协议（常量在 Python 里，这里不能导入，所以写字面量）：
// 小窗写出请求后等 APP_REFRESH_WAIT_SECONDS = 8 秒的确认；等不到就放弃，再过 APP_REFRESH_RETRY_SECONDS = 10 秒才接受下一次点击。
appScenario("app flow: a retry at the widget's earliest retry time is not throttled by the call that timed out", async (a, $) => {
  // 第一次点击写于 t=2，t=3 的检查读到并调用；插件 5 秒后放弃，小窗 8 秒后放弃（t=10），再过 10 秒（t=20）才接受重试。
  // 重试请求写于 t=20，t=21 被读到，离第一次调用 18 秒：不能被限频，否则有桌面会话时小窗还是显示 no session。
  a.mcp.ccd_session_mgmt = 'hang'
  await boot(a, $)
  stageRequest(a, 'try1')
  await tick(a)
  await a.clock.advance(5000)
  expect(whys(a), 'first call given up').toEqual(['mcp_timeout'])
  a.mcp.ccd_session_mgmt = APP_OK
  await a.clock.advance(10000)
  stageRequest(a, 'try2')
  await tick(a)
  expect(a.mcpCalls, 'the retry is handled').toHaveLength(2)
  expect(whys(a), 'timeout then wrote').toEqual(['mcp_timeout', ''])
  expect(JSON.parse(a.files.get(ACK) ?? 'null'), 'ack').toEqual({ ...okAck('try2'), at: T0 + 21 })
})

appScenario("app flow: the slowest answer the plugin waits for still lands within the widget's 8s wait", async (a, $) => {
  // 最坏情形：点击恰好发生在一次定时检查之后，要等满 3 秒才被发现；调用在 5 秒上限前 1 毫秒才返回。
  // 3 + 5 = 8：确认离请求时间不能超过小窗等待的 8 秒。
  a.mcp.ccd_session_mgmt = 'late'
  await boot(a, $)
  stageRequest(a, 'slow1', 3)
  await tick(a)
  expect(a.release, 'the call is waiting').toHaveLength(1)
  await a.clock.advance(4999)
  a.release[0]?.()
  await a.clock.settle()
  const ack = JSON.parse(a.files.get(ACK) ?? 'null') as { id: string; status: string; at: number }
  expect(ack.id, 'ack id').toBe('slow1')
  expect(ack.status, 'ack status').toBe('ok')
  expect(ack.at - T0, 'ack lands within the widget wait').toBeLessThanOrEqual(8)
  expect(ack.at - T0, 'ack is written when the call returns').toBeGreaterThan(7.9)
  expect(whys(a), 'wrote').toEqual([''])
})

appScenario('app flow: written files omit the raw payload, paths, and the server name', async (a, $) => {
  const payload = appPayload()
  payload.plan.plan = 'SENTINEL_PLAN'
  ;(payload.plan as { extraUsage?: unknown }).extraUsage = {
    enabled: true,
    spent: 'SENTINEL_SPENT',
    monthlyLimit: 'SENTINEL_LIMIT',
    currency: 'SENTINEL_CUR',
  }
  payload.context.session = 'SENTINEL_SESSION'
  payload.plan.windows.push({ label: 'SENTINEL_LABEL Fable', percentUsed: 1, resetsAt: iso(R7), resetsIn: '4d' })
  a.mcp.ccd_session_mgmt = textResult(payload)
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  const banned = ['SENTINEL', 'Max', 'get_usage', 'ccd_session_mgmt', 'D:/', 'D:\\', 'usage-feed-test', 'proj', 'cwd']
  const files = [
    ['usage.json', a.files.get(TARGET) ?? ''],
    ['ack', a.files.get(ACK) ?? ''],
    ['log', a.files.get(EVENTS) ?? ''],
  ] as const
  for (const [name, text] of files) {
    for (const needle of banned) expect(text.includes(needle), name + ' has ' + needle).toBe(false)
  }
  expect((a.files.get(EVENTS) ?? '').includes('req1'), 'log has the request id').toBe(false)
  expect(Object.keys(JSON.parse(a.files.get(ACK) ?? 'null')), 'ack keys').toEqual([
    'schema',
    'id',
    'status',
    'at',
    'windows',
  ])
  expect(Object.keys(JSON.parse(a.files.get(TARGET) ?? 'null')), 'snapshot keys').toEqual([
    'schema',
    'written_at',
    'windows',
  ])
})

appScenario('app flow: both hooks still return what the chain returned', async (a, $) => {
  expect(await start($), 'start').toStrictEqual({ cwd: START_MARK + 'D:/proj' })
  a.mcp.ccd_session_mgmt = APP_OK
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'request handled').toHaveLength(1)
  expect(await measure($, []), 'measure').toStrictEqual({ changed: CHANGED })
})

appScenario('app flow: session.measure alone starts the watcher', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await measure($, [])
  quiet(a)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'handled').toHaveLength(1)
})

appScenario('app flow: start and two measures share one interval', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await start($)
  await measure($, [])
  await measure($, [])
  quiet(a)
  await tick(a)
  expect(a.exists, 'one exists').toEqual([REQUEST])
  expect(a.events, 'one period').toEqual(['fs.exists', 'clock.every'])
})

// 定时器不挂 session.end，也不主动取消（理由见 register.ts 文件头）：热重载时引擎丢掉旧环境的计时器，进程退出时计时器随进程消失；
// session.end 在 /clear 和 resume 时也会触发，但那时进程和模块继续活着（/clear 之后不再触发 session.start），取消只会让点击没人处理。
// 这个用例钉住这个决定：session.end 不引起模块的任何引擎调用，之后间隔照常处理点击。
appScenario('app flow: session.end of any reason causes no engine call and the interval keeps serving clicks', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  const reasons = ['clear', 'resume', 'prompt_input_exit', 'logout', 'other'] as const
  for (const reason of reasons) {
    const res = await $.session.end({ reason, sessionId: 'sess-new', resume: { id: 'sess-new' } })
    expect(res, reason).toEqual({ sessionId: 'sess-new' })
  }
  expect(a.events, 'only the five session.end events, nothing from the module').toEqual(reasons.map(() => 'session.end'))
  stageRequest(a, 'end1')
  await tick(a)
  expect(a.mcpCalls, 'the interval is still alive and serves the click').toHaveLength(1)
  expect(whys(a)).toEqual([''])
})

// 事件日志的"读不出"记号在整个会话里共用：hook 和刷新检查写的是同一份日志。
appScenario('log: the pending count for an unreadable log is shared by the hooks and the click handler', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  a.files.set(EVENTS, 'not json{')
  await measure($, LIMS) // hook 第一次见到：不写
  expect(a.files.get(EVENTS), 'left alone by the hook').toBe('not json{')
  stageRequest(a, 'sh1')
  await tick(a) // 刷新检查第二次见到同一段：重新开始
  expect(a.files.get(EVENTS), 'replaced by the click handler').not.toBe('not json{')
  const entries = (JSON.parse(a.files.get(EVENTS) ?? 'null') as { events: LogEntry[] }).events
  expect(entries.map((e) => e.ev), 'a fresh log holding only the refresh.app entry').toEqual(['refresh.app'])
  expect(a.mcpCalls, 'the click itself was served').toHaveLength(1)
})

scenario('app timer: start and two measures arm one 3000ms interval', undefined, async (w, $) => {
  // 这个夹具的时钟不走，三次 hook 读到的时间相同，看门狗不会判定间隔已死，所以只布一次；时间走远后会重布见下面的用例。
  await start($)
  await measure($, [lim('five_hour', 20, R5)])
  await measure($, [lim('five_hour', 20, R5)])
  expect(w.timers, 'one interval').toEqual([3000])
})

scenario('app timer: measure alone arms one 3000ms interval', undefined, async (w, $) => {
  await measure($, [lim('five_hour', 20, R5)])
  expect(w.timers, 'one interval').toEqual([3000])
})

const gateRows = REFUSED
for (const [gateLabel, gateOptions] of gateRows) {
  scenario('app timer: gate refuses, so no interval either: ' + gateLabel, gateOptions, async (w, $) => {
    await start($)
    await measure($, [lim('five_hour', 20, R5)])
    expect(w.timers, gateLabel).toEqual([])
    expect(w.events, gateLabel).toEqual(['session.start', 'session.measure'])
  })
}

// ---------------------------------------------------------------- 定时检查的看门狗与卡住复位
// 链上的 hook 拒绝 $.clock.every 的某一期时，间隔静默结束、不抛异常；拒绝 $.clock.after 时回调永不执行。
// 夹具开关 denyEvery、denyAfter 在最外层的 '*' hook 里拒绝这两类调用，与真实引擎的表现一致。
// 看门狗只用 feed 已经读到的时间：带 limits 的 measure 才有时间，空列表的 measure 与 start 没有。

const LIMS = [lim('five_hour', 20, R5)]
// 经过引擎的 clock.every 事件数。时钟不动的区间里它等于布下定时器的次数；
// 时钟推进时每一期还会再出现一次，所以只在时钟没动的区间内比较。
const everyEvents = (a: AppWorld): number => a.events.filter((name) => name === 'clock.every').length

appScenario('app watchdog: a refused interval is armed again once more than two periods pass without a tick', async (a, $) => {
  a.denyEvery = 1
  a.mcp.ccd_session_mgmt = APP_OK
  await start($)
  expect(everyEvents(a), 'armed once, refused').toBe(1)
  await measure($, LIMS)
  expect(everyEvents(a), 'the first time reading only sets the baseline').toBe(1)
  await a.clock.advance(6000)
  await measure($, LIMS)
  expect(everyEvents(a), 'exactly two periods is not enough').toBe(1)
  await a.clock.advance(3000)
  await measure($, LIMS)
  expect(everyEvents(a), 'armed again').toBe(2)
  await measure($, LIMS)
  expect(everyEvents(a), 'the baseline moved with the new arm, no third arm').toBe(2)
  quiet(a)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'the new interval serves the request').toHaveLength(1)
  expect(whys(a), 'wrote').toEqual([''])
  expect(JSON.parse(a.files.get(ACK) ?? 'null').id, 'ack id').toBe('req1')
})

appScenario('app watchdog: an interval that keeps ticking is left alone and the hook adds no engine call', async (a, $) => {
  await start($)
  await measure($, LIMS)
  await tick(a, 5)
  quiet(a)
  await measure($, LIMS)
  expect(a.events, 'only the usual measure calls').toEqual(MEASURE_EVENTS)
  await tick(a)
  expect(a.exists, 'one interval, one exists per period').toEqual([REQUEST])
})

appScenario('app watchdog: a hook without a time reading skips the check and leaves the baseline alone', async (a, $) => {
  a.denyEvery = 1
  await start($)
  await measure($, LIMS)
  await a.clock.advance(20000)
  quiet(a)
  await measure($, [])
  expect(a.events, 'no extra engine call, no arm').toEqual(MEASURE_LOG_ONLY)
  await measure($, LIMS)
  expect(everyEvents(a), 'the untouched baseline is old enough').toBe(1)
})

appScenario('app watchdog: session.start also supplies a time reading for the baseline', async (a, $) => {
  a.denyEvery = 1
  a.usageLimits = LIMS
  await start($)
  expect(everyEvents(a), 'armed once, refused').toBe(1)
  await a.clock.advance(6001)
  await measure($, LIMS)
  expect(everyEvents(a), 'one later measure is enough because start set the baseline').toBe(2)
})

appScenario('app watchdog: a false alarm cancels the old interval so only one stays alive', async (a, $) => {
  await start($)
  await measure($, LIMS)
  a.clockScript = [a.clock.now() + 10000]
  await measure($, LIMS)
  expect(everyEvents(a), 'armed again').toBe(2)
  quiet(a)
  await tick(a)
  expect(a.exists, 'the old interval was cancelled').toEqual([REQUEST])
})

appScenario('app watchdog: a clock set back only moves the baseline and the check still works afterwards', async (a, $) => {
  a.denyEvery = 1
  await start($)
  await measure($, LIMS)
  const back = a.clock.now() - 3_600_000
  a.clockScript = [back]
  await measure($, LIMS)
  expect(everyEvents(a), 'a negative gap decides nothing').toBe(1)
  a.clockScript = [back + 6000]
  await measure($, LIMS)
  expect(everyEvents(a), 'exactly two periods after the new baseline').toBe(1)
  a.clockScript = [back + 6001]
  await measure($, LIMS)
  expect(everyEvents(a), 'armed again').toBe(2)
})

appScenario('app watchdog: periods skipped as busy still count as ticks', async (a, $) => {
  a.mcp.ccd_session_mgmt = 'hang'
  // 计时器被拒绝：调用一直挂着、一直忙。否则真实的 5 秒超时在 t=8 到期，之后的几期就不是被挡掉的了。
  a.denyAfter = true
  await start($)
  await measure($, LIMS)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'the call is under way').toHaveLength(1)
  await a.clock.advance(3001)
  quiet(a)
  await measure($, LIMS)
  expect(a.events, 'ticked since the baseline, so only the baseline moves').toEqual(MEASURE_EVENTS)
  // 新基线之后的两期（t=9、t=12）全是被挡掉的忙期；挡到第 5 期才强制复位，所以还留着一期余量。
  await a.clock.advance(6001)
  quiet(a)
  await measure($, LIMS)
  expect(a.events, 'only busy periods since the new baseline, still alive, no arm').toEqual(MEASURE_EVENTS)
})

appScenario('app watchdog: a call stuck without a timeout frees the watcher after more than four skipped periods', async (a, $) => {
  a.denyAfter = true
  a.mcp.ccd_session_mgmt = 'hang'
  await boot(a, $)
  stageRequest(a, 'req1')
  await tick(a)
  expect(a.mcpCalls, 'one call').toHaveLength(1)
  expect(a.afterMs, 'the timeout was asked for but refused').toEqual([5000])
  expect(a.exists, 'one exists so far').toEqual([REQUEST])
  // 上限是调用超时折成的期数（5 秒向上取整为 2 期）再加两期余量，共 4 期。
  await tick(a, 4)
  expect(a.exists, 'four skipped periods do not touch the file').toEqual([REQUEST])
  expect(whys(a), 'nothing is logged for the stuck call').toEqual([])
  a.mcp.ccd_session_mgmt = APP_OK
  await tick(a)
  expect(a.exists, 'the fifth period resets busy and runs').toEqual([REQUEST, REQUEST])
  expect(a.mcpCalls, 'req1 is already seen').toHaveLength(1)
  await tick(a)
  stageRequest(a, 'req2')
  await tick(a)
  expect(a.mcpCalls, 'req2 is handled 21s after the first call, past the 10s gap').toHaveLength(2)
  expect(whys(a), 'wrote').toEqual([''])
  expect(JSON.parse(a.files.get(ACK) ?? 'null').id, 'ack id').toBe('req2')
})

appScenario('app watchdog: a stuck call that returns late does not clear the busy flag of the newer run', async (a, $) => {
  a.denyAfter = true
  a.mcp.ccd_session_mgmt = 'late'
  await boot(a, $)
  stageRequest(a, 'reqA')
  await tick(a)
  expect(a.release, 'call A is waiting').toHaveLength(1)
  // 4 期被挡掉，第 5 期复位并运行（上限的取值见上一个用例）。
  await tick(a, 5)
  await tick(a)
  stageRequest(a, 'reqB')
  await tick(a)
  expect(a.release, 'call B is waiting too').toHaveLength(2)
  a.release[0]?.()
  await a.clock.settle()
  const before = a.exists.length
  await tick(a)
  expect(a.exists.length, 'the stale run did not clear busy').toBe(before)
  a.release[1]?.()
  await a.clock.settle()
  await tick(a)
  expect(a.exists.length, 'the watcher runs again once the newer run is done').toBe(before + 1)
})

appScenario('app flow: a clock set back does not extend the 10s throttle', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'idA')
  await tick(a)
  expect(a.mcpCalls, 'first call').toHaveLength(1)
  const back = a.clock.now() + POLL - 3_600_000
  a.files.set(REQUEST, JSON.stringify({ schema: 1, id: 'idB', requested_at: back / 1000 - 1 }))
  a.clockScript = [back]
  await tick(a)
  expect(a.mcpCalls, 'handled, not throttled').toHaveLength(2)
  expect(whys(a), 'both wrote').toEqual(['', ''])
})

appScenario('app flow: a request read at exactly the time of the last call is still throttled', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'idA')
  await tick(a)
  expect(a.mcpCalls, 'first call').toHaveLength(1)
  const same = a.clock.now()
  a.files.set(REQUEST, JSON.stringify({ schema: 1, id: 'idB', requested_at: same / 1000 - 1 }))
  a.clockScript = [same]
  await tick(a)
  expect(a.mcpCalls, 'throttled, not handled').toHaveLength(1)
  expect(whys(a), 'second request was throttled').toEqual(['', 'throttled'])
})

appScenario('app flow: a request read exactly 10s after the last call is handled', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'idA')
  await tick(a)
  expect(a.mcpCalls, 'first call').toHaveLength(1)
  // 周期是 3 秒，格点上碰不到恰好 10 秒，所以直接给这一期的 clock.now 一个读数。
  const exact = a.clock.now() + 10000
  a.files.set(REQUEST, JSON.stringify({ schema: 1, id: 'idB', requested_at: exact / 1000 - 1 }))
  a.clockScript = [exact]
  await tick(a)
  expect(a.mcpCalls, 'handled, not throttled').toHaveLength(2)
  expect(whys(a), 'both wrote').toEqual(['', ''])
})

appScenario('app flow: a request read 1 ms short of 10s after the last call is still throttled', async (a, $) => {
  a.mcp.ccd_session_mgmt = APP_OK
  await boot(a, $)
  stageRequest(a, 'idA')
  await tick(a)
  expect(a.mcpCalls, 'first call').toHaveLength(1)
  const almost = a.clock.now() + 9999
  a.files.set(REQUEST, JSON.stringify({ schema: 1, id: 'idB', requested_at: almost / 1000 - 1 }))
  a.clockScript = [almost]
  await tick(a)
  expect(a.mcpCalls, 'throttled, not handled').toHaveLength(1)
  expect(whys(a), 'second request was throttled').toEqual(['', 'throttled'])
})

scenario('app timer: a refused interval is armed again once the engine time has moved on', undefined, async (w, $) => {
  await start($)
  await measure($, [lim('five_hour', 20, R5)])
  expect(w.timers, 'armed once').toEqual([3000])
  w.nowMs = NOW_MS + 6000
  await measure($, [lim('five_hour', 20, R5)])
  expect(w.timers, 'exactly two periods is not enough').toEqual([3000])
  w.nowMs = NOW_MS + 6001
  await measure($, [lim('five_hour', 20, R5)])
  expect(w.timers, 'armed again').toEqual([3000, 3000])
  w.nowMs = NOW_MS + 7001
  await measure($, [lim('five_hour', 20, R5)])
  expect(w.timers, 'the baseline moved with the new arm').toEqual([3000, 3000])
})
