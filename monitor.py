#!/usr/bin/env python3
"""Rakiskes HE (life.meteo.lt / meteo.lt official hydro data, station 88) publication monitor.
(Until 2026-11-24 this read hidro.biip.lt plant 69, which stopped publishing on 2026-11-16.)

check   : red Telegram alert if the latest hourly reading is >= 1 quarter late
          (one alert, repeated every 3 h while late) + a green message when data returns.
summary : one daily message (green or red) with the last 24 h.
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. DRY_RUN=1 prints instead of sending.
"""
import json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import quiet

PLANT = "88"
API = "https://life.meteo.lt/hidro/api/heStation/map"
PUBLISH_DELAY = timedelta(minutes=40)   # reading for hour H normally appears within ~40 min (most stations by H+60 min)
QUARTER = timedelta(minutes=15)
# The official site publishes with a variable delay (often 1.5-2.5 h), so only a long gap is an alert.
LATE_AFTER = timedelta(hours=float(os.environ.get("LATE_AFTER_H", "4")))   # latest reading older than this = alert
SUMMARY_GRACE = timedelta(hours=3)      # the daily summary ignores the newest 3 h (normal publishing delay)
KEEP_SEEN = 96                          # remembered first-seen times (readings)
REPEAT = timedelta(hours=3)
# Permitted water levels for Rakiskes HE (official limits from the meteo.lt station record, station 88)
UP_MAX = float(os.environ.get("UPPER_MAX", "80.60"))    # upper basin (virsutinis bjefas) must stay <= this
UP_MIN = float(os.environ.get("UPPER_MIN", "79.70"))    #  ... and >= this
LOW_MIN = float(os.environ.get("LOWER_MIN", "75.90"))   # lower basin (apatinis bjefas) must stay >= this
LEVEL_MAX_AGE = timedelta(hours=12)                     # ignore readings older than this for level alerts
STATE = "state.json"
VILNIUS = ZoneInfo("Europe/Vilnius")


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fetch(now):
    """Readings of station PLANT from the official meteo.lt API, normalised to the old event shape."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    q = ("pageSize=500&page=1"
         "&query[obstime][$gte]=" + (now - timedelta(hours=72)).strftime(fmt) +
         "&query[obstime][$lt]=" + (now + timedelta(hours=1)).strftime(fmt))
    req = urllib.request.Request(API + "?" + q, headers={"User-Agent": "dasma-hidro-monitor"})
    with urllib.request.urlopen(req, timeout=30) as r:
        stations = json.load(r)
    out = []
    for st in stations:
        if str(st.get("id")) == PLANT:
            for e in st.get("events", []):
                try:
                    out.append({"time": e["obstime"], "upperBasin": float(e["highLevel"]),
                                "lowerBasin": float(e["lowLevel"]), "createdAt": None})
                except (KeyError, TypeError, ValueError):
                    continue
    return out


def lt(dt):
    return dt.astimezone(VILNIUS).strftime("%Y-%m-%d %H:%M")


def m(v):
    return f"{v:.2f}".replace(".", ",") + " m"


def status(events, now):
    """Latest reading and how many quarters late the next one is."""
    if not events:
        return None
    last = max(events, key=lambda e: e["time"])
    t = parse(last["time"])
    due = t + timedelta(hours=1) + PUBLISH_DELAY
    late = now - due
    q = int(late // QUARTER) if late > timedelta(0) else 0
    return {"last": last, "time": t, "due": due, "quarters_late": q}


def level_text(last):
    return f"viršutinis bjefas {m(last['upperBasin'])}, apatinis {m(last['lowerBasin'])}"


def _raw_send(text):
    if os.environ.get("DRY_RUN"):
        print("TELEGRAM:", text)
        return
    data = urllib.parse.urlencode({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}).encode()
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30).read()


HELD_FILE = "held_hidro.json"


def send(text, force=False):
    quiet.deliver(text, _raw_send, HELD_FILE, force=force)


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=1)


def age_text(delta):
    m = int(delta.total_seconds() // 60)
    return f"{m // 60} val. {m % 60} min"


def check(events, now, state):
    st = status(events, now)
    if st is None:
        st = {"last": None, "quarters_late": 99, "time": None}
    age = (now - st["time"]) if st["time"] else None
    if st["time"] is None or age >= LATE_AFTER:
        key = st["time"].isoformat() if st["time"] else "none"
        last_alert = parse(state["alert_at"]) if state.get("alert_at") else None
        if state.get("alert_for") != key or now - last_alert >= REPEAT:
            if st["last"]:
                missing_h = max(1, int(age.total_seconds() // 3600) - 1)
                msg = (f"🟠 Rakiškės HE: duomenų skelbimas vėluoja (matavimai gali būti, bet svetainė jų dar nerodo).\n"
                       f"Paskutinis skelbtas rodmuo: {lt(st['time'])} (Vilnius), {level_text(st['last'])}.\n"
                       f"Jam jau {age_text(age)} (trūksta ~{missing_h} val. rodmenų).")
            else:
                msg = "🟠 Rakiškės HE: per 72 val. svetainėje nėra jokių rodmenų."
            send(msg)
            state = {"alert_for": key, "alert_at": now.isoformat()}
    elif state.get("alert_for"):
        send(f"🟢 Rakiškės HE: duomenys vėl skelbiami.\n"
             f"Paskutinis rodmuo: {lt(st['time'])} (Vilnius), {level_text(st['last'])}.")
        state = {}
    return state


def level_issues(last):
    """Which permitted limits the reading breaks (empty list = all fine)."""
    out = []
    if last["upperBasin"] > UP_MAX:
        out.append(("up_hi", f"viršutinis bjefas {m(last['upperBasin'])} viršija leistiną {m(UP_MAX)}"))
    elif last["upperBasin"] < UP_MIN:
        out.append(("up_lo", f"viršutinis bjefas {m(last['upperBasin'])} žemiau leistino {m(UP_MIN)}"))
    if last["lowerBasin"] < LOW_MIN:
        out.append(("low_lo", f"apatinis bjefas {m(last['lowerBasin'])} žemiau leistino {m(LOW_MIN)}"))
    return out


def level_check(events, now, lvl):
    """Alert when the newest published reading is outside the permitted water levels.
    One alert per kind of breach, repeated every 3 h while it lasts, green message when back in range."""
    st = status(events, now)
    if st is None or now - st["time"] > LEVEL_MAX_AGE:
        return lvl
    issues = level_issues(st["last"])
    kinds = sorted(k for k, _ in issues)
    prev = lvl.get("kinds", [])
    last_at = parse(lvl["at"]) if lvl.get("at") else None
    if issues:
        if kinds != prev or (last_at and now - last_at >= REPEAT):
            send("🟠 Rakiškės HE: vandens lygis už leistinų ribų.\n"
                 f"Rodmuo: {lt(st['time'])} (Vilnius): " + "; ".join(t for _, t in issues) + ".\n"
                 f"Leistina: viršutinis {m(UP_MIN)} – {m(UP_MAX)}, apatinis ne mažiau {m(LOW_MIN)}.\n"
                 f"Dabar: {level_text(st['last'])}.")
            return {"kinds": kinds, "at": now.isoformat()}
        return lvl
    if prev:
        send(f"🟢 Rakiškės HE: vandens lygis vėl leistinose ribose.\nRodmuo: {lt(st['time'])} (Vilnius), {level_text(st['last'])}.")
    return {}


def track_seen(events, now, seen):
    """Remember when each reading first appeared, to learn the real publishing delay.
    First ever run only records a baseline (None) so old readings do not count."""
    first_run = seen is None
    seen = {} if first_run else dict(seen)
    for e in events:
        t = e["time"]
        if t not in seen:
            seen[t] = None if first_run else now.isoformat()
    for t in sorted(seen)[:-KEEP_SEEN]:
        del seen[t]
    return seen


def delays(seen):
    """Publishing delays in minutes (first seen - reading time) for readings first seen by this monitor."""
    out = []
    for t, first in seen.items():
        if first:
            out.append((parse(first) - parse(t)).total_seconds() / 60)
    return out


def summary(events, now, seen=None):
    st = status(events, now)
    start = now - timedelta(hours=24)
    marks = []
    h = now.replace(minute=0, second=0, microsecond=0)
    while h > start:
        if h + SUMMARY_GRACE <= now:
            marks.append(h)
        h -= timedelta(hours=1)
    have = {parse(e["time"]) for e in events}
    missing = [x for x in marks if x not in have]
    ok = st is not None and not missing and (now - st["time"]) < LATE_AFTER
    head = "🟢 Rakiškės HE: viskas gerai." if ok else "🔴 Rakiškės HE: yra praleistų rodmenų."
    lines = [head, f"Per 24 val. (be paskutinių 3 val.) paskelbta {len(marks) - len(missing)} iš {len(marks)} rodmenų."]
    if st:
        lines.append(f"Paskutinis: {lt(st['time'])} (Vilnius), {level_text(st['last'])}.")
    d = delays(seen or {})
    if d:
        d.sort()
        lines.append(f"Skelbimo vėlavimas po valandos pradžios: tipinis {d[len(d) // 2]:.0f} min, didžiausias {d[-1]:.0f} min ({len(d)} rodm.).")
    if missing:
        lines.append("Trūksta: " + ", ".join(lt(x)[11:] for x in sorted(missing)[:12]) +
                     (" ..." if len(missing) > 12 else ""))
    return "\n".join(lines)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    now = datetime.now(timezone.utc)
    events = fetch(now)
    st0 = status(events, now)
    print("latest:", st0 and (lt(st0["time"]), level_text(st0["last"]), "age:", age_text(now - st0["time"])) or "no readings")
    quiet.flush(_raw_send, HELD_FILE)
    state = load_state()
    seen = track_seen(events, now, state.pop("seen", None))
    if mode == "summary":
        send(summary(events, now, seen))
        state["seen"] = seen
        save_state(state)
    else:
        lvl = state.pop("lvl", {})
        state = check(events, now, state)
        state["seen"] = seen
        state["lvl"] = level_check(events, now, lvl)
        save_state(state)


if __name__ == "__main__":
    main()
