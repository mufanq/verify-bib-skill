#!/usr/bin/env python3
"""Verify BibTeX entries against Semantic Scholar to detect AI-hallucinated citations.

Mirrors the TrueCite (wispaper.ai) verification approach: for each entry, query
Semantic Scholar for the closest real paper, then compute fuzzy-match scores on
title, authors, and venue. An entry is "verified" when its title matches closely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pybtex.database import parse_file, parse_string
from rapidfuzz import fuzz

S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "title,authors,venue,year,externalIds"
TITLE_THRESHOLD = 0.85
CACHE_TTL_DAYS = 30
MAX_RETRIES = 5


@dataclass
class EntryResult:
    key: str
    input_title: str
    input_authors: str
    input_year: str
    input_venue: str
    verified: bool = False
    title_match: bool = False
    author_match: bool = False
    venue_match: bool = False
    title_score: float = 0.0
    author_score: float = 0.0
    venue_score: float = 0.0
    verified_title: str | None = None
    verified_authors: list[str] = field(default_factory=list)
    verified_venue: str | None = None
    verified_year: str | None = None
    verified_doi: str | None = None
    verified_s2_id: str | None = None
    error: str | None = None


def _cache_path() -> Path:
    d = Path.home() / ".cache" / "verify-bib"
    d.mkdir(parents=True, exist_ok=True)
    return d / "s2_cache.sqlite"


def _open_cache() -> sqlite3.Connection:
    conn = sqlite3.connect(_cache_path())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        "  key TEXT PRIMARY KEY,"
        "  payload TEXT NOT NULL,"
        "  fetched_at INTEGER NOT NULL"
        ")"
    )
    return conn


def _cache_key(title: str) -> str:
    normalized = re.sub(r"\s+", " ", title.strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _cache_get(conn: sqlite3.Connection, key: str) -> dict | None:
    cutoff = int(time.time()) - CACHE_TTL_DAYS * 86400
    row = conn.execute(
        "SELECT payload FROM cache WHERE key = ? AND fetched_at > ?", (key, cutoff)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _cache_put(conn: sqlite3.Connection, key: str, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cache (key, payload, fetched_at) VALUES (?, ?, ?)",
        (key, json.dumps(payload), int(time.time())),
    )
    conn.commit()


def _s2_search(client: httpx.Client, title: str, api_key: str | None) -> dict | None:
    headers = {"x-api-key": api_key} if api_key else {}
    url = f"{S2_BASE}/paper/search/match"
    params = {"query": title, "fields": S2_FIELDS}

    for attempt in range(MAX_RETRIES):
        try:
            r = client.get(url, params=params, headers=headers, timeout=20.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("data"):
                    return data["data"][0]
                return None
            if r.status_code == 404:
                return None
            if r.status_code in (429, 503):
                time.sleep(2**attempt)
                continue
            return None
        except httpx.RequestError:
            time.sleep(2**attempt)
    return None


def _fallback_search(
    client: httpx.Client, title: str, api_key: str | None
) -> dict | None:
    headers = {"x-api-key": api_key} if api_key else {}
    url = f"{S2_BASE}/paper/search"
    params = {"query": title, "limit": 1, "fields": S2_FIELDS}

    for attempt in range(MAX_RETRIES):
        try:
            r = client.get(url, params=params, headers=headers, timeout=20.0)
            if r.status_code == 200:
                data = r.json()
                if data.get("data"):
                    return data["data"][0]
                return None
            if r.status_code in (429, 503):
                time.sleep(2**attempt)
                continue
            return None
        except httpx.RequestError:
            time.sleep(2**attempt)
    return None


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _authors_to_lastnames(authors_str: str) -> list[str]:
    parts = re.split(r"\s+and\s+", authors_str.strip(), flags=re.IGNORECASE)
    lastnames = []
    for p in parts:
        p = p.strip().strip(",")
        if not p:
            continue
        if "," in p:
            lastnames.append(p.split(",")[0].strip().lower())
        else:
            lastnames.append(p.split()[-1].strip().lower())
    return lastnames


def _score_authors(input_authors: str, verified_authors: list[str]) -> float:
    if not input_authors or not verified_authors:
        return 0.0
    input_last = set(_authors_to_lastnames(input_authors))
    verified_last = {a.split()[-1].lower() for a in verified_authors if a}
    if not input_last or not verified_last:
        return 0.0
    overlap = input_last & verified_last
    return len(overlap) / max(len(input_last), len(verified_last))


def _score_text(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(_normalize(a), _normalize(b)) / 100.0


def _extract_entry_fields(entry) -> tuple[str, str, str, str]:
    fields_lower = {k.lower(): v for k, v in entry.fields.items()}
    title = fields_lower.get("title", "")
    year = fields_lower.get("year", "")
    venue = fields_lower.get("journal") or fields_lower.get("booktitle") or ""

    persons = entry.persons.get("author", [])
    author_str = " and ".join(
        f"{' '.join(p.last_names)}, {' '.join(p.first_names)}".strip(", ")
        for p in persons
    )
    return title, author_str, year, venue


def verify_entry(
    client: httpx.Client,
    conn: sqlite3.Connection,
    key: str,
    title: str,
    author: str,
    year: str,
    venue: str,
    api_key: str | None,
) -> EntryResult:
    result = EntryResult(
        key=key,
        input_title=title,
        input_authors=author,
        input_year=year,
        input_venue=venue,
    )

    if not title.strip():
        result.error = "empty title"
        return result

    ck = _cache_key(title)
    match = _cache_get(conn, ck)
    if match is None:
        match = _s2_search(client, title, api_key)
        if match is None:
            match = _fallback_search(client, title, api_key)
        if match is not None:
            _cache_put(conn, ck, match)

    if not match:
        result.error = "not found in Semantic Scholar"
        return result

    vt = match.get("title") or ""
    va = [a.get("name", "") for a in (match.get("authors") or [])]
    vv = match.get("venue") or ""
    vy = str(match.get("year") or "")
    vd = (match.get("externalIds") or {}).get("DOI")
    vs = match.get("paperId")

    result.verified_title = vt
    result.verified_authors = va
    result.verified_venue = vv
    result.verified_year = vy
    result.verified_doi = vd
    result.verified_s2_id = vs

    result.title_score = _score_text(title, vt)
    result.author_score = _score_authors(author, va)
    result.venue_score = _score_text(venue, vv) if venue else 0.0

    result.title_match = result.title_score >= TITLE_THRESHOLD
    result.author_match = result.author_score >= 0.5
    result.venue_match = result.venue_score >= 0.6
    result.verified = result.title_match
    return result


def verify_bibtex(
    bib_path: str | None = None,
    bib_text: str | None = None,
    api_key: str | None = None,
    sleep_between: float = 0.0,
) -> list[EntryResult]:
    if bib_path:
        bib_data = parse_file(bib_path)
    elif bib_text:
        bib_data = parse_string(bib_text, bib_format="bibtex")
    else:
        raise ValueError("must pass bib_path or bib_text")

    api_key = api_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if not sleep_between:
        sleep_between = 0.2 if not api_key else 1.05

    results: list[EntryResult] = []
    with httpx.Client() as client, _open_cache() as conn:
        for key, entry in bib_data.entries.items():
            if entry.type.lower() in {"comment", "preamble", "string"}:
                continue
            title, author, year, venue = _extract_entry_fields(entry)
            r = verify_entry(
                client, conn, key, title, author, year, venue, api_key
            )
            results.append(r)
            time.sleep(sleep_between)
    return results


def _print_report(results: list[EntryResult], as_json: bool) -> None:
    if as_json:
        print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
        return

    ok = sum(1 for r in results if r.verified)
    missing = sum(1 for r in results if r.error and "not found" in r.error)
    mismatched = len(results) - ok - missing

    print(f"\n📚 Verified {len(results)} entries\n")
    print(f"  ✅ Verified:     {ok}")
    print(f"  ⚠️  Mismatched:   {mismatched}")
    print(f"  ❌ Not found:    {missing}\n")

    for r in results:
        if r.verified and r.author_match and (not r.input_venue or r.venue_match):
            continue
        status = "✅" if r.verified else ("❌" if r.error else "⚠️")
        print(f"{status} {r.key}")
        print(f"   Title : {r.input_title}")
        if r.error:
            print(f"   Error : {r.error}")
        else:
            print(
                f"   → S2  : {r.verified_title}  "
                f"[t={r.title_score:.2f} a={r.author_score:.2f} v={r.venue_score:.2f}]"
            )
            if not r.author_match:
                print(f"   Authors mismatch: input={r.input_authors!r}")
                print(f"                     S2   ={r.verified_authors}")
            if r.input_venue and not r.venue_match:
                print(
                    f"   Venue mismatch  : input={r.input_venue!r}  S2={r.verified_venue!r}"
                )
        print()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Verify BibTeX entries against Semantic Scholar."
    )
    p.add_argument("bib_file", help="Path to .bib file")
    p.add_argument("--json", action="store_true", help="Output JSON instead of report")
    p.add_argument(
        "--api-key",
        help="Semantic Scholar API key (or set SEMANTIC_SCHOLAR_API_KEY env var)",
    )
    args = p.parse_args()

    try:
        results = verify_bibtex(bib_path=args.bib_file, api_key=args.api_key)
    except FileNotFoundError:
        print(f"Error: {args.bib_file} not found", file=sys.stderr)
        return 2

    _print_report(results, as_json=args.json)

    has_problem = any(not r.verified for r in results)
    return 1 if has_problem else 0


if __name__ == "__main__":
    sys.exit(main())
