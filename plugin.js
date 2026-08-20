/**
 * Resetwatch: remaining subscription quota and reset clocks for Hermes Desktop.
 *
 * One uncompiled plugin.js. A full page (sidebar + palette +
 * keybind), not a HUD. Live rows come from gateway RPCs plus probe.py
 * for CLI and app logins Hermes does not OAuth itself.
 * Manual clocks cover plans with no public remaining-quota API.
 *
 * 1. Import the SDK as a namespace so missing named exports cannot crash load.
 * 2. No hardcoded colours. No polling faster than 5 minutes.
 * 3. No cookies, no scrape, no composer chip, no status bar, no right pane.
 */

import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'resetwatch'
const PLUGIN_NAME = 'Resetwatch'
const ROUTE = '/resetwatch'
const VERSION = '0.2.0'
const POLL_MS = 5 * 60 * 1000

const host = sdk.host
const {
  useValue,
  atom,
  useQuery,
  queryClient,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  PALETTE_AREA,
  KEYBINDS_AREA,
  Badge,
  haptic
} = sdk

const text = {
  primary: 'var(--ui-text-primary)',
  secondary: 'var(--ui-text-secondary)',
  tertiary: 'var(--ui-text-tertiary)',
  quaternary: 'var(--ui-text-quaternary)',
  red: 'var(--ui-red)',
  yellow: 'var(--ui-yellow)',
  green: 'var(--ui-green)',
  accent: 'var(--ui-accent)'
}

const PRESETS = [
  { id: 'cursor', name: 'Cursor', url: 'https://cursor.com/dashboard/spending' },
  { id: 'claude', name: 'Claude', url: 'https://claude.ai/settings/usage' },
  { id: 'chatgpt', name: 'ChatGPT', url: 'https://chatgpt.com' },
  { id: 'gemini', name: 'Gemini', url: 'https://gemini.google.com/app' },
  { id: 'grok', name: 'Grok', url: 'https://grok.com' },
  { id: 'perplexity', name: 'Perplexity', url: 'https://www.perplexity.ai' },
  { id: 'custom', name: 'Custom', url: '' }
]

let storage = null
let os = null

const $clocks = atom([])
const $now = atom(Date.now())

function stored(key, fallback) {
  return storage ? storage.get(key, fallback) : fallback
}

function remember(key, value) {
  if (storage) storage.set(key, value)
}

function loadClocks() {
  const raw = stored('clocks', [])
  $clocks.set(Array.isArray(raw) ? raw : [])
}

function saveClocks(next) {
  $clocks.set(next)
  remember('clocks', next)
}

function tap() {
  if (typeof haptic === 'function') haptic('tap')
}

function clampPercent(value) {
  if (value === null || value === undefined || value === '') return null
  const n = Number(value)
  if (!Number.isFinite(n)) return null
  return Math.max(0, Math.min(100, Math.round(n)))
}

function remainingFromUsed(used) {
  const usedPct = clampPercent(used)
  if (usedPct === null) return null
  return Math.max(0, 100 - usedPct)
}

function toneForRemaining(remaining) {
  if (remaining === null || remaining === undefined) return 'ok'
  if (remaining <= 10) return 'bad'
  if (remaining <= 30) return 'warn'
  return 'ok'
}

function toneColor(tone) {
  if (tone === 'bad') return text.red
  if (tone === 'warn') return text.yellow
  return text.secondary
}

function formatReset(resetAt, resetText, nowMs) {
  if (resetText) return resetText
  if (!resetAt) return ''
  const date = new Date(resetAt)
  if (Number.isNaN(date.getTime())) return String(resetAt)
  const delta = date.getTime() - (nowMs || Date.now())
  const local = date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit'
  })
  if (delta <= 0) return `Reset now · ${local}`
  const minutes = Math.round(delta / 60000)
  if (minutes < 60) return `Resets in ${minutes}m · ${local}`
  const hours = Math.floor(minutes / 60)
  const rem = minutes % 60
  if (hours < 24) return `Resets in ${hours}h ${rem}m · ${local}`
  const days = Math.floor(hours / 24)
  return `Resets in ${days}d ${hours % 24}h · ${local}`
}

function toDatetimeLocal(iso) {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = n => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function fromDatetimeLocal(value) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date.toISOString()
}

function isNousProvider(name) {
  return /^nous\b/i.test(String(name || '').trim())
}

function providerKey(name) {
  const key = String(name || '').trim().toLowerCase()
  if (key === 'kimi-coding') return 'kimi'
  if (key === 'xai-oauth' || key === 'xai') return 'grok'
  if (key === 'zai' || key === 'zcode' || key === 'zhipu' || key === 'zai-coding-plan') return 'glm'
  if (key === 'deep-seek') return 'deepseek'
  if (key === 'opencode_go' || key === 'opencode-go-sub' || key === 'go') return 'opencode-go'
  if (key === 'ollama-cloud' || key === 'ollama_cloud') return 'ollama'
  if (key === 'minimax-cn' || key === 'minimax_cn' || key === 'minimax-token-plan') return 'minimax'
  if (key === 'novita-ai' || key === 'novitaai') return 'novita'
  if (key === 'deep-infra') return 'deepinfra'
  if (key === 'ai-gateway' || key === 'vercel' || key === 'vercel-ai-gateway') return 'ai-gateway'
  return key
}

function isHttpUrl(url) {
  return /^https?:\/\//i.test(String(url || '').trim())
}

function newClockId() {
  return `clock:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`
}

function errorMessage(error, fallback) {
  if (typeof error === 'string' && error && error !== '[object Object]') return error
  if (error && typeof error.message === 'string' && error.message && error.message !== '[object Object]') {
    return error.message
  }
  return fallback
}

const PROVIDER_LABELS = {
  anthropic: 'Claude',
  'openai-codex': 'Codex',
  openrouter: 'OpenRouter',
  cursor: 'Cursor',
  kimi: 'Kimi',
  'kimi-coding': 'Kimi',
  grok: 'Grok',
  'xai-oauth': 'Grok',
  xai: 'Grok',
  glm: 'GLM',
  zai: 'GLM',
  zcode: 'GLM',
  zhipu: 'GLM',
  'zai-coding-plan': 'GLM',
  deepseek: 'DeepSeek',
  'deep-seek': 'DeepSeek',
  'opencode-go': 'OpenCode Go',
  opencode_go: 'OpenCode Go',
  go: 'OpenCode Go',
  ollama: 'Ollama Cloud',
  'ollama-cloud': 'Ollama Cloud',
  ollama_cloud: 'Ollama Cloud',
  minimax: 'MiniMax',
  'minimax-cn': 'MiniMax',
  novita: 'Novita',
  'novita-ai': 'Novita',
  deepinfra: 'DeepInfra',
  'deep-infra': 'DeepInfra',
  'ai-gateway': 'AI Gateway',
  vercel: 'AI Gateway',
  'vercel-ai-gateway': 'AI Gateway',
  nous: 'Nous'
}

function nousPortalTitle(planName) {
  const plan = String(planName || '').trim()
  if (!plan || /^nous(\s+portal)?$/i.test(plan)) return 'Nous Portal'
  return `Nous Portal (${plan})`
}

function providerTitle(provider, plan) {
  const key = String(provider || '').trim().toLowerCase()
  const base = PROVIDER_LABELS[key] || provider || 'Account'
  return plan ? `${base} (${plan})` : base
}

function parseUsageOutput(text) {
  const groups = []
  let current = null
  let inLimits = false
  for (const raw of String(text || '').split('\n')) {
    const line = raw.replace(/\*\*/g, '').replace(/^📈\s*/, '').trim()
    if (!line) continue
    if (/^(account limits|nous credits)$/i.test(line)) {
      inLimits = true
      current = null
      continue
    }
    if (/^(session (token )?usage|session info|rate limits)\b/i.test(line)) {
      inLimits = false
      current = null
      continue
    }
    if (!inLimits && !/^provider:/i.test(line)) continue
    const provider = line.match(/^Provider:\s+(.+)$/i)
    if (provider) {
      inLimits = true
      current = { provider: provider[1].trim(), windows: [], details: [] }
      groups.push(current)
      continue
    }
    const windowMatch = line.match(
      /^(.+?):\s+(\d+)% remaining \((\d+)% used\)(?:\s+[•·-]\s+(.+))?$/i
    )
    if (windowMatch) {
      if (!current) {
        current = { provider: 'Account', windows: [], details: [] }
        groups.push(current)
      }
      const suffix = (windowMatch[4] || '').trim()
      const resetText = /^resets\s+/i.test(suffix) ? suffix.replace(/^resets\s+/i, '') : ''
      const detail = resetText ? '' : suffix
      current.windows.push({
        label: windowMatch[1].trim(),
        remaining: Number(windowMatch[2]),
        used: Number(windowMatch[3]),
        resetText,
        detail
      })
      continue
    }
    if (/^unavailable:/i.test(line)) continue
    if (current) current.details.push(line)
  }
  return groups
}

function remainingFromBar(bar) {
  if (!bar) return { remaining: null, used: null }
  const used = clampPercent(bar.pct_used)
  if (used !== null) return { remaining: remainingFromUsed(used), used }
  if (typeof bar.fill_fraction === 'number' && Number.isFinite(bar.fill_fraction)) {
    // Hermes fill_fraction is remaining (remaining_usd / total_usd), not consumed.
    const remaining = clampPercent(bar.fill_fraction * 100)
    return { remaining, used: remainingFromUsed(remaining) }
  }
  return { remaining: null, used: null }
}

function cardsFromUsageBars(bars) {
  if (!bars || bars.available === false) return []
  const cards = []
  const planName = nousPortalTitle(bars.plan_name)
  const pushBar = (bar, label, { showPercent = true } = {}) => {
    if (!bar) return
    const { remaining, used } = remainingFromBar(bar)
    cards.push({
      id: `nous:${label}`,
      source: 'live',
      provider: planName,
      label,
      remaining: showPercent ? remaining : null,
      used: showPercent ? used : null,
      resetAt: bars.renews_at || null,
      resetText: bars.renews_display || '',
      detail:
        bar.remaining_display && bar.total_display
          ? `${bar.remaining_display} of ${bar.total_display} left`
          : bars.subscription_remaining_display || ''
    })
  }
  pushBar(bars.plan_bar, 'Subscription')
  if (bars.has_topup) pushBar(bars.topup_bar, 'Top-up credits', { showPercent: false })
  return cards
}

function cardsFromUsageGroups(groups, skipNous) {
  const cards = []
  for (const group of groups || []) {
    if (skipNous && isNousProvider(group.provider)) continue
    const extra = (group.details || []).filter(line => !/^\(or run \/topup\)$/i.test(line))
    const windows = group.windows || []
    windows.forEach((window, index) => {
      cards.push({
        id: `usage:${group.provider}:${window.label}`,
        source: 'live',
        provider: group.provider,
        label: window.label,
        remaining: clampPercent(window.remaining),
        used: clampPercent(window.used),
        resetAt: null,
        resetText: window.resetText || '',
        detail: [window.detail, index === windows.length - 1 ? extra.join(' · ') : ''].filter(Boolean).join(' · ')
      })
    })
    if (!windows.length && extra.length) {
      cards.push({
        id: `usage:${group.provider}:note`,
        source: 'live',
        provider: group.provider,
        label: group.provider,
        remaining: null,
        used: null,
        resetAt: null,
        resetText: '',
        detail: extra.join(' · ')
      })
    }
  }
  return cards
}

function cardsFromAccountSnapshots(snapshots) {
  const cards = []
  for (const snap of snapshots || []) {
    const provider = providerTitle(snap.provider, snap.plan)
    const extra = (snap.details || []).join(' · ')
    const windows = snap.windows || []
    windows.forEach((window, index) => {
      cards.push({
        id: `account:${snap.provider}:${window.label}`,
        source: 'live',
        provider,
        label: window.label,
        remaining: clampPercent(window.remaining_percent) ?? remainingFromUsed(window.used_percent),
        used: clampPercent(window.used_percent),
        resetAt: window.reset_at || null,
        resetText: '',
        detail: [window.detail, index === windows.length - 1 ? extra : ''].filter(Boolean).join(' · ')
      })
    })
    if (!windows.length && extra) {
      cards.push({
        id: `account:${snap.provider}:note`,
        source: 'live',
        provider,
        label: provider,
        remaining: null,
        used: null,
        resetAt: null,
        resetText: '',
        detail: extra
      })
    }
  }
  return cards
}

function readSessionId() {
  const focused = host.state.focusedSessionId
  const active = host.state.activeSessionId
  return (focused && focused.get && focused.get()) || (active && active.get && active.get()) || ''
}

function hermesHomeFromConfig(payload) {
  const sections = (payload && payload.sections) || []
  for (const section of sections) {
    for (const row of section.rows || []) {
      if (row && row[0] === 'Config File' && row[1]) {
        return String(row[1]).replace(/[\\/]+config\.yaml$/i, '')
      }
    }
  }
  return ''
}

function quoteShell(path) {
  // shell.exec takes a single command string (no argv). Quote for spaces.
  return `"${String(path).replace(/"/g, '\\"')}"`
}

function probePythonCandidates(home) {
  const root = String(home || '').replace(/[\\/]+$/, '')
  return [
    `${root}/hermes-agent/.venv/bin/python`,
    `${root}/hermes-agent/venv/bin/python`,
    `${root}/hermes-agent/.venv/Scripts/python.exe`,
    `${root}/hermes-agent/venv/Scripts/python.exe`
  ]
}

async function probeStockAccountUsage(opts) {
  try {
    const shown = await host.request('config.show', {})
    const home = hermesHomeFromConfig(shown)
    if (!home) return { snapshots: null, error: 'Could not find Hermes home for probe.py' }
    const probe = `${home}/desktop-plugins/resetwatch/probe.py`
    const flags = [
      opts && opts.cliOnly ? '--cli-only' : '',
      opts && opts.fresh ? '--fresh' : ''
    ]
      .filter(Boolean)
      .map(flag => ` ${flag}`)
      .join('')
    const failures = []
    for (const python of probePythonCandidates(home)) {
      try {
        const result = await host.request('shell.exec', {
          command: `${quoteShell(python)} ${quoteShell(probe)}${flags}`
        })
        if (!result || result.code) {
          const err = result && result.stderr ? String(result.stderr).trim() : ''
          failures.push(err || (result ? `probe exit ${result.code}` : 'probe returned nothing'))
          continue
        }
        const text = String(result.stdout || '').trim()
        if (!text.startsWith('[')) {
          failures.push('probe returned non-JSON')
          continue
        }
        const parsed = JSON.parse(text)
        if (Array.isArray(parsed)) return { snapshots: parsed, error: null }
        failures.push('probe JSON was not a list')
      } catch (error) {
        failures.push(errorMessage(error, 'probe failed'))
      }
    }
    return {
      snapshots: null,
      error: failures.find(Boolean) || 'Could not run probe.py (no working Hermes Python)'
    }
  } catch (error) {
    return { snapshots: null, error: errorMessage(error, 'Could not run probe.py') }
  }
}

function go(route) {
  if (typeof host.navigate === 'function') host.navigate(route)
}

async function fetchLiveCards(sessionId, opts) {
  const cards = []
  const errors = []
  const sid = sessionId || readSessionId()
  const fresh = !!(opts && opts.fresh)
  let haveAccountRpc = false

  try {
    const bars = await host.request('usage.bars', {})
    cards.push(...cardsFromUsageBars(bars))
  } catch (error) {
    errors.push(error && error.message ? error.message : 'Could not read Nous usage')
  }

  if (!cards.length) {
    try {
      const sub = await host.request('subscription.state', {})
      if (sub && sub.usage) cards.push(...cardsFromUsageBars(sub.usage))
    } catch (error) {
      errors.push(error && error.message ? error.message : 'Could not read subscription state')
    }
  }

  const accountProviders = new Set()
  try {
    const account = await host.request('account.usage', {})
    haveAccountRpc = true
    const snaps = (account && account.snapshots) || []
    for (const snap of snaps) {
      if (snap && snap.provider) accountProviders.add(providerKey(snap.provider))
    }
    cards.push(...cardsFromAccountSnapshots(snaps))
  } catch (error) {
    const message = error && error.message ? error.message : ''
    if (!/unknown method|not found|-32601/i.test(message)) {
      errors.push(message || 'Could not read signed-in account limits')
    }
  }

  const probeResult = await probeStockAccountUsage({ cliOnly: haveAccountRpc, fresh })
  const probed = probeResult && probeResult.snapshots
  if (probeResult && probeResult.error && !(probed && probed.length)) {
    errors.push(probeResult.error)
  }
  let haveClaudeProbe = false
  if (probed && probed.length) {
    const extra = haveAccountRpc
      ? probed.filter(snap => snap && snap.provider && !accountProviders.has(providerKey(snap.provider)))
      : probed
    for (const snap of extra) {
      const key = providerKey(snap && snap.provider)
      if (key === 'anthropic' || key === 'claude') haveClaudeProbe = true
    }
    if (extra.length) cards.push(...cardsFromAccountSnapshots(extra))
  }

  // Claude often comes from Hermes /usage when the probe has Codex/etc. but
  // no Anthropic row. Only skip that fallback once Claude is already present.
  // Never surface a stale focused-session "session not found".
  if (!haveAccountRpc && sid && !haveClaudeProbe) {
    try {
      const result = await host.request('slash.exec', { command: 'usage', session_id: sid })
      const output = result && typeof result.output === 'string' ? result.output : ''
      const skipNous = cards.some(card => String(card.id).startsWith('nous:'))
      cards.push(...cardsFromUsageGroups(parseUsageOutput(output), skipNous))
    } catch (error) {
      const message = error && error.message ? error.message : ''
      if (!/session not found/i.test(message)) {
        errors.push(message || 'Could not run /usage')
      }
    }
  }

  const seen = new Set()
  const unique = []
  for (const card of cards) {
    if (seen.has(card.id)) continue
    seen.add(card.id)
    unique.push(card)
  }
  return { cards: unique, errors, hadSession: Boolean(sid), haveAccountRpc }
}

function SmallButton({ onClick, children, active, title, disabled }) {
  return jsx('button', {
    type: 'button',
    title,
    disabled: !!disabled,
    onClick,
    style: {
      fontSize: '0.6875rem',
      padding: '2px 8px',
      border: `1px solid ${active ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}`,
      borderRadius: 4,
      color: active ? text.primary : text.secondary,
      background: active ? 'var(--ui-control-active-background)' : 'transparent',
      opacity: disabled ? 0.5 : 1,
      cursor: disabled ? 'default' : 'pointer'
    },
    children
  })
}

function Field({ label, children }) {
  return jsxs('label', {
    style: { display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0, flex: 1 },
    children: [
      jsx('span', { style: { fontSize: '0.6875rem', color: text.tertiary }, children: label }),
      children
    ]
  })
}

function NativeInput({ value, onChange, type, placeholder, style }) {
  return jsx('input', {
    type: type || 'text',
    value: value || '',
    placeholder,
    onChange: event => onChange(event.target.value),
    style: {
      height: 28,
      padding: '0 8px',
      borderRadius: 6,
      border: '1px solid var(--ui-stroke-secondary)',
      background: 'transparent',
      color: text.primary,
      fontSize: '0.8125rem',
      outline: 'none',
      ...style
    }
  })
}

function UsageBar({ remaining }) {
  const used = remaining === null || remaining === undefined ? 0 : Math.max(0, 100 - remaining)
  const tone = toneForRemaining(remaining)
  const fill = tone === 'bad' ? text.red : tone === 'warn' ? text.yellow : 'var(--ui-text-primary)'
  return jsx('div', {
    style: {
      width: 92,
      height: 6,
      borderRadius: 99,
      background: 'var(--ui-stroke-secondary)',
      overflow: 'hidden',
      flexShrink: 0
    },
    children: jsx('div', {
      style: {
        width: `${used}%`,
        height: '100%',
        borderRadius: 99,
        background: fill
      }
    })
  })
}

function displayDetail(card) {
  const detail = String((card && card.detail) || '').trim()
  if (!detail) return ''
  // Bar + "% left" already show the same unitless fraction.
  if (card.remaining !== null && card.remaining !== undefined) {
    if (/^[\d,]+\s+of\s+[\d,]+\s+left$/i.test(detail)) return ''
  }
  return detail
}

function LimitCard({ card, nowMs, actions }) {
  const remaining = card.remaining
  const tone = toneForRemaining(remaining)
  const reset = formatReset(card.resetAt, card.resetText, nowMs)
  const leftLabel = remaining === null || remaining === undefined ? '—' : `${remaining}% left`
  const detail = displayDetail(card)
  return jsxs('div', {
    style: {
      display: 'flex',
      alignItems: 'center',
      gap: 16,
      padding: '12px 14px',
      borderRadius: 10,
      border: '1px solid var(--ui-stroke-secondary)',
      background: 'var(--ui-bg-secondary, transparent)'
    },
    children: [
      jsxs('div', {
        style: { flex: 1, minWidth: 0 },
        children: [
          jsx('div', {
            style: { fontSize: '0.875rem', fontWeight: 600, color: text.primary },
            children: card.label
          }),
          reset
            ? jsx('div', {
                style: { fontSize: '0.75rem', color: text.tertiary, marginTop: 2 },
                children: reset.startsWith('Resets') || reset.startsWith('Reset') ? reset : `Resets ${reset}`
              })
            : null,
          detail
            ? jsx('div', {
                style: { fontSize: '0.75rem', color: text.tertiary, marginTop: 2 },
                children: detail
              })
            : null
        ]
      }),
      remaining === null || remaining === undefined
        ? null
        : jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 },
            children: [
              jsx(UsageBar, { remaining }),
              jsx('div', {
                style: { fontSize: '0.8125rem', color: toneColor(tone), minWidth: 64, textAlign: 'right' },
                children: leftLabel
              })
            ]
          }),
      actions || null
    ]
  })
}

function useSectionOpen(key) {
  const [open, setOpen] = useState(() => {
    const saved = stored(`sectionOpen:${key}`, null)
    return saved === null || saved === undefined ? true : !!saved
  })
  return [
    open,
    () => {
      const next = !open
      setOpen(next)
      remember(`sectionOpen:${key}`, next)
    }
  ]
}

function SectionHeader({ title, open, onToggle, extra }) {
  return jsxs('div', {
    style: { display: 'flex', alignItems: 'center', gap: 8 },
    children: [
      jsxs('button', {
        type: 'button',
        onClick: () => {
          tap()
          onToggle()
        },
        'aria-expanded': open,
        style: {
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          flex: 1,
          minWidth: 0,
          padding: 0,
          border: 'none',
          background: 'transparent',
          color: text.primary,
          cursor: 'pointer',
          textAlign: 'left'
        },
        children: [
          jsx('span', {
            style: { fontSize: '0.7rem', color: text.tertiary, width: 10 },
            children: open ? '▾' : '▸'
          }),
          jsx('h2', {
            style: { fontSize: '0.75rem', fontWeight: 600, color: text.primary, margin: 0 },
            children: title
          })
        ]
      }),
      extra || null
    ]
  })
}

function ProviderBlock({ title, cards, nowMs, empty, actionsFor }) {
  const [open, toggle] = useSectionOpen(`provider:${title}`)
  return jsxs('section', {
    style: { display: 'flex', flexDirection: 'column', gap: 8 },
    children: [
      jsx(SectionHeader, { title, open, onToggle: toggle }),
      !open
        ? null
        : !cards.length
          ? jsx('div', { style: { fontSize: '0.8125rem', color: text.tertiary }, children: empty })
          : cards.map(card =>
              jsx(
                LimitCard,
                { card, nowMs, actions: actionsFor ? actionsFor(card) : null },
                card.id
              )
            )
    ]
  })
}

function ClockForm({ onSave, onCancel }) {
  const [presetId, setPresetId] = useState('gemini')
  const preset = PRESETS.find(item => item.id === presetId) || PRESETS[0]
  const [name, setName] = useState(preset.name)
  const [left, setLeft] = useState('70')
  const [resetLocal, setResetLocal] = useState('')
  const [url, setUrl] = useState(preset.url)

  useEffect(() => {
    const next = PRESETS.find(item => item.id === presetId) || PRESETS[0]
    if (presetId !== 'custom') {
      setName(next.name)
      setUrl(next.url)
    }
  }, [presetId])

  const remaining = clampPercent(left)
  const canSave = Boolean(name.trim()) && remaining !== null

  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 10,
      padding: 12,
      borderRadius: 10,
      border: '1px solid var(--ui-stroke-secondary)'
    },
    children: [
      jsxs('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 8 }, children: [
        PRESETS.map(item =>
          jsx(
            SmallButton,
            {
              active: presetId === item.id,
              onClick: () => {
                tap()
                setPresetId(item.id)
              },
              children: item.name
            },
            item.id
          )
        )
      ] }),
      jsxs('div', { style: { display: 'grid', gridTemplateColumns: '1fr 90px 1fr', gap: 8 }, children: [
        jsx(Field, { label: 'Name', children: jsx(NativeInput, { value: name, onChange: setName, placeholder: 'Plan name' }) }),
        jsx(Field, {
          label: '% left',
          children: jsx(NativeInput, { value: left, onChange: setLeft, type: 'number', placeholder: '70' })
        }),
        jsx(Field, {
          label: 'Resets',
          children: jsx(NativeInput, { value: resetLocal, onChange: setResetLocal, type: 'datetime-local' })
        })
      ] }),
      jsx(Field, {
        label: 'Dashboard URL (optional)',
        children: jsx(NativeInput, { value: url, onChange: setUrl, placeholder: 'https://' })
      }),
      jsxs('div', { style: { display: 'flex', gap: 8, justifyContent: 'flex-end' }, children: [
        jsx(SmallButton, { onClick: onCancel, children: 'Cancel' }),
        jsx(SmallButton, {
          active: true,
          disabled: !canSave,
          onClick: () => {
            if (!canSave) return
            tap()
            onSave({
              id: newClockId(),
              name: name.trim(),
              remaining,
              resetAt: fromDatetimeLocal(resetLocal),
              url: url.trim()
            })
          },
          children: 'Add clock'
        })
      ] })
    ]
  })
}

function EditClock({ clock, onSave, onCancel }) {
  const [name, setName] = useState(clock.name)
  const [left, setLeft] = useState(String(clock.remaining))
  const [resetLocal, setResetLocal] = useState(toDatetimeLocal(clock.resetAt))
  const [url, setUrl] = useState(clock.url || '')
  const remaining = clampPercent(left)
  const canSave = Boolean(name.trim()) && remaining !== null
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 8, minWidth: 220 },
    children: [
      jsx(NativeInput, { value: name, onChange: setName }),
      jsxs('div', { style: { display: 'flex', gap: 8 }, children: [
        jsx(NativeInput, { value: left, onChange: setLeft, type: 'number', style: { width: 72 } }),
        jsx(NativeInput, { value: resetLocal, onChange: setResetLocal, type: 'datetime-local' })
      ] }),
      jsx(NativeInput, { value: url, onChange: setUrl, placeholder: 'https://' }),
      jsxs('div', { style: { display: 'flex', gap: 6, justifyContent: 'flex-end' }, children: [
        jsx(SmallButton, { onClick: onCancel, children: 'Cancel' }),
        jsx(SmallButton, {
          active: true,
          disabled: !canSave,
          onClick: () => {
            if (!canSave) return
            onSave({ ...clock, name: name.trim(), remaining, resetAt: fromDatetimeLocal(resetLocal), url: url.trim() })
          },
          children: 'Save'
        })
      ] })
    ]
  })
}

function useLiveCardsPolled(gateway, sessionId) {
  const sid = sessionId || ''
  const [data, setData] = useState({ cards: [], errors: [], hadSession: false, haveAccountRpc: false })
  const [isFetching, setFetching] = useState(false)
  const genRef = useRef(0)

  const load = (opts) => {
    if (gateway !== 'open') return
    const gen = ++genRef.current
    setFetching(true)
    fetchLiveCards(sid, opts)
      .then(next => {
        if (gen !== genRef.current) return
        setData(next)
      })
      .catch(error => {
        if (gen !== genRef.current) return
        setData({
          cards: [],
          errors: [errorMessage(error, 'Could not read live usage')],
          hadSession: Boolean(sid),
          haveAccountRpc: false
        })
      })
      .finally(() => {
        if (gen !== genRef.current) return
        setFetching(false)
      })
  }

  useEffect(() => {
    load()
    const id = setInterval(() => load(), POLL_MS)
    return () => {
      genRef.current += 1
      clearInterval(id)
    }
  }, [gateway, sid])

  return { data, isFetching, refetch: () => load({ fresh: true }) }
}

function useLiveCardsQuery(gateway, sessionId) {
  const sid = sessionId || ''
  const query = useQuery({
    queryKey: [PLUGIN_ID, 'live', sid],
    queryFn: () => fetchLiveCards(sid),
    enabled: gateway === 'open',
    refetchInterval: POLL_MS,
    retry: false
  })
  const refetch = () => {
    if (!queryClient || typeof queryClient.fetchQuery !== 'function') {
      return query.refetch && query.refetch()
    }
    return queryClient
      .fetchQuery({
        queryKey: [PLUGIN_ID, 'live', sid, 'fresh'],
        queryFn: () => fetchLiveCards(sid, { fresh: true }),
        staleTime: 0
      })
      .then(data => {
        queryClient.setQueryData([PLUGIN_ID, 'live', sid], data)
        return data
      })
  }
  return { data: query.data, isFetching: query.isFetching, refetch }
}

const useLiveCards = typeof useQuery === 'function' ? useLiveCardsQuery : useLiveCardsPolled

function groupLiveCards(cards) {
  const groups = []
  const index = new Map()
  for (const card of cards || []) {
    const key = card.provider || 'Account'
    if (!index.has(key)) {
      const group = { title: key, cards: [] }
      index.set(key, group)
      groups.push(group)
    }
    index.get(key).cards.push(card)
  }
  return groups
}

function Page() {
  const gateway = useValue(host.state.gateway)
  const nowMs = useValue($now)
  const clocks = useValue($clocks)
  const sessionId = useValue(host.state.focusedSessionId)
  const live = useLiveCards(gateway, sessionId)
  const [adding, setAdding] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [liveOpen, toggleLive] = useSectionOpen('live')
  const [manualOpen, toggleManual] = useSectionOpen('manual')
  const payload = live.data || { cards: [], errors: [], hadSession: false }
  const groups = useMemo(
    () => groupLiveCards(payload.cards).filter(group => group.cards.length),
    [payload.cards]
  )

  useEffect(() => {
    const id = setInterval(() => $now.set(Date.now()), 30000)
    return () => clearInterval(id)
  }, [])

  const openExternal = url => {
    if (!isHttpUrl(url)) return
    tap()
    if (os && typeof os.openExternal === 'function') os.openExternal(url)
  }

  const clockCards = clocks.map(clock => ({
    id: clock.id,
    source: 'manual',
    provider: clock.name,
    label: clock.name,
    remaining: clock.remaining,
    used: remainingFromUsed(clock.remaining),
    resetAt: clock.resetAt,
    resetText: '',
    detail: clock.url ? clock.url.replace(/^https?:\/\//, '') : 'Typed by you. Update when the vendor page changes.',
    url: clock.url
  }))

  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 },
    children: [
      jsxs('div', {
        style: {
          display: 'flex',
          alignItems: 'baseline',
          gap: 10,
          padding: '10px 16px 6px',
          borderBottom: '1px solid var(--ui-stroke-secondary)'
        },
        children: [
          jsx('h1', { style: { fontSize: '1rem', fontWeight: 600, color: text.primary, margin: 0 }, children: PLUGIN_NAME }),
          jsx('span', {
            style: { color: text.tertiary, fontSize: '0.75rem' },
            children: 'How much is left, and when it comes back'
          }),
          Badge ? jsx(Badge, { variant: 'muted', children: VERSION }) : null,
          jsxs('div', {
            style: { marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' },
            children: [
              jsx('span', {
                style: { fontSize: '0.6875rem', color: text.tertiary },
                children: `gateway ${gateway || 'idle'}`
              }),
              jsx(SmallButton, {
                onClick: () => {
                  tap()
                  if (live.refetch) live.refetch()
                  else if (queryClient) queryClient.invalidateQueries({ queryKey: [PLUGIN_ID, 'live'] })
                },
                children: live.isFetching ? 'Refreshing…' : 'Refresh'
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        style: {
          flex: 1,
          minHeight: 0,
          overflowY: 'auto',
          padding: 16,
          display: 'flex',
          flexDirection: 'column',
          gap: 22
        },
        children: [
          jsx('p', {
            style: { margin: 0, maxWidth: 640, fontSize: '0.8125rem', color: text.secondary, lineHeight: 1.45 },
            children:
              'Live rows are plans already signed in on this machine: Hermes OAuth first, then Claude Code, Codex, Cursor, Kimi, Grok, GLM, DeepSeek, OpenCode Go, Ollama Cloud, MiniMax, Novita, DeepInfra, and AI Gateway when those CLIs, apps, or API keys are logged in. Kimi and GLM can also use Hermes Coding Plan API keys. Click a section name to fold it up.'
          }),
          jsxs('section', {
            style: { display: 'flex', flexDirection: 'column', gap: 12 },
            children: [
              jsx(SectionHeader, {
                title: 'Automatic',
                open: liveOpen,
                onToggle: toggleLive
              }),
              !liveOpen
                ? null
                : jsxs('div', {
                    style: { display: 'flex', flexDirection: 'column', gap: 16, paddingLeft: 16 },
                    children: [
                      groups.length
                        ? jsx('div', {
                            style: {
                              display: 'grid',
                              gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 380px), 1fr))',
                              gap: 22,
                              alignItems: 'start'
                            },
                            children: groups.map(group =>
                              jsx(ProviderBlock, { title: group.title, cards: group.cards, nowMs }, group.title)
                            )
                          })
                        : jsx('div', {
                            style: { fontSize: '0.8125rem', color: text.tertiary },
                            children:
                              'No remaining-quota windows yet. Sign into Claude, Codex, Cursor, Kimi, Grok, GLM, DeepSeek, OpenCode Go, Ollama Cloud, MiniMax, Novita, DeepInfra, AI Gateway, OpenRouter, or Nous, then refresh.'
                          }),
                      payload.errors && payload.errors.length
                        ? jsx('div', {
                            style: { fontSize: '0.75rem', color: text.tertiary },
                            children: payload.errors.join(' · ')
                          })
                        : null
                    ]
                  })
            ]
          }),
          jsxs('section', {
            style: { display: 'flex', flexDirection: 'column', gap: 8 },
            children: [
              jsx(SectionHeader, {
                title: 'Manual clocks',
                open: manualOpen,
                onToggle: toggleManual,
                extra: jsx(SmallButton, {
                  active: adding,
                  onClick: () => {
                    tap()
                    if (!manualOpen) toggleManual()
                    setAdding(open => !open)
                  },
                  children: adding ? 'Close' : 'Add clock'
                })
              }),
              !manualOpen || !adding
                ? null
                : jsx(ClockForm, {
                    onSave: clock => {
                      saveClocks([...clocks, clock])
                      setAdding(false)
                    },
                    onCancel: () => setAdding(false)
                  }),
              !manualOpen
                ? null
                : clockCards.map(card => {
                    const clock = clocks.find(item => item.id === card.id)
                    return editingId === card.id && clock
                      ? jsx(
                          EditClock,
                          {
                            clock,
                            onSave: next => {
                              saveClocks(clocks.map(item => (item.id === next.id ? next : item)))
                              setEditingId(null)
                            },
                            onCancel: () => setEditingId(null)
                          },
                          card.id
                        )
                      : jsx(
                          LimitCard,
                          {
                            card,
                            nowMs,
                            actions: jsxs('div', {
                              style: { display: 'flex', gap: 6 },
                              children: [
                                isHttpUrl(card.url)
                                  ? jsx(SmallButton, {
                                      onClick: () => openExternal(card.url),
                                      children: 'Open'
                                    })
                                  : null,
                                jsx(SmallButton, {
                                  onClick: () => {
                                    tap()
                                    setEditingId(card.id)
                                  },
                                  children: 'Edit'
                                }),
                                jsx(SmallButton, {
                                  onClick: () => {
                                    tap()
                                    saveClocks(clocks.filter(item => item.id !== card.id))
                                  },
                                  children: 'Remove'
                                })
                              ]
                            })
                          },
                          card.id
                        )
                  })
            ]
          })
        ]
      })
    ]
  })
}

export default {
  id: PLUGIN_ID,
  name: PLUGIN_NAME,
  description: 'Remaining quota and reset clocks for the plans you already pay for.',
  defaultEnabled: true,
  register(ctx) {
    const onDispose = typeof ctx.onDispose === 'function' ? fn => ctx.onDispose(fn) : () => {}
    storage = ctx.storage || null
    os = ctx.os || null
    loadClocks()

    const contributions = [
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(Page, {}) },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 62,
        data: { path: ROUTE, label: PLUGIN_NAME, codicon: 'watch' }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'resetwatch.open',
          label: 'Resetwatch: Open',
          keywords: ['usage', 'quota', 'reset', 'limits', 'subscription'],
          run: () => go(ROUTE)
        }
      }
    ]
    if (KEYBINDS_AREA) {
      contributions.push({
        id: 'open-key',
        area: KEYBINDS_AREA,
        data: {
          id: 'resetwatch.open',
          label: 'Open Resetwatch',
          category: PLUGIN_NAME,
          defaults: ['mod+alt+r'],
          run: () => go(ROUTE)
        }
      })
    }
    ctx.registerMany(contributions)
    onDispose(() => {
      storage = null
      os = null
    })
  }
}
