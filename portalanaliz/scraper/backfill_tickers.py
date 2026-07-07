"""Backfill topics.ticker_hint for titles that are plain company names.

Fetches the GPW listed-companies table (name + 3-char code), normalizes names,
and matches topic titles that bracket-based extraction in sync.py missed.

Usage:
    python -m portalanaliz.scraper.backfill_tickers            # dry run
    python -m portalanaliz.scraper.backfill_tickers --apply
"""

from __future__ import annotations

import argparse
import re
import subprocess
import unicodedata

from sqlalchemy import select

from portalanaliz.core.db import get_session, init_db
from portalanaliz.core.models import Topic

LIST_URLS = [
    # main market
    "https://www.gpw.pl/ajaxindex.php"
    "?action=GPWQuotations&start=showTable&tab=all&lang=PL",
    # NewConnect
    "https://newconnect.pl/ajaxindex.php"
    "?action=NCExternalDataFrontController&start=showTable&tab=all&lang=PL&type=ALL",
]

ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{10}")
CODE_RE = re.compile(r"[A-Z0-9]{3}")

# listing names too generic for prefix matching (match forum-jargon titles)
GENERIC_NAMES = {"ANALIZY", "PARTNER", "EXCELLENCE", "LOKATYBUDOWLANE"}

# title (normalized) -> ticker, for delisted/renamed/colloquial names
ALIASES = {
    "PKOBP": "PKO",
    "ALIORBANKI": "ALR",
    "ORZELBIALY": "OBL",
    "CELONPHARMA": "CLN",
    "MENNICA": "MNC",
    "EUCO": "EUC",
    "TRAKCJA": "TRK",
    "MEX": "MEX",
    "CAPTORTHERAPEUTICS": "CTX",
    "PAMAPOL": "PMP",
    "KRYNICAVITAMIN": "KVT",
    "ELEMENTAL": "EMT",
    "RONSON": "RON",
    "SOHO": "SHD",
    "BAH": "BAH",
    "MASTERPHARM": "MPH",
    "CDA": "CDA",
    "TRITON": "TRI",
    "EFEKT": "EFK",
    "KLABATER": "KBT",
    "AQUATECH": "AQT",
}


def normalize(name: str) -> str:
    """Uppercase, strip diacritics and non-alphanumerics: 'PKO BP' -> 'PKOBP'."""
    name = name.upper().replace("Ł", "L")
    name = unicodedata.normalize("NFKD", name)
    return re.sub(r"[^A-Z0-9]", "", name)


def fetch_gpw_map() -> dict[str, str]:
    """normalized company name -> 3-char GPW code (codes also map to themselves).

    Rows are anchored on the ISIN cell (column layout differs per market):
    name is the cell before it, code the cell after.
    """
    mapping: dict[str, str] = {}
    for url in LIST_URLS:
        # gpw.pl resets plain urllib connections; curl gets through
        html = subprocess.run(
            ["curl", "-s", "--max-time", "30", url],
            capture_output=True, text=True, check=True,
        ).stdout
        for row in re.findall(r"<tr.*?</tr>", html, re.S):
            cells = [
                re.sub(r"<[^>]+>", "", c).strip()
                for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
            ]
            for i, cell in enumerate(cells):
                if ISIN_RE.fullmatch(cell) and 0 < i < len(cells) - 1:
                    name, code = cells[i - 1], cells[i + 1]
                    if name and CODE_RE.fullmatch(code):
                        mapping[normalize(name)] = code
                        mapping.setdefault(code, code)
                    break
    return mapping


def match_title(title: str, mapping: dict[str, str]) -> str | None:
    """Try full title, then the part before a separator ('ROBYG - IPO' -> 'ROBYG')."""
    candidates = [title, re.split(r"\s*[-–/(]\s*", title)[0]]
    for cand in candidates:
        key = normalize(cand)
        if len(key) >= 3 and key in mapping:
            return mapping[key]
    # truncated listing names ('LSISOFT' vs title 'LSI SOFTWARE'): unique
    # prefix match either way; whole short titles only to limit false hits
    key = normalize(title)
    if len(key) >= 5 and len(title.split()) <= 3:
        hits = {
            code for name, code in mapping.items()
            if len(name) >= 5 and name not in GENERIC_NAMES
            and (key.startswith(name) or name.startswith(key))
        }
        if len(hits) == 1:
            return hits.pop()
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write updates to DB")
    args = parser.parse_args()

    mapping = fetch_gpw_map() | ALIASES
    print(f"GPW map: {len(mapping)} keys")

    init_db()
    with get_session() as session:
        topics = session.execute(
            select(Topic).where(Topic.ticker_hint.is_(None))
        ).scalars().all()
        hits = 0
        for topic in topics:
            ticker = match_title(topic.title, mapping)
            if ticker:
                hits += 1
                print(f"{ticker:>4}  {topic.title}")
                if args.apply:
                    topic.ticker_hint = ticker
        if args.apply:
            session.commit()
        print(f"\n{hits}/{len(topics)} matched" + ("" if args.apply else " (dry run)"))


if __name__ == "__main__":
    main()
