const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')

// Hermes main serves the session-scoped shell.exec; older backends serve the
// sidecar variant whose closed params refuse session_id (issue #44).
for (const file of ['plugin.js', 'catalog/desktop/plugin.js']) {
  const source = fs.readFileSync(path.join(__dirname, file), 'utf8')
    .replace(/^import .*$/gm, '').replace('export default {', 'const pluginDefinition = {')
  function setup(backend) {
    const calls = []
    const context = vm.createContext({ sdk: { host: {}, atom: value => ({ get: () => value, set: () => {} }) }, console })
    vm.runInContext(source, context)
    context.request = async (method, params) => {
      if (method === 'config.show') return { sections: [{ rows: [['Config File', '/test-home/config.yaml']] }] }
      assert.equal(method, 'shell.exec')
      calls.push({ ...params })
      const scoped = 'session_id' in params
      if (backend === 'canonical' && !scoped) throw new Error('invalid_params')
      if (backend === 'canonical-prose' && !scoped) throw new Error('invalid params')
      if (backend === 'sidecar' && scoped) throw new Error('invalid_params')
      if (backend === 'sidecar-prose' && scoped) throw new Error('Extra inputs are not permitted')
      if (backend === 'scoped-prose' && scoped) throw new Error('invalid params')
      if (backend === 'denied') throw new Error('permission_denied')
      return { code: 0, stdout: JSON.stringify([{ provider: 'vendor' }]) }
    }
    return { calls, run: opts => vm.runInContext(`probeStockAccountUsage(request, ${JSON.stringify(opts)})`, context) }
  }
  test(`${file}: canonical shell.exec gets the session id`, async () => {
    const runner = setup('canonical')
    const result = await runner.run({ sessionId: 'sess-1' })
    assert.equal(result.error, null)
    assert.equal(runner.calls.length, 1)
    assert.equal(runner.calls[0].session_id, 'sess-1')
  })
  test(`${file}: sidecar shell.exec falls back to the bare call`, async () => {
    const runner = setup('sidecar')
    const result = await runner.run({ sessionId: 'sess-1' })
    assert.equal(result.error, null)
    assert.deepEqual(runner.calls.map(call => 'session_id' in call), [true, false])
  })
  test(`${file}: no focused session on canonical names the cause and stops`, async () => {
    const runner = setup('canonical')
    const result = await runner.run({})
    assert.match(result.error, /focused local session/)
    assert.equal(runner.calls.length, 1, 'a refusal must not be retried for every interpreter')
  })
  test(`${file}: other refusals are labelled and not retried`, async () => {
    const runner = setup('denied')
    const result = await runner.run({ sessionId: 'sess-1' })
    assert.equal(result.error, 'shell.exec refused: permission_denied')
    assert.equal(runner.calls.length, 1)
  })
  test(`${file}: sidecar prose refusal still falls back to the bare call`, async () => {
    const runner = setup('sidecar-prose')
    const result = await runner.run({ sessionId: 'sess-1' })
    assert.equal(result.error, null)
    assert.deepEqual(runner.calls.map(call => 'session_id' in call), [true, false])
  })
  test(`${file}: scoped prose invalid params retries without session_id`, async () => {
    const runner = setup('scoped-prose')
    const result = await runner.run({ sessionId: 'sess-1' })
    assert.equal(result.error, null)
    assert.deepEqual(runner.calls.map(call => 'session_id' in call), [true, false])
  })
  test(`${file}: canonical prose refusal without a session names the cause`, async () => {
    const runner = setup('canonical-prose')
    const result = await runner.run({})
    assert.match(result.error, /focused local session/)
    assert.equal(runner.calls.length, 1, 'a refusal must not be retried for every interpreter')
  })
}
