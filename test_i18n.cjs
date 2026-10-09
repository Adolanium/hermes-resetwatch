const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')

const file = process.env.HERMES_TEST_CATALOG ? 'catalog/desktop/plugin.js' : 'plugin.js'
const source = fs.readFileSync(path.join(__dirname, file), 'utf8')
  .replace(/^import .*$/gm, '')
  .replace('export default {', 'const plugin = {')

function atom(value) {
  return { get: () => value, set: next => { value = next }, listen: () => () => {} }
}

// Desktop's lookup: dotted path, functions get the arguments.
function resolve(bundle, key, args) {
  const value = key.split('.').reduce((node, part) => node && typeof node === 'object' ? node[part] : undefined, bundle)
  if (typeof value === 'function') return value(...args)
  return typeof value === 'string' ? value : null
}

// i18n: false is a Desktop build without ctx.i18n or usePluginI18n.
function load({ i18n = false } = {}) {
  const registered = []
  const bundles = {}
  const listeners = []
  let locale = 'en'
  let hookCalls = 0
  const t = (key, ...args) => resolve(bundles[locale], key, args) ?? resolve(bundles.en, key, args) ?? key
  const sdk = { host: { state: {} }, atom, useValue: store => store.get(),
    ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebar', PALETTE_AREA: 'palette', KEYBINDS_AREA: 'keybinds' }
  if (i18n) sdk.usePluginI18n = id => { assert.equal(id, 'resetwatch'); hookCalls++; return t }
  const context = vm.createContext({
    sdk, console, URL, setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
    Fragment: 'fragment', useState: value => [typeof value === 'function' ? value() : value, () => {}],
    useRef: value => ({ current: value }), useEffect: () => {}, useMemo: fn => fn(),
    jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props })
  })
  vm.runInContext(source, context)
  context.ctx = {
    storage: { get: (key, fallback) => fallback, set: () => {} },
    registerMany: rows => registered.push(...rows),
    onDispose: () => {}
  }
  if (i18n) context.ctx.i18n = {
    register: next => { Object.assign(bundles, next); return () => {} },
    t,
    onLocaleChange: listener => { listeners.push(listener); return () => {} }
  }
  vm.runInContext('plugin.register(ctx)', context)
  return {
    bundles,
    hookCalls: () => hookCalls,
    run: code => vm.runInContext(code, context),
    label: id => registered.filter(row => row.id === id).at(-1).data.label,
    switchTo: next => { locale = next; listeners.forEach(listener => listener()) }
  }
}

function shownText(tree, out = []) {
  if (typeof tree === 'string' || typeof tree === 'number') out.push(String(tree))
  else if (Array.isArray(tree)) tree.forEach(item => shownText(item, out))
  else if (tree && tree.props) {
    for (const prop of ['title', 'aria-label', 'placeholder', 'label']) {
      if (typeof tree.props[prop] === 'string') out.push(tree.props[prop])
    }
    shownText(tree.props.children, out)
  }
  return out
}

const NOW = Date.UTC(2026, 0, 1, 12)
const limitCard = card => `LimitCard({ card: { id: 'a', label: 'Weekly', remaining: 72, used: 28, resetAt: null, resetText: '', ...${JSON.stringify(card)} }, nowMs: ${NOW} })`
const inNinetyMinutes = new Date(NOW + 90 * 60000).toISOString()

test(`${file}: every key the UI looks up has English text, and every English string is used`, () => {
  const app = load()
  const leaves = app.run(`(function walk(node, prefix) {
    return Object.entries(node).flatMap(([key, value]) =>
      value && typeof value === 'object' ? walk(value, prefix + key + '.') : [prefix + key])
  })(EN, '')`)
  // Keys are quoted literals inside t(...) or tr(...), including both arms of a ternary.
  const used = new Set()
  for (const call of source.matchAll(/\btr?\(([^)]*)\)/g)) {
    for (const literal of call[1].matchAll(/'([^']+)'/g)) {
      if (literal[1] === 'name' || /^[a-z]+(\.[A-Za-z]+)+$/.test(literal[1])) used.add(literal[1])
    }
  }
  for (const key of used) assert.ok(leaves.includes(key), `${key} has no English text`)
  for (const key of leaves) assert.ok(used.has(key), `${key} is never shown`)
})

test(`${file}: Desktop without i18n shows English`, () => {
  const app = load()
  assert.equal(app.label('nav'), 'Resetwatch')
  assert.equal(app.label('open'), 'Resetwatch: Open')
  assert.equal(app.label('open-key'), 'Open Resetwatch')
  const vendorReset = shownText(app.run(limitCard({ resetText: 'Mon 09:00' })))
  assert.ok(vendorReset.includes('72% left'))
  assert.ok(vendorReset.includes('Resets Mon 09:00'))
  const computed = shownText(app.run(limitCard({ resetAt: inNinetyMinutes })))
  assert.ok(computed.some(line => line.startsWith('Resets in 1h 30m · ')), computed.join(' | '))
  assert.equal(app.run(`pickProbeFailure([{ kind: 'no-deps', message: 'x' }])`).split('.')[0], 'Found Python without httpx on this Gateway')
})

test(`${file}: Desktop i18n translates components and labels, with English for missing keys`, () => {
  const app = load({ i18n: true })
  assert.deepEqual(Object.keys(app.bundles), ['en'])
  // A partial bundle: everything it leaves out stays English.
  app.bundles.zh = {
    name: '重置表',
    open: { palette: '重置表：打开' },
    card: { left: percent => `剩余 ${percent}%` },
    reset: { at: when => `${when} 重置`, hours: (hours, minutes, when) => `${hours} 小时 ${minutes} 分钟后重置 · ${when}` }
  }
  app.switchTo('zh')
  assert.equal(app.label('nav'), '重置表')
  assert.equal(app.label('open'), '重置表：打开')
  assert.equal(app.label('open-key'), 'Open Resetwatch')
  const vendorReset = shownText(app.run(limitCard({ resetText: 'Mon 09:00' })))
  // Components read text through the SDK hook, which re-renders them on a switch.
  assert.ok(app.hookCalls() > 0)
  assert.ok(vendorReset.includes('剩余 72%'))
  assert.ok(vendorReset.includes('Mon 09:00 重置'))
  // A translated computed reset is already a full phrase and is not wrapped again.
  const computed = shownText(app.run(limitCard({ resetAt: inNinetyMinutes })))
  const resetLine = computed.find(line => line.startsWith('1 小时 30 分钟后重置 · '))
  assert.ok(resetLine, computed.join(' | '))
  assert.ok(!resetLine.endsWith(' 重置'), resetLine)
  const controls = shownText(app.run('ProviderControls({ preferences: { disabled: [], order: [] } })'))
  assert.ok(controls.includes('Providers and order'))
  app.bundles.zh.providers = { title: '服务商和顺序' }
  assert.ok(shownText(app.run('ProviderControls({ preferences: { disabled: [], order: [] } })')).includes('服务商和顺序'))
  // Text built outside React follows the same language.
  app.bundles.zh.errors = { noPython: '找不到可用的 Python' }
  assert.equal(app.run(`pickProbeFailure([{ kind: 'no-python', message: 'x' }])`), '找不到可用的 Python')
})
