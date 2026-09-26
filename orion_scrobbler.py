#!/usr/bin/env python3
"""
DJ Orion playlist scrobbler for Last.fm (pylast 5.2 compatible).

Features:
  - Scrapes djorion.fi biisilistat pages
  - Robust to markup shifts: uses <article> + .entry-content
  - Filters promo/social/link and "other years" navigation lines
  - Parses "Artist – Title" (dash may be -/–/—) and "Artist : Title"
  - Avoids false splits like "Golden Girl (Re – Recorded Version)" by
    requiring spaces around the separator.
  - Scrobbles using dicts for pylast 5.2 (no pylast.Scrobble class)
  - DST-aware timestamps (Europe/Helsinki) across 20:00–22:00
  - Keeps state: remembers done dates to avoid ping-pong
  - Tools: --list, --date DD.MM.YYYY, --dry-run, --debug, --dump-raw, --dump-candidates
"""

import os
import re
import sys
import json
import time
import argparse
import unicodedata
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
import pylast

# ───────────────────────── Settings ─────────────────────────
RADIO_CATEGORY_URL = "http://www.djorion.fi/category/radio/"
STATE_FILE = os.path.expanduser("~/.orion_scrobbler_state.json")

TZ_HELSINKI = ZoneInfo("Europe/Helsinki")
SHOW_START_HM = (20, 0)     # 20:00 local
SHOW_DURATION_H = 2         # 2 hours

MAX_BATCH = 50
MAX_RETRIES = 4
RETRY_WAIT = 5

UA = "orion_scrobbler/2.0"

# dash-like chars: hyphen + unicode dashes
DASH_CHARS = r"[\u002d\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]"  # includes '-' and various dash forms
WSP_CHARS  = r"[\s\u00A0\u202F]"                              # whitespace + NBSP + narrow NBSP

# Requires spaces around separator to avoid splitting inside parentheses text
SEP_RE = re.compile(rf"{WSP_CHARS}+(?:{DASH_CHARS}|:){WSP_CHARS}+")

DATE_DOT_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")  # 14.11.2025
DATE_DASH_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")   # 14-11-2025
DATE_FMT_FI = "%d.%m.%Y"
DATE_FMT_ISO = "%Y-%m-%d"

# Leading list indices
INDEX_RE = re.compile(r"^\s*(?:#?\d{1,3})[.\):]?\s+")

# Trailing notes
CLEAN_RE = re.compile(
    r"""(?ix)
        \s*\(toive:[^)]+\)\s*$     # (toive: Mikko)
      | \s*\[[^\]]+\]\s*$          # [live], [edit], etc.
      | \s*\#[\w\-]+\s*$           # trailing #Hashtag
    """
)

QUOTE_CHARS = " '\"“”‘’‚‛«»‹›„‟"

PROMO_WORDS = {
    "whatsapp", "facebook", "instagram", "spotify", "areena",
    "kanava", "kanavalle", "grouppi", "grouppiin",
    "ylex", "playlist", "megalista", "perjantai", "konemusiikin",
    "http", "https", "bit.ly", "t.co", "discord",
}
PROMO_RE = re.compile("|".join(re.escape(w) for w in PROMO_WORDS), re.I)

# Lines like "DJ Orion @ YleX 13.12.2024 – Biisilistat"
POST_LINK_RE = re.compile(r"DJ\s+Orion\s*@\s*YleX\s+\d{1,2}[.\-]\d{1,2}[.\-]\d{4}.*biisil", re.I)

# Hour headers like "20:00-21:00 – DJ Orion"
HOUR_HEADER_RE = re.compile(rf"^\s*\d{{1,2}}:\d{{2}}\s*{DASH_CHARS}\s*\d{{1,2}}:\d{{2}}(?:\s*{DASH_CHARS}\s*.*)?$")

def is_section_header(s: str) -> bool:
    s = _normalize_line(s).lower()
    return s in {
        "viikon paska saksa",
        "demokratiaraidat",
        "klassikkokähinät",
        "kotimaan katsaus",
    } or s.startswith("tällä viikolla aiempina vuosina")

def is_vs_token(s: str) -> bool:
    s = _normalize_line(s).upper()
    return s in {"VS", "VS.", "V.S.", "V.S", "VS:"}

def clean_piece(s: str) -> str:
    s = _normalize_line(s)
    s = strip_leading_index(s)
    s = CLEAN_RE.sub("", s)
    s = strip_quotes(s)
    return s.strip()


# ───────────────────────── Helpers ─────────────────────────
def _dbg(enabled: bool, *a):
    if enabled:
        print(*a)

def print_numbered(title, lines, limit=None):
    print(title)
    if limit is not None and len(lines) > limit:
        shown = lines[:limit]
        truncated = True
    else:
        shown = lines
        truncated = False
    for i, L in enumerate(shown, 1):
        print(f"{i:3d}: {L}")
    if truncated:
        print(f"... ({len(lines)} total lines, showing first {len(shown)} only)")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            st = json.load(fh)
    else:
        st = {}
    st.setdefault("done_dates", [])
    st["done_dates"] = normalize_done_dates(st.get("done_dates", []))
    return st

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_FILE)

def md5(pw: str) -> str:
    import hashlib
    return hashlib.md5(pw.encode("utf-8")).hexdigest()


def format_fi_date(d: date) -> str:
    return d.strftime(DATE_FMT_FI)


def parse_date_any(s: str):
    s = (s or "").strip()
    for fmt in (DATE_FMT_FI, DATE_FMT_ISO):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def normalize_done_dates(values):
    out = []
    seen = set()
    for raw in values or []:
        d = parse_date_any(str(raw))
        if not d:
            continue
        k = format_fi_date(d)
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out

def _normalize_line(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\u00A0", " ").replace("\u202F", " ")
    s = re.sub(r"[\u200B\u200C\u200D\u2060\uFEFF]", "", s)  # zero-width
    return s.strip()

def strip_leading_index(s: str) -> str:
    return INDEX_RE.sub("", s, count=1)

def strip_quotes(s: str) -> str:
    return (s or "").strip(QUOTE_CHARS).strip()

def is_promo_or_link(s: str) -> bool:
    s = _normalize_line(s).lower()
    if PROMO_RE.search(s):
        return True
    if "://" in s:
        return True
    return False

def looks_like_post_link_or_heading(s: str) -> bool:
    s_norm = _normalize_line(s)
    if POST_LINK_RE.search(s_norm):
        return True
    if "biisilista" in s_norm.lower():
        # catches Biisilista/Biisilistat headings and nav blocks
        return True
    return False

def is_hour_header(s: str) -> bool:
    return bool(HOUR_HEADER_RE.match(_normalize_line(s)))

def looks_like_areena_promo(s: str) -> bool:
    s = _normalize_line(s).lower()
    return ("areena" in s) and ("dj orion" in s)

def last_friday_local(now=None):
    if now is None:
        now = datetime.now(TZ_HELSINKI)
    days_back = (now.weekday() - 4) % 7  # Friday=4
    return (now - timedelta(days=days_back)).date()

DASH_ONLY_RE = re.compile(rf"^(?:{WSP_CHARS})*(?:{DASH_CHARS})(?:{WSP_CHARS})*$", re.UNICODE)

def is_junk_line(s: str) -> bool:
    s = _normalize_line(s)
    if not s:
        return True
    if s.upper() in {"VS", "VS.", "VS:", "V.S.", "V.S"}:
        return True
    # section headers seen in your dump
    if s.lower() in {
        "demokratiaraidat", "klassikkokähinät", "kotimaan katsaus",
        "viikon paska saksa",
    }:
        return True
    return False

def guess_roles(prev: str, nxt: str):
    """
    Decide whether prev/nxt is (artist,title) or (title,artist).
    """
    p = _normalize_line(prev)
    n = _normalize_line(nxt)

    # XmiX numbered titles: "1   Flux Capacitor"
    if re.match(r"^\d{1,3}\s+", p):
        title = re.sub(r"^\d{1,3}\s+", "", p).strip()
        return (n, title)  # artist, title

def artistish(x: str) -> bool:
    x = x.strip()
    return bool(
        re.search(r"(\s&\s|,|\bvs\b|\bvs\.\b)", x, re.I)
        or re.search(r"\b(feat|ft)\.?\b", x, re.I)
        or re.match(r"^\d+\s*\w+", x)          # 4 Strings, 3LAU
        or re.match(r"^(dj|mc)\s+", x, re.I)
        or re.match(r"[A-ZÅÄÖ][a-zåäö]+(?:\s+[A-ZÅÄÖ][a-zåäö]+)+$", x)
    )

def titleish(x: str) -> bool:
    x = x.lower()
    return bool(
        re.search(r"\(|\)|\b(remix|edit|bootleg|vip|mix|rework)\b", x)
        or re.search(r"\b(feat|ft)\.?\b", x)
    )

def tidy_artist_title(artist: str, title: str):
    a = artist.strip()
    t = title.strip()

    # Normalize weird apostrophes/accents
    a = a.replace("’", "'").replace("`", "'").replace("´", "").strip()
    t = t.replace("’", "'").replace("`", "'").replace("´", "").strip()

    # Fix common spacing around '&'
    a = re.sub(r"\s*&\s*", " & ", a)
    a = re.sub(r"\s{2,}", " ", a).strip()

    # Fix "K – System" style split in artist (turn "K – System" into "K-System")
    a = re.sub(r"\bK\s*-\s*System\b", "K-System", a, flags=re.I)
    a = re.sub(r"\bK\s*–\s*System\b", "K-System", a, flags=re.I)

    # Fix "Joonas Hahmo, K – System – Ocean Drive" where K-System got split
    if re.search(r",\s*K$", a, re.I):
        parts = re.split(r"\s*(?:–|—|-)\s*", t, 1)
        if len(parts) == 2 and parts[0].strip().lower() == "system":
            a = re.sub(r",\s*K$", ", K-System", a, flags=re.I)
            t = parts[1].strip()


    # Title casing fix for stray "It’S" etc: only fix "'S" after a letter
    t = re.sub(r"([A-Za-z])'S\b", r"\1's", t)
    t = re.sub(r"([A-Za-z])’S\b", r"\1's", t)

    # Remove dangling combining accents at end
    t = re.sub(r"[\u0300-\u036f]+$", "", t).strip()

    return a, t

def strong_artistish(x: str) -> bool:
    x = x.strip()
    return bool(
        re.search(r"(\s&\s|,|\bvs\b|\bvs\.\b)", x, re.I)
        or re.search(r"\b(feat|ft)\.?\b", x, re.I)
        or re.match(r"^\d+\s*\w+", x)          # 4 Strings, 3LAU
        or re.match(r"^(dj|mc)\s+", x, re.I)
    )

def join_artist_fragments(lines):
    out = []
    i = 0
    while i < len(lines):
        # Merge patterns like: "K" / "–" / "System"  => "K-System"
        if i + 2 < len(lines) and lines[i].strip().lower() == "k" and DASH_ONLY_RE.match(lines[i+1]) and lines[i+2].strip().lower() == "system":
            out.append("K-System")
            i += 3
            continue
        out.append(lines[i])
        i += 1
    return out

def merge_k_system(lines):
    out = []
    i = 0
    while i < len(lines):
        if (
            i + 2 < len(lines)
            and lines[i].strip().lower() == "k"
            and DASH_ONLY_RE.match(lines[i + 1])
            and lines[i + 2].strip().lower() == "system"
        ):
            out.append("K-System")
            i += 3
            continue
        out.append(lines[i])
        i += 1
    return out


# ───────────────────────── Listing posts ─────────────────────────
def collect_posts(debug=False):
    """
    Return list[(date, url)] newest→oldest from /category/radio/.
    Robust to WP block theme changes: does NOT assume h2.entry-title.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
    }

    r = requests.get(RADIO_CATEGORY_URL, timeout=25, headers=headers, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    candidates = []

    # 1) WordPress classic permalinks
    for a in soup.select("a[rel='bookmark'][href]"):
        candidates.append(a)

    # 2) Block theme headings often use h3/h4
    for a in soup.select("h1 a[href], h2 a[href], h3 a[href], h4 a[href]"):
        candidates.append(a)

    # 3) Fallback: any link that looks like the post slug pattern
    for a in soup.select("a[href*='dj-orion-ylex'][href]"):
        candidates.append(a)

    posts = []
    for a in candidates:
        url = a.get("href", "").strip()
        if not url or "djorion.fi" not in url:
            continue

        title = _normalize_line(a.get_text(" ", strip=True))
        low = title.lower()

        # Must be a playlist post
        if "biisilista" not in low:
            continue

        # Extract date from title (dd.mm.yyyy)
        m = DATE_DOT_RE.search(title)
        if m:
            d, mo, y = map(int, m.groups())
            posts.append((date(y, mo, d), url))
            continue

        # Or from url (dd-mm-yyyy)
        m = DATE_DASH_RE.search(url)
        if m:
            d, mo, y = map(int, m.groups())
            posts.append((date(y, mo, d), url))
            continue

    # De-dupe by URL, keep newest date for same URL
    by_url = {}
    for d, u in posts:
        by_url[u] = max(d, by_url.get(u, d))
    posts = [(d, u) for (u, d) in by_url.items()]
    posts.sort(reverse=True)

    _dbg(debug, f"collect_posts(): found {len(posts)} posts (from {len(candidates)} link candidates)")
    return posts

def choose_post(target_date=None, state=None, ignore_state=False, debug=False):
    posts = collect_posts(debug=debug)
    if not posts:
        return None, None

    if target_date:
        for d, url in posts:
            if d == target_date:
                return d, url
        raise RuntimeError(f"No playlist post found for {format_fi_date(target_date)}. Try: ./orion_scrobbler.py --list")

    if ignore_state:
        return posts[0]

    done = set(state.get("done_dates", [])) if state else set()
    for d, url in posts:
        if format_fi_date(d) not in done:
            return d, url

    # Everything already done; return newest
    return posts[0]


# ───────────────────────── Fetching playlist ─────────────────────────
def fetch_playlist(url: str, debug: bool = False):
    """
    Fetch playlist page and return raw lines from the correct article content.
    Uses .entry-content to avoid related-posts / nav blocks.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
    }
    r = requests.get(url, timeout=25, headers=headers, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Remove obvious chrome
    for tag in soup.find_all(["nav", "header", "footer", "aside", "form", "script", "style"]):
        tag.decompose()

    candidates = soup.find_all("article")
    _dbg(debug, f"Found {len(candidates)} <article> tags")

    url_date = None
    m = DATE_DASH_RE.search(url)
    if m:
        d, mo, y = m.groups()
        url_date = f"{d}.{mo}.{y}"

    def score(el):
        txt = _normalize_line(el.get_text(" ", strip=True)).lower()
        s = 0
        if "biisilistat" in txt or "biisilista" in txt:
            s += 50
        if "dj orion" in txt:
            s += 30
        if url_date and url_date in txt:
            s += 40
        s += min(100, len(txt) // 200)
        return s

    if candidates:
        target = max(candidates, key=score)
        _dbg(debug, f"Chosen article score: {score(target)}")
    else:
        target = soup.select_one(".entry-content") or soup.select_one("main") or soup

    # Important: prefer entry-content inside the selected article
    content = None
    if hasattr(target, "select_one"):
        content = target.select_one(".entry-content")
    if content is None:
        content = target

    # Make <br> behave like newlines
    for br in content.find_all("br"):
        br.replace_with("\n")

    # Prefer list items ONLY if they look like actual track lines,
    # not "previous years" navigation (DJ Orion @ YleX … Biisilistat).
    lis = content.select("ol li, ul li")
    if lis:
        li_lines = [_normalize_line(li.get_text(" ", strip=True)) for li in lis]
        li_lines = [L for L in li_lines if L]

        # Track-like by separator
        li_sep = [L for L in li_lines if SEP_RE.search(L)]

        # But exclude obvious nav/history entries:
        # - contain "biisilista"
        # - match "DJ Orion @ YleX dd.mm.yyyy …"
        # - start with a year "2024 …"
        li_playlistish = []
        for L in li_sep:
            if looks_like_post_link_or_heading(L):
                continue
            if re.match(r"^\d{4}\s+", L):  # "2024 DJ Orion @ …"
                continue
            li_playlistish.append(L)

        if len(li_playlistish) >= 10:
            _dbg(debug, f"Returning {len(li_playlistish)} raw lines from <li> (playlist-ish)")
            return li_playlistish

        _dbg(
            debug,
            f"Ignoring <li> block: {len(li_lines)} li lines, {len(li_sep)} sep-matches, "
            f"{len(li_playlistish)} playlist-ish; falling back to text"
        )
#tää
    raw_text = content.get_text("\n", strip=True)
    lines = [_normalize_line(L) for L in raw_text.splitlines()]
    lines = [L for L in lines if L]
    _dbg(debug, f"Returning {len(lines)} raw lines")
    return lines


# ───────────────────────── Parsing tracks ─────────────────────────
def parse_track(line: str):
    """
    Return (artist, title) or None.
    - skips promo/social/link lines, hour headers, "biisilista" navigation lists, and Areena promo.
    - requires a separator with spaces around it to avoid splitting inside parentheses.
    """
    line = _normalize_line(line)
    if not line:
        return None

    if is_hour_header(line) or looks_like_areena_promo(line):
        return None

    if is_promo_or_link(line):
        return None

    if looks_like_post_link_or_heading(line):
        return None

    # strip leading numbering, clean trailing notes
    line = strip_leading_index(line)
    line = CLEAN_RE.sub("", line).strip(" -–—:|")

    # Must contain separator with spaces around it
    if not SEP_RE.search(line):
        return None

    parts = SEP_RE.split(line, maxsplit=1)
    if len(parts) != 2:
        return None

    artist = strip_quotes(parts[0].strip())
    title  = strip_quotes(parts[1].strip())

    if not artist or not title:
        return None

    # guard against stray time tokens
    if re.match(r"^\d{1,2}:\d{2}$", artist):
        return None

    # promo words still leak sometimes
    if is_promo_or_link(artist) or is_promo_or_link(title):
        return None

    # Avoid bogus "artist" like "Uusi YleX – n Perjantai"
    if looks_like_post_link_or_heading(f"{artist} – {title}"):
        return None

    return (artist, title)

def parse_tracks_from_raw(raw_lines, debug=False):
    raw = [_normalize_line(x) for x in raw_lines]
    raw = [x for x in raw if x]

    raw = merge_k_system(raw)

    raw = join_artist_fragments(raw)
    tracks = []

    def ok_line(s: str) -> bool:
        if not s:
            return False
        if is_hour_header(s) or looks_like_areena_promo(s):
            return False
        if is_promo_or_link(s) or looks_like_post_link_or_heading(s):
            return False
        if is_section_header(s) or is_vs_token(s):
            return False
        return True

    # ---- stitch around dash-only lines ----
    for i, line in enumerate(raw):
        if not DASH_ONLY_RE.match(line):
            continue

        prev = raw[i - 1] if i > 0 else ""
        nxt  = raw[i + 1] if i + 1 < len(raw) else ""

        if not (ok_line(prev) and ok_line(nxt)):
            continue

        prev_c = clean_piece(prev)
        nxt_c  = clean_piece(nxt)
        if not prev_c or not nxt_c:
            continue

        # XmiX numbered titles: "1   Flux Capacitor"
        if re.match(r"^\d{1,3}\s+", prev):
            title  = re.sub(r"^\d{1,3}\s+", "", prev_c).strip()
            artist = nxt_c

        # Title-first layout: Title / – / Artist
        elif titleish(prev_c) and artistish(nxt_c) and not artistish(prev_c):
            artist = nxt_c
            title  = prev_c

        # Normal layout: Artist / – / Title
        else:
            artist = prev_c
            title  = nxt_c

        # Swap if feat. tag landed in artist slot
        if re.search(r"\b(feat|ft)\.?\b", artist, re.I) and not re.search(r"\b(feat|ft)\.?\b", title, re.I):
            artist, title = title, artist


        artist, title = tidy_artist_title(artist, title)

        if not artist or not title:
            continue

        tracks.append((artist, title))


    # Deduplicate
    seen, dedup = set(), []
    for a, t in tracks:
        key = (a.lower(), t.lower())
        if key not in seen:
            seen.add(key)
            dedup.append((a, t))

    if debug:
        print(f"parse_tracks_from_raw(): raw={len(raw)} → tracks={len(dedup)}")

    return dedup


# ───────────────────────── Build scrobbles ─────────────────────────
def build_scrobbles(tracks, show_date: date, start_hm=SHOW_START_HM, duration_h=SHOW_DURATION_H):
    """
    pylast 5.2: scrobble_many expects list[dict] with artist/title/timestamp.
    Distribute tracks evenly within show window; clamp so timestamps are not in the future.
    """
    start_local = datetime.combine(show_date, datetime.min.time(), TZ_HELSINKI)
    start_local = start_local.replace(hour=start_hm[0], minute=start_hm[1])
    end_local = start_local + timedelta(hours=duration_h)

    start_ts = int(start_local.timestamp())
    end_ts   = int(end_local.timestamp())
    window   = max(1, end_ts - start_ts)

    n = max(1, len(tracks))
    step = max(60, window // n)

    latest_allowed = int(time.time()) - 1
    max_start = latest_allowed - (n - 1) * step
    base_ts = min(start_ts, max_start)

    scrobs = []
    for i, (artist, title) in enumerate(tracks):
        scrobs.append({
            "artist": artist,
            "title": title,
            "timestamp": base_ts + i * step,
        })
    return scrobs

def chunk(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

def scrobble_batches(network, batches):
    for batch in batches:
        wait = RETRY_WAIT
        for attempt in range(MAX_RETRIES):
            try:
                network.scrobble_many(batch)
                break
            except pylast.MalformedResponseError:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(wait)
                wait *= 2


# ───────────────────────── Main ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Scrobble DJ Orion playlists to Last.fm")
    ap.add_argument("--date", metavar="DD.MM.YYYY",
                    help="Which Friday to import (strict: must exist in --list)")
    ap.add_argument("--start", metavar="HH:MM", default="%02d:%02d" % SHOW_START_HM,
                    help="Show start time, Helsinki (default: %(default)s)")
    ap.add_argument("--hours", metavar="N", type=float, default=SHOW_DURATION_H,
                    help="Show length in hours (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse & print but don’t send to Last.fm")
    ap.add_argument("--debug", action="store_true",
                    help="Verbose diagnostics")
    ap.add_argument("--list", action="store_true",
                    help="List found playlist posts and exit")
    ap.add_argument("--latest", action="store_true",
                    help="Always pick newest post (ignore state)")
    ap.add_argument("--dump-raw", action="store_true",
                    help="Print ALL raw lines from chosen page and exit")
    ap.add_argument("--dump-candidates", action="store_true",
                    help="Print only track-like candidate raw lines and exit")
    args = ap.parse_args()
    try:
        start_hm = tuple(map(int, args.start.split(":")))
        assert len(start_hm) == 2 and 0 <= start_hm[0] < 24 and 0 <= start_hm[1] < 60
    except (ValueError, AssertionError):
        ap.error("--start must be HH:MM")
    if args.hours <= 0:
        ap.error("--hours must be positive")

    state = load_state()

    if args.list:
        posts = collect_posts(debug=args.debug)
        for d, u in posts:
            print(format_fi_date(d), u)
        return

    target_date = None
    if args.date:
        target_date = datetime.strptime(args.date, "%d.%m.%Y").date()

    show_date, url = choose_post(
        target_date=target_date,
        state=state,
        ignore_state=args.latest,
        debug=args.debug
    )
    if not show_date or not url:
        print("No playlist URL could be determined.")
        sys.exit(1)

    # Warn if this date was already scrobbled
    done = state.get("done_dates", [])
    show_date_fi = format_fi_date(show_date)
    if show_date_fi in done:
        print(f"WARNING: {show_date_fi} has already been scrobbled.")
        try:
            answer = input("Re-upload anyway? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "y":
            print("Aborted.")
            return

    print(f"Fetching playlist {show_date_fi} – {url}")

    raw = fetch_playlist(url, debug=args.debug)
    if not raw:
        print("No raw lines found.")
        sys.exit(1)

    if args.dump_raw:
        print_numbered(f"Raw lines from {url} ({len(raw)} lines):", raw)
        return

    if args.dump_candidates:
        cand = []
        for L in raw:
            n = _normalize_line(L)
            if SEP_RE.search(n) or DASH_ONLY_RE.match(n):
                cand.append(n)
        print_numbered(
            f"Track-like candidate lines from {url} ({len(cand)} candidates):",
            cand,
            limit=300
        )
        return

    # ✅ New parsing path (multi-line aware)
    tracks = parse_tracks_from_raw(raw, debug=args.debug)

    if args.debug:
        print(f"Raw lines: {len(raw)} | Parsed tracks: {len(tracks)}")
        if tracks:
            print("First 10 parsed:")
            for a, t in tracks[:10]:
                print("  ·", a, "–", t)

    if not tracks:
        print("No valid tracks found, aborting.")
        print_numbered("First 80 raw lines:", raw, limit=80)
        sys.exit(1)

    if args.dry_run:
        print(f"Parsed {len(tracks)} tracks")
        for a, t in tracks:
            print(" ", a, "–", t)
        return

    # Last.fm auth (pylast 5.2 compatible)
    API_KEY = os.getenv("LASTFM_API_KEY")
    API_SECRET = os.getenv("LASTFM_API_SECRET")
    SESSION = os.getenv("LASTFM_SESSION_KEY")

    if not API_KEY or not API_SECRET:
        print("Set LASTFM_API_KEY and LASTFM_API_SECRET in your environment.")
        sys.exit(1)

    if SESSION:
        network = pylast.LastFMNetwork(API_KEY, API_SECRET, session_key=SESSION)
    else:
        USER = os.getenv("LASTFM_USER")
        PASS = os.getenv("LASTFM_PASSWORD")
        if not USER or not PASS:
            print("Either LASTFM_SESSION_KEY or LASTFM_USER + LASTFM_PASSWORD required.")
            sys.exit(1)
        network = pylast.LastFMNetwork(API_KEY, API_SECRET, USER, md5(PASS))

    try:
        network.update_now_playing("Auth Check", "Auth Check")
    except pylast.WSError as e:
        print("Authentication failed:", e)
        sys.exit(1)

    scrobs = build_scrobbles(tracks, show_date, start_hm, args.hours)
    batches = list(chunk(scrobs, MAX_BATCH))

    print(f"Uploading to Last.fm in {len(batches)} batch(es)…")
    try:
        scrobble_batches(network, batches)
    except Exception as e:
        print("Upload failed:", e)
        sys.exit(1)

    print(f"Uploaded {len(scrobs)} tracks successfully.")

    # Save done date to prevent ping-pong
    done = state.get("done_dates", [])
    if show_date_fi not in done:
        done.append(show_date_fi)
        if len(done) > 80:
            done = done[-80:]
    state["done_dates"] = done
    save_state(state)


if __name__ == "__main__":
    main()
