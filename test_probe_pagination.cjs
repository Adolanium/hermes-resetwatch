const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')

for (const file of ['plugin.js', 'catalog/desktop/plugin.js']) {
  const source = fs.readFileSync(path.join(__dirname, file), 'utf8')
    .replace(/^import .*$/gm, '').replace('export default {', 'const pluginDefinition = {')
  function setup(rows, { fresh = false, pageBudget = Infinity } = {}) {
    const commands = []
    const context = vm.createContext({ sdk: { host: {}, atom: value => ({ get: () => value, set: () => {} }) }, console })
    vm.runInContext(source, context)
    context.request = async (method, params) => {
      if (method === 'config.show') return { sections: [{ rows: [['Config File', '/test-home/config.yaml']] }] }
      assert.equal(method, 'shell.exec')
      commands.push(params.command)
      const match = params.command.match(/--slice=(\d+):(\d+)/)
      assert.ok(match, 'every call, including the first, must request a slice')
      const [offset, limit] = match.slice(1).map(Number)
      const page = []
      for (const row of rows.slice(offset, offset + limit)) {
        if (JSON.stringify([...page, row]).length > pageBudget) break
        page.push(row)
      }
      const stdout = JSON.stringify(page)
      return { code: 0, stdout: stdout.slice(-4000) }
    }
    return { commands, run: () => vm.runInContext(`probeStockAccountUsage(request, ${JSON.stringify({ fresh })})`, context) }
  }
  test(`${file}: collects all rows across RPC slices, fresh only on first request`, async () => {
    const rows = Array.from({ length: 19 }, (_, i) => ({ provider: `vendor-${i}`, details: ['x'.repeat(210)] }))
    assert.ok(JSON.stringify(rows).length > 4000)
    const runner = setup(rows, { fresh: true })
    const result = await runner.run()
    assert.equal(result.error, null)
    assert.deepEqual(JSON.parse(JSON.stringify(result.snapshots)), rows)
    assert.ok(runner.commands.length > 1)
    assert.match(runner.commands[0], /--slice=0:/)
    assert.ok(runner.commands[0].includes('--fresh'))
    assert.ok(runner.commands.slice(1).every(command => !command.includes('--fresh')))
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
}
