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
REPEAT = timedelta(hours=3)
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


def check(events, now, state):
    st = status(events, now)
    if st is None:
        st = {"last": None, "quarters_late": 99, "time": None}
    if st["quarters_late"] >= 1:
        key = st["time"].isoformat() if st["time"] else "none"
        last_alert = parse(state["alert_at"]) if state.get("alert_at") else None
        if state.get("alert_for") != key or now - last_alert >= REPEAT:
            if st["last"]:
                msg = (f"🔴 Rakiškės HE: vandens lygis neskelbiamas.\n"
                       f"Paskutinis skelbtas rodmuo: {lt(st['time'])} (Vilnius), {level_text(st['last'])}.\n"
                       f"Vėluoja {st['quarters_late']} ketv. ({st['quarters_late'] * 15} min).")
            else:
                msg = "🔴 Rakiškės HE: per 72 val. nėra jokių skelbtų rodmenų."
            send(msg)
            state = {"alert_for": key, "alert_at": now.isoformat()}
    elif state.get("alert_for"):
        send(f"🟢 Rakiškės HE: duomenys vėl skelbiami.\n"
             f"Paskutinis rodmuo: {lt(st['time'])} (Vilnius), {level_text(st['last'])}.")
        state = {}
    return state


def summary(events, now):
    st = status(events, now)
    start = now - timedelta(hours=24)
    marks = []
    h = now.replace(minute=0, second=0, microsecond=0)
    while h > start:
        if h + PUBLISH_DELAY <= now:
            marks.append(h)
        h -= timedelta(hours=1)
    have = {parse(e["time"]) for e in events}
    missing = [x for x in marks if x not in have]
    lags = [(parse(e["createdAt"]) - parse(e["time"])).total_seconds() / 60
            for e in events if parse(e["time"]) in set(marks) and e.get("createdAt")]
    ok = st is not None and not missing and st["quarters_late"] == 0
    head = "🟢 Rakiškės HE: viskas gerai." if ok else "🔴 Rakiškės HE: yra praleistų rodmenų."
    lines = [head, f"Per 24 val. paskelbta {len(marks) - len(missing)} iš {len(marks)} rodmenų."]
    if st:
        lines.append(f"Paskutinis: {lt(st['time'])} (Vilnius), {level_text(st['last'])}.")
    if lags:
        lines.append(f"Didžiausias vėlavimas: {max(lags):.0f} min.")
    if missing:
        lines.append("Trūksta: " + ", ".join(lt(x)[11:] for x in sorted(missing)[:12]) +
                     (" ..." if len(missing) > 12 else ""))
    return "\n".join(lines)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    now = datetime.now(timezone.utc)
    events = fetch(now)
    st0 = status(events, now)
    print("latest:", st0 and (lt(st0["time"]), level_text(st0["last"]), "late quarters:", st0["quarters_late"]) or "no readings")
    quiet.flush(_raw_send, HELD_FILE)
    if mode == "summary":
        send(summary(events, now))
    else:
        save_state(check(events, now, load_state()))


if __name__ == "__main__":
    main()
