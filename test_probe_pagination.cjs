const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')

for (const file of ['plugin.js', 'catalog/desktop/plugin.js']) {
  const source = fs.readFileSync(path.join(__dirname, file), 'utf8')
    .replace(/^import .*$/gm, '').replace('export default {', 'const pluginDefinition = {')
  function setup(rows, { fresh = false, pageBudget = Infinity, onPage = () => {} } = {}) {
    const commands = []
    const context = vm.createContext({ sdk: { host: {}, atom: value => ({ get: () => value, set: () => {} }) }, console })
    vm.runInContext(source, context)
    let cache = rows
    let nextResult = rows
    let counter = 0
    const pinned = new Map()
    context.request = async (method, params) => {
      if (method === 'config.show') return { sections: [{ rows: [['Config File', '/test-home/config.yaml']] }] }
      assert.equal(method, 'shell.exec')
      commands.push(params.command)
      const match = params.command.match(/--slice=(\d+):(\d+)/)
      assert.ok(match, 'every call, including the first, must request a slice')
      const [offset, limit] = match.slice(1).map(Number)
      const tokenMatch = params.command.match(/--snapshot-token=([a-f0-9]{32})/)
      let snapshot, token
      if (params.command.includes('--pin-snapshot')) {
        assert.equal(offset, 0)
        token = (++counter).toString(16).padStart(32, '0')
        snapshot = nextResult
        pinned.set(token, snapshot)
        if (snapshot.length) cache = snapshot
      } else {
        assert.ok(tokenMatch, 'continuations must use a pinned snapshot')
        token = tokenMatch[1]
        snapshot = pinned.get(token)
        assert.ok(snapshot, 'invalid snapshot token')
      }
      // A fresh collection can be incomplete (not cached), and another refresh
      // can replace the shared cache between two pages of this request.
      if (offset > 0) onPage({ setCache: value => { cache = value }, setNext: value => { nextResult = value }, expire: () => pinned.delete(token), cache })
      if (!pinned.has(token)) return { code: 0, stdout: JSON.stringify([{ provider: 'resetwatch', error: 'pin expired' }]) }
      const page = []
      for (const row of snapshot.slice(offset, offset + limit)) {
        if (JSON.stringify([...page, row]).length > pageBudget) break
        page.push(row)
      }
      const stdout = JSON.stringify({ snapshot_token: token, snapshots: page })
      return { code: 0, stdout: stdout.slice(-4000) }
    }
    return { commands, run: () => vm.runInContext(`probeStockAccountUsage(request, ${JSON.stringify({ fresh })})`, context) }
  }
  test(`${file}: collects all rows across pinned RPC slices`, async () => {
    const rows = Array.from({ length: 19 }, (_, i) => ({ provider: `vendor-${i}`, details: ['x'.repeat(210)] }))
    assert.ok(JSON.stringify(rows).length > 4000)
    const runner = setup(rows, { fresh: true })
    const result = await runner.run()
    assert.equal(result.error, null)
    assert.deepEqual(JSON.parse(JSON.stringify(result.snapshots)), rows)
    assert.ok(runner.commands.length > 1)
    assert.match(runner.commands[0], /--pin-snapshot --slice=0:/)
    assert.ok(runner.commands[0].includes('--fresh'))
    assert.ok(runner.commands.slice(1).every(command => !command.includes('--fresh') && /--snapshot-token=[a-f0-9]{32}/.test(command)))
  })
  test(`${file}: short first slice checks for another page before stopping`, async () => {
    const runner = setup([{ provider: 'only' }])
    const result = await runner.run()
    assert.equal(result.error, null)
    assert.equal(result.snapshots.length, 1)
    assert.equal(runner.commands.length, 2)
    assert.match(runner.commands[1], /--slice=1:/)
  })
  test(`${file}: byte-limited short pages advance by actual count`, async () => {
    const rows = Array.from({ length: 14 }, (_, i) => ({ provider: `vendor-${i}`, details: ['x'.repeat(1300)] }))
    const runner = setup(rows, { pageBudget: 3500 })
    const result = await runner.run()
    assert.equal(result.error, null)
    assert.deepEqual(JSON.parse(JSON.stringify(result.snapshots)), rows)
    assert.match(runner.commands[1], /--slice=2:/)
  })
  test(`${file}: never splices old cached rows into an incomplete fresh result`, async () => {
    const newer = Array.from({ length: 8 }, (_, i) => ({ provider: `new-${i}` }))
    const runner = setup(newer, { fresh: true, onPage: ({ setCache }) => setCache(Array.from({ length: 12 }, (_, i) => ({ provider: `old-${i}` }))) })
    const result = await runner.run()
    assert.equal(result.error, null)
    assert.deepEqual(JSON.parse(JSON.stringify(result.snapshots)), newer)
  })
  test(`${file}: refuses a continuation with an unavailable pin instead of another run`, async () => {
    const runner = setup(Array.from({ length: 12 }, (_, i) => ({ provider: `p-${i}` })), { onPage: ({ expire }) => expire() })
    const result = await runner.run()
    assert.equal(result.snapshots, null)
    assert.match(result.error, /snapshot|pin/i)
  })
}
