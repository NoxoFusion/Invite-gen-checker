"""Tiện ích dùng chung cho main.py và scraper.py."""

from __future__ import annotations

import os


def format_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")
