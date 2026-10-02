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
const MEASURE_EVENTS = ['session.measure', ...FEED_CALLS]
const START_EVENTS = ['session.start', 'session.usage', ...FEED_CALLS]

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
    failing: new Set<string>(),
    failMode: 'deny' as 'deny' | 'throw',
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
  on('clock.now', () => (w.failing.has('clock') ? refuse('clock') : { value: w.nowMs as number }))
  on('session.id', () => (w.failing.has('id') ? refuse('id') : { value: w.sessionId as string }))
  on('session.usage', () =>
    w.failing.has('usage')
      ? refuse('usage')
      : { value: { startedAt: 0, context: { window: 200000 }, rateLimits: w.usageLimits as never } },
  )
  on('fs.read', (_$, e) => {
    if (w.failing.has('read')) return refuse('read')
    const text = w.files.get(norm(e.path))
    return text === undefined ? { deny: 'ENOENT' } : { value: text }
  })
  on('fs.write', (_$, e) => {
    if (w.failing.has('write')) return refuse('write')
    w.writes.push({ path: e.path, text: e.text })
    w.files.set(norm(e.path), e.text)
    return { value: undefined }
  })
  on('session.start', (_$, e) => ({ cwd: START_MARK + e.cwd }))
  on('session.measure', (_$, e) => ({ changed: e.changed }))
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  return w
}
type World = ReturnType<typeof makeWorld>

const reset = (w: World): void => {
  w.files.clear()
  w.writes.length = 0
  w.events.length = 0
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

scenario('convert: when every window is dropped nothing is written and nothing is called', undefined, async (w, $) => {
  stage(w, { five_hour: ow(40, R5, T0 - 300, 'old-a') })
  const before = w.files.get(TARGET)
  const res = await measure($, [{ kind: 'monthly', percentUsed: 1, resetsAt: iso(R5) }, { kind: 'five_hour', percentUsed: -1, resetsAt: iso(R5) }])
  expect(res).toEqual({ changed: CHANGED })
  expect(w.events).toEqual(['session.measure'])
  expect(w.writes).toHaveLength(0)
  expect(w.files.get(TARGET)).toBe(before)
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
    expect(w.writes).toHaveLength(1)
    expect(norm(w.writes[0]!.path)).toBe(expected)
    expect(w.events).toEqual(MEASURE_EVENTS)
  })
}

// ---------------------------------------------------------------- session.measure

scenario('measure: empty rateLimits writes nothing and calls nothing', undefined, async (w, $) => {
  const res = await measure($, [])
  expect(res).toEqual({ changed: CHANGED })
  expect(w.events).toEqual(['session.measure'])
  expect(w.writes).toHaveLength(0)
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
  expect(w.writes).toHaveLength(2)
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

scenario('start: an empty usage reading writes nothing', undefined, async (w, $) => {
  w.usageLimits = []
  const res = await start($)
  expect(res).toEqual({ cwd: START_MARK + 'D:/proj' })
  expect(w.events).toEqual(['session.start', 'session.usage'])
  expect(w.writes).toHaveLength(0)
})

scenario('start: a usage reading of only invalid windows writes nothing', undefined, async (w, $) => {
  w.usageLimits = [{ kind: 'five_hour', percentUsed: -3, resetsAt: iso(R5) }]
  await start($)
  expect(w.events).toEqual(['session.start', 'session.usage'])
  expect(w.writes).toHaveLength(0)
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

  scenario('silent failure (' + mode + '): session.usage fails, start returns and writes nothing; measure is unaffected', undefined, async (w, $) => {
    w.failMode = mode
    w.failing.add('usage')
    expect(await start($)).toEqual({ cwd: START_MARK + 'D:/proj' })
    expect(w.events).toEqual(['session.start', 'session.usage'])
    expect(w.files.size).toBe(0)
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
    expect(w.events).toEqual(['session.start', 'session.usage', 'clock.now', 'session.measure', 'clock.now'])
    expect(w.files.size).toBe(0)
  })
}

scenario('silent failure: a non-finite clock or a non-string session id never corrupts the snapshot', undefined, async (w, $) => {
  w.nowMs = NaN
  expect(await measure($, [lim('five_hour', 31, R5)])).toEqual({ changed: CHANGED })
  expect(w.events).toEqual(['session.measure', 'clock.now'])
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
