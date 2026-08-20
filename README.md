# Resetwatch

Know how much of each plan is left, and when it comes back. A plugin for
Hermes Desktop.

Bars for what you still have, and the time it fills back up.

<img width="1629" height="1040" alt="demo" src="https://github.com/user-attachments/assets/633e7697-a50b-42c1-86c7-cc36c28dcd64" />



## What you get

**It shows what's left.** Live cards for Nous, Claude, Codex, Cursor, Kimi,
Grok, and GLM when you are already signed in. Each card is one window: how
full it is, how much is still there, and when it resets. No chat has to be
open.

**It names the plan.** Claude shows Pro, Max, Max 5x, or Max 20x. Codex shows
Plus. Cursor shows Ultra (or whatever the app is on). Kimi shows Advanced.
GLM shows Lite, Pro, or Max. Nous Portal shows the portal plan, not a bare
Plus.

**It covers the rest by hand.** Gemini, Perplexity, or anything you type.
Paste the percent left and the reset time from the vendor page. Open takes
you to that dashboard in the system browser.

**It stays a page.** Not a HUD, not a chip, not a side pane. Open it from the
sidebar, the palette ("Resetwatch: Open"), or Ctrl/Cmd+Alt+R. Click a section
name to fold it up. They start open, and they remember.

**It does not scrape the web.** Live rows come from Hermes OAuth plus the
same CLI and app logins those vendors already use. No browser cookies.
Nothing is sent off this machine except the same usage calls those apps
already make for you.

## What's supported

Live cards fill on their own when that login is already on the machine:

- **Nous Portal:** Hermes
- **Claude:** Hermes OAuth, or Claude Code
- **Codex:** Hermes OAuth, or the Codex CLI
- **OpenRouter:** Hermes
- **Cursor:** Cursor app or `cursor-agent`
- **Kimi:** Kimi Code
- **Grok:** Grok CLI
- **GLM:** ZCode, with a Coding Plan login (includes peak / off-peak pricing)
- **DeepSeek:** `DEEPSEEK_API_KEY` in Hermes env (balance plus peak / off-peak)

Gemini, Perplexity, and anything else can be a manual clock. Type the percent
left and the reset time.

Want another live row? Open an issue. We can add it if that app or CLI already
has a remaining-quota path we can read on your machine. We do not scrape vendor
websites.

## Install

Copy `plugin.js` and `probe.py` to `$HERMES_HOME/desktop-plugins/resetwatch/`
(`%LOCALAPPDATA%\hermes` on Windows, `~/.hermes` on Mac). The desktop
picks it up within seconds and hot-reloads on every save. If it does not
appear, run "Reload desktop plugins" from the palette.

Live rows work on stock Hermes. `probe.py` asks the gateway Python for
Claude, Codex, and OpenRouter. If Hermes OAuth is missing, it reads the
Claude Code and Codex CLIs instead. Cursor, Kimi, Grok, GLM (ZCode), and
DeepSeek come from those apps' own CLI, desktop logins, or API keys. Copy
both `plugin.js` and `probe.py`.

## What's inside

- A full `/resetwatch` page in the main workspace
- Sidebar row (watch icon)
- Palette command and `mod+alt+r`
- Live cards, polled every 30s while the page is open
- Fold-up sections that start open
- Manual clocks stored in plugin-scoped `ctx.storage`

## Where the numbers come from

Nous dollars and renewal time come from the gateway (`usage.bars`, then
`subscription.state` if needed). If the gateway has `account.usage`, that
RPC fills Claude, Codex, OpenRouter, and any other providers it already
knows. On stock Hermes, `probe.py` fills the rest through `shell.exec`:
the same Claude / Codex / OpenRouter fetchers. If Hermes OAuth is missing,
Claude Code (`~/.claude`) and Codex CLI (`~/.codex`) fill those cards.
Cursor (app or `cursor-agent` login), Kimi Code (`~/.kimi-code`), Grok
CLI (`~/.grok`), and GLM via ZCode (`~/.zcode`) always come from those
logins. Older `/usage` output is still parsed when a session is focused,
as a last fallback.

Manual clocks are whatever you typed. They do not refresh themselves.

## How it reaches the gateway

Live data goes through the desktop plugin SDK (`host.request` JSON-RPC),
plus `probe.py` through `shell.exec` when a signed-in CLI or app has
quota the gateway does not expose. The page only reads. It does not log
into vendor sites, and it does not write to the gateway host.

## Contributing

Contributions are welcome. Open an issue first for anything bigger than a
small fix so we can agree on the shape before you spend time on it.

## License

MIT
