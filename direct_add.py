#!/usr/bin/env python3
"""direct-add.json — "the worker adds this feed's episodes itself; nobody needs to follow the playlist".

Roee, 17/11/2026: «בזמן בחירת כותרת ביוטיוב — מיד אחרי עדכון הפרטים והוספה לפלייליסט אפשר כבר להוסיף פרק
לפודקאסט, ולא צריך בכלל לעקוב על הפלייליסט כמו עכשיו כל עוד השגרה הזו פעילה.»

The weekly «בעין טובה» round adds its episode the moment it publishes (refresh.py --video-id). Every such add
writes this marker into the feed repo, valid for VALID_DAYS. The nightly playlist followers — the repo's own
GitHub Action, the updater on Roee's server, and refresh.py without --video-id — ask `gate` first and skip
following the playlist while the marker is fresh. When the routine stops, the marker goes stale and they
follow again by themselves: nobody has to remember to switch anything back.

Fail-open everywhere: a missing, unreadable, foreign (another playlist) or implausible marker (valid for more
than MAX_DAYS) means "follow". A follower that runs when it did not have to costs nothing (episodes are added
by id); a follower that stays silent when it had to is a lost episode.

This one file is the only implementation. The feed repo gets a byte-identical copy (repo-changes/README.md).
Standard library only, Python 3.8+ (the Action runs 3.12, Roee's server its venv).

    python direct_add.py gate   [--repo DIR]     # stdout: "follow=true" or "follow=false" (GitHub step output)
    python direct_add.py status [--repo DIR]     # stdout: one JSON line
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

MARKER = "direct-add.json"
VALID_DAYS = 10          # a weekly routine renews it every ~7 days; 3 days of slack for a late week
MAX_DAYS = 14            # a marker that claims more than this is not trusted (hand-edited, clock gone wrong)
EXPLAIN = ("עובד 16 מוסיף את הפרקים של הפודקאסט הזה בעצמו, ברגע שהשיחה מתפרסמת ביוטיוב. כל עוד valid_until לא עבר, "
           "העדכונים הליליים לא עוקבים אחרי הפלייליסט. אם השגרה נעצרת, הקובץ מתיישן והם חוזרים לעקוב לבד. "
           "אין צורך לערוך את הקובץ ביד.")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value):
    try:
        dt = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc)


def playlist_id_of(url):
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else None


def feed_playlist(repo_dir):
    """The playlist this feed follows (podcast_info.json), or None."""
    try:
        with open(os.path.join(repo_dir, "podcast_info.json"), encoding="utf-8") as fh:
            return playlist_id_of(json.load(fh).get("youtube_playlist_url"))
    except (OSError, ValueError, AttributeError):
        return None


def read(repo_dir):
    try:
        with open(os.path.join(repo_dir, MARKER), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def active(repo_dir, now=None, playlist_id=None):
    """(True, why) when following the playlist is left to the direct add; (False, why) otherwise. Never raises."""
    try:
        now = now or utcnow()
        m = read(repo_dir)
        if m is None:
            return False, "אין סימון של הוספה ישירה"
        until, last = parse(m.get("valid_until")), parse(m.get("last_add"))
        if until is None or last is None:
            return False, "הסימון של ההוספה הישירה לא קריא"
        want = playlist_id or feed_playlist(repo_dir)
        if not want or m.get("playlist_id") != want:
            return False, "הסימון של ההוספה הישירה שייך לפלייליסט אחר"
        if until - last > timedelta(days=MAX_DAYS) or until > now + timedelta(days=MAX_DAYS) or last > now + timedelta(hours=1):
            return False, "הסימון של ההוספה הישירה לא סביר (תוקף ארוך מדי או זמן עתידי)"
        if now >= until:
            return False, "ההוספה הישירה לא חודשה מאז %s (התוקף עבר ב-%s)" % (m.get("last_add"), m.get("valid_until"))
        return True, "עובד 16 מוסיף את הפרקים ישירות (פרק אחרון: %s, ב-%s; בתוקף עד %s)" % (
            m.get("last_video_id"), m.get("last_add"), m.get("valid_until"))
    except Exception as e:  # noqa: BLE001 — fail open
        return False, "בדיקת הסימון נכשלה (%s)" % type(e).__name__


def renew(repo_dir, video_id, playlist_id=None, now=None, by="worker16"):
    """Write (or renew) the marker after a direct add. Returns the marker."""
    now = now or utcnow()
    m = {"_": EXPLAIN, "by": by, "playlist_id": playlist_id or feed_playlist(repo_dir),
         "last_video_id": video_id, "last_add": iso(now), "valid_until": iso(now + timedelta(days=VALID_DAYS))}
    if not m["playlist_id"]:
        raise ValueError("no playlist for this feed (podcast_info.json)")
    _write(repo_dir, m)
    return m


def release(repo_dir, reason, now=None):
    """A direct add failed: hand the playlist back to the nightly followers at once (valid_until = now),
    so the episode comes in tonight instead of after the marker's slack. Returns the marker or None."""
    m = read(repo_dir)
    if m is None:
        return None
    now = now or utcnow()
    m = dict(m, valid_until=iso(now), released=iso(now), released_reason=str(reason)[:300])
    _write(repo_dir, m)
    return m


def _write(repo_dir, m):
    path = os.path.join(repo_dir, MARKER)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("gate", "status"))
    ap.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    on, why = active(a.repo)
    if a.cmd == "gate":
        print("follow=%s" % ("false" if on else "true"))
        sys.stderr.write(("לא עוקבים אחרי הפלייליסט: " if on else "עוקבים אחרי הפלייליסט: ") + why + "\n")
    else:
        print(json.dumps({"direct_add_active": on, "why": why, "marker": read(a.repo)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — the gate never breaks a follower: unknown = follow
        print("follow=true")
        sys.stderr.write("בדיקת הסימון נכשלה (%s); עוקבים כרגיל\n" % type(e).__name__)
        sys.exit(0)
