const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')

const source = fs.readFileSync(path.join(__dirname, process.env.HERMES_TEST_CATALOG ? 'catalog/desktop/plugin.js' : 'plugin.js'), 'utf8')
  .replace(/^import .*$/gm, '')
  .replace('export default {', 'globalThis.plugin = {')

// Synthetic, credential-free fixtures. These are not live provider responses.
const snapshots = [
  { provider: 'anthropic', plan: 'Example', windows: [
    { label: 'Session', used_percent: 13, remaining_percent: 87, reset_at: '2030-01-01T12:00:00Z' },
    { label: 'Weekly', used_percent: 3, remaining_percent: 97, reset_at: '2030-01-08T12:00:00Z' }
  ] },
  { provider: 'openai-codex', plan: 'Example', windows: [
    { label: 'Weekly', used_percent: 9, remaining_percent: 91, reset_at: '2030-01-08T12:00:00Z' }
  ] },
  { provider: 'grok', plan: 'Example', windows: [
    { label: 'Weekly', used_percent: null, remaining_percent: null, reset_at: null }
  ] }
]
function atom(value) { return { get: () => value, set: next => { value = next } } }
function load({ modern = true, lifecycle = false, withQueryClient = true } = {}) {
  const saved = new Map(), registered = [], disposers = [], calls = [], queries = [], writes = [], cleared = []
  const queryResult = {}
  const consumerStates = new Map()
  let activeConsumer = 'default', stateCursor = 0
  const state = Object.fromEntries(Object.entries({ gateway: 'open', focusedSessionId: 'one',
    focusedSessionOwner: { connectionId: 'local', profile: 'default' }, focusedSessionProfile: 'default',
    connectionId: 'local', profile: 'default' }).map(([key, value]) => [key, atom(value)]))
  const host = { state, navigate: route => { host.route = route }, request: async (method, params) => {
    calls.push({ method, params })
    if (method === 'usage.bars') return { available: false }
    if (method === 'account.usage') return { snapshots }
    if (method === 'subscription.state') return {}
    if (method === 'config.show') return { sections: [{ rows: [['Config File', '/fixture/hermes/config.yaml']] }] }
    if (method === 'shell.exec') return { code: 0, stdout: JSON.stringify({ snapshot_token: 'a'.repeat(32), snapshots: [] }) }
    throw new Error('Unexpected method: ' + method)
  } }
  const sdk = { host, atom, useValue: store => store.get(), useQuery: options => {
    queries.push(options)
    return { data: options.initialData?.(), isFetching: false, ...queryResult }
  }, queryClient: { fetchQuery: async options => options.queryFn(), setQueryData: (key, data) => writes.push({ key, data }) },
    ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebarNav', PALETTE_AREA: 'palette', KEYBINDS_AREA: 'keybinds' }
  if (modern) Object.assign(sdk, { Popover: 'Popover', PopoverContent: 'PopoverContent', PopoverTrigger: 'PopoverTrigger' })
  if (!withQueryClient) delete sdk.queryClient
  const jsx = (type, props, key) => ({ type, props, key })
  const context = vm.createContext({ console, Date, Math, Number, String, JSON, Array, Set, Map, Promise, sdk,
    setInterval: () => 42, clearInterval: id => cleared.push(id), setTimeout, clearTimeout,
    Fragment: 'Fragment', useEffect: () => {}, useMemo: fn => fn(), useRef: value => ({ current: value }),
    useState: initial => {
      const value = typeof initial === 'function' ? initial() : initial
      if (!lifecycle) return [value, () => {}]
      if (!consumerStates.has(activeConsumer)) consumerStates.set(activeConsumer, [])
      const states = consumerStates.get(activeConsumer), index = stateCursor++
      if (!(index in states)) states[index] = value
      return [states[index], next => { states[index] = typeof next === 'function' ? next(states[index]) : next }]
    }, jsx, jsxs: jsx })
  vm.runInContext(source + '\nglobalThis.api = { clampPercent, quotaPercent, formatReset, cardsFromAccountSnapshots, liveQueryKey, quotaCacheKey, readQuotaCache, saveQuotaCache, QuotaRing, fetchLiveCards, useLiveCardsQuery, QuickUsage };', context)
  context.plugin.register({ storage: { get: (key, fallback) => saved.has(key) ? saved.get(key) : fallback,
    set: (key, value) => saved.set(key, value) }, onDispose: fn => disposers.push(fn),
    registerMany: rows => registered.push(...rows) })
  const api = context.api
  const key = api.liveQueryKey('local', 'default', [])
  const cards = api.cardsFromAccountSnapshots(snapshots)
  const payload = { cards, errors: [], checkedAt: Date.now(), hadSession: true, haveAccountRpc: true }
  const renderConsumer = (name, ...args) => {
    activeConsumer = name
    stateCursor = 0
    return api.useLiveCardsQuery(...args)
  }
  return { api, key, cards, payload, context, sdk, host, saved, registered, disposers, calls, queries, writes, cleared, renderConsumer, queryResult }
}

test('modern SDK registers one status-bar popup and preserves page/sidebar', () => {
  const app = load()
  assert.equal(app.registered.filter(row => row.area === 'statusBar.right').length, 1)
  assert(app.registered.some(row => row.id === 'page'))
  assert(app.registered.some(row => row.id === 'nav'))
})
test('older SDK without Popover keeps the full page without a broken chip', () => {
  const app = load({ modern: false })
  assert(!app.registered.some(row => row.area === 'statusBar.right'))
  assert(app.registered.some(row => row.id === 'page'))
})
test('unknown quota stays unknown and numeric boundaries clamp', () => {
  const { api } = load()
  for (const value of [null, undefined, '', 'bad']) assert.equal(api.clampPercent(value), null)
  assert.equal(api.clampPercent(-1), 0)
  assert.equal(api.clampPercent(101), 100)
})
test('used and remaining ring values match display mode', () => {
  const { api, cards } = load()
  for (const mode of ['used', 'remaining']) {
    const ring = api.QuotaRing({ card: cards[0], mode })
    assert.equal(ring.props.role, 'meter')
    assert.equal(ring.props['aria-valuenow'], api.quotaPercent(cards[0], mode))
    assert.equal(ring.props.children[1].props.children, `${api.quotaPercent(cards[0], mode)}%`)
  }
})
test('failed and unknown quota rings never get a fake numeric value', () => {
  const { api, cards } = load()
  for (const card of [{ ...cards[0], error: true }, { id: 'unknown', label: 'Unknown', used: null, remaining: null }]) {
    const ring = api.QuotaRing({ card, mode: 'used' })
    assert.equal(ring.props.role, 'img')
    assert(!('aria-valuenow' in ring.props))
    assert.equal(ring.props.children[1].props.children, '?')
  }
})
test('zero usage has an empty used arc rather than a rounded-cap dot', () => {
  const { api } = load()
  const ring = api.QuotaRing({ card: { id: 'zero', label: 'Zero', used: 0, remaining: 100 }, mode: 'used' })
  const arc = ring.props.children[0].props.children[1]
  assert.equal(arc.props.strokeDasharray, '0 100')
  assert.equal(arc.props.strokeLinecap, 'butt')
})
test('reset display includes countdown and absolute local date', () => {
  const { api, cards } = load()
  assert.match(api.formatReset(cards[0].resetAt, '', Date.parse('2030-01-01T10:00:00Z')), /Resets in 2h.*\u00b7/)
  assert.equal(api.formatReset(null, '', Date.now()), '')
})
test('cache reads synchronously and stores only display fields', () => {
  const { api, key, payload, saved } = load()
  api.saveQuotaCache(key, { ...payload, token: 'fixture-private', raw: { anything: 'private' } })
  const cached = api.readQuotaCache(key)
  assert.equal(cached.cards.length, payload.cards.length)
  assert.equal(cached.cached, true)
  assert(!JSON.stringify([...saved.values()]).includes('fixture-private'))
  assert(!('raw' in cached))
})
test('failed provider rows are not written to persistent quota cache', () => {
  const { api, key, payload } = load()
  api.saveQuotaCache(key, { ...payload, cards: [...payload.cards, { id: 'failed', label: 'Error', error: true, detail: 'sensitive error' }] })
  assert(!api.readQuotaCache(key).cards.some(card => card.id === 'failed'))
})
test('cache cannot cross a connection, profile, or provider selection', () => {
  const { api, key, payload } = load()
  api.saveQuotaCache(key, payload)
  for (const other of [api.liveQueryKey('remote', 'default', []), api.liveQueryKey('local', 'other', []),
    api.liveQueryKey('local', 'default', ['anthropic']), api.liveQueryKey(null, 'default', [])]) {
    assert.equal(api.readQuotaCache(other), undefined)
  }
})
test('a successful empty check replaces cached usage after logout', () => {
  const { api, key, payload } = load()
  api.saveQuotaCache(key, payload)
  const empty = { ...payload, cards: [], errors: [], checkedAt: Date.now() }
  api.saveQuotaCache(key, empty)
  const cached = api.readQuotaCache(key)
  assert.equal(cached.cards.length, 0, 'logged-out accounts must not return after reload')
  assert.equal(cached.checkedAt, empty.checkedAt)
})
test('a failed empty check retains last good persistent usage', () => {
  const { api, key, payload } = load()
  api.saveQuotaCache(key, payload)
  api.saveQuotaCache(key, { ...payload, cards: [], errors: ['Fixture backend unavailable'] })
  assert.equal(api.readQuotaCache(key).cards.length, payload.cards.length)
})
test('expired, future, and malformed persistent quota cache is rejected', () => {
  const { api, key, payload, saved } = load()
  for (const cached of [{ ...payload, checkedAt: Date.now() - 86400001 }, { ...payload, checkedAt: Date.now() + 60000 },
    { ...payload, cards: [null] }, { ...payload, cards: [{}] }, { ...payload, cards: 'not an array' }]) {
    saved.set(api.quotaCacheKey(key), cached)
    assert.equal(api.readQuotaCache(key), undefined)
  }
})
test('provider selection normalizes ordering without mixing selections', () => {
  const { api, key } = load()
  assert.equal(JSON.stringify(api.liveQueryKey('local', 'default', ['nous', 'anthropic'])),
    JSON.stringify(api.liveQueryKey('local', 'default', ['anthropic', 'nous'])))
  assert.notEqual(JSON.stringify(key), JSON.stringify(api.liveQueryKey('local', 'default', ['anthropic'])))
})
test('cached display fields reject objects before they can reach JSX', () => {
  const { api, key, payload, saved } = load()
  for (const field of ['source', 'provider', 'group', 'account', 'resetText', 'detail', 'remaining', 'used', 'resetAt']) {
    for (const value of [{ invalid: true }, [], true]) {
      saved.set(api.quotaCacheKey(key), { ...payload, cards: [{ ...payload.cards[0], [field]: value }] })
      assert.equal(api.readQuotaCache(key), undefined, `${field} must not accept ${JSON.stringify(value)}`)
    }
  }
})
test('cached quota and reset values reject invalid numeric and date types', () => {
  const { api, key, payload, saved } = load()
  for (const card of [{ ...payload.cards[0], used: '13' }, { ...payload.cards[0], remaining: -1 },
    { ...payload.cards[0], used: 101 }, { ...payload.cards[0], resetAt: 'not-a-date' }]) {
    saved.set(api.quotaCacheKey(key), { ...payload, cards: [card] })
    assert.equal(api.readQuotaCache(key), undefined)
  }
})
test('same-profile chat switches share query identity and cached initial data', () => {
  const { api, key, payload, queries } = load()
  api.saveQuotaCache(key, payload)
  api.useLiveCardsQuery('open', 'one', 'local', 'default', [])
  const first = queries.at(-1)
  assert.equal(first.staleTime, 300000)
  assert.equal(first.refetchInterval, 300000)
  assert(first.initialData().cards.length)
  api.useLiveCardsQuery('open', 'two', 'local', 'default', [])
  assert.equal(JSON.stringify(queries.at(-1).queryKey), JSON.stringify(first.queryKey))
})
test('offline or unresolved owner disables fetching', () => {
  const { api, queries } = load()
  api.useLiveCardsQuery('closed', 'one', 'local', 'default', [])
  assert.equal(queries.at(-1).enabled, false)
  api.useLiveCardsQuery('open', 'one', null, null, [])
  assert.equal(queries.at(-1).enabled, false)
})
test('popup opens upward and has a viewport-bounded scroll body', () => {
  const { api } = load()
  const popup = api.QuickUsage().props.children[1]
  assert.equal(popup.props.side, 'top')
  assert.equal(popup.props.align, 'end')
  assert.equal(popup.props.children[2].props.style.overflowY, 'auto')
})
test('manual refresh floor and in-flight state are shared across consumers', async () => {
  const { api, sdk, key } = load()
  let finish, requests = 0
  sdk.queryClient.fetchQuery = () => { requests++; return new Promise(resolve => { finish = resolve }) }
  const first = api.useLiveCardsQuery('open', 'one', 'local', 'default', [])
  const pending = first.refetch()
  const second = api.useLiveCardsQuery('open', 'two', 'local', 'default', [])
  assert.equal(second.isFetching, true)
  assert(second.cooldownUntil > Date.now())
  assert.equal(await second.refetch(), null)
  assert.equal(requests, 1)
  finish({ cards: [], errors: [], checkedAt: Date.now() })
  await pending
  assert.equal(api.useLiveCardsQuery('open', 'two', 'local', 'default', []).isFetching, false)
  assert.equal(await first.refetch(), null)
})
test('manual refresh failure does not overwrite last good query data', async () => {
  const { api, sdk, writes, key, payload } = load()
  api.saveQuotaCache(key, payload)
  sdk.queryClient.fetchQuery = async () => { throw new Error('Fixture refresh failed') }
  assert.equal(await api.useLiveCardsQuery('open', 'one', 'local', 'default', []).refetch(), null)
  assert.equal(writes.length, 0)
  assert.equal(api.readQuotaCache(key).cards.length, payload.cards.length)
})
test('manual refresh errors are shared by separate page and popup consumers', async () => {
  const { api, sdk, key, payload, renderConsumer } = load({ lifecycle: true })
  api.saveQuotaCache(key, payload)
  sdk.queryClient.fetchQuery = async () => { throw new Error('Fixture shared failure') }
  await renderConsumer('page', 'open', 'one', 'local', 'default', []).refetch()
  const page = renderConsumer('page', 'open', 'one', 'local', 'default', [])
  const popup = renderConsumer('popup', 'open', 'two', 'local', 'default', [])
  assert.equal(page.data.errors.length, 1)
  assert.equal(popup.data.errors.length, 1, 'a different consumer must see the failed refresh')
  assert.match(popup.data.errors[0], /Fixture shared failure/)
  assert.equal(popup.data.cards.length, payload.cards.length)
  const other = renderConsumer('other', 'open', 'one', 'remote', 'other', [])
  assert.equal(other.data?.errors?.length || 0, 0, 'refresh errors must not leak to a different owner')
})
test('successful background checks clear refresh errors for every consumer', async () => {
  const { api, sdk, key, payload, renderConsumer, queries } = load({ lifecycle: true })
  api.saveQuotaCache(key, payload)
  sdk.queryClient.fetchQuery = async () => { throw new Error('Fixture shared failure') }
  await renderConsumer('page', 'open', 'one', 'local', 'default', []).refetch()
  assert.equal(renderConsumer('page', 'open', 'one', 'local', 'default', []).data.errors.length, 1)
  await queries.at(-1).queryFn()
  assert.equal(renderConsumer('page', 'open', 'one', 'local', 'default', []).data.errors.length, 0)
  assert.equal(renderConsumer('popup', 'open', 'two', 'local', 'default', []).data.errors.length, 0)
})
test('older SDK query.refetch success does not require queryClient.setQueryData', async () => {
  const { api, key, payload, renderConsumer, queryResult } = load({ lifecycle: true, withQueryClient: false })
  queryResult.refetch = async () => ({ data: payload })
  const result = await renderConsumer('page', 'open', 'one', 'local', 'default', []).refetch()
  assert(result, 'a successful fallback must not be reported as a failure')
  assert.equal(api.readQuotaCache(key).cards.length, payload.cards.length)
  assert.equal(renderConsumer('popup', 'open', 'two', 'local', 'default', []).data.errors.length, 0)
})
test('older SDK query.refetch errors are shared between consumers', async () => {
  const { api, key, payload, renderConsumer, queryResult } = load({ lifecycle: true, withQueryClient: false })
  api.saveQuotaCache(key, payload)
  queryResult.refetch = async () => ({ error: new Error('Fixture fallback failure') })
  assert.equal(await renderConsumer('page', 'open', 'one', 'local', 'default', []).refetch(), null)
  assert.match(renderConsumer('popup', 'open', 'two', 'local', 'default', []).data.errors[0], /Fixture fallback failure/)
})
test('RPC path maps fixture windows without duplicates', async () => {
  const { api, cards, calls } = load()
  const data = await api.fetchLiveCards('one', { disabled: [] }, 'local', 'default')
  assert.equal(data.cards.length, cards.length)
  assert.equal(new Set(data.cards.map(card => card.id)).size, cards.length)
  assert(Number.isFinite(data.checkedAt))
  assert(calls.some(call => call.method === 'shell.exec'))
})
test('original keyboard handler still opens the full page', () => {
  const { registered, host } = load()
  registered.find(row => row.id === 'open-key').data.run()
  assert.equal(host.route, '/resetwatch')
})
test('plugin disposal releases the countdown interval', () => {
  const { disposers, cleared } = load()
  for (const dispose of disposers) dispose()
  assert(cleared.includes(42))
})
