# Resetwatch

Know how much of each plan is left, and when it comes back. One plugin file
for Hermes Desktop.

Bars for what you still have, and the time it fills back up.

<img width="1629" height="1040" alt="demo" src="https://github.com/user-attachments/assets/633e7697-a50b-42c1-86c7-cc36c28dcd64" />



## What you get

**It shows what's left.** Live cards for Nous, Claude, Codex, Cursor, and Kimi
when you are already signed in. Each card is one window: how full it is, how
much is still there, and when it resets. No chat has to be open.

**It names the plan.** Claude shows Pro, Max, Max 5x, or Max 20x. Codex shows
Plus. Cursor shows Ultra (or whatever the app is on). Kimi shows Advanced.
Nous Portal shows the portal plan, not a bare Plus.

**It covers the rest by hand.** Gemini, Grok, Perplexity, or anything you
type. Paste the percent left and the reset time from the vendor page. Open
takes you to that dashboard in the system browser.

**It stays a page.** Not a HUD, not a chip, not a side pane. Open it from the
sidebar, the palette ("Resetwatch: Open"), or Ctrl/Cmd+Alt+R. Click a section
name to fold it up. They start open, and they remember.

**It does not scrape the web.** Live rows come from logins Hermes already
uses for usage. No browser cookies. Nothing is sent off this machine except
the same usage calls those apps already make for you.

## Install

Copy `plugin.js` to `$HERMES_HOME/desktop-plugins/resetwatch/plugin.js`
(`%LOCALAPPDATA%\hermes` on Windows, `~/.hermes` on Mac). The desktop
picks it up within seconds and hot-reloads on every save. If it does not
appear, run "Reload desktop plugins" from the palette.

Cursor and Kimi live rows need a gateway that already has `account.usage`
for those CLIs. If those sections are empty, restart the gateway after that
update.

## What's inside

- A full `/resetwatch` page in the main workspace
- Sidebar row (watch icon)
- Palette command and `mod+alt+r`
- Live cards, polled every 30s while the page is open
- Fold-up sections that start open
- Manual clocks stored in plugin-scoped `ctx.storage`

## Where the numbers come from

Nous dollars and renewal time come from the gateway (`usage.bars`, then
`subscription.state` if needed). Claude, Codex, OpenRouter, Cursor, and Kimi
come from `account.usage`. No focused chat required.

Cursor uses the same DashboardService period-usage call the official CLI
uses, from the Cursor CLI or app login on this machine. Kimi uses the
official Kimi Code CLI login against `/coding/v1/usages`. Older gateways
without `account.usage` fall back to `/usage` on the focused session.

Grok is not a live row. A Grok CLI login does not add a section. Add it as
a manual clock.

Manual clocks are whatever you typed. They do not refresh themselves.

## How it reaches the gateway

Live data goes through the desktop plugin SDK (`host.request` JSON-RPC). The
page only reads. It does not log into vendor sites, and it does not write
to the gateway host.

## Contributing

Contributions are welcome. Open an issue first for anything bigger than a
small fix so we can agree on the shape before you spend time on it.

## License

MIT
