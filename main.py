from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import random
import sys
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import aiohttp  # noqa: E402
from aiohttp_socks import ProxyConnector  # noqa: E402
from rich import box  # noqa: E402
from rich.align import Align  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.prompt import Confirm, Prompt  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from paths import (  # noqa: E402
    CHECKED_GUILD_IDS_FILE,
    CONFIG_FILE,
    CORE_DIR,
    INPUT_DIR,
    INVITES_FILE,
    OUTPUT_DIR,
    PROXY_FILE,
    VALID_TXT_FILE,
)
from utils import clear_screen, format_elapsed  # noqa: E402

DISCORD_INVITE_API_TEMPLATE = "https://discord.com/api/{api_version}/invites/{code}?with_counts=true&with_expiration=true"
BRAND_GRADIENT = ("#8B5CF6", "#06B6D4", "#22C55E", "#F59E0B")
STATUS_STYLES = {
    "valid": "bold #22C55E",
    "invalid": "bold #EF4444",
    "error": "bold #F59E0B",
}
STATUS_BADGE_STYLES = {
    "valid": "bold white on #16A34A",
    "invalid": "bold white on #DC2626",
    "error": "bold black on #F59E0B",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "tor": {
        "enabled": False,
        "proxy_amount": 10,
    },
    "scraper": {
        "concurrency": 10,
        "max_browser_solves": 2,
    },
    "scanner": {
        "concurrency": 24,
        "timeout": 10,
        "retries": 3,
        "target_invites": 0,
    },
    "webhook": {
        "url": "",
    },
}

# --- Internal constants (không cần user chỉnh) ---
TOR_START_PORT = 9050
API_VERSION = "v10"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
REQUEST_DELAY = 0.03
JITTER = 0.07
RETRY_BACKOFF = 1.25
CONNECTION_LIMIT_MULTIPLIER = 3
TTL_DNS_CACHE = 900
UI_REFRESH_RATE = 6
UI_RECENT_RESULTS = 12
MAX_SCRAPE_ROUNDS = 3

console = Console()


@dataclass(frozen=True)
class InviteCandidate:
    code: str


@dataclass
class InviteResult:
    code: str
    valid: bool
    checked_at: str
    status: str
    reason: str
    guild_id: str = "N/A"
    http_status: int | None = None
    attempts: int = 1


@dataclass
class ScannerStats:
    checked: int = 0
    valid: int = 0
    invalid: int = 0
    errors: int = 0
    rate_limited: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def elapsed_seconds(self) -> float:
        return max(time.time() - self.started_at, 0.001)

    @property
    def cpm(self) -> float:
        return (self.checked / self.elapsed_seconds) * 60


def deep_merge(default: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(default)
    for key, value in loaded.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as file:
                loaded = json.load(file)
            config = deep_merge(DEFAULT_CONFIG, loaded)
            if config != loaded:
                save_config(config)
            return config
        except (OSError, json.JSONDecodeError) as error:
            console.print(
                f"[yellow]Không đọc được config, dùng config mặc định:[/] {error}"
            )

    save_config(DEFAULT_CONFIG)
    return deepcopy(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(
        json.dumps(config, indent=4, ensure_ascii=False), encoding="utf-8"
    )


def resolve_target(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Ưu tiên --target > prompt > config. Trả int >= 0 và ghi lại vào config."""
    if args.target is not None:
        value = args.target
    elif not args.no_prompt:
        raw = Prompt.ask(
            "Bạn muốn lấy bao nhiêu invite? (0 = không giới hạn)",
            default=str(config["scanner"].get("target_invites", 0)),
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = config["scanner"].get("target_invites", 0)
    else:
        value = config["scanner"].get("target_invites", 0)
    value = max(int(value), 0)
    config["scanner"]["target_invites"] = value
    save_config(config)
    return value


def load_scraped_codes() -> list[str]:
    """Đọc scraped codes từ data/input/invites.txt."""
    from scraper import load_codes
    return load_codes()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_proxy_file() -> None:
    """Tạo proxy.txt mẫu nếu chưa tồn tại."""
    if PROXY_FILE.exists():
        return
    PROXY_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROXY_FILE.write_text(
        "# Mỗi dòng 1 proxy. Hỗ trợ: http://, https://, socks4://, socks5://\n"
        "# Nếu không ghi scheme thì mặc định http://\n"
        "# Có thể kèm auth: http://user:pass@host:port\n"
        "# Dòng bắt đầu bằng # sẽ bị bỏ qua.\n"
        "#\n"
        "# Ví dụ:\n"
        "# http://1.2.3.4:8080\n"
        "# socks5://1.2.3.4:1080\n"
        "# user:pass@1.2.3.4:8080\n",
        encoding="utf-8",
    )


def load_external_proxies() -> list[str]:
    """Đọc proxy.txt, trả về list URL string đã chuẩn hoá."""
    ensure_proxy_file()
    proxies: list[str] = []
    seen: set[str] = set()
    for line in PROXY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Mặc định http:// nếu chưa có scheme.
        if "://" not in line:
            line = f"http://{line}"
        if line not in seen:
            seen.add(line)
            proxies.append(line)
    return proxies


def collect_proxies(config: dict[str, Any], no_tor: bool = False) -> list[str]:
    """Khởi tạo Tor proxies (nếu bật) + proxy.txt, dedup, giữ thứ tự."""
    tor_proxies: list[str] = []
    if not no_tor and config.get("tor", {}).get("enabled", False):
        tor_cfg = config.get("tor", {})
        proxy_amount = max(int(tor_cfg.get("proxy_amount", 10)), 1)
        try:
            from tor_manager import Make_Proxies

            tor_proxies = Make_Proxies(proxy_amount, TOR_START_PORT)
            console.print(f"[bold green][Tor] {len(tor_proxies)} proxies ready![/]")
        except Exception as error:
            console.print(f"[bold red][Tor] Failed to start: {error}[/]")
            console.print("[yellow]Continuing without Tor...[/]")

    external_proxies = load_external_proxies()
    if external_proxies:
        console.print(
            f"[bold cyan][Proxy] Loaded {len(external_proxies)} from proxy.txt[/]"
        )

    seen: set[str] = set()
    result: list[str] = []
    for url in tor_proxies + external_proxies:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def atomic_write_json(path: Path, data: Any, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    if compact:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    else:
        payload = json.dumps(data, indent=4, ensure_ascii=False)
    temp_path.write_text(payload, encoding="utf-8")
    temp_path.replace(path)


def coerce_optional_str(value: object, default: str = "N/A") -> str:
    if value is None:
        return default
    return str(value)


def gradient_text(
    value: str, colors: tuple[str, ...] = BRAND_GRADIENT, style: str = "bold"
) -> Text:
    if not value:
        return Text()

    text = Text()
    color_count = max(len(colors), 1)
    denominator = max(len(value) - 1, 1)
    for index, char in enumerate(value):
        color_index = min(int(index / denominator * (color_count - 1)), color_count - 1)
        char_style = f"{style} {colors[color_index]}".strip()
        text.append(char, style=char_style)
    return text


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)] + "…"


def status_badge(status: str) -> str:
    normalized = status.lower()
    style = STATUS_BADGE_STYLES.get(normalized, STATUS_BADGE_STYLES["error"])
    return f"[{style}] {normalized.upper()} [/]"


class InviteAuditor:
    def __init__(
        self,
        config: dict[str, Any],
        proxy_urls: list[str] | None = None,
        target: int = 0,
    ) -> None:
        self.config: dict[str, Any] = config
        self.proxy_urls: list[str] = proxy_urls or []
        self.proxy_index: int = 0
        self.target: int = max(int(target), 0)
        self.codes: list[str] = []
        self.stats: ScannerStats = ScannerStats()
        self.recent_results: deque[InviteResult] = deque(maxlen=UI_RECENT_RESULTS)
        self.all_results: list[InviteResult] = []
        self.valid_results: list[InviteResult] = []
        self.valid_txt_entries: list[str] = []
        self.seen_guild_ids: set[str] = set()
        self.checked_guild_ids: dict[str, dict] = {}
        self.stop_event: asyncio.Event = asyncio.Event()
        self.lock: asyncio.Lock = asyncio.Lock()
        self.rate_limit_until: float = 0.0
        self.proxy_rate_limits: dict[str, float] = {}

    def load_checked_guild_ids(self) -> None:
        if CHECKED_GUILD_IDS_FILE.exists():
            try:
                with open(CHECKED_GUILD_IDS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Chi giu guild da valid (entry invalid la rac, khong dung de skip)
                self.checked_guild_ids = (
                    {
                        gid: info
                        for gid, info in data.items()
                        if isinstance(info, dict) and info.get("status") == "valid"
                    }
                    if isinstance(data, dict)
                    else {}
                )
            except (json.JSONDecodeError, OSError):
                self.checked_guild_ids = {}

    def _next_proxy(self) -> str | None:
        """Round-robin chọn proxy tiếp theo."""
        if not self.proxy_urls:
            return None
        proxy = self.proxy_urls[self.proxy_index % len(self.proxy_urls)]
        self.proxy_index += 1
        return proxy

    @property
    def scanner(self) -> dict[str, Any]:
        return self.config["scanner"]

    def build_api_url(self, code: str) -> str:
        return DISCORD_INVITE_API_TEMPLATE.format(
            api_version=API_VERSION, code=quote(code, safe="")
        )

    def build_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Language": ACCEPT_LANGUAGE,
            "User-Agent": USER_AGENT,
        }

    async def _create_session(self, proxy_url: str | None = None) -> aiohttp.ClientSession:
        """Tạo aiohttp session, có thể qua HTTP proxy (Tor tunnel)."""
        timeout_val = float(self.scanner.get("timeout", 10))
        timeout = aiohttp.ClientTimeout(
            total=timeout_val,
            connect=min(timeout_val, 15.0),
        )
        if proxy_url:
            connector = ProxyConnector.from_url(proxy_url, rdns=True)
        else:
            concurrency = max(int(self.scanner.get("concurrency", 24)), 1)
            connection_limit = max(concurrency * CONNECTION_LIMIT_MULTIPLIER, concurrency)
            connector = aiohttp.TCPConnector(
                limit=connection_limit,
                ttl_dns_cache=TTL_DNS_CACHE,
                enable_cleanup_closed=True,
            )
        return aiohttp.ClientSession(
            headers=self.build_headers(), timeout=timeout, connector=connector
        )

    def _quota_reached(self) -> bool:
        return bool(self.target) and self.stats.valid >= self.target

    async def run(self, codes: list[str] | None = None) -> None:
        concurrency = max(int(self.scanner.get("concurrency", 24)), 1)
        refresh_rate = UI_REFRESH_RATE
        self.codes = list(codes or [])
        self.load_checked_guild_ids()

        if codes:
            workers = [
                asyncio.create_task(self.code_worker(i, codes, concurrency))
                for i in range(concurrency)
            ]
        else:
            workers = [
                asyncio.create_task(self.code_worker(i, [], concurrency))
                for i in range(concurrency)
            ]
        try:
            with Live(
                self.render(),
                console=console,
                refresh_per_second=refresh_rate,
                transient=False,
            ) as live:
                while not self.stop_event.is_set():
                    live.update(self.render())
                    if self._quota_reached():
                        self.stop_event.set()
                    await asyncio.sleep(1 / max(refresh_rate, 1))
                    # Check workers done → stop
                    if all(w.done() for w in workers):
                        self.stop_event.set()
        except asyncio.CancelledError:
            pass
        finally:
            self.stop_event.set()
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await self.save_outputs()

    async def code_worker(self, worker_id: int, codes: list[str], concurrency: int) -> None:
        """Worker check codes từ danh sách scraped. Mỗi worker nhận shard riêng."""
        proxy_url = self._next_proxy()
        session = await self._create_session(proxy_url)
        try:
            my_codes = codes[worker_id::concurrency]
            for code in my_codes:
                if self.stop_event.is_set():
                    break

                candidate = InviteCandidate(code=code)
                try:
                    result = await self.check_invite(candidate, session, proxy_url)
                except Exception as error:
                    result = self._make_result(
                        candidate, "error", f"Unexpected worker error: {error}"
                    )

                await self.record_result(result)
                await self.polite_delay()
        except asyncio.CancelledError:
            pass
        finally:
            await session.close()

    async def polite_delay(self) -> None:
        base_delay = REQUEST_DELAY
        jitter = JITTER
        delay = base_delay + random.uniform(0, max(jitter, 0))
        if delay > 0:
            await asyncio.sleep(delay)

    async def wait_for_rate_limit(self, proxy_url: str | None = None) -> None:
        if proxy_url:
            async with self.lock:
                delay = max(self.proxy_rate_limits.get(proxy_url, 0.0) - time.monotonic(), 0.0)
        else:
            async with self.lock:
                delay = max(self.rate_limit_until - time.monotonic(), 0.0)
        if delay > 0:
            await asyncio.sleep(delay + random.uniform(0.01, 0.08))

    async def retry_sleep(self, attempt: int) -> None:
        retry_backoff = RETRY_BACKOFF
        await asyncio.sleep((retry_backoff * attempt) + random.uniform(0.05, 0.25))

    def _make_result(
        self,
        candidate: InviteCandidate,
        status: str,
        reason: str,
        *,
        valid: bool = False,
        http_status: int | None = None,
        attempts: int = 1,
    ) -> InviteResult:
        return InviteResult(
            code=candidate.code,
            valid=valid,
            checked_at=now_iso(),
            status=status,
            reason=reason,
            http_status=http_status,
            attempts=attempts,
        )

    async def check_invite(
        self, candidate: InviteCandidate, session: aiohttp.ClientSession, proxy_url: str | None = None
    ) -> InviteResult:
        max_retries = max(int(self.scanner.get("retries", 3)), 1)
        url = self.build_api_url(candidate.code)
        last_reason = "unknown_error"
        last_status: int | None = None

        for attempt in range(1, max_retries + 1):
            try:
                await self.wait_for_rate_limit(proxy_url)
                async with session.get(url) as response:
                    last_status = response.status
                    if response.status == 200:
                        try:
                            payload = await response.json(content_type=None)
                        except (aiohttp.ClientError, ValueError, TypeError) as error:
                            return self._make_result(
                                candidate,
                                "error",
                                f"Không parse được JSON trả về: {error}",
                                http_status=response.status,
                                attempts=attempt,
                            )
                        if not isinstance(payload, dict):
                            return self._make_result(
                                candidate,
                                "error",
                                "Discord API trả về dữ liệu không đúng định dạng",
                                http_status=response.status,
                                attempts=attempt,
                            )
                        return self.build_valid_result(
                            candidate, payload, attempt, response.status
                        )

                    if response.status in {400, 404}:
                        reason = (
                            "Invite code không hợp lệ"
                            if response.status == 400
                            else "Invite không tồn tại hoặc đã hết hạn"
                        )
                        return self._make_result(
                            candidate,
                            "invalid",
                            reason,
                            http_status=response.status,
                            attempts=attempt,
                        )

                    if response.status == 429:
                        retry_after = await self.extract_retry_after(response)
                        await self.mark_rate_limited(retry_after, proxy_url)
                        last_reason = f"Rate limited, chờ {retry_after:.2f}s"
                        if attempt < max_retries:
                            await asyncio.sleep(retry_after)
                            continue
                        break

                    if 500 <= response.status <= 599 and attempt < max_retries:
                        last_reason = f"Discord API lỗi {response.status}, thử lại"
                        await self.retry_sleep(attempt)
                        continue

                    body = await response.text()
                    return self._make_result(
                        candidate,
                        "error",
                        f"HTTP {response.status}: {body[:120]}",
                        http_status=response.status,
                        attempts=attempt,
                    )

            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_reason = f"Network error: {error}"
                if attempt < max_retries:
                    await self.retry_sleep(attempt)
                    continue

        return self._make_result(
            candidate,
            "error",
            last_reason,
            http_status=last_status,
            attempts=max_retries,
        )

    async def extract_retry_after(self, response: aiohttp.ClientResponse) -> float:
        header_value = response.headers.get("Retry-After")
        if header_value:
            try:
                return min(float(header_value), 120.0)
            except ValueError:
                pass

        try:
            payload = await response.json(content_type=None)
            retry_after = float(payload.get("retry_after", 1.0))
            return min(retry_after, 120.0)
        except (aiohttp.ClientError, ValueError, TypeError, json.JSONDecodeError):
            return 5.0

    def build_valid_result(
        self,
        candidate: InviteCandidate,
        payload: dict[str, Any],
        attempts: int,
        http_status: int,
    ) -> InviteResult:
        guild_raw = payload.get("guild")
        guild = guild_raw if isinstance(guild_raw, dict) else {}
        return InviteResult(
            code=candidate.code,
            valid=True,
            checked_at=now_iso(),
            status="valid",
            reason="Invite hợp lệ",
            guild_id=coerce_optional_str(guild.get("id")),
            http_status=http_status,
            attempts=attempts,
        )

    async def mark_rate_limited(self, retry_after: float, proxy_url: str | None = None) -> None:
        if proxy_url:
            async with self.lock:
                self.stats.rate_limited += 1
                self.proxy_rate_limits[proxy_url] = max(
                    self.proxy_rate_limits.get(proxy_url, 0.0), time.monotonic() + retry_after
                )
        else:
            async with self.lock:
                self.stats.rate_limited += 1
                self.rate_limit_until = max(
                    self.rate_limit_until, time.monotonic() + retry_after
                )

    async def record_result(self, result: InviteResult) -> None:
        async with self.lock:
            self.stats.checked += 1
            self.all_results.append(result)
            self.recent_results.appendleft(result)

            if result.valid:
                self._register_valid(result)
            elif result.status == "invalid":
                self._register_invalid(result)
            else:
                self.stats.errors += 1

            if self._quota_reached():
                self.stop_event.set()

    def _register_valid(self, result: InviteResult) -> None:
        """Ghi nhận invite hợp lệ, dedup theo guild (chỉ chặn guild đã valid)."""
        guild_id = result.guild_id
        has_guild = bool(guild_id) and guild_id != "N/A"
        if has_guild and self.checked_guild_ids.get(guild_id, {}).get("status") == "valid":
            return
        if has_guild and guild_id in self.seen_guild_ids:
            return
        if has_guild:
            self.seen_guild_ids.add(guild_id)
            self.checked_guild_ids[guild_id] = {
                "status": "valid",
                "code": result.code,
                "checked_at": result.checked_at,
            }
        self.stats.valid += 1
        self.valid_results.append(result)
        self.valid_txt_entries.append(result.code)

    def _register_invalid(self, result: InviteResult) -> None:
        self.stats.invalid += 1

    def flush_valid_txt_entries(self) -> None:
        if not self.valid_txt_entries:
            return

        VALID_TXT_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing: set[str] = set()
        if VALID_TXT_FILE.exists():
            try:
                existing = {
                    line.strip()
                    for line in VALID_TXT_FILE.read_text("utf-8").splitlines()
                    if line.strip()
                }
            except OSError:
                existing = set()
        new_codes = [c for c in self.valid_txt_entries if c not in existing]
        if not new_codes:
            return
        with VALID_TXT_FILE.open("a", encoding="utf-8") as file:
            file.write("\n".join(new_codes) + "\n")

    async def save_outputs(self) -> None:
        for directory in (INPUT_DIR, OUTPUT_DIR, CORE_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        self.flush_valid_txt_entries()

        # Save checked guild_ids (cross-run persistence)
        atomic_write_json(
            CHECKED_GUILD_IDS_FILE,
            self.checked_guild_ids,
        )

        # Queue: code lỗi mạng (error) ở lại để retry; code đã check bị tiêu thụ.
        done = {r.code for r in self.all_results if r.status != "error"}
        remaining = [c for c in self.codes if c not in done]
        INVITES_FILE.write_text(
            "\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8"
        )

    def render(self) -> Group:
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(ratio=2)
        grid.add_row(
            self.render_progress_panel(),
            self.render_recent_table(),
        )
        return Group(
            grid,
            self.render_footer(),
        )

    def render_progress_panel(self) -> Table:
        table = Table(
            title=gradient_text("Scan Statistics"),
            expand=True,
            show_header=False,
            border_style="#06B6D4",
            box=box.ROUNDED,
        )
        table.add_column("Metric", style="bold white")
        table.add_column("Value", justify="right")
        
        table.add_row("Checked", f"[bold]{self.stats.checked:,}[/]")
        table.add_row("Valid", f"[#22C55E]{self.stats.valid:,}[/]")
        table.add_row("Invalid", f"[#EF4444]{self.stats.invalid:,}[/]")
        table.add_row("Errors", f"[#F59E0B]{self.stats.errors:,}[/]")
        table.add_row("Rate Limited", f"[#F59E0B]{self.stats.rate_limited:,}[/]")
        table.add_row("Speed", f"[bold #22C55E]{self.stats.cpm:,.0f} CPM[/]")
        table.add_row("Elapsed", f"[dim]{format_elapsed(self.stats.elapsed_seconds)}[/]")
        table.add_row("Proxies", f"[bold #06B6D4]{len(self.proxy_urls)}[/]")
            
        return table



    def render_recent_table(self) -> Table:
        table = Table(
            title=gradient_text("Recent Results"),
            expand=True,
            show_header=True,
            header_style="bold #8B5CF6",
            border_style="#334155",
            box=box.ROUNDED,
        )
        table.add_column("Status", width=12, no_wrap=True)
        table.add_column("Code", no_wrap=True, style="bold #06B6D4")
        table.add_column("HTTP", justify="right", width=6)
        table.add_column("Reason")

        if not self.recent_results:
            table.add_row("-", "-", "-", "Chưa có kết quả")
            return table

        for result in self.recent_results:
            http_status = str(result.http_status) if result.http_status else "-"
            reason_style = STATUS_STYLES.get(result.status, STATUS_STYLES["error"])
            table.add_row(
                status_badge(result.status),
                result.code,
                http_status,
                f"[{reason_style}]{truncate_text(result.reason, 72)}[/]",
            )
        return table

    def render_footer(self) -> Panel:
        footer = Text()
        footer.append("Mode ", style="bold #06B6D4")
        footer.append("Check Invites")
        footer.append("  •  Proxies ", style="bold #8B5CF6")
        footer.append(f"{len(self.proxy_urls)}" if self.proxy_urls else "Direct")
        footer.append("  •  Output ", style="bold #22C55E")
        footer.append("data/output/valid_invites.txt (codes) · data/data-core/*.json")
        footer.append("  •  Ctrl+C để dừng an toàn", style="dim")
        return Panel(Align.center(footer), border_style="#334155", box=box.ROUNDED)


async def send_pipeline_webhook(
    config: dict[str, Any], codes: list[str], valid_count: int, target: int
) -> None:
    """Gửi DUY NHẤT 1 tin nhắn webhook kèm file codes của run hiện tại."""
    webhook_cfg = config.get("webhook", {})
    url = str(webhook_cfg.get("url", "")).strip()
    if not url or not codes:
        return
    payload = {
        "content": f"✅ Thu thập được {valid_count}/{target if target else '∞'} invite hợp lệ"
    }
    body = "\n".join(codes).encode("utf-8")
    data = aiohttp.FormData()
    data.add_field("payload_json", json.dumps(payload), content_type="application/json")
    data.add_field(
        "files[0]", body, filename="valid_invites.txt", content_type="text/plain"
    )
    timeout = aiohttp.ClientTimeout(total=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=data) as response:
                if response.status not in {200, 204}:
                    console.log(f"Webhook tra ve HTTP {response.status}")
    except aiohttp.ClientError as error:
        console.log(f"Webhook loi: {error}")


def render_config_summary(config: dict[str, Any], proxy_count: int = 0) -> None:
    scanner = config["scanner"]
    concurrency = max(int(scanner.get("concurrency", 24)), 1)

    table = Table(
        title=gradient_text("Configuration"),
        show_header=True,
        header_style="bold #06B6D4",
        border_style="#334155",
        box=box.ROUNDED,
    )
    table.add_column("Key", style="bold white")
    table.add_column("Value")

    tor_enabled = bool(config.get("tor", {}).get("enabled", False))
    table.add_row("Mode", "[bold #8B5CF6]Check / Pipeline[/]")
    table.add_row("Tor", "[bold #22C55E]Enabled[/]" if tor_enabled else "[dim]Disabled[/]")
    table.add_row("Tor Proxies", f"[bold #8B5CF6]{proxy_count}[/]" if proxy_count > 0 else "[bold #EF4444]0 (failed to start)[/]" if tor_enabled else "[dim]0[/]")
    table.add_row("Concurrency", str(concurrency))
    table.add_row("Timeout", f"{scanner.get('timeout', 10)}s")
    table.add_row("Retries", str(scanner.get("retries", 3)))
    console.print(table)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discord invite scanner - Scrape from Disboard and check validity.",
        epilog=(
            "Ctrl+C: kết quả local được lưu, webhook chỉ gửi khi run kết thúc bình thường."
        ),
    )
    parser.add_argument(
        "--scrape",
        action="store_true",
        help="Scrape invite codes từ Disboard.org.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check invites từ file data/input/invites.txt.",
    )
    parser.add_argument(
        "--tags",
        nargs="+",
        default=None,
        help="Tags để scrape (mặc định: tự động khám phá từ Disboard).",
    )
    parser.add_argument(
        "--no-tor",
        action="store_true",
        help="Tắt Tor proxies, kết nối trực tiếp.",
    )
    parser.add_argument(
        "--tor-amount",
        type=int,
        help="Số lượng Tor HTTP tunnel proxies.",
    )
    parser.add_argument(
        "--scrape-concurrency", type=int, help="So worker scrape song song (1-50)."
    )
    parser.add_argument(
        "--concurrency", type=int, help="So worker check song song."
    )
    parser.add_argument(
        "--timeout", type=float, help="Timeout mỗi request, tính bằng giây."
    )
    parser.add_argument(
        "--max-retries", type=int, help="Số lần retry khi lỗi tạm thời."
    )
    parser.add_argument(
        "--no-prompt", action="store_true", help="Chạy ngay, không hỏi xác nhận."
    )
    parser.add_argument(
        "--target",
        type=int,
        help="Số invite hợp lệ cần lấy (0 = không giới hạn).",
    )
    return parser.parse_args()


def apply_cli_overrides(config: dict[str, Any], args: argparse.Namespace) -> None:
    scanner = config["scanner"]

    # Tor override
    if args.no_tor:
        config["tor"]["enabled"] = False
    if args.tor_amount is not None:
        config["tor"]["proxy_amount"] = max(args.tor_amount, 1)

    if args.concurrency is not None:
        scanner["concurrency"] = max(args.concurrency, 1)
    if args.timeout is not None:
        scanner["timeout"] = max(args.timeout, 1.0)
    if args.max_retries is not None:
        scanner["retries"] = max(args.max_retries, 1)


async def run_pipeline(config: dict[str, Any], args: argparse.Namespace, target: int) -> None:
    """Mode 3: scrape + check lặp đến khi đủ quota (tối đa MAX_SCRAPE_ROUNDS vòng scrape)."""
    from scraper import save_codes, scrape_disboard, show_summary

    proxy_urls = collect_proxies(config, no_tor=args.no_tor)
    scraper_cfg = config.get("scraper", {})
    max_rounds = MAX_SCRAPE_ROUNDS if target else 1

    console.print(
        Panel.fit(
            Align.center(
                Group(
                    gradient_text("LAKO SCRAPE + CHECK PIPELINE"),
                    Text(
                        f"Target: {target if target else '∞'} invite | "
                        f"Proxies: {len(proxy_urls)} | Max scrape rounds: {max_rounds}",
                        style="dim",
                    ),
                )
            ),
            border_style="#8B5CF6",
            box=box.ROUNDED,
        )
    )
    render_config_summary(config, proxy_count=len(proxy_urls))

    total_valid = 0
    valid_codes: list[str] = []
    scrape_rounds = 0

    while True:
        codes = load_scraped_codes()
        if codes:
            round_target = max(target - total_valid, 0)
            auditor = InviteAuditor(config, proxy_urls=proxy_urls, target=round_target)
            await auditor.run(codes=codes)
            total_valid += auditor.stats.valid
            valid_codes.extend(auditor.valid_txt_entries)
            if target and total_valid >= target:
                break

        if scrape_rounds >= max_rounds:
            break

        remaining_quota = max(target - total_valid, 1) if target else 0
        scrape_concurrency = args.scrape_concurrency or scraper_cfg.get("concurrency", 5)
        actual_concurrency = max(
            min(scrape_concurrency, len(proxy_urls) if proxy_urls else 1), 1
        )
        new_codes = await scrape_disboard(
            tags=args.tags or None,
            tor_proxies=proxy_urls or None,
            concurrency=actual_concurrency,
            safety_page_limit=scraper_cfg.get("safety_page_limit", 200),
            max_browser_solves=scraper_cfg.get("max_browser_solves", 2),
            target=remaining_quota,
        )
        scrape_rounds += 1
        if not new_codes:
            console.print("[yellow]Không tìm thêm được code mới.[/]")
            break
        save_codes(new_codes)
        show_summary(new_codes)

    await send_pipeline_webhook(config, valid_codes, total_valid, target)
    console.print(
        f"\n[bold green]Hoàn tất![/] Đã lấy {total_valid}/{target if target else '∞'} invite hợp lệ."
    )


async def async_main(args: argparse.Namespace) -> int:
    config = load_config()
    target = resolve_target(args, config)  # persist config SẠCH (chỉ target_invites)
    apply_cli_overrides(config, args)  # chỉ in-memory cho phiên này

    # --- INTERACTIVE MODE SELECTION ---
    if not args.scrape and not args.check:
        console.print("")
        console.print("[bold]Chon mode:[/]")
        console.print("  [bold cyan]1[/] - Scrape invites tu Disboard")
        console.print("  [bold cyan]2[/] - Check invites da scrape")
        console.print("  [bold cyan]3[/] - Scrape + Check")
        console.print("")
        choice = Prompt.ask("Chon", choices=["1", "2", "3"], default="1")
        if choice == "1":
            args.scrape = True
        elif choice == "2":
            args.check = True
        else:
            args.scrape = True
            args.check = True

    # --- MODE 3: SCRAPE + CHECK (quota pipeline) ---
    if args.scrape and args.check:
        await run_pipeline(config, args, target)
        return 0

    # --- MODE 1: SCRAPE ---
    if args.scrape:
        from scraper import scrape_disboard, save_codes, show_summary

        scraper_cfg = config.get("scraper", {})
        tags = args.tags or None  # None = auto-discover from Disboard
        scrape_concurrency = args.scrape_concurrency or scraper_cfg.get("concurrency", 5)
        safety_limit = scraper_cfg.get("safety_page_limit", 200)
        max_browser_solves = scraper_cfg.get("max_browser_solves", 2)

        all_proxies = collect_proxies(config, no_tor=args.no_tor)

        # Adjust concurrency based on available proxies
        actual_concurrency = min(scrape_concurrency, len(all_proxies) if all_proxies else 1)
        actual_concurrency = max(actual_concurrency, 1)

        tag_display = "Auto-discover" if not args.tags else ", ".join(tags[:5]) + ("..." if tags and len(tags) > 5 else "")
        console.print(
            Panel.fit(
                Align.center(
                    Group(
                        gradient_text("DISCORD INVITE SCRAPER"),
                        Text(
                            f"Tags: {tag_display} | Workers: {actual_concurrency} | "
                            f"Proxies: {len(all_proxies)}",
                            style="dim",
                        ),
                    )
                ),
                border_style="#8B5CF6",
                box=box.ROUNDED,
            )
        )

        codes = await scrape_disboard(
            tags=tags,
            tor_proxies=all_proxies or None,
            concurrency=actual_concurrency,
            safety_page_limit=safety_limit,
            max_browser_solves=max_browser_solves,
            target=target,
        )
        if codes:
            save_codes(codes)
            show_summary(codes)
        else:
            console.print("[yellow]No codes found.[/]")

        return 0

    # --- MODE 2: CHECK ---
    if args.check:
        codes = load_scraped_codes()
        if not codes:
            console.print("[red]No codes found in data/input/invites.txt[/]")
            console.print("Run: [bold]python main.py --scrape[/] first")
            return 1

        console.print(f"[cyan]Loaded {len(codes)} codes from invites.txt[/]")

        proxy_urls = collect_proxies(config, no_tor=args.no_tor)

        console.print(
            Panel.fit(
                Align.center(
                    Group(
                        gradient_text("LAKO DISCORD INVITE AUDITOR"),
                        Text("Check Mode • Async • Tor Proxies", style="dim"),
                    )
                ),
                border_style="#8B5CF6",
                box=box.ROUNDED,
            )
        )
        render_config_summary(config, proxy_count=len(proxy_urls))

        if not args.no_prompt:
            if not Confirm.ask(f"Bắt đầu check {len(codes)} invites?", default=True):
                return 0
            else:
                clear_screen()

        auditor = InviteAuditor(config, proxy_urls=proxy_urls, target=target)
        await auditor.run(codes=codes)

        console.print("\n[bold green]Hoàn tất![/]")
        console.print(
            f"Checked: {auditor.stats.checked:,} | Valid: {auditor.stats.valid:,} | Invalid: {auditor.stats.invalid:,} | Errors: {auditor.stats.errors:,}"
        )
        if target and auditor.stats.valid < target:
            console.print(
                f"[yellow]Chỉ lấy được {auditor.stats.valid}/{target} invite — chạy --scrape để lấy thêm.[/]"
            )
        await send_pipeline_webhook(
            config, auditor.valid_txt_entries, auditor.stats.valid, target
        )
        console.print(f"Output: [bold]{VALID_TXT_FILE}[/] và [bold]{OUTPUT_DIR}[/]")
        return 0

    return 0


def main() -> int:
    args = parse_args()

    try:
        from tor_manager import Stop_Running_Tor

        atexit.register(Stop_Running_Tor)
    except Exception:
        pass

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        console.print("\n[yellow]Dung boi nguoi dung.[/]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
