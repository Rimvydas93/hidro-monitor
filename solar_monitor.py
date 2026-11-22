#!/usr/bin/env python3
"""Kairiškiai solar park watchdog -> Telegram.

Every run (GitHub Actions cron, ~15 min):
  1. LT price now (Elering, 15-min + hourly average)
  2. Log in to FIMER Aurora, read every inverter
  3. Alert if price >= threshold, sun is up, and any inverter (except IGNORE) is not producing
     Alert if price < threshold and park is still producing (shutdown failed)
     Alert if the portal login/data fails
  Alerts only on state change (+ reminder every REMIND_MIN while a fault lasts), plus a recovery message.
"""
import hashlib, json, math, os, re, sys, time
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup

BASE = "https://auroraonline.solar.fimer.com/fimer/"
USER = os.environ["FIMER_USER"]
PWD = os.environ["FIMER_PASSWORD"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
THRESHOLD = float(os.environ.get("PRICE_THRESHOLD", "19"))
IGNORE = {x.strip() for x in os.environ.get("IGNORE_INVERTERS", "111747").split(",") if x.strip()}
MIN_SUN_ELEV = float(os.environ.get("MIN_SUN_ELEV", "6"))      # degrees; below this, low output is normal
REMIND_MIN = int(os.environ.get("REMIND_MIN", "60"))
LOW_RATIO = float(os.environ.get("LOW_RATIO", "0.4"))          # inverter < 40% of park median = underperforming
LAT, LON = 55.98, 22.37   # approx. Kairiškiai area; adjust if needed
STATE_FILE = "solar_state.json"

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (solar-watchdog)"


def tg(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=20)
    print("telegram:", r.status_code, r.text[:200])


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
    data = {"posted_username": USER,
            "posted_password": hashlib.md5(PWD.encode()).hexdigest(),
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


def main():
    now = datetime.now(timezone.utc)
    elev = sun_elevation(now)
    state = load_state()
    problems, report = [], ""

    try:
        p15, phour = price_now(now)
    except Exception as e:
        p15 = phour = None
        print("price error", e)

    try:
        invs = read_inverters(login())
    except Exception as e:
        invs = None
        if elev > 0:
            problems.append(f"Portal/data error: {e}")

    if invs is not None and p15 is not None:
        active = [i for i in invs if i["id"] not in IGNORE]
        running = [i for i in active if "run" in (i["status"] + i["global"]).lower() and i["power"] > 0]
        total = sum(i["power"] for i in invs)
        should_run = min(p15, phour) >= THRESHOLD     # controller may use 15-min or hourly price
        should_stop = max(p15, phour) < THRESHOLD

        if should_run and elev >= MIN_SUN_ELEV:
            down = [i for i in active if i not in running]
            if down:
                problems.append(f"{len(down)}/{len(active)} inverters NOT producing: "
                                + ", ".join(f"{i['id']} ({i['global'] or i['status']}; {i['alarm']})" for i in down))
            powers = sorted(i["power"] for i in running)
            if len(powers) >= 4:
                med = powers[len(powers) // 2]
                if med > 500:
                    low = [i for i in running if i["power"] < LOW_RATIO * med]
                    if low:
                        problems.append("Underperforming: " + ", ".join(
                            f"{i['id']} {i['power']/1000:.1f} kW vs median {med/1000:.1f}" for i in low))
        if should_stop and total > 1000 and elev >= MIN_SUN_ELEV:
            problems.append(f"Price below {THRESHOLD:g} €/MWh but park still producing {total/1000:.1f} kW (shutdown failed?)")

        report = (f"Price {p15:.1f} €/MWh (hour avg {phour:.1f}) · park {total/1000:.1f} kW · "
                  f"{len(running)}/{len(active)} running · sun {elev:.0f}°")
    print(report or "no report")
    for p in problems:
        print("PROBLEM:", p)

    key = " | ".join(sorted(re.sub(r"\d+(\.\d+)? kW.*?median [\d.]+", "", p) for p in problems))
    last_key = state.get("key", "")
    last_sent = state.get("sent", 0)
    t = time.time()
    if problems and (key != last_key or t - last_sent > REMIND_MIN * 60):
        tg("⚠️ <b>Kairiškiai solar park</b>\n" + "\n".join("• " + p for p in problems) + (f"\n\n{report}" if report else ""))
        state.update(key=key, sent=t)
    elif not problems and last_key:
        tg("✅ <b>Kairiškiai solar park</b> back to normal.\n" + report)
        state.update(key="", sent=0)
    state["last_run"] = now.isoformat()
    state["last_report"] = report
    json.dump(state, open(STATE_FILE, "w"), indent=1)


if __name__ == "__main__":
    if "--test" in sys.argv:
        tg("🔔 Kairiškiai solar watchdog connected.")
    main()
