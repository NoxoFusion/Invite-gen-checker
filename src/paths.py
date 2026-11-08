"""Đường dẫn dùng chung cho toàn bộ tool — sửa path ở đây, không sửa rải rác."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT_DIR / "config.json"

DATA_DIR = ROOT_DIR / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
CORE_DIR = DATA_DIR / "data-core"
TOR_DIR = DATA_DIR / "assets" / "tor"

PROXY_FILE = INPUT_DIR / "proxy.txt"
INVITES_FILE = INPUT_DIR / "invites.txt"  # hàng đợi code chờ check
VALID_TXT_FILE = OUTPUT_DIR / "valid_invites.txt"
CHECKED_GUILD_IDS_FILE = CORE_DIR / "checked_guild_ids.json"
SEEN_SERVERS_FILE = CORE_DIR / "seen_servers.txt"
TOR_LOG_FILE = TOR_DIR / "tor.log"
