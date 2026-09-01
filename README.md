<div align="center">

  <a href="https://github.com/NousResearch/hermes-agent">
    <img src="https://github.com/user-attachments/assets/ac2f5702-c842-4b2e-9340-737481fa0ece" width="96" height="96" alt="Nous Research Hermes mark" />
  </a>

  # Resetwatch

  **Plans run out. This page tells you when.**

  Resetwatch is a Hermes Desktop plugin for remaining quota. Live bars for the plans you already signed into. A clock for when each one comes back. No chat has to be open.

  <sub>POWERED BY <a href="https://github.com/NousResearch/hermes-agent">HERMES AGENT</a> &nbsp;·&nbsp; COMMUNITY PLUGIN &nbsp;·&nbsp; VERSION 0.2.12</sub>

  <br /><br />

  [See the cards](#whats-left-on-one-page) &nbsp;·&nbsp; [Install it](#make-it-yours) &nbsp;·&nbsp; [Where the numbers come from](#where-the-numbers-come-from)

</div>

<img width="1629" height="1040" alt="Resetwatch live quota cards" src="https://github.com/user-attachments/assets/633e7697-a50b-42c1-86c7-cc36c28dcd64" />

## Powered by Hermes

Resetwatch is a community plugin for [Hermes Desktop](https://github.com/NousResearch/hermes-agent). It uses the Hermes plugin SDK and the same desktop you already run. Stock Hermes. No fork, no extra server, no build step.

Copy two files and open the page.

## What's left, on one page

Most usage pages live on a vendor site you have to remember to open. Resetwatch puts the bars in Hermes.

| | |
| --- | --- |
| **Live cards**<br />Nous, Claude, Codex, Cursor, and the rest fill themselves from logins already on this machine. Each card is one window: how full it is, how much is left, and when it resets. | **Plan names**<br />Claude shows Pro, Max, Max 5x, or Max 20x. Codex shows Plus. Cursor shows Ultra, or whatever that app is on. Kimi shows Advanced. GLM shows Lite, Pro, or Max. Nous shows the portal plan, not a bare Plus. |
| **Manual clocks**<br />Gemini, Perplexity, or anything you type. Paste the percent left and the reset time from the vendor page. Open takes you there in the system browser. | **A full page**<br />Sidebar, palette ("Resetwatch: Open"), or Ctrl/Cmd+Alt+R. Not a HUD, not a chip, not a side pane. Click a section name to fold it. They start open, and they remember. |

It does not scrape vendor websites. Live rows come from Hermes OAuth plus the same CLI and app logins those vendors already use. Nothing leaves this machine except the usage calls those apps already make for you.

## Leave it open

- Live cards refresh every 5 minutes while the page is open.
- Probe results are cached for 5 minutes. Refresh skips that cache, with a one-minute floor so repeated clicks do not hammer vendor APIs.
- If a login exists but the vendor call fails (HTTP error, timeout, changed payload), the card stays on the page marked "unavailable" with the reason. A vendor you never signed into shows nothing.
- Claude and Codex pool accounts share one fold per vendor. Each account has its own rate-limit backoff.
- Codex extra limits like Spark show up when that account has them.
- Vendor fetches run in parallel with a time budget, so one slow API cannot wipe the page.
- Tokens never go to stdout.

Want another live row? Open an issue. We can add it if that app or CLI already has a remaining-quota path we can read on your machine.

## Works with the logins you already have

Live cards fill on their own when that login is already on the machine:

- **Nous Portal:** Hermes
- **Claude:** every Hermes OAuth account in the credential pool, or Claude Code
- **Codex:** every Hermes OAuth account in the credential pool, or the Codex CLI
- **OpenRouter:** Hermes
- **Cursor:** Cursor app or `cursor-agent`
- **Kimi:** Kimi Code CLI, or `KIMI_CODING_API_KEY` / `KIMI_API_KEY` in Hermes env (Coding Plan)
- **Grok:** Grok CLI
- **GLM:** ZCode Coding Plan, or `ZAI_API_KEY` / `GLM_API_KEY` in Hermes env (includes peak / off-peak pricing)
- **DeepSeek:** `DEEPSEEK_API_KEY` in Hermes env (balance plus peak / off-peak)
- **OpenCode Go:** `OPENCODE_GO_API_KEY` in Hermes env (5h, weekly, monthly)
- **Ollama Cloud:** `OLLAMA_API_KEY` in Hermes env (5h / weekly; no exact reset time from the API)
- **MiniMax:** `MINIMAX_API_KEY` (or `MINIMAX_CN_API_KEY`) in Hermes env (Token Plan 5h / weekly)
- **Novita:** `NOVITA_API_KEY` in Hermes env (dollar balance)
- **DeepInfra:** `DEEPINFRA_API_KEY` in Hermes env (prepaid balance)
- **AI Gateway:** `AI_GATEWAY_API_KEY` in Hermes env (Vercel credits)
- **Command Code:** `COMMANDCODE_API_KEY` in Hermes env, or the `cmd` CLI login (credits, 5-hour / weekly windows, plan, and this period's spend)

Gemini, Perplexity, and anything else can be a manual clock. Type the percent left and the reset time.

## Make it yours

### Install

Copy [`plugin.js`](plugin.js) and [`probe.py`](probe.py) into the Hermes desktop plugin folder:

```text
~/.hermes/desktop-plugins/resetwatch/
```

On Windows:

```text
%LOCALAPPDATA%\hermes\desktop-plugins\resetwatch\
```

Open Hermes and choose **Resetwatch** in the sidebar. If it is missing, use **Cmd+K** (**Ctrl+K** on Windows) then **Reload desktop plugins**. The desktop picks the files up within seconds and reloads on every save.

Copy both files. Live rows need `probe.py` for CLI and app logins Hermes does not OAuth itself.

## Where the numbers come from

```text
Your Hermes Desktop  →  gateway RPCs and probe.py  →  the same usage APIs those apps already call
```

- **Nous.** Dollars and renewal time come from the gateway (`usage.bars`, then `subscription.state` if needed).
- **Gateway accounts.** If Hermes has `account.usage`, that RPC fills Claude, Codex, OpenRouter, and any other providers it already knows.
- **Stock Hermes.** `probe.py` fills the rest through `shell.exec`. Claude and Codex also read every `anthropic` / `openai-codex` row in `$HERMES_HOME/auth.json` (read only) and show one labelled card set per account.
- **CLI fallback.** If Hermes OAuth is missing, Claude Code (`~/.claude`) and Codex CLI (`~/.codex`) fill those cards. Cursor, Kimi, Grok, and GLM come from those apps first.
- **Env keys.** If Kimi or GLM CLI login is missing, Hermes env keys fill the same cards. DeepSeek, OpenCode Go, Ollama Cloud, MiniMax, Novita, DeepInfra, and AI Gateway always use Hermes env (process env or `$HERMES_HOME/.env`). Command Code uses Hermes env first, then `~/.commandcode/auth.json`.
- **Last resort.** Older `/usage` output is still parsed when a session is focused.

Manual clocks are whatever you typed. They do not refresh themselves.

## How it talks to vendors

Live data goes through the desktop plugin SDK (`host.request` JSON-RPC), plus `probe.py` through `shell.exec` when a signed-in CLI or app has quota the gateway does not expose. The page does not log into vendor sites.

`probe.py` does not refresh Claude or Codex credentials. For Kimi and Grok it may refresh on 401 and write that vendor's file back. Before writing it re-reads the file and merges token fields into that fresh record so concurrent CLI edits to other keys are kept. That protects the file. It does not make a shared refresh-token exchange safe if the CLI refreshes in the same window.

**Heads up on Kimi and Grok.** Those vendors rotate refresh tokens. If Resetwatch and the CLI both refresh close together, one of them can get signed out and you will need to log into that CLI again. It is rare, it is harmless, and when Resetwatch did refresh a token the card says so. If you would rather it never happen, run the CLI once so its token is fresh before opening the page.

It may also write a small cache under `$HERMES_HOME/cache/resetwatch`. Incomplete timed-out runs and empty runs are not cached.

## Compatibility

Resetwatch uses the desktop plugin SDK and the standard Hermes gateway methods. It is one uncompiled `plugin.js` plus `probe.py`. No package manager.

It runs on Windows and Mac with stock Hermes Desktop.

## Contributing

Contributions are welcome. Open an issue first for anything bigger than a small fix so we can agree on the shape before you spend time on it.

## License

MIT

<br />

<div align="center">
  <strong>Resetwatch</strong><br />
  <sub>Know what's left. Know when it comes back.</sub>
</div>

<br />

> **Community project**
>
> Resetwatch is an independent community plugin. It is not affiliated with, endorsed by, sponsored by, or officially associated with [Nous Research](https://github.com/NousResearch) or the [Hermes Agent project](https://github.com/NousResearch/hermes-agent). Hermes, Hermes Agent, and Nous Research are names and marks belonging to their respective owners.
