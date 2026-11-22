#!/usr/bin/env python3
"""Kairiškiai solar park watchdog -> Telegram.

Every run (GitHub Actions cron, ~15 min):
  1. LT price now (Elering, 15-min + hourly average)
  2. Sun irradiance on the panels now (Open-Meteo)
  3. Log in to FIMER Aurora, read every inverter
Alerts (only on change, reminder every REMIND_MIN, plus a recovery message):
  - inverter not producing while price >= threshold and sun is up
  - inverter far below the park median (dirty / shaded / faulty string)
  - whole park far below what the sun should give (grid fault, curtailment, ...)
  - inverter data frozen (communication lost)
  - inverter disappeared from the portal
  - price < threshold but park still producing (shutdown failed)
  - FIMER login / data error
Every alert shows the estimated money lost per hour.
--daily sends an evening summary with a per-inverter league table.
--test sends a "connected" message first.
"""
import hashlib, json, math, os, re, statistics, sys, time
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup

BASE = "https://auroraonline.solar.fimer.com/fimer/"
THRESHOLD = float(os.environ.get("PRICE_THRESHOLD", "19"))
IGNORE = {x.strip() for x in os.environ.get("IGNORE_INVERTERS", "111747").split(",") if x.strip()}
MIN_SUN_ELEV = float(os.environ.get("MIN_SUN_ELEV", "6"))      # degrees; below this, low output is normal
REMIND_MIN = int(os.environ.get("REMIND_MIN", "60"))
LOW_RATIO = float(os.environ.get("LOW_RATIO", "0.4"))          # inverter < 40% of park median = underperforming
PARK_KW = float(os.environ.get("PARK_KW", "420"))              # nameplate DC power of the whole park
PERF_RATIO = float(os.environ.get("PERF_RATIO", "0.80"))       # realistic output / ideal output
MIN_GTI = float(os.environ.get("MIN_GTI", "150"))              # W/m2 on panels needed before judging the park
LOW_PARK = float(os.environ.get("LOW_PARK", "0.4"))            # park < 40% of expected = problem
LOW_PARK_RUNS = int(os.environ.get("LOW_PARK_RUNS", "2"))      # ... on this many runs in a row
TILT = os.environ.get("PANEL_TILT", "35")
AZIMUTH = os.environ.get("PANEL_AZIMUTH", "0")                 # 0 = south (Open-Meteo convention)
STALE_MIN = int(os.environ.get("STALE_MIN", "40"))             # inverter data unchanged this long = frozen
LEAGUE_LOW = float(os.environ.get("LEAGUE_LOW", "0.85"))       # daily kWh < 85% of median = flag
LAT, LON = 55.98, 22.37   # approx. Kairiškiai area; adjust if needed
STATE_FILE = os.environ.get("STATE_FILE", "solar_state.json")

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (solar-watchdog)"


def tg(text):
    r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                      data={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML"},
                      timeout=20)
    print("telegram:", r.status_code, r.text[:80])


def sun_elevation(dt):
    # NOAA simplified solar position
    doy = dt.timetuple().tm_yday
    h = dt.hour + dt.minute / 60
    g = 2 * math.pi / 365 * (doy - 1 + (h - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = h * 60 + eqt + 4 * LON
    ha = math.radians(tst / 4 - 180)
    lat = math.radians(LAT)
    cosz = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    return 90 - math.degrees(math.acos(max(-1, min(1, cosz))))


def price_now(now):
    start = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00.000Z")
    end = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:00:00.000Z")
    d = requests.get("https://dashboard.elering.ee/api/nps/price",
                     params={"start": start, "end": end}, timeout=20).json()["data"]["lt"]
    ts = now.timestamp()
    cur = [x for x in d if x["timestamp"] <= ts][-1]
    hstart = ts - (ts % 3600)
    hour = [x["price"] for x in d if hstart <= x["timestamp"] < hstart + 3600]
    return cur["price"], (sum(hour) / len(hour) if hour else cur["price"])


def irradiance_now(now):
    """Sun power on the tilted panels now, W/m2 (Open-Meteo). None if unavailable."""
    base = {"latitude": LAT, "longitude": LON, "tilt": TILT, "azimuth": AZIMUTH, "timezone": "UTC"}
    try:
        j = requests.get("https://api.open-meteo.com/v1/forecast", timeout=20,
                         params={**base, "current": "global_tilted_irradiance"}).json()
        v = j["current"]["global_tilted_irradiance"]
        if v is not None:
            return float(v)
    except Exception as e:
        print("irradiance (current) error:", e)
    try:
        j = requests.get("https://api.open-meteo.com/v1/forecast", timeout=20,
                         params={**base, "hourly": "global_tilted_irradiance", "forecast_days": 1}).json()
        want = now.strftime("%Y-%m-%dT%H:00")
        v = dict(zip(j["hourly"]["time"], j["hourly"]["global_tilted_irradiance"])).get(want)
        return None if v is None else float(v)
    except Exception as e:
        print("irradiance (hourly) error:", e)
        return None


def num(txt):
    m = re.search(r"-?\d+(?:[.,]\d+)?", txt or "")
    if not m:
        return 0.0
    v = float(m.group().replace(",", "."))
    t = txt.replace(" ", "")
    if re.search(r"\d\s*k", txt) or "kW" in t:
        v *= 1000
    elif "MW" in t:
        v *= 1e6
    return v


def kwh(txt):
    """Energy text -> kWh (None if no number)."""
    m = re.search(r"-?\d+(?:[.,]\d+)?", txt or "")
    if not m:
        return None
    v = float(m.group().replace(",", "."))
    t = (txt or "").lower().replace(" ", "")
    if "mwh" in t:
        v *= 1000
    elif "kwh" in t:
        pass
    elif "wh" in t:
        v /= 1000
    return v


def field(soup, label):
    lab = soup.find("label", string=re.compile(r"^\s*" + re.escape(label) + r"\s*:?\s*$"))
    if not lab:
        return ""
    td = lab.find_parent("td")
    nxt = td.find_next_sibling("td") if td else None
    return nxt.get_text(" ", strip=True).replace("\xa0", " ").strip() if nxt else ""


def login():
    r = S.get(BASE + "login.php", timeout=30)
    tok = BeautifulSoup(r.text, "html.parser").find("input", {"name": "CsrfToken"})
    data = {"posted_username": os.environ["FIMER_USER"],
            "posted_password": hashlib.md5(os.environ["FIMER_PASSWORD"].encode()).hexdigest(),
            "CsrfToken": tok["value"] if tok else ""}
    S.post(BASE + "login.php", data=data, timeout=30)
    r = S.get(BASE + "park.php", timeout=30)
    if "posted_username" in r.text:
        raise RuntimeError("FIMER login failed")
    ids = sorted(set(re.findall(r'data-tree-item="inv"[^>]*data-tree-id="([^"]+)"', r.text)))
    if not ids:
        raise RuntimeError("FIMER: no inverters found on park page")
    return ids


def read_inverters(ids):
    out = []
    for inv in ids:
        r = S.post(BASE + "inv.php", data={"InvID": inv}, timeout=30)
        s = BeautifulSoup(r.text, "html.parser")
        out.append({
            "id": inv.split("-")[0],
            "time": field(s, "Data time"),
            "global": field(s, "Global Status"),
            "status": field(s, "Inverter Status"),
            "alarm": field(s, "Alarm Status"),
            "power": num(field(s, "Output Power")),
            "daily": field(s, "Daily Energy"),
        })
    return out


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def is_running(i):
    return "run" in (i["status"] + i["global"]).lower() and i["power"] > 0


def evaluate(invs, p15, phour, elev, gti, state, now_ts):
    """Pure check logic. Returns (problems, report); problems = [(key, text)]. Updates state in place."""
    problems = []
    active = [i for i in invs if i["id"] not in IGNORE]
    running = [i for i in active if is_running(i)]
    total = sum(i["power"] for i in active)
    day = elev >= MIN_SUN_ELEV
    should_run = p15 is not None and min(p15, phour) >= THRESHOLD   # controller may use 15-min or hourly price
    should_stop = p15 is not None and max(p15, phour) < THRESHOLD
    price = p15 if p15 is not None else 0.0
    med_w = statistics.median([i["power"] for i in running]) if running else 0.0

    # --- frozen data: the "Data time" text has not changed for STALE_MIN minutes ---------------------
    seen = state.setdefault("seen", {})
    for i in invs:
        s = seen.get(i["id"])
        if not s or s["t"] != i["time"]:
            seen[i["id"]] = {"t": i["time"], "since": now_ts}
    stale = [i["id"] for i in active
             if i["time"] and now_ts - seen[i["id"]]["since"] > STALE_MIN * 60]
    if stale and day:
        problems.append(("stale:" + ",".join(stale),
                         f"Data frozen > {STALE_MIN} min (communication lost?): " + ", ".join(stale)))

    # --- inverter vanished from the portal ----------------------------------------------------------
    cur_ids = {i["id"] for i in invs}
    gone = sorted(set(state.get("known", [])) - cur_ids)
    if gone and day:
        problems.append(("gone:" + ",".join(gone), "Inverter missing from portal: " + ", ".join(gone)))
    state["known"] = sorted(set(state.get("known", [])) | cur_ids)

    # --- park vs what the sun should give --------------------------------------------------------------
    ratio = None
    if gti is not None and active:
        share = len(active) / max(1, len(invs))
        expected_w = PARK_KW * 1000 * share * gti / 1000 * PERF_RATIO
        if expected_w > 0:
            ratio = total / expected_w
        if should_run and day and gti >= MIN_GTI and ratio is not None and ratio < LOW_PARK:
            state["low_count"] = state.get("low_count", 0) + 1
        else:
            state["low_count"] = 0
        if state["low_count"] >= LOW_PARK_RUNS:
            lost = (expected_w - total) / 1000
            problems.append(("park_low",
                             f"Park gives {total/1000:.1f} kW, only {ratio*100:.0f}% of the {expected_w/1000:.0f} kW "
                             f"the sun should give ({gti:.0f} W/m²). Lost ≈ {lost*price/1000:.1f} €/h"))
    else:
        state["low_count"] = 0

    # --- price good + sun up: every inverter should run -----------------------------------------------
    if should_run and day:
        down = [i for i in active if i not in running]
        if down:
            lost_eur = len(down) * med_w / 1000 * price / 1000
            problems.append(("down:" + ",".join(sorted(i["id"] for i in down)),
                             f"{len(down)}/{len(active)} inverters NOT producing: "
                             + ", ".join(f"{i['id']} ({i['global'] or i['status']}; {i['alarm']})" for i in down)
                             + (f". Lost ≈ {lost_eur:.1f} €/h" if med_w > 500 else "")))
        powers = sorted(i["power"] for i in running)
        if len(powers) >= 4 and med_w > 500:
            low = [i for i in running if i["power"] < LOW_RATIO * med_w]
            if low:
                lost_eur = sum(med_w - i["power"] for i in low) / 1000 * price / 1000
                problems.append(("low:" + ",".join(sorted(i["id"] for i in low)),
                                 "Underperforming: " + ", ".join(
                                     f"{i['id']} {i['power']/1000:.1f} kW vs median {med_w/1000:.1f}" for i in low)
                                 + f". Lost ≈ {lost_eur:.1f} €/h"))
    if should_stop and total > 1000 and day:
        problems.append(("not_stopped",
                         f"Price below {THRESHOLD:g} €/MWh but park still producing {total/1000:.1f} kW (shutdown failed?)"))

    ptxt = "n/a" if p15 is None else f"{p15:.1f} €/MWh (hour avg {phour:.1f})"
    report = (f"Price {ptxt} · park {total/1000:.1f} kW · {len(running)}/{len(active)} running · sun {elev:.0f}°"
              + ("" if gti is None else f" · {gti:.0f} W/m²")
              + ("" if ratio is None else f" · {ratio*100:.0f}% of expected"))
    return problems, report


def daily_summary(invs, now):
    """Evening league table: each inverter's energy today vs the median."""
    rows = [(i["id"], kwh(i["daily"])) for i in invs if i["id"] not in IGNORE]
    rows = [(a, b) for a, b in rows if b is not None]
    if not rows:
        return "📊 <b>Kairiškiai solar park</b> – daily summary: no energy values read from the portal."
    vals = sorted(b for _, b in rows)
    med = statistics.median(vals)
    total = sum(vals)
    flagged = [(a, b) for a, b in rows if med > 0 and b < LEAGUE_LOW * med]
    lines = [f"📊 <b>Kairiškiai solar park</b> – {now.strftime('%Y-%m-%d')}",
             f"Total {total:.0f} kWh · median inverter {med:.1f} kWh · "
             f"best {vals[-1]:.1f} · worst {vals[0]:.1f}"]
    if flagged:
        lines.append("⚠️ Below {:.0f}% of median (dirty / shaded / faulty string?):".format(LEAGUE_LOW * 100))
        for a, b in sorted(flagged, key=lambda x: x[1]):
            lines.append(f"• {a}: {b:.1f} kWh ({b/med*100:.0f}%)")
    else:
        lines.append("✅ All inverters within {:.0f}% of the median.".format(LEAGUE_LOW * 100))
    return "\n".join(lines)


def main():
    now = datetime.now(timezone.utc)
    elev = sun_elevation(now)
    state = load_state()
    daily = "--daily" in sys.argv

    try:
        p15, phour = price_now(now)
    except Exception as e:
        p15 = phour = None
        print("price error", e)
    gti = irradiance_now(now)

    portal_error = None
    try:
        invs = read_inverters(login())
    except Exception as e:
        invs, portal_error = None, str(e)
        print("portal error", e)

    if invs is not None:
        for i in invs:
            print(f"inv {i['id']}: time='{i['time']}' global='{i['global']}' status='{i['status']}' "
                  f"alarm='{i['alarm']}' power={i['power']:.0f}W daily='{i['daily']}'")

    if daily:
        tg(daily_summary(invs, now) if invs is not None
           else f"📊 Kairiškiai solar park – daily summary failed: {portal_error}")
        state["last_run"] = now.isoformat()
        json.dump(state, open(STATE_FILE, "w"), indent=1)
        return

    if invs is None:
        problems = [("portal", f"Portal/data error: {portal_error}")] if elev > 0 else []
        report = ""
    else:
        problems, report = evaluate(invs, p15, phour, elev, gti, state, now.timestamp())
    print(report or "no report")
    for _, p in problems:
        print("PROBLEM:", p)

    key = " | ".join(sorted(k for k, _ in problems))
    last_key = state.get("key", "")
    last_sent = state.get("sent", 0)
    t = time.time()
    if problems and (key != last_key or t - last_sent > REMIND_MIN * 60):
        tg("⚠️ <b>Kairiškiai solar park</b>\n" + "\n".join("• " + p for _, p in problems)
           + (f"\n\n{report}" if report else ""))
        state.update(key=key, sent=t)
    elif not problems and last_key:
        tg("✅ <b>Kairiškiai solar park</b> back to normal.\n" + report)
        state.update(key="", sent=0)
    state["last_run"] = now.isoformat()
    state["last_report"] = report
    json.dump(state, open(STATE_FILE, "w"), indent=1)


if __name__ == "__main__":
    if "--test" in sys.argv:
        tg("🔔 Kairiškiai solar watchdog connected (v2: sun check, stale data, missing inverters, daily table).")
    main()
