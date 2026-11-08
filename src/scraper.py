"""
Scraper - Lay Discord invite codes tu Disboard.org

Phase 1: patchright solve Cloudflare Turnstile (1 lan per proxy)
Phase 2: curl_cffi cho tat ca requests
  - Auto-discover tags from Disboard
  - GET listing pages -> parse server IDs
  - POST /site/get-invite/{id} -> lay invite code
  - Parallel workers voi Tor proxy sharding
  - Re-solve Cloudflare khi bi block

Browser chi dung 1 lan per proxy cho Cloudflare. Con lai curl_cffi het.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from curl_cffi import Session
from curl_cffi.requests.exceptions import RequestException
from patchright.async_api import async_playwright
from playwright_captcha import CaptchaType, ClickSolver, FrameworkType

from paths import CORE_DIR, INPUT_DIR, INVITES_FILE, SEEN_SERVERS_FILE
from utils import clear_screen, format_elapsed

# ---------------------------------------------------------------------------
# ScraperStats + logging
# ---------------------------------------------------------------------------

@dataclass
class ScraperStats:
    codes_found: int = 0
    failed: int = 0
    rate_limits: int = 0
    tags_done: int = 0
    tags_total: int = 0
    workers: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def cpm(self) -> float:
        return (self.codes_found / max(self.elapsed, 1)) * 60


_recent_logs: deque[str] = deque(maxlen=10)
_LOGS_START_LINE = 3  # stats=line1, separator=line2, logs=line3


def render_separator() -> None:
    sys.stdout.write("\033[2;1H\033[2K")
    sys.stdout.write("─" * 72 + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    _recent_logs.append(msg)
    sys.stdout.write(f"\033[{_LOGS_START_LINE}H\033[J")
    for line in list(_recent_logs)[-10:]:
        sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def update_stats_line(stats: ScraperStats) -> None:
    sys.stdout.write("\033[1;1H\033[2K")
    line = (
        f"Codes: {stats.codes_found} | Failed: {stats.failed} | "
        f"Rate: {stats.rate_limits} | Tags: {stats.tags_done}/{stats.tags_total} | "
        f"W: {stats.workers} | {stats.cpm:.0f} CPM | {format_elapsed(stats.elapsed)}"
    )
    sys.stdout.write(line)
    sys.stdout.flush()

DEFAULT_TAGS = [
    "gaming", "music", "programming", "chill", "anime",
    "meme", "art", "crypto", "minecraft", "valorant",
    "fortnite", "roblox", "technology", "study", "chill-vibes"
]

SAFETY_PAGE_LIMIT = 200
DISBOARD_URL = "https://disboard.org"


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _extract_code(text: str) -> str | None:
    """Extract invite code tu Discord invite URL."""
    match = re.search(
        r"(?:discord\.gg|discord\.com/invite)/([A-Za-z0-9\-]+)", text,
    )
    return match.group(1) if match else None


def _extract_csrf(html: str) -> str | None:
    """Extract CSRF token tu HTML page."""
    match = re.search(r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)', html)
    if not match:
        match = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']_csrf', html)
    return match.group(1) if match else None


def _is_cloudflare_blocked(status_code: int, text: str) -> bool:
    """Kiem tra response bi Cloudflare block."""
    if status_code in (403, 503):
        return True
    text_lower = text[:2000].lower()
    if "just a moment" in text_lower or "cf-turnstile" in text_lower:
        return True
    return False


def _parse_listing_page(html: str) -> list[tuple[str, str]]:
    """Parse listing page, extract (server_id, join_url) pairs."""
    servers = []
    matches = re.findall(r'href="(/server/join/\d+)"[^>]*data-id="(\d+)"', html)
    servers.extend([(sid, url) for url, sid in matches])

    seen = set()
    return [(sid, url) for sid, url in servers if sid not in seen and not seen.add(sid)]


# ---------------------------------------------------------------------------
# File operations - codes + seen server IDs
# ---------------------------------------------------------------------------

def _save_code_to_file(code: str) -> None:
    """Luu 1 code vao file ngay lap tuc (append mode)."""
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    with INVITES_FILE.open("a", encoding="utf-8") as f:
        f.write(code + "\n")


def load_codes() -> list[str]:
    """Doc codes tu invites.txt (1 code/dong)."""
    if not INVITES_FILE.exists():
        return []
    try:
        return [line.strip() for line in INVITES_FILE.read_text("utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def load_seen_server_ids() -> set[str]:
    """Doc server IDs da scrape tu seen_servers.txt (1 ID/dong)."""
    if not SEEN_SERVERS_FILE.exists():
        return set()
    try:
        return {line.strip() for line in SEEN_SERVERS_FILE.read_text("utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def save_seen_server_ids(seen: set[str]) -> None:
    """Luu server IDs da scrape vao file (1 ID/dong)."""
    CORE_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_SERVERS_FILE.write_text(
        "\n".join(sorted(seen)) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Cloudflare solving (patchright)
# ---------------------------------------------------------------------------

async def _solve_cloudflare(proxy: str | None = None) -> tuple[str, str]:
    """
    Solve Cloudflare Turnstile.
    Args:
        proxy: Optional HTTP/SOCKS5 proxy (e.g. "http://127.0.0.1:9150")
    Returns: (cf_clearance, user_agent)
    """
    logging.getLogger("playwright_captcha").setLevel(logging.CRITICAL)
    proxy_label = proxy or "direct"
    log(f"Solving Cloudflare via {proxy_label}...")

    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        launch_opts = {"channel": "chrome", "headless": False}
        if proxy:
            launch_opts["proxy"] = {"server": proxy}
        browser = await pw.chromium.launch(**launch_opts)

        context = await browser.new_context()
        page = await context.new_page()

        # Solver phai duoc tao TRUOC khi navigate (theo docs playwright-captcha)
        async with ClickSolver(framework=FrameworkType.PATCHRIGHT, page=page) as solver:
            await page.goto(DISBOARD_URL, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            try:
                await solver.solve_captcha(
                    captcha_container=page,
                    captcha_type=CaptchaType.CLOUDFLARE_TURNSTILE,
                )
            except Exception as e:
                log(f"Solver error: {e!r}")

            await asyncio.sleep(3)

        title = (await page.title()).lower()
        if "just a moment" in title:
            raise RuntimeError("Still on Cloudflare page after solve")

        cookies = await context.cookies()
        cf_clearance = next(
            (c["value"] for c in cookies if c["name"] == "cf_clearance"), ""
        )
        if not cf_clearance:
            raise RuntimeError("cf_clearance cookie not found")

        user_agent = await page.evaluate("navigator.userAgent")
        log(f"Cloudflare solved via {proxy_label}")

        return cf_clearance, user_agent

    finally:
        if browser:
            await browser.close()
        if pw:
            await pw.stop()


# ---------------------------------------------------------------------------
# curl_cffi session management
# ---------------------------------------------------------------------------

def _create_session(
    cf_clearance: str,
    user_agent: str,
    proxy: str | None = None,
) -> Session:
    """
    Tao curl_cffi session voi cf_clearance cookie.
    Args:
        proxy: Optional HTTP/SOCKS5 proxy (e.g. "http://127.0.0.1:9150")
    """
    session = Session(impersonate="chrome")

    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    session.cookies.update({"cf_clearance": cf_clearance})
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": DISBOARD_URL,
    })
    return session


# ---------------------------------------------------------------------------
# Join flow qua browser (Disboard yeu cau Turnstile tren /server/join/{id})
# ---------------------------------------------------------------------------

class JoinBrowser:
    """Drive trang /server/join/{id}: trang tu giai Turnstile roi tu POST
    get-invite va redirect ve discord.gg/{code}. Ta chan navigation Discord
    va bat URL de lay code.

    Chong "blocked": click widget Turnstile khi no khong tu giai, retry 2 lan,
    va xoay browser context khi bi CF danh dau."""

    CONTEXT_ROTATE_AFTER = 25
    BLOCKED_ROTATE_AFTER = 3

    def __init__(self, cf_clearance: str, user_agent: str, max_concurrency: int = 2):
        self.cf_clearance = cf_clearance
        self.user_agent = user_agent
        self.semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._pw = None
        self._browser = None
        self._context = None
        self._context_lock = asyncio.Lock()
        self._resolved_count = 0
        self._blocked_streak = 0
        self._active = 0
        self._retired: list = []

    async def _new_context(self):
        context = await self._browser.new_context()
        if self.cf_clearance:
            try:
                await context.add_cookies(
                    [
                        {
                            "name": "cf_clearance",
                            "value": self.cf_clearance,
                            "domain": ".disboard.org",
                            "path": "/",
                        }
                    ]
                )
            except Exception:
                pass
        await context.route(
            re.compile(r"https://discord\.(gg|com)/.*"),
            lambda route: asyncio.ensure_future(route.abort()),
        )
        return context

    async def _ensure_started(self) -> None:
        if self._browser is not None:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(channel="chrome", headless=False)
        self._context = await self._new_context()

    async def _rotate_context(self) -> None:
        async with self._context_lock:
            old_context = self._context
            self._context = await self._new_context()
            if old_context is not None:
                self._retired.append(old_context)
        await self._cleanup_retired()

    async def _cleanup_retired(self) -> None:
        """Chi dong context cu khi khong con resolve nao dang chay (tranh race)."""
        if self._active > 0 or not self._retired:
            return
        async with self._context_lock:
            if self._active > 0:
                return
            retired, self._retired = self._retired, []
        for context in retired:
            try:
                await context.close()
            except Exception:
                pass

    async def _attempt(
        self, page, solver, join_url: str, captured: list[str], guild_id: str
    ) -> tuple[str | None, str]:
        await page.goto(join_url, wait_until="domcontentloaded", timeout=30000)
        clicked = 0
        has_widget = False
        title = ""
        for tick in range(50):
            if captured:
                return _extract_code(captured[0]), "ok"
            title = (await page.title()).lower()
            if "just a moment" in title:
                try:
                    await solver.solve_captcha(
                        captcha_container=page,
                        captcha_type=CaptchaType.CLOUDFLARE_TURNSTILE,
                    )
                except Exception:
                    pass
                await asyncio.sleep(3)
                try:
                    await page.goto(
                        join_url, wait_until="domcontentloaded", timeout=30000
                    )
                except Exception:
                    pass
                continue
            try:
                error_text = await page.evaluate(
                    "() => { const e = document.getElementById('join-error');"
                    " return e && !e.hidden ? e.textContent.trim() : null; }"
                )
            except Exception:
                error_text = None
            if error_text:
                return None, "no_invite"
            # J1: widget Turnstile khong tu giai -> click (toi da 2 lan)
            if tick >= 12 and clicked < 2:
                try:
                    has_widget = await page.evaluate(
                        "() => !!document.querySelector('#join-verification iframe')"
                        " || !!document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]')"
                    )
                except Exception:
                    has_widget = False
                if has_widget:
                    clicked += 1
                    try:
                        await solver.solve_captcha(
                            captcha_container=page,
                            captcha_type=CaptchaType.CLOUDFLARE_TURNSTILE,
                        )
                    except Exception:
                        pass
            await asyncio.sleep(0.5)
        log(
            f"[JOIN] {guild_id} timeout title={title!r} "
            f"widget={has_widget} clicked={clicked}"
        )
        return None, "blocked"

    async def resolve(self, guild_id: str) -> tuple[str | None, str]:
        """Tra (code, status) — status: ok | no_invite | blocked."""
        code, status = None, "blocked"
        async with self.semaphore:
            self._active += 1
            try:
                await self._ensure_started()
                join_url = f"{DISBOARD_URL}/server/join/{guild_id}"
                for attempt in range(2):
                    page = await self._context.new_page()
                    captured: list[str] = []

                    def on_request(request) -> None:
                        url = request.url
                        if "discord.gg" in url or "discord.com/invite" in url:
                            captured.append(url)

                    page.on("request", on_request)
                    try:
                        async with ClickSolver(
                            framework=FrameworkType.PATCHRIGHT, page=page
                        ) as solver:
                            code, status = await self._attempt(
                                page, solver, join_url, captured, guild_id
                            )
                    except Exception:
                        code, status = None, "blocked"
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass
                    if code or status == "no_invite":
                        break
                    if attempt == 0:
                        await asyncio.sleep(1.5)

                if code:
                    self._resolved_count += 1
                    self._blocked_streak = 0
                    if self._resolved_count % self.CONTEXT_ROTATE_AFTER == 0:
                        await self._rotate_context()
                elif status == "blocked":
                    self._blocked_streak += 1
                    log(f"[JOIN] {guild_id} blocked (streak={self._blocked_streak})")
                    if self._blocked_streak >= self.BLOCKED_ROTATE_AFTER:
                        self._blocked_streak = 0
                        await self._rotate_context()
                        log("[JOIN] rotated browser context sau nhieu lan blocked")
            finally:
                self._active -= 1
        await self._cleanup_retired()
        return code, status

    async def close(self) -> None:
        retired, self._retired = self._retired, []
        for context in retired:
            try:
                await context.close()
            except Exception:
                pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None
        self._context = None


# ---------------------------------------------------------------------------
# Listing fetching (sync + async wrapper)
# ---------------------------------------------------------------------------

def _fetch_listing(
    session: Session, tag: str, page_num: int,
) -> tuple[str | None, list[tuple[str, str]], int]:
    """
    Fetch listing page.
    Returns: (csrf_token, servers, status_code)
    csrf_token None = Cloudflare blocked.
    """
    url = f"{DISBOARD_URL}/servers/tag/{tag}/{page_num}?sort=-member_count"
    try:
        resp = session.get(url, timeout=(10, 30))
    except RequestException as e:
        log(f"Listing network error ({tag} p{page_num}): {e!r}")
        return None, [], 0

    if _is_cloudflare_blocked(resp.status_code, resp.text):
        return None, [], resp.status_code

    csrf = _extract_csrf(resp.text)
    servers = _parse_listing_page(resp.text)
    return csrf, servers, resp.status_code


# ---------------------------------------------------------------------------
# Tag discovery
# ---------------------------------------------------------------------------

def _fetch_all_tags_from_session(session: Session) -> list[str]:
    """Fetch all available tags tu Disboard homepage + listing pages."""
    all_tags: set[str] = set()

    try:
        # Try homepage first - usually has popular tags
        for url_path in ["/", "/servers/"]:
            try:
                resp = session.get(f"{DISBOARD_URL}{url_path}", timeout=(10, 30))
                if _is_cloudflare_blocked(resp.status_code, resp.text):
                    continue

                # Parse tag links: /servers/tag/{tag-name}
                tags = re.findall(r'/servers/tag/([a-z0-9\-]+)', resp.text)
                all_tags.update(tags)

                if len(all_tags) >= 10:
                    break
            except Exception:
                continue

        if all_tags:
            log(f"Discovered {len(all_tags)} tags")
            return sorted(all_tags)

        log("No tags found, using defaults")
        return DEFAULT_TAGS

    except Exception:
        log("Tag discovery failed, using defaults")
        return DEFAULT_TAGS


# ---------------------------------------------------------------------------
# Single worker - scrape batch of tags voi 1 proxy
# ---------------------------------------------------------------------------

async def _scrape_worker(
    worker_id: int,
    tags: list[str],
    proxy: str | None,
    seen: set[str],
    lock: asyncio.Lock,
    stop_event: asyncio.Event,
    stats: ScraperStats,
    pre_solved_session: Session | None = None,
    browser_solve_semaphore: asyncio.Semaphore | None = None,
    target: int = 0,
    safety_limit: int = 200,
    join_browser: "JoinBrowser | None" = None,
) -> list[str]:
    """
    Worker function - scrape batch of tags voi 1 proxy.
    Each worker:
      - Solve Cloudflare cho proxy rieng
      - Scrape assigned tags voi dynamic pagination
      - Dedup servers qua shared seen set
    """
    codes: list[str] = []
    proxy_label = proxy or "direct"
    w = f"W{worker_id}"

    try:
        # Use pre-solved session or solve Cloudflare for this worker's proxy
        if pre_solved_session is not None:
            session = pre_solved_session
            log(f"{w} Ready ({proxy_label}, pre-solved)")
        else:
            if browser_solve_semaphore:
                async with browser_solve_semaphore:
                    log(f"{w} Waiting for browser slot...")
                    cf_clearance, user_agent = await _solve_cloudflare(proxy=proxy)
            else:
                cf_clearance, user_agent = await _solve_cloudflare(proxy=proxy)
            session = _create_session(cf_clearance, user_agent, proxy=proxy)

            # Verify session
            resp = session.get(DISBOARD_URL, timeout=(10, 30))
            if _is_cloudflare_blocked(resp.status_code, resp.text):
                log(f"{w} Session blocked, skipping")
                return codes

            log(f"{w} Ready ({proxy_label})")

        for tag in tags:
            if stop_event.is_set():
                break

            log(f"{w} Tag: {tag}")
            pg = 1
            network_retries = 0
            rate_limit_hits = 0

            while pg <= safety_limit:
                if stop_event.is_set():
                    break

                # Fetch page
                page_csrf, servers, status = await asyncio.to_thread(
                    _fetch_listing, session, tag, pg
                )

                # Network error -> retry page
                if status == 0:
                    network_retries += 1
                    if network_retries > 2:
                        log(f"{w} Network errors, skip tag")
                        break
                    log(f"{w} Network error page {pg}, retry...")
                    await asyncio.sleep(5)
                    continue

                # 429 Rate limited -> wait + retry (check TRUOC Cloudflare)
                if status == 429:
                    rate_limit_hits += 1
                    if rate_limit_hits > 3:
                        log(f"{w} Rate limited too many times, skip tag")
                        break
                    wait = random.uniform(30, 60)
                    async with lock:
                        stats.rate_limits += 1
                    log(f"{w} Rate limited page {pg}, waiting {wait:.0f}s...")
                    await asyncio.sleep(wait)
                    continue

                # Fetch thanh cong -> reset counters
                network_retries = 0
                rate_limit_hits = 0

                # Cloudflare blocked -> re-solve on same proxy
                if page_csrf is None:
                    log(f"{w} Cloudflare expired, re-solving...")
                    try:
                        if browser_solve_semaphore:
                            async with browser_solve_semaphore:
                                cf_clearance, user_agent = await _solve_cloudflare(proxy=proxy)
                        else:
                            cf_clearance, user_agent = await _solve_cloudflare(proxy=proxy)
                        session = _create_session(cf_clearance, user_agent, proxy=proxy)
                        page_csrf, servers, status = await asyncio.to_thread(
                            _fetch_listing, session, tag, pg
                        )
                        if page_csrf is None:
                            log(f"{w} Re-solve failed, skip tag")
                            break
                    except Exception as e:
                        log(f"{w} Re-solve error: {e!r}")
                        break

                # Update CSRF
                if page_csrf:
                    csrf = page_csrf
                else:
                    log(f"{w} No CSRF, skip tag")
                    break

                if not servers:
                    log(f"{w} No servers on page {pg}")
                    break

                # Filter out already-seen servers
                async with lock:
                    new_servers = [
                        (sid, url) for sid, url in servers if sid not in seen
                    ]

                # Smart page check: if too many servers are duplicates, skip tag
                duplicate_ratio = 1 - (len(new_servers) / len(servers)) if servers else 0
                if duplicate_ratio > 0.8 and pg > 1:
                    log(f"{w} {duplicate_ratio:.0%} seen on page {pg}, skip tag")
                    break

                if not new_servers:
                    log(f"{w} All {len(servers)} seen on page {pg}")
                    break

                log(f"{w} Page {pg}: {len(new_servers)} new/{len(servers)}")

                # Resolve invite codes for new servers (browser join flow)
                new_codes_on_page = 0
                for sid, url in new_servers:
                    if stop_event.is_set():
                        break

                    if join_browser is not None:
                        code, join_status = await join_browser.resolve(sid)
                    else:
                        code, join_status = None, "blocked"
                    if code:
                        async with lock:
                            seen.add(sid)
                            _save_code_to_file(code)
                            stats.codes_found += 1
                        codes.append(code)
                        new_codes_on_page += 1
                        log(f"{w} +{code}")
                        update_stats_line(stats)
                        if target and stats.codes_found >= target:
                            stop_event.set()
                    else:
                        async with lock:
                            seen.add(sid)
                            stats.failed += 1
                        log(f"{w} !No code for {sid} ({join_status})")
                        update_stats_line(stats)
                    await asyncio.sleep(random.uniform(1.0, 2.0))

                # Batch persist 1 lan / page (tranh ghi file moi server)
                async with lock:
                    save_seen_server_ids(seen)

                log(f"{w} Page {pg} done | {len(codes)} codes")

                # Stop if no new codes found (all servers exhausted)
                if new_codes_on_page == 0:
                    log(f"{w} No new codes, next tag")
                    break

                pg += 1
                await asyncio.sleep(random.uniform(5, 10))

            # Cool down between tags
            async with lock:
                stats.tags_done += 1
            update_stats_line(stats)
            log(f"{w} Cooling down...")
            await asyncio.sleep(random.uniform(15, 25))

    except asyncio.CancelledError:
        log(f"{w} Cancelled")
    except Exception as e:
        log(f"{w} Error: {e!r}")

    return codes


# ---------------------------------------------------------------------------
# Main async scrape flow - parallel workers voi tag sharding
# ---------------------------------------------------------------------------

async def _async_scrape(
    tags: list[str],
    concurrency: int,
    tor_proxies: list[str] | None = None,
    safety_limit: int = SAFETY_PAGE_LIMIT,
    max_browser_solves: int = 2,
    target: int = 0,
) -> list[str]:
    """
    Main async scrape flow.
    - Auto-discover tags (if not provided)
    - Shuffle tags randomly
    - Shard tags into N workers (N = concurrency)
    - Each worker solve Cloudflare voi proxy rieng
    - Parallel scraping voi shared dedup
    """
    all_codes: list[str] = []
    seen: set[str] = load_seen_server_ids()
    lock = asyncio.Lock()
    stop_event = asyncio.Event()
    browser_solve_semaphore = asyncio.Semaphore(max_browser_solves)
    stats = ScraperStats()

    # Phase 1: Solve Cloudflare 1 lan de fetch tags (neu chua co tags)
    # Save session cho worker dau tien (tranh solve 2 lan)
    # Retry voi nhieu proxy neu Tor solve that bai
    pre_solved_session: Session | None = None
    pre_solved_proxy: str | None = None
    pre_solved_clearance = ""
    pre_solved_ua = ""
    if not tags:
        proxies_to_try = list(tor_proxies) if tor_proxies else [None]
        for attempt_proxy in proxies_to_try:
            try:
                cf_clearance, user_agent = await _solve_cloudflare(proxy=attempt_proxy)
                pre_solved_clearance, pre_solved_ua = cf_clearance, user_agent
                pre_solved_session = _create_session(cf_clearance, user_agent, proxy=attempt_proxy)
                pre_solved_proxy = attempt_proxy
                tags = _fetch_all_tags_from_session(pre_solved_session)
                break
            except Exception:
                log(f"Cloudflare solve failed via {attempt_proxy}, trying next...")
                continue

        # If all proxies failed, try direct
        if not tags and None not in proxies_to_try:
            try:
                cf_clearance, user_agent = await _solve_cloudflare(proxy=None)
                pre_solved_clearance, pre_solved_ua = cf_clearance, user_agent
                pre_solved_session = _create_session(cf_clearance, user_agent, proxy=None)
                pre_solved_proxy = None
                tags = _fetch_all_tags_from_session(pre_solved_session)
            except Exception:
                log("All Cloudflare solves failed, using defaults")
                tags = DEFAULT_TAGS

    log(f"Total tags: {len(tags)}")

    # Phase 2: Shuffle tags
    random.shuffle(tags)

    # Phase 3: Shard tags into workers
    actual_workers = min(concurrency, len(tags), len(tor_proxies) if tor_proxies else concurrency)
    actual_workers = max(actual_workers, 1)

    tag_batches: list[list[str]] = [[] for _ in range(actual_workers)]
    for i, tag in enumerate(tags):
        tag_batches[i % actual_workers].append(tag)

    # Phase 4: Assign proxies
    proxies: list[str | None] = []
    if tor_proxies:
        for i in range(actual_workers):
            proxies.append(tor_proxies[i % len(tor_proxies)])
    else:
        proxies = [None] * actual_workers

    # Clear screen and setup display
    clear_screen()
    render_separator()

    # Init stats
    stats.tags_total = len(tags)
    stats.workers = actual_workers

    # Display config
    for i in range(actual_workers):
        proxy_display = proxies[i] or "direct"
        tag_list = ", ".join(tag_batches[i][:3]) + ("..." if len(tag_batches[i]) > 3 else "")
        log(f"W{i} [{proxy_display}] {tag_list} ({len(tag_batches[i])} tags)")

    # Phase 5: Run workers in parallel
    log(f"Starting {actual_workers} workers...")

    join_browser = JoinBrowser(pre_solved_clearance, pre_solved_ua, max_browser_solves)

    tasks = [
        asyncio.create_task(
            _scrape_worker(
                i, tag_batches[i], proxies[i], seen, lock, stop_event,
                stats,
                pre_solved_session=pre_solved_session if i == 0 else None,
                browser_solve_semaphore=browser_solve_semaphore,
                target=target,
                safety_limit=safety_limit,
                join_browser=join_browser,
            ),
            name=f"worker-{i}",
        )
        for i in range(actual_workers)
    ]

    # Stats updater task
    async def stats_updater():
        while not stop_event.is_set():
            update_stats_line(stats)
            await asyncio.sleep(1)

    updater = asyncio.create_task(stats_updater())

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        log("Cancelling workers...")
        stop_event.set()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stop_event.set()
        updater.cancel()
        await asyncio.gather(updater, return_exceptions=True)
        await join_browser.close()

    # Merge results
    for r in results:
        if isinstance(r, list):
            all_codes.extend(r)
        elif isinstance(r, Exception):
            log(f"Worker error: {r!r}")

    # Save seen server IDs
    save_seen_server_ids(seen)
    update_stats_line(stats)
    sys.stdout.write("\n")
    sys.stdout.flush()

    log(f"=== Done ===")
    log(f"Codes: {stats.codes_found} | Failed: {stats.failed} | Rate Limits: {stats.rate_limits}")
    log(f"Tags: {stats.tags_done}/{stats.tags_total} | Time: {format_elapsed(stats.elapsed)}")
    return all_codes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def scrape_disboard(
    tags: list[str] | None = None,
    tor_proxies: list[str] | None = None,
    concurrency: int = 1,
    safety_page_limit: int = SAFETY_PAGE_LIMIT,
    max_browser_solves: int = 2,
    target: int = 0,
) -> list[str]:
    """Public API: Scrape invite codes tu Disboard.org.

    Args:
        tags: List of tags to scrape (None = auto-discover)
        tor_proxies: Optional list of Tor proxy URLs for rotation
        concurrency: Number of parallel workers
        safety_page_limit: Max pages per tag before stopping
        max_browser_solves: Max concurrent browser solves for Cloudflare
        target: Stop scraping once this many codes are found (0 = unlimited)
    """
    return await _async_scrape(
        tags=tags or [],
        concurrency=concurrency,
        tor_proxies=tor_proxies,
        safety_limit=safety_page_limit,
        max_browser_solves=max_browser_solves,
        target=target,
    )


def save_codes(codes: list[str]) -> Path:
    """Luu codes ra invites.txt (1 code/dong), merge voi file cu neu co."""
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    existing: list[str] = []
    if INVITES_FILE.exists():
        try:
            existing = [line.strip() for line in INVITES_FILE.read_text("utf-8").splitlines() if line.strip()]
        except OSError:
            existing = []

    merged = list(dict.fromkeys(existing + codes))
    INVITES_FILE.write_text("\n".join(merged) + "\n", encoding="utf-8")

    log(f"Saved {len(merged)} codes to {INVITES_FILE}")
    return INVITES_FILE


def show_summary(codes: list[str]) -> None:
    """Hien thi tom tat."""
    log(f"Total codes: {len(codes)}")
    log(f"File: {INVITES_FILE}")
