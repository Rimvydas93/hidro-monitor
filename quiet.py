"""Quiet hours: Telegram messages are only sent 07:00-21:00 Vilnius time.
Messages produced outside the window are held in a small file and delivered as one
digest at the first run after 07:00."""
import json, os
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Vilnius")
START, END = 7, 21          # allowed window: START <= hour < END
MAX_SHOWN = 8


def in_window(now=None):
    now = (now or datetime.now(TZ)).astimezone(TZ)
    return START <= now.hour < END


def _load(path):
    try:
        return json.load(open(path))
    except Exception:
        return []


def _save(path, held):
    if held:
        json.dump(held, open(path, "w"), ensure_ascii=False, indent=1)
    elif os.path.exists(path):
        os.remove(path)


def flush(send, path, now=None):
    """Send held messages (as one digest) if we are inside the window."""
    held = _load(path)
    if not held or not in_window(now):
        return
    shown = held[-MAX_SHOWN:]
    head = "🌙 Held during quiet hours (21:00–07:00):"
    if len(held) > len(shown):
        head += f" showing last {len(shown)} of {len(held)}"
    body = "\n\n".join(f"[{h['at']}] {h['text']}" for h in shown)
    send(head + "\n\n" + body)
    _save(path, [])


def deliver(text, send, path, force=False, now=None):
    """Send now if allowed, otherwise hold. force=True bypasses quiet hours (manual tests)."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    if force or in_window(now):
        flush(send, path, now)
        send(text)
    else:
        held = _load(path)
        held.append({"at": now.strftime("%d %H:%M"), "text": text})
        _save(path, held)
        print("held (quiet hours):", text[:60].replace("\n", " "))
