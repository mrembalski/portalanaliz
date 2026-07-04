"""Connectivity probe: verifies config, login, and forum listing.

Usage: python -m portalanaliz.scraper.probe
"""

from __future__ import annotations

import logging

from portalanaliz.core.config import load_settings
from portalanaliz.scraper.tapatalk import TapatalkClient


def _walk_forums(forums: list, depth: int = 0, lines: list[str] | None = None) -> list[str]:
    lines = lines if lines is not None else []
    for f in forums:
        fid = f.get("forum_id")
        name = f.get("forum_name", "?")
        sub_only = f.get("sub_only", False)
        lines.append(f"{'  ' * depth}[{fid}] {name}{' (category)' if sub_only else ''}")
        children = f.get("child") or []
        _walk_forums(children, depth + 1, lines)
    return lines


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    with TapatalkClient(settings) as client:
        config = client.get_config()
        print("--- get_config ---")
        for key in ("version", "api_level", "is_open", "guest_okay"):
            print(f"  {key}: {config.get(key)}")

        login = client.login()
        print("--- login ---")
        print(f"  result: {login.get('result')}")
        print(f"  username: {login.get('username')}")
        print(f"  user_id: {login.get('user_id')}")

        forums = client.get_forum()
        print("--- get_forum ---")
        lines = _walk_forums(forums)
        print(f"  total forums/categories: {len(lines)}")
        for line in lines[:30]:
            print("  " + line)
        if len(lines) > 30:
            print(f"  ... and {len(lines) - 30} more")


if __name__ == "__main__":
    main()
