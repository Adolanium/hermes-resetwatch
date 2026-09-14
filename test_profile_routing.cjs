const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')

const source = fs.readFileSync(path.join(__dirname, process.env.HERMES_TEST_CATALOG ? 'catalog/desktop/plugin.js' : 'plugin.js'), 'utf8')
  .replace(/^import .*$/gm, '')
  .replace('export default {', 'const plugin = {')

function atom(value) {
  return { get: () => value, set: next => { value = next }, listen: () => () => {} }
}

function load({ legacy = false, route, owner, profile = 'default', connectionId = 'local', probeHome = '/srv/hermes', python, configHome = '/srv/hermes/profiles/backend-worker' } = {}) {
  const calls = []
  const queries = []
  const cacheWrites = []
  const queryResult = { data: undefined }
  const state = { gateway: atom('open'), focusedSessionId: atom('session-a'), profile: atom(profile) }
  if (!legacy) Object.assign(state, {
    connectionId: atom(connectionId),
    focusedSessionOwner: atom(owner === undefined ? { connectionId, profile } : owner),
    focusedSessionProfile: atom(profile)
  })
  async function reply(method, params) {
    if (method === 'config.show') return { sections: [{ rows: [['Config File', `${configHome}/config.yaml`]] }] }
    if (method === 'shell.exec') {
      if (python && !params.command.startsWith(`"${python}" `)) return { code: 127, stderr: 'interpreter not found' }
      if (!params.command.includes(`"${probeHome}/desktop-plugins/resetwatch/probe.py"`)) {
        return { code: 2, stderr: "can't open file 'probe.py'" }
      }
      return { code: 0, stdout: '[]' }
    }
    if (method === 'account.usage') throw new Error('unknown method')
    return {}
  }
  const host = {
    state,
    request: async (method, params) => { calls.push({ method, params }); return reply(method, params) }
  }
  if (!legacy) Object.assign(host, {
    profileRoutes: async () => route ? [route] : [{ connectionId, profile, targetProfile: profile, mode: 'local' }],
    requestProfile: async (selected, method, params) => { calls.push({ route: selected, method, params }); return reply(method, params) }
  })
  const sdk = {
    host, atom,
    useValue: store => store.get(),
    useQuery: options => { queries.push(options); return queryResult },
    queryClient: {
      fetchQuery: options => { queries.push(options); return options.queryFn() },
      setQueryData: (key, data) => cacheWrites.push({ key, data })
    }
  }
  const states = []
  const effects = new Map()
  const pendingEffects = []
  let cursor = 0
  const context = vm.createContext({
    sdk, console, URL, setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
    Fragment: 'fragment',
    useState: value => {
      const index = cursor++
      if (!(index in states)) states[index] = typeof value === 'function' ? value() : value
      return [states[index], next => { states[index] = typeof next === 'function' ? next(states[index]) : next }]
    },
    useRef: value => {
      const index = cursor++
      if (!(index in states)) states[index] = { current: value }
      return states[index]
    },
    useEffect: (effect, deps) => {
      const index = cursor++
      const previous = effects.get(index)
      if (!previous || deps.some((value, i) => value !== previous.deps[i])) {
        pendingEffects.push(() => {
          previous?.cleanup?.()
          effects.set(index, { deps, cleanup: effect() })
        })
      }
    },
    useMemo: fn => fn(),
    jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props })
  })
  vm.runInContext(source + '\nglobalThis.page = PluginPageContent; globalThis.queryHook = useLiveCardsQuery; globalThis.polledHook = useLiveCardsPolled;', context)
  const renderHook = (...args) => { cursor = 0; return context.queryHook(...args) }
  const renderPolled = (...args) => {
    cursor = 0
    const result = context.polledHook(...args)
    while (pendingEffects.length) pendingEffects.shift()()
    return result
  }
  return { context, host, sdk, calls, queries, cacheWrites, queryResult, renderHook, renderPolled }
}

test('older SDK state can render and use the normal request method', async () => {
  const app = load({ legacy: true })
  app.context.page()
  await app.queries[0].queryFn()
  assert.ok(app.calls.some(call => call.method === 'usage.bars'))
  assert.ok(app.calls.every(call => !call.route))
})

test('a remote alias sends every request to the route and the backend name to the probe', async () => {
  const route = { connectionId: 'remote', profile: 'remote-worker', targetProfile: 'backend-worker', mode: 'remote' }
  const app = load({ route, connectionId: 'remote', profile: 'remote-worker' })
  app.context.page()
  await app.queries[0].queryFn()
  assert.ok(app.calls.every(call => call.route === route))
  for (const method of ['usage.bars', 'account.usage', 'subscription.state', 'config.show', 'shell.exec', 'slash.exec']) {
    assert.ok(app.calls.some(call => call.method === method), method)
  }
  const commands = app.calls.filter(call => call.method === 'shell.exec').map(call => call.params.command)
  assert.ok(commands.length)
  assert.ok(commands.every(command => command.includes('--profile "backend-worker"')))
})

test('a profile gateway can find the probe installed in the base home', async () => {
  const app = load({ profile: 'backend-worker' })
  app.context.page()
  const data = await app.queries[0].queryFn()
  assert.ok(!data.errors.some(error => /probe.py not found/.test(error)))
  assert.ok(app.calls.some(call => call.params.command?.includes('"/srv/hermes/desktop-plugins/resetwatch/probe.py"')))
})

test('unknown focused ownership does not query the active account', async () => {
  const app = load({ owner: null })
  app.context.page()
  await assert.rejects(app.queries[0].queryFn(), /focused session|owner/i)
  assert.equal(app.calls.length, 0)
})

test('the focused owner supplies both the connection and profile', async () => {
  const route = { connectionId: 'other', profile: 'beta', targetProfile: 'beta', mode: 'remote' }
  const app = load({ owner: { connectionId: 'other', profile: 'beta' }, route, profile: 'alpha' })
  app.context.page()
  await app.queries[0].queryFn()
  assert.ok(app.calls.every(call => call.route === route))
})

test('query keys separate connections and profiles', () => {
  const keys = []
  for (const [connectionId, profile] of [['one', 'alpha'], ['one', 'beta'], ['two', 'alpha']]) {
    const app = load({ connectionId, profile })
    app.context.page()
    keys.push(JSON.stringify(app.queries[0].queryKey))
  }
  assert.equal(new Set(keys).size, 3)
})

test('a missing route never falls back to another account', async () => {
  const app = load({ route: { connectionId: 'other', profile: 'default', targetProfile: 'default' } })
  app.context.page()
  await assert.rejects(app.queries[0].queryFn(), /No Desktop route/)
  assert.equal(app.calls.length, 0)
})

test('a profile-local probe can use the base home Python', async () => {
  const app = load({ profile: 'backend-worker', probeHome: '/srv/hermes/profiles/backend-worker', python: '/srv/hermes/hermes-agent/.venv/bin/python' })
  app.context.page()
  const data = await app.queries[0].queryFn()
  assert.equal(data.errors.length, 0)
})

test('Windows profile paths can find the base install and interpreter', async () => {
  const base = 'C:\\Users\\Test User\\hermes'
  const app = load({ profile: 'beta', configHome: `${base}\\profiles\\beta`, probeHome: base, python: `${base}/hermes-agent/.venv/Scripts/python.exe` })
  app.context.page()
  const data = await app.queries[0].queryFn()
  assert.equal(data.errors.length, 0)
})

test('legacy focused profiles without a source do not borrow the active connection', async () => {
  const app = load({ legacy: true })
  app.host.state.focusedSessionProfile = atom('other')
  app.context.page()
  await assert.rejects(app.queries[0].queryFn(), /owner/)
  assert.equal(app.calls.length, 0)
})

test('an old refresh writes only to its own profile cache', async () => {
  const app = load()
  let finish
  app.sdk.queryClient.fetchQuery = () => new Promise(resolve => { finish = resolve })
  const first = app.renderHook('open', 'same-session', 'one', 'alpha')
  const pending = first.refetch()
  const second = app.renderHook('open', 'same-session', 'two', 'beta')
  assert.equal(second.isFetching, false)
  const result = { cards: [{ id: 'alpha' }], errors: [] }
  finish(result)
  await pending
  assert.equal(JSON.stringify(app.cacheWrites[0].key), JSON.stringify(['resetwatch', 'live', 'one', 'alpha', 'same-session']))
  assert.equal(app.renderHook('open', 'same-session', 'two', 'beta').data, undefined)
})

test('an old refresh error does not appear under the new profile', async () => {
  const app = load()
  let fail
  app.sdk.queryClient.fetchQuery = () => new Promise((resolve, reject) => { fail = reject })
  const pending = app.renderHook('open', 'same-session', 'one', 'alpha').refetch()
  app.renderHook('open', 'same-session', 'two', 'beta')
  fail(new Error('alpha failed'))
  await pending
  assert.equal(app.renderHook('open', 'same-session', 'two', 'beta').data, undefined)
})

test('route errors are shown on the page', () => {
  const app = load()
  app.queryResult.error = new Error('Could not find the owner of the focused session')
  const result = app.renderHook('open', 'same-session', null, null)
  assert.match(result.data.errors[0], /owner of the focused session/)
})

test('an old poll cannot unblock or replace a new profile request', async () => {
  const app = load()
  const routes = ['alpha', 'beta'].map(profile => ({ connectionId: 'one', profile, targetProfile: profile, mode: 'local' }))
  app.host.profileRoutes = async () => routes
  const original = app.host.requestProfile
  const finish = {}
  const starts = { alpha: 0, beta: 0 }
  app.host.requestProfile = (route, method, params) => {
    if (method === 'usage.bars') {
      starts[route.profile]++
      return new Promise(resolve => { finish[route.profile] = resolve })
    }
    return original(route, method, params)
  }
  const settle = () => new Promise(resolve => setImmediate(resolve))
  app.renderPolled('open', 'same-session', 'one', 'alpha')
  await settle()
  app.renderPolled('open', 'same-session', 'one', 'beta')
  await settle()
  finish.alpha({})
  await settle()
  app.renderPolled('open', 'same-session', 'one', 'beta').refetch()
  assert.equal(starts.beta, 1, 'the old request must not clear the new request lock')
  assert.equal(app.renderPolled('open', 'same-session', 'one', 'beta').isFetching, true)
  finish.beta({})
  await settle()
  assert.equal(app.renderPolled('open', 'same-session', 'one', 'beta').isFetching, false)
})
