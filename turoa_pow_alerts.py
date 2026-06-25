#!/usr/bin/env python3
"""
TŪROA POW ALERTS  🏂❄️
A three-tier "perfect snowboarding day" alarm for Tūroa, Mt Ruapehu.

Pulls the mountain forecast from Open-Meteo (free, no API key), checks it against
YOUR definition of a perfect day, and pings your phone via ntfy.sh ONLY when the
stars actually line up — at three confidence levels:

  TIER 1  POW RADAR    (3-7 days out)  -> low-confidence "somethin's brewing"
  TIER 2  LOCK IT IN   (<=3 days out)  -> high-confidence "start scheming"
  TIER 3  SEND IT      (tonight/dawn)  -> ~90% "go, just check lift status"

Tune everything in CONFIG. Prove the logic with no alerts sent:
    python3 turoa_pow_alerts.py --selftest
Run for real (used by the GitHub Action):
    python3 turoa_pow_alerts.py
"""

import os
import sys
import json
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# ───────────────────────────────────────────────────────────────────────────
# CONFIG  — this is your cockpit. Change the numbers, save, done.
# ───────────────────────────────────────────────────────────────────────────
CONFIG = {
    # WHERE — Tūroa upper mountain (the snow + wind that matters for the top chairs)
    "latitude": -39.30,
    "longitude": 175.53,
    "elevation_m": 2000,        # forecast point elevation (mid-upper mountain)
    "base_elevation_m": 1600,   # Tūroa base — used for the rain-at-base check
    "timezone": "Pacific/Auckland",

    # WHO TO PING — your private topic. Anyone who knows this name can read your
    # alerts, so it's deliberately random. This is already set to yours.
    "ntfy_topic": "turoa-pow-shred-4471",

    # WHEN LIFTS RUN — local hours we judge wind & cloud over
    "op_start_hour": 8,
    "op_end_hour": 16,

    # WHAT COUNTS AS A PERFECT DAY
    "fresh_snow_min_cm": 12,        # min fresh snow in the trailing window
    "fresh_window_h": 48,           # how far back "fresh" counts (hours)
    "max_gust_kmh": 45,             # above this = wind-hold risk (the Tūroa killer)
    "tier3_max_gust_kmh": 38,       # stricter gust bar for the same-day SEND call
    "freezing_level_buffer_m": 0,   # freezing lvl must be <= base + this (rain check)
    "bluebird_max_cloud_pct": 35,   # max cloud for a "perfect" bluebird day
    "min_base_cm": 40,              # modeled base — stops getting stoked on snow-over-dirt

    # TIER HORIZONS (days ahead)
    "tier1_min_day": 3,
    "tier1_max_day": 7,
    "tier2_max_day": 3,

    "state_file": "state.json",
}

# If True, never actually POST (used by --selftest)
DRY_RUN = False


# ───────────────────────────────────────────────────────────────────────────
# FORECAST: fetch (live) or fake (selftest) — both return Open-Meteo-shaped JSON
# ───────────────────────────────────────────────────────────────────────────
HOURLY_VARS = [
    "snowfall", "freezing_level_height", "wind_gusts_10m",
    "wind_speed_10m", "cloud_cover", "temperature_2m",
    "snow_depth", "precipitation",
]


def fetch_forecast(cfg):
    """Live pull from Open-Meteo. snowfall=cm, snow_depth=m, gusts=km/h, fl=m."""
    base = "https://api.open-meteo.com/v1/forecast"
    q = (
        f"?latitude={cfg['latitude']}&longitude={cfg['longitude']}"
        f"&elevation={cfg['elevation_m']}"
        f"&hourly={','.join(HOURLY_VARS)}"
        f"&timezone={cfg['timezone'].replace('/', '%2F')}"
        f"&forecast_days=8&past_days=2&wind_speed_unit=kmh"
    )
    url = base + q
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        sys.exit(f"[pow] Could not reach Open-Meteo: {e}")


def mock_forecast(base_cm, injections, cfg):
    """
    Build a fake Open-Meteo response so we can test the real parse/logic path.
    `injections` maps a day-offset (e.g. +1) to dict(fresh, gust, cloud, fl),
    where `fresh` cm is dropped overnight (00:00-07:00) of that day.
    """
    tz = ZoneInfo(cfg["timezone"])
    today = datetime.now(tz).date()
    start = datetime(today.year, today.month, today.day) - timedelta(days=2)

    times, snowfall, fl, gust, wspd, cloud, temp, depth, precip = ([] for _ in range(9))
    for h in range(24 * 10):  # -2 days .. +8 days
        t = start + timedelta(hours=h)
        off = (t.date() - today).days
        inj = injections.get(off, {})
        # defaults: calm, cloudy-ish, mild, no snow
        s, g, c, f = 0.0, 20.0, 50.0, 2500.0
        if inj:
            g = inj.get("gust", g)
            c = inj.get("cloud", c)
            f = inj.get("fl", f)
            if 0 <= t.hour < 7:               # overnight dump window
                s = inj["fresh"] / 7.0
        times.append(t.strftime("%Y-%m-%dT%H:%M"))
        snowfall.append(round(s, 2))
        fl.append(f)
        gust.append(g)
        wspd.append(g * 0.6)
        cloud.append(c)
        temp.append(0.0)
        depth.append(base_cm / 100.0)        # metres
        precip.append(round(s, 2))
    return {
        "utc_offset_seconds": 43200,
        "hourly": {
            "time": times, "snowfall": snowfall, "freezing_level_height": fl,
            "wind_gusts_10m": gust, "wind_speed_10m": wspd, "cloud_cover": cloud,
            "temperature_2m": temp, "snow_depth": depth, "precipitation": precip,
        },
    }


# ───────────────────────────────────────────────────────────────────────────
# PARSE: roll hourly data into one summary per day
# ───────────────────────────────────────────────────────────────────────────
@dataclass
class DaySummary:
    d: date
    fresh_cm: float      # fresh snow in trailing window by first chair
    gust_kmh: float      # max gust during op hours
    cloud_pct: float     # mean cloud during op hours
    fl_min_m: float      # lowest freezing level during op hours
    base_cm: float       # modeled snow depth at start of day


def parse_days(data, cfg):
    H = data["hourly"]
    fmt = "%Y-%m-%dT%H:%M"
    rows = []
    for i, ts in enumerate(H["time"]):
        rows.append({
            "t": datetime.strptime(ts, fmt),
            "snow": H["snowfall"][i] or 0.0,
            "fl": H["freezing_level_height"][i],
            "gust": H["wind_gusts_10m"][i] or 0.0,
            "cloud": H["cloud_cover"][i] if H["cloud_cover"][i] is not None else 0.0,
            "depth": (H["snow_depth"][i] or 0.0) * 100.0,  # m -> cm
        })

    def snow_sum(t0, t1):
        return sum(r["snow"] for r in rows if t0 <= r["t"] < t1)

    tz = ZoneInfo(cfg["timezone"])
    today = datetime.now(tz).date()
    days = sorted({r["t"].date() for r in rows})
    out = []
    for d in days:
        if (d - today).days < 0:
            continue  # skip past days (they're only there for the trailing window)
        morning = datetime(d.year, d.month, d.day, cfg["op_start_hour"])
        op = [r for r in rows
              if r["t"].date() == d and cfg["op_start_hour"] <= r["t"].hour < cfg["op_end_hour"]]
        if not op:
            continue
        start_rows = [r for r in rows if r["t"].date() == d]
        out.append(DaySummary(
            d=d,
            fresh_cm=snow_sum(morning - timedelta(hours=cfg["fresh_window_h"]), morning),
            gust_kmh=max(r["gust"] for r in op),
            cloud_pct=sum(r["cloud"] for r in op) / len(op),
            fl_min_m=min(r["fl"] for r in op),
            base_cm=start_rows[0]["depth"],
        ))
    return out, today


# ───────────────────────────────────────────────────────────────────────────
# CLASSIFY: which tier (if any) does this day earn?
# ───────────────────────────────────────────────────────────────────────────
def classify(day, today, cfg):
    """Return (tier|None, lead_days, reason_string)."""
    lead = (day.d - today).days
    base_ok = day.base_cm >= cfg["min_base_cm"]
    snow_ok = day.fresh_cm >= cfg["fresh_snow_min_cm"]
    dry_base = day.fl_min_m <= cfg["base_elevation_m"] + cfg["freezing_level_buffer_m"]
    wind_ok = day.gust_kmh <= cfg["max_gust_kmh"]
    blue = day.cloud_pct <= cfg["bluebird_max_cloud_pct"]

    perfect = base_ok and snow_ok and dry_base and wind_ok and blue
    promising = base_ok and snow_ok and dry_base  # looser: real snow on a base

    if lead <= 1 and perfect and day.gust_kmh <= cfg["tier3_max_gust_kmh"]:
        return 3, lead, "PERFECT + calm — send it"
    if lead <= cfg["tier2_max_day"] and perfect:
        return 2, lead, "PERFECT — go-time"
    if cfg["tier1_min_day"] <= lead <= cfg["tier1_max_day"] and promising:
        extra = "" if (wind_ok and blue) else " (snow's there, wind/cloud still iffy)"
        return 1, lead, "PROMISING — watch it" + extra

    # ── not firing: say why ──
    if not base_ok:
        why = f"base too thin ({int(day.base_cm)}cm < {cfg['min_base_cm']})"
    elif not snow_ok:
        why = f"not enough fresh ({int(round(day.fresh_cm))}cm < {cfg['fresh_snow_min_cm']})"
    elif not dry_base:
        why = f"rain risk (freezing lvl {int(day.fl_min_m)}m above base)"
    elif promising and not (wind_ok and blue):
        bits = []
        if not wind_ok: bits.append(f"too windy ({int(day.gust_kmh)}km/h)")
        if not blue:    bits.append(f"too cloudy ({int(day.cloud_pct)}%)")
        why = "snow ok but " + " & ".join(bits) + " — not a go-day"
    else:
        why = "outside alert horizon"
    return None, lead, why


# ───────────────────────────────────────────────────────────────────────────
# MESSAGE: build the stoke 🤙
# ───────────────────────────────────────────────────────────────────────────
def _stats_line(day, cfg):
    fl = int(day.fl_min_m)
    if day.fl_min_m <= cfg["base_elevation_m"]:
        fl_txt = f"🧊 freezing lvl down to {fl}m (below base = all snow, no rain)"
    else:
        fl_txt = f"🧊 freezing lvl {fl}m"
    return (f"❄️ {int(round(day.fresh_cm))}cm freshies · "
            f"💨 gusts {int(round(day.gust_kmh))}km/h · {fl_txt} · "
            f"☁️ {int(round(day.cloud_pct))}% cloud · base ~{int(round(day.base_cm))}cm")


def build_message(day, tier, lead, cfg):
    when = f"{day.d:%a} {day.d.day} {day.d:%b}"
    stats = _stats_line(day, cfg)

    if tier == 3 and lead == 0:
        title = "DAWN PATROL: TUROA IS GO TODAY"
        header = "❄️🏂 DAWN PATROL — TODAY IS THE ONE 🏂❄️"
        caveat = ("Goggles on. Confirm the chairs are spinning on Pure Tūroa, "
                  "get up there before it tracks out, and shred ya face off. 🤘")
        tags, prio = "rotating_light,snowboarder,snowflake,fire", "max"
    elif tier == 3:
        title = "SEND IT: TUROA LOADED TOMORROW"
        header = "🚨🏂 SEND IT — TOMORROW'S THE ONE 🏂🚨"
        caveat = ("~90% on the forecast fam. Only thing that can cook it now is a "
                  "sneaky morning wind-hold — peep Pure Tūroa's lift status first "
                  "thing, then FULL SEND. 🤙")
        tags, prio = "rotating_light,snowboarder,snowflake,fire", "max"
    elif tier == 2:
        title = "LOCK IT IN: GO-TIME BREWING"
        header = "🔥 LOCK IT IN — GO-TIME'S BREWING 🔥"
        caveat = (f"{lead} days out and the models are locked enough to start scheming. "
                  "Sort ya crew + accom, but confirm the top chairs are actually open "
                  "on Pure Tūroa before committing the 4hr haul from Welly. 🤘")
        tags, prio = "fire,snowboarder,snowflake", "high"
    else:  # tier 1
        title = "POW RADAR: SOMETHIN'S BREWING"
        header = "👀 POW RADAR PINGING — EYES UP 👀"
        caveat = (f"Still {lead} days out so the models are wobbly — don't book a thing "
                  "yet, just keep it on ya radar. Could fizzle, could be an all-timer. "
                  "I'll ping again if it firms up. 🏂")
        tags, prio = "eyes,snowflake", "default"

    body = f"{header}\n📅 {when} @ Tūroa\n\n{stats}\n\n⚠️ {caveat}"
    return title, body, tags, prio


# ───────────────────────────────────────────────────────────────────────────
# DELIVERY + STATE
# ───────────────────────────────────────────────────────────────────────────
def _ascii(s):
    """HTTP headers are latin-1 only — strip emoji/macrons from Title & Tags."""
    return s.encode("ascii", "ignore").decode("ascii")


def post_ntfy(title, body, tags, prio, cfg):
    if DRY_RUN:
        print("─" * 60)
        print(f"TITLE: {title}\nTAGS:  {tags}  PRIORITY: {prio}\n\n{body}")
        print("─" * 60)
        return 200
    topic = os.environ.get("NTFY_TOPIC") or cfg["ntfy_topic"]
    if "CHANGE-ME" in topic:
        sys.exit("[pow] Set your ntfy topic in CONFIG (or the NTFY_TOPIC secret) first.")
    req = urllib.request.Request(f"https://ntfy.sh/{topic}",
                                 data=body.encode("utf-8"), method="POST")
    req.add_header("Title", _ascii(title))
    req.add_header("Tags", _ascii(tags))
    req.add_header("Priority", prio)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.URLError as e:
        print(f"[pow] ntfy push failed: {e}")
        return 0


def load_state(cfg):
    try:
        with open(cfg["state_file"]) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerted": {}}


def save_state(state, cfg):
    # prune keys older than 14 days so the file stays tiny
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    state["alerted"] = {k: v for k, v in state["alerted"].items() if v >= cutoff}
    with open(cfg["state_file"], "w") as f:
        json.dump(state, f, indent=2)


# ───────────────────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────────────────
def run(data, cfg, use_state=True):
    days, today = parse_days(data, cfg)
    state = load_state(cfg) if use_state else {"alerted": {}}
    fired = 0
    for day in days:
        tier, lead, _reason = classify(day, today, cfg)
        if not tier:
            continue
        key = f"{day.d.isoformat()}:T{tier}"
        if use_state and key in state["alerted"]:
            continue  # already pinged this day at this tier
        title, body, tags, prio = build_message(day, tier, lead, cfg)
        post_ntfy(title, body, tags, prio, cfg)
        state["alerted"][key] = date.today().isoformat()
        fired += 1
    if use_state:
        save_state(state, cfg)
    if fired == 0 and not DRY_RUN:
        print(f"[pow] Checked {len(days)} days — no stars aligned. Stay patient. 🤙")
    return fired


def _verdict_table(data, cfg):
    days, today = parse_days(data, cfg)
    tier_label = {3: "🚨 TIER 3 SEND", 2: "🔥 TIER 2 LOCK", 1: "👀 TIER 1 WATCH", None: "·  quiet"}
    print(f"  {'date':<11}{'+d':>3}  {'fresh':>6}{'gust':>6}{'cloud':>6}{'frzlvl':>7}{'base':>6}   verdict")
    print("  " + "-" * 86)
    for day in days:
        tier, lead, reason = classify(day, today, cfg)
        print(f"  {day.d.isoformat():<11}{lead:>3}  "
              f"{int(round(day.fresh_cm)):>5}c{int(round(day.gust_kmh)):>5}k"
              f"{int(round(day.cloud_pct)):>5}%{int(day.fl_min_m):>6}m{int(round(day.base_cm)):>5}c"
              f"   {tier_label[tier]:<16} {reason}")
    print()


def selftest():
    global DRY_RUN
    DRY_RUN = True
    cfg = dict(CONFIG)

    print("\n=========================  SELF-TEST  =========================")
    print("SCENARIO A — 60cm base already down. Storms spaced out so each")
    print("case reads clean. NOTE: 48h 'fresh' window means a storm also")
    print("counts toward the next morning — intentional (stacked freshies).\n")
    scen_a = mock_forecast(60, {
        0: {"fresh": 22, "gust": 28, "cloud": 12, "fl": 1350},   # today -> DAWN PATROL (T3)
        2: {"fresh": 16, "gust": 35, "cloud": 20, "fl": 1500},   # +2    -> LOCK IT IN (T2)
        3: {"fresh": 25, "gust": 30, "cloud": 15, "fl": 2050},   # +3    -> quiet (rain at base)
        5: {"fresh": 6,  "gust": 25, "cloud": 10, "fl": 1450},   # +5    -> quiet (not enough)
        6: {"fresh": 18, "gust": 60, "cloud": 85, "fl": 1500},   # +6    -> WATCH (windy/cloudy, T1)
    }, cfg)
    _verdict_table(scen_a, cfg)
    print("  Alerts that would actually be pushed:\n")
    run(scen_a, cfg, use_state=False)

    print("\nSCENARIO B — monster 30cm dump but only 8cm base (classic early")
    print("season). Expect TOTAL SILENCE — no sending it over rocks:\n")
    scen_b = mock_forecast(8, {1: {"fresh": 30, "gust": 22, "cloud": 8, "fl": 1300}}, cfg)
    _verdict_table(scen_b, cfg)
    n = run(scen_b, cfg, use_state=False)
    print("  ✅ Stayed quiet — no base, no party." if n == 0 else "  ⚠️ unexpected alert!")
    print("===============================================================\n")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run(fetch_forecast(CONFIG), CONFIG, use_state=True)
