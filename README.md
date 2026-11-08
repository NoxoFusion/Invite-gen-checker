<div align="center">

# Lako Invite Scanner

**Scrape invite codes từ Disboard → xác thực qua Discord API → thu về đúng N invite hợp lệ, gửi 1 file codes qua webhook.**

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

> Output thật từ một run trên Windows 11 · Python 3.11.6 · kết nối trực tiếp (không Tor). Webhook Discord nhận đúng 1 tin nhắn kèm file `valid_invites.txt` chứa 3 codes.

---

## Mục đích & Tính năng

Tool thu thập invite Discord còn sống từ Disboard.org. Điểm khác biệt: **từ tháng 11/2026, Disboard yêu cầu giải Turnstile riêng cho từng server** (`POST /site/get-invite` trả `400 "Human verification failed"` nếu thiếu token) — mọi scraper dùng curl/requests thuần đều chết ở bước này. Tool dùng **JoinBrowser**: mở trang `/server/join/{id}` bằng Chromium khó-nhận-biết (patchright), để trang tự giải Turnstile của nó, rồi chặn và bắt URL redirect `discord.gg/{code}`.

| Tính năng | Bằng chứng trong code |
|---|---|
| **Quota chính xác** — hỏi "cần bao nhiêu invite?", dừng đúng N, tự scrape thêm vòng mới nếu thiếu (tối đa 3 vòng) | `run_pipeline()` với `round_target = target − total_valid` |
| **Bypass Turnstile per-server** — click widget khi không tự giải, retry 2 lần, xoay browser context khi bị CF đánh dấu | `JoinBrowser` class, `src/scraper.py` |
| **Checker async** — aiohttp, tối đa 50 workers, retry theo `Retry-After` của Discord | `check_invite()`, `main.py` |
| **Dedup theo guild ID xuyên các lần chạy** — không bao giờ thu lại server đã có | `checked_guild_ids.json` (2.461 guilds đã ghi nhận) |
| **Queue thông minh** — code lỗi mạng ở lại để retry, code đã check bị tiêu thụ | `data/input/invites.txt` |
| **Tor proxy pool** (12 tunnel, binary 0.4.9.15 đi kèm) + `proxy.txt` (http/socks4/socks5, có auth) | `tor_manager.py`, `collect_proxies()` |
| **Output tối giản** — chỉ invite codes, 1 code/dòng, tự dedup khi ghi | `data/output/valid_invites.txt` |
| **30 unit tests** (stdlib unittest, không cần mạng) | `tests/` |

Tốc độ đo được: **~4 codes/phút** khi scrape qua browser join flow · **110 CPM** khi check qua Discord API.

---

## Disclaimer & Security

- Tool mang tính **giáo dục/nghiên cứu**. Scrape Disboard và tự động hoá Discord **có thể vi phạm TOS** của hai nền tảng — rủi ro **khóa tài khoản/IP là của bạn**. Tác giả **không chịu trách nhiệm** cho bất kỳ thiệt hại nào.
- **TUYỆT ĐỐI không chia sẻ** token / cookie / password / webhook URL của bạn cho bất kỳ ai.
- `config.json` chứa webhook URL (là secret!). `.gitignore` đã chặn `config.json`, `data/` và `.env` — nhưng **luôn kiểm tra lại** trước khi push. Nếu bạn đã lỡ commit `config.json` ở đâu đó, hãy **rotate webhook** ngay (Discord → Server Settings → Integrations → Webhooks).

---

## Quick Start

**Yêu cầu:** Python 3.10+ (test trên 3.11.6) · Windows · **Google Chrome đã cài** (tool mở Chrome thật để giải Turnstile — cần màn hình, không chạy headless trên VPS).

```bash
git clone <repo-url>
cd invite-gen-check
pip install -r requirements.txt
```

**Cấu hình `config.json`** (tự tạo với giá trị mặc định ở lần chạy đầu):

```json
{
    "tor":     { "enabled": false, "proxy_amount": 12 },
    "scraper": { "concurrency": 6, "max_browser_solves": 2 },
    "scanner": { "concurrency": 50, "timeout": 30, "retries": 2, "target_invites": 0 },
    "webhook": { "url": "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN" }
}
```

- `webhook.url` để trống = tắt webhook (kết quả vẫn lưu file local)
- Cho proxy vào `data/input/proxy.txt` (1 dòng/proxy, `#` = comment)

**Chạy:**

```bash
python main.py                              # tương tác: chọn mode + nhập số lượng
python main.py --scrape --check --target 10 # chạy đủ: scrape → check → webhook
python main.py --check --target 5           # check queue có sẵn
python main.py --no-tor                     # tắt Tor (mặc định đã tắt trong config)
```

---

## Cấu trúc & Workflow

```text
invite-gen-check/
├── main.py              # CLI · checker · pipeline
├── config.json          # toàn bộ settings (chứa webhook secret!)
├── requirements.txt     # 6 dependencies
├── src/
│   ├── paths.py         # mọi đường dẫn ở 1 chỗ duy nhất
│   ├── scraper.py       # Disboard scraper + JoinBrowser (Turnstile)
│   ├── tor_manager.py   # Tor proxy pool (tor.exe 0.4.9.15)
│   └── utils.py
├── tests/               # 30 tests · stdlib unittest
└── data/
    ├── input/           # invites.txt (queue) · proxy.txt
    ├── output/          # valid_invites.txt — invite codes 1/dòng
    ├── data-core/       # checked_guild_ids.json · seen_servers.txt
    └── assets/tor/      # tor.exe · geoip · geoip6
```

```mermaid
flowchart LR
    A["Disboard listing<br/>GET /servers/tag/{tag}"] --> B["Cloudflare Turnstile<br/>patchright Chrome"]
    B --> C["Trang join /server/join/{id}<br/>tự giải Turnstile → redirect"]
    C --> D["Bắt discord.gg/{code}<br/>→ data/input/invites.txt"]
    D --> E["Checker: Discord API v10<br/>aiohttp · tối đa 50 workers"]
    E --> F{"Valid?"}
    F -- "yes" --> G["Dedup theo guild ID<br/>checked_guild_ids.json"]
    F -- "no" --> H["Đếm invalid/error<br/>error ở lại queue retry"]
    G --> I["data/output/valid_invites.txt<br/>1 code/dòng"]
    I --> J["Webhook: 1 tin + file codes"]
```

---

## Xử lý lỗi thường gặp

**`ModuleNotFoundError: No module named 'curl_cffi'` (hoặc `patchright`, `playwright_captcha`)**
→ Thiếu thư viện: `pip install -r requirements.txt`. Đủ 6 gói: aiohttp, aiohttp-socks, rich, curl-cffi, patchright, playwright-captcha.

**Proxy không được dùng / sai format**
→ `data/input/proxy.txt`: mỗi dòng 1 proxy. Không ghi scheme thì mặc định `http://`. Hỗ trợ `http://`, `https://`, `socks4://`, `socks5://`, kèm auth: `http://user:pass@host:port`. Dòng `#` bị bỏ qua.

**Webhook không gửi gì cả**
→ Để `webhook.url` rỗng = tắt. Nếu đã điền mà không thấy tin: URL sai / webhook đã bị xoá trên Discord (rotate lại) — test nhanh bằng `python main.py --check --target 1` với 1 code trong queue. Tool **im lặng** khi url rỗng (by design).

**Bị rate-limit / Cloudflare block liên tục**
→ 429 đã được xử lý tự động theo `Retry-After`. Nếu vẫn nặng: giảm `scanner.concurrency` (50 → 10), tắt Tor khi scrape (`tor.enabled: false` — Cloudflare rất khắc nghiệt với Tor exit), hoặc thêm proxy chất lượng vào `proxy.txt`.

---

## Connect & Support

- **Discord:** [https://discord.gg/rxw8Hwk3qA](https://discord.gg/rxw8Hwk3qA)
- **TikTok:** [@noxofusion](https://www.tiktok.com/@noxofusion)
- **YouTube:** [@noxofusion2010](https://www.youtube.com/@noxofusion2010)

---

## License

Dự án phát hành theo **[MIT License](LICENSE)**.

```
MIT License

Copyright (c) 2026 noxofusion

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
