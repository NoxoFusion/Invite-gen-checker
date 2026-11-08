<div align="center">

# Noxo Invite Scanner

**Scrape invite codes from Disboard → verify against the Discord API → collect exactly N valid invites, delivered as a single codes file via webhook.**

![version](https://img.shields.io/badge/version-2.0.0-8B5CF6)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![tests](https://img.shields.io/badge/tests-30%2F30%20passing-22C55E)
![license](https://img.shields.io/badge/license-MIT-22C55E)
![platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows11)

</div>

---

## Demo

![Demo](assets/demo.png)

```text
$ python main.py --scrape --check --target 3

W0 +ChRCcyJjGf
W0 +kGVpPjKNN2
W0 !No code for 532011252143226886 (blocked)
W0 +WSEcV65JVb
W0 Page 1 done | 3 codes
=== Done ===
Codes: 3 | Failed: 1 | Rate Limits: 0 | Tags: 1/230 | Time: 01:33
Saved 3 codes to data\input\invites.txt

Hoàn tất!
Checked: 3 | Valid: 3 | Invalid: 0 | Errors: 0
Output: data\output\valid_invites.txt

$ cat data\output\valid_invites.txt
ChRCcyJjGf
kGVpPjKNN2
WSEcV65JVb
```

> Real output from a run on Windows 11 · Python 3.11.6 · direct connection (no Tor). The Discord webhook receives exactly one message with a `valid_invites.txt` file containing the 3 codes. *(The console prints a few Vietnamese words — e.g. `Hoàn tất!` means "Done".)*

---

## Purpose & Features

The tool collects live Discord invites from Disboard.org. The key differentiator: **as of November 2026, Disboard requires solving a Turnstile challenge per server** (`POST /site/get-invite` returns `400 "Human verification failed"` without a token) — plain curl/requests scrapers all die at this step. This tool uses a **JoinBrowser**: it opens `/server/join/{id}` in stealth Chromium (patchright), lets the page solve its own Turnstile, then intercepts and captures the `discord.gg/{code}` redirect.

| Feature | Evidence in code |
|---|---|
| **Exact quota** — asks "how many invites?", stops at exactly N, auto-scrapes more rounds if short (max 3) | `run_pipeline()` with `round_target = target − total_valid` |
| **Per-server Turnstile bypass** — clicks the widget when it doesn't auto-solve, retries twice, rotates the browser context when Cloudflare flags it | `JoinBrowser` class, `src/scraper.py` |
| **Async checker** — aiohttp, up to 50 workers, honors Discord's `Retry-After` | `check_invite()`, `main.py` |
| **Cross-run guild dedup** — never collects the same server twice | `checked_guild_ids.json` (2,461 guilds recorded) |
| **Smart queue** — network-error codes stay for retry, checked codes are consumed | `data/input/invites.txt` |
| **Tor proxy pool** (12 tunnels, bundled 0.4.9.15 binary) + `proxy.txt` (http/socks4/socks5, auth supported) | `tor_manager.py`, `collect_proxies()` |
| **Minimal output** — invite codes only, one per line, deduplicated on write | `data/output/valid_invites.txt` |
| **30 unit tests** (stdlib unittest, no network) | `tests/` |

Measured speed: **~4 codes/min** scraping through the browser join flow · **110 CPM** checking via the Discord API.

---

## Disclaimer & Security

- This tool is for **educational/research purposes**. Scraping Disboard and automating Discord **may violate the TOS** of both platforms — the risk of **account/IP bans is yours**. The author **accepts no liability** for any damage.
- **NEVER share** your tokens / cookies / passwords / webhook URLs with anyone.
- `config.json` contains your webhook URL (a secret!). `.gitignore` already blocks `config.json`, `data/` and `.env` — but **always double-check before pushing**. If you ever committed `config.json` somewhere, **rotate the webhook immediately** (Discord → Server Settings → Integrations → Webhooks).

---

## Quick Start

**Requirements:** Python 3.10+ (tested on 3.11.6) · Windows · **Google Chrome installed** (the tool opens a real Chrome window to solve Turnstile — a display is required; it won't run headless on a VPS).

```bash
git clone <repo-url>
cd invite-gen-check
pip install -r requirements.txt
```

**`config.json`** (auto-created with default values on first run):

```json
{
    "tor":     { "enabled": false, "proxy_amount": 12 },
    "scraper": { "concurrency": 6, "max_browser_solves": 2 },
    "scanner": { "concurrency": 50, "timeout": 30, "retries": 2, "target_invites": 0 },
    "webhook": { "url": "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN" }
}
```

- Leave `webhook.url` empty to disable the webhook (results are still saved locally)
- Put proxies in `data/input/proxy.txt` (one per line, `#` = comment)

**Run:**

```bash
python main.py                              # interactive: pick a mode + enter the amount
python main.py --scrape --check --target 10 # full run: scrape → check → webhook
python main.py --check --target 5           # check the existing queue
python main.py --no-tor                     # disable Tor (already off by default in config)
```

---

## Structure & Workflow

```text
invite-gen-check/
├── main.py              # CLI · checker · pipeline
├── requirements.txt     # 6 dependencies
├── src/
│   ├── paths.py         # every path defined in exactly one place
│   ├── scraper.py       # Disboard scraper + JoinBrowser (Turnstile)
│   ├── tor_manager.py   # Tor proxy pool (tor.exe 0.4.9.15)
│   └── utils.py
├── tests/               # 30 tests · stdlib unittest
└── data/
    ├── input/           # invites.txt (queue) · proxy.txt
    ├── output/          # valid_invites.txt — invite codes, one per line
    ├── data-core/       # checked_guild_ids.json · seen_servers.txt
    └── assets/tor/      # tor.exe · geoip · geoip6
```

```mermaid
flowchart LR
    A["Disboard listing<br/>GET /servers/tag/{tag}"] --> B["Cloudflare Turnstile<br/>patchright Chrome"]
    B --> C["Join page /server/join/{id}<br/>auto-solves Turnstile → redirect"]
    C --> D["Capture discord.gg/{code}<br/>→ data/input/invites.txt"]
    D --> E["Checker: Discord API v10<br/>aiohttp · up to 50 workers"]
    E --> F{"Valid?"}
    F -- "yes" --> G["Dedup by guild ID<br/>checked_guild_ids.json"]
    F -- "no" --> H["Count invalid/error<br/>errors stay in queue for retry"]
    G --> I["data/output/valid_invites.txt<br/>1 code per line"]
    I --> J["Webhook: 1 message + codes file"]
```

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'curl_cffi'` (or `patchright`, `playwright_captcha`)**
→ Missing packages: `pip install -r requirements.txt`. Six in total: aiohttp, aiohttp-socks, rich, curl-cffi, patchright, playwright-captcha.

**Proxies not being used / wrong format**
→ `data/input/proxy.txt`: one proxy per line. Without a scheme, `http://` is assumed. Supports `http://`, `https://`, `socks4://`, `socks5://`, with auth: `http://user:pass@host:port`. Lines starting with `#` are skipped.

**Webhook sends nothing**
→ An empty `webhook.url` disables it. If you filled it in and still get nothing: wrong URL / the webhook was deleted on Discord (rotate it) — quick test: `python main.py --check --target 1` with one code in the queue. The tool is **silent** when the URL is empty (by design).

**Constant rate-limits / Cloudflare blocks**
→ 429s are handled automatically via `Retry-After`. If it stays heavy: lower `scanner.concurrency` (50 → 10), keep Tor off while scraping (`tor.enabled: false` — Cloudflare is harsh on Tor exits), or add quality proxies to `proxy.txt`.

---

## Connect & Support

- **Discord:** [https://discord.gg/rxw8Hwk3qA](https://discord.gg/rxw8Hwk3qA)
- **TikTok:** [@noxofusion](https://www.tiktok.com/@noxofusion)
- **YouTube:** [@noxofusion2010](https://www.youtube.com/@noxofusion2010)

---

## License

Released under the **[MIT License](LICENSE)**.

```
MIT License

Copyright (c) 2026 Noxo

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
