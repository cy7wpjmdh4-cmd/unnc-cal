#!/usr/bin/env python3
"""
UNNC timetable -> stable ICS relay.

Why this exists
---------------
The school's Scientia feed (ical/api/Activity/Personal/<identity>/ics) has three
defects that make an Apple Calendar subscription drop its contents periodically:

  1. It answers HTTP 200 with a plain-text body "Error: Unable to Fetch your ics"
     when its backend hiccups. A 200 is not an error for any HTTP client, so the
     calendar client parses garbage and can drop the subscription's events.
  2. It declares TZID=Asia/Hong_Kong but ships an EMPTY VTIMEZONE (no STANDARD /
     DAYLIGHT rules). Strict parsers cannot resolve DTSTART;TZID=... .
  3. Content-Type is text/plain, not text/calendar.

This script pulls the upstream feed, validates it hard, rewrites every timestamp
to a UTC instant (dropping the broken VTIMEZONE entirely), and only promotes the
new copy when it looks sane. If anything is wrong it leaves the previous
published file untouched, so subscribers never receive an invalid or empty
calendar.

Exit codes: 0 = published or safely kept last-good; 1 = last-good is stale
beyond STALE_DAYS (used to trigger a GitHub Actions failure email).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "docs" / "timetable.ics"
STATE = Path(__file__).resolve().parent / "docs" / "state.json"

STALE_DAYS = 14          # last-good older than this -> fail loudly
MIN_KEEP_RATIO = 0.6     # reject a feed that suddenly loses >40% of events
HKT = dt.timezone(dt.timedelta(hours=8))


def log(msg: str) -> None:
    print(f"[sync] {msg}", flush=True)


# ---------------------------------------------------------------- fetch
def fetch(url: str, attempts: int = 4) -> str | None:
    last = ""
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "unnc-cal-relay/1.0 (+github actions)",
                    "Accept": "text/calendar, text/plain;q=0.9, */*;q=0.5",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=45) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "")
            text = body.decode("utf-8-sig", errors="replace")
            log(f"attempt {i}: HTTP {r.status} {len(body)}B {ctype!r}")
            if validate(text):
                return text
            last = f"invalid body ({len(body)}B, starts {text[:60]!r})"
            log(f"attempt {i}: {last}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"{type(e).__name__}: {e}"
            log(f"attempt {i}: {last}")
        if i < attempts:
            time.sleep(5 * i)
    log(f"giving up after {attempts} attempts (last error: {last})")
    return None


def validate(text: str) -> bool:
    """Reject anything that is not a real, non-empty VCALENDAR."""
    if not text or text.lstrip("\ufeff \r\n").startswith("Error"):
        return False
    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        return False
    if "BEGIN:VEVENT" not in text:
        return False
    if text.count("BEGIN:VEVENT") < 1:
        return False
    # every event must have a summary and a start
    return text.count("BEGIN:VEVENT") == text.count("SUMMARY:") and (
        text.count("BEGIN:VEVENT") == text.count("DTSTART")
    )


# ---------------------------------------------------------------- rewrite
def unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))


def ics_dt(value: str, tzid: str | None) -> str:
    """Return a UTC (Z-suffixed) iCalendar timestamp."""
    value = value.strip()
    if value.endswith("Z"):
        return value
    if tzid:
        if tzid not in ("Asia/Hong_Kong", "Asia/Shanghai"):
            raise ValueError(f"unexpected tzid {tzid!r}")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            naive = dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=HKT).astimezone(dt.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
    raise ValueError(f"unparsable date-time {value!r}")


def fold(line: str) -> str:
    """Fold a content line at 75 octets per RFC 5545."""
    out, buf, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 73:
            out.append(buf)
            buf, size = " " + ch, 1 + n
        else:
            buf += ch
            size += n
    out.append(buf)
    return "\r\n".join(out)


def rewrite(text: str) -> tuple[str, int]:
    """Rebuild a clean VCALENDAR with UTC timestamps, no broken VTIMEZONE, and
    DETERMINISTIC UIDs.

    The upstream feed mints a fresh random UUID for every event on every
    request, so a calendar client sees a full delete+recreate on each refresh
    (the actual reason the subscription empties out). We derive the UID from the
    event's own content instead, so identity only changes when the class really
    moves. DTSTAMP is pinned to DTSTART for the same reason: it keeps the file
    byte-identical between polls unless the timetable actually changed.
    """
    raw = unfold(text)
    parsed: dict[str, dict[str, str]] = {}
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
        props: dict[str, str] = {}
        for line in block.split("\n"):
            line = line.rstrip("\r")
            if not line.strip():
                continue
            name = line.split(":", 1)[0].split(";", 1)[0].upper()
            if name in ("DTSTART", "DTEND"):
                params, _, val = line.partition(":")
                m = re.search(r"TZID=([^;:]+)", params, re.I)
                props[name] = ics_dt(val, m.group(1) if m else None)
            elif name in ("DTSTAMP", "CREATED", "LAST-MODIFIED"):
                continue  # regenerated below, never trust upstream's
            elif name in ("VTIMEZONE", "TZID", "X-LIC-LOCATION", "BEGIN", "END", "UID"):
                continue
            else:
                props[name] = line

        if "DTSTART" not in props:
            log("dropping event without DTSTART")
            continue
        if "DTEND" not in props:
            props["DTEND"] = props["DTSTART"]
        fingerprint = "|".join(
            props.get(k, "") for k in ("SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION")
        )
        props["UID"] = (
            hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:32] + "@unnc-cal"
        )
        props["DTSTAMP"] = props["DTSTART"]
        parsed[props["UID"]] = props

    events = [parsed[u] for u in parsed]  # de-duplicates identical events
    events.sort(key=lambda p: (p["DTSTART"], p.get("SUMMARY", ""), p["UID"]))

    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//unnc-cal-relay//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:UNNC Timetable",
        "X-WR-TIMEZONE:Asia/Shanghai",
        "X-PUBLISHED-TTL:PT3H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT3H",
    ]
    order = ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "LOCATION", "DESCRIPTION",
             "SEQUENCE", "STATUS")
    body = "\r\n".join(header) + "\r\n"
    for ev in events:
        body += "BEGIN:VEVENT\r\n"
        for key in order:
            if key in ev:
                body += fold(f"{key}:{ev[key]}") + "\r\n"
        for key in sorted(k for k in ev if k not in order):
            body += fold(f"{key}:{ev[key]}") + "\r\n"
        body += "END:VEVENT\r\n"
    body += "END:VCALENDAR\r\n"
    return body, len(events)


# ---------------------------------------------------------------- state
def read_state() -> dict:
    import json

    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    url = os.environ.get("ICS_URL", "").strip()
    if not url:
        log("ICS_URL is empty (missing secret?) — nothing to do")
        return 0

    state = read_state()
    prev_count = int(state.get("events") or 0)

    text = fetch(url)
    if text is None:
        age = None
        if OUT.exists():
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromtimestamp(
                OUT.stat().st_mtime, dt.timezone.utc)).days
        log(f"keeping last-good copy ({prev_count} events, {age} days old)")
        return 1 if (age is None or age > STALE_DAYS) else 0

    try:
        body, count = rewrite(text)
    except ValueError as e:
        log(f"rewrite failed ({e}) — keeping last-good copy")
        return 1 if not OUT.exists() else 0

    if count < 1:
        log("rewritten calendar has no events — keeping last-good copy")
        return 1 if not OUT.exists() else 0
    if prev_count and count < prev_count * MIN_KEEP_RATIO:
        log(
            f"suspicious shrink {prev_count} -> {count} events "
            f"(<{MIN_KEEP_RATIO:.0%}) — keeping last-good copy"
        )
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    changed = (not OUT.exists()) or OUT.read_text(encoding="utf-8", errors="replace") != body
    if changed:
        tmp = OUT.with_suffix(".ics.tmp")
        tmp.write_text(body, encoding="utf-8", newline="")
        tmp.replace(OUT)
        log(f"published {count} events -> {OUT}")
    else:
        log(f"unchanged ({count} events)")

    import json

    STATE.write_text(
        json.dumps(
            {
                "events": count,
                "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "source": re.sub(r"/Personal/[^/]+/", "/Personal/<redacted>/", url),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
