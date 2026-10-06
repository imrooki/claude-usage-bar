// usage-feed 的行为测试（claude plugin test 运行）。
// 测试环境没有真实的 fs、网络、进程：被测模块的每个引擎调用都落到下面 makeWorld 注册的
// 底层 hook 上，文件在内存里模拟，所以这里任何一次"写"都不会碰磁盘。
// 引擎宿主会把传给 fs 的路径换成反斜杠，所以断言路径时统一用 norm 换回正斜杠；
// 模块自己传出的原始字符串（正斜杠、去末尾斜杠）不在这里断言。
import { test, expect } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

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

const norm = (p: string): string => p.replace(/\\/g, '/')
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

// 底层 hook：代表引擎，应答被测模块的每个调用，并把"经过的事件"全部记下来。
const makeWorld = (on: On) => {
  const w = {
    nowMs: NOW_MS as unknown,
    sessionId: 'sess-new' as unknown,
    usageLimits: [] as unknown[],
    files: new Map<string, string>(),
    events: [] as string[],
    writes: [] as { path: string; text: string }[],
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
    if (!BOOT_NOISE.has(name)) w.events.push(name)
    return next(e)
  })
  on('clock.now', () => {
    const step = w.clockScript.shift()
    if (step === 'fail') return refuse('clock')
    if (step !== undefined) return { value: step }
    return w.failing.has('clock') ? refuse('clock') : { value: w.nowMs as number }
  })
  on('session.id', () => (w.failing.has('id') ? refuse('id') : { value: w.sessionId as string }))
  on('session.usage', () =>
    w.failing.has('usage')
      ? refuse('usage')
      : { value: { startedAt: 0, context: { window: 200000 }, rateLimits: w.usageLimits as never } },
  )
  on('fs.read', (_$, e) => {
    const p = norm(e.path)
    w.reads.push(p)
    if (w.failing.has('read') || w.failReadPaths.has(p)) return refuse('read')
    const text = w.files.get(p)
    return text === undefined ? { deny: 'ENOENT' } : { value: text }
  })
  on('fs.write', (_$, e) => {
    if (w.failing.has('write') || w.failWritePaths.has(norm(e.path))) return refuse('write')
    w.writes.push({ path: e.path, text: e.text })
    w.files.set(norm(e.path), e.text)
    return { value: undefined }
  })
  on('session.start', (_$, e) => {
    const value = { cwd: START_MARK + e.cwd }
    w.lastStart = value
    return value
  })
  on('session.measure', (_$, e) => {
    const value = { changed: e.changed }
    w.lastMeasure = value
    return value
  })
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
  expect(text).toBe(JSON.stringify(JSON.parse(text), null, 2))
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

scenario('log: an unusable existing log is replaced by a fresh one that holds only the new entry', undefined, async (w, $) => {
  const sd = [{ seq: 1 }]
  const bad: [string, string][] = [
    ['broken JSON', 'not json{'],
    ['empty text', ''],
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
  ]
  for (const [label, text] of bad) {
    reset(w)
    w.files.set(EVENTS, text)
    await measure($, [lim('five_hour', 20, R5)])
    expect(logOf(w), label).toHaveLength(1)
    expect(lastLog(w).ev, label).toBe('session.measure')
    expect(lastLog(w).out, label).toBe('wrote')
    expect(w.files.get(EVENTS), label).not.toContain('seq')
    expect(snap(w).windows.five_hour, label).toEqual(win(20, R5, T0, 'sess-new'))
  }

  reset(w)
  w.files.set(EVENTS, JSON.stringify({ schema: 1, events: sd }))
  await measure($, [lim('five_hour', 20, R5)])
  expect(logOf(w)).toHaveLength(2)
  expect(logOf(w)[0] as unknown as { seq: number }).toEqual({ seq: 1 })
  expect(lastLog(w).ev).toBe('session.measure')
  expect(lastLog(w).out).toBe('wrote')
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
