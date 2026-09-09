#!/usr/bin/env python3
"""US Open good-seat watcher.

Finds listings that are BOTH under your price AND in a seat tier you'd
actually sit in, then pushes them to your phone when they appear or drop.

The point: at Arthur Ashe, $200 only ever buys Promenade (top of a 24,000
seat stadium). At Louis Armstrong, $200 buys Courtside Reserved. This
watches tiers, not just prices.

  ./watch.py            one check (what launchd runs)
  ./watch.py --scan     what qualifies RIGHT NOW, best first
  ./watch.py --tiers    seat tiers + prices for every session
  ./watch.py --history  best qualifying price over time
  ./watch.py --test     test notifications
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "prices.db")
CFG_PATH = os.path.join(HERE, "config.json")

PERFORMER_ID = "58b9c673f80c414a31f2b13b"
EVENTS_API = "https://mobile.gametime.co/v1/events"
LISTINGS_API = "https://mobile.gametime.co/v1/listings"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def load_config(path=CFG_PATH):
    with open(path) as f:
        cfg = json.load(f)
    cfg["_path"] = path
    return cfg


def db_path_for(cfg):
    return os.path.join(HERE, cfg["db"]) if cfg.get("db") else DB_PATH


def dollars(cfg, key, default=None):
    v = cfg.get(key, default)
    return None if v is None else int(round(float(v) * 100))


def api(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def venue_of(name):
    """Gametime folds the venue into the event name."""
    n = name.lower()
    if "louis armstrong" in n:
        return "Louis Armstrong"
    if "grandstand" in n:
        return "Grandstand"
    if "grounds admission" in n or "grounds pass" in n:
        return "Grounds"
    return "Arthur Ashe"


def fetch_events():
    url = f"{EVENTS_API}?{urllib.parse.urlencode({'performer_id': PERFORMER_ID, 'per_page': 200})}"
    out = []
    for w in api(url).get("events", []):
        e = w.get("event", w)
        cents = (e.get("min_price") or {}).get("total") or 0
        if not cents:
            continue
        name = (e.get("name") or "").strip()
        out.append({
            "id": e["id"], "name": name, "venue": venue_of(name),
            "round": ((e.get("banner") or {}).get("headline") or "").strip(),
            "when": e.get("datetime_local") or "", "floor": int(cents),
            "url": e.get("seo_url") or "",
        })
    out.sort(key=lambda x: x["when"])
    return out


def fetch_listings(event_id):
    url = f"{LISTINGS_API}?{urllib.parse.urlencode({'event_id': event_id})}"
    return api(url).get("listings", []) or []


def qualifying(listings, ev, cfg, cap=True):
    """Listings in an acceptable tier, buyable at your party size, under budget.

    cap=False skips the budget test: the cheapest buyable seat regardless of
    price, which is what you want logged while you wait for it to fall.
    """
    max_cents = dollars(cfg, "max_price") if cap else None
    party = int(cfg.get("party_size", 1))
    allowed = cfg.get("tiers", {}).get(ev["venue"])
    if allowed is None:
        return []

    hits = []
    for l in listings:
        price = (l.get("price") or {}).get("total") or 0
        tier = l.get("section_group") or "?"
        if not price or (max_cents is not None and price > max_cents):
            continue
        if allowed != "*" and tier not in allowed:
            continue
        # `lots` is the set of purchasable quantities, not a max.
        lots = l.get("lots") or []
        if party not in lots:
            continue
        hits.append({
            "cents": price, "tier": tier, "section": l.get("section", "?"),
            "row": l.get("row", "?"), "lots": lots,
            "photo": l.get("view_url") or "",
        })
    hits.sort(key=lambda h: h["cents"])
    return hits


def open_db(path=DB_PATH):
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS event (
      id TEXT PRIMARY KEY, name TEXT, round TEXT, when_local TEXT, url TEXT);
    CREATE TABLE IF NOT EXISTS price (
      event_id TEXT, ts INTEGER, cents INTEGER);
    CREATE INDEX IF NOT EXISTS price_ev ON price(event_id, ts);
    CREATE TABLE IF NOT EXISTS alerted (
      event_id TEXT PRIMARY KEY, cents INTEGER, ts INTEGER, key TEXT);
    CREATE TABLE IF NOT EXISTS seat (
      event_id TEXT, ts INTEGER, cents INTEGER, tier TEXT, section TEXT, row TEXT);
    CREATE INDEX IF NOT EXISTS seat_ev ON seat(event_id, ts);
    CREATE TABLE IF NOT EXISTS floor (
      event_id TEXT, ts INTEGER, cents INTEGER, tier TEXT, section TEXT, row TEXT);
    CREATE INDEX IF NOT EXISTS floor_ev ON floor(event_id, ts);
    """)
    try:
        db.execute("ALTER TABLE alerted ADD COLUMN key TEXT")
    except sqlite3.OperationalError:
        pass  # column already there
    return db


def money(c):
    return f"${c / 100:,.0f}"


def notify(cfg, title, body, url=None, urgent=False, photo=None):
    ok = False
    topic = cfg.get("ntfy_topic")
    if topic:
        try:
            h = {"Title": title, "Priority": "urgent" if urgent else "high",
                 "Tags": "rotating_light,tennis" if urgent else "tennis"}
            if url:
                h["Click"] = url      # tap -> the buy page, not the seat photo
            if photo:
                h["Attach"] = photo   # seat view shows inline in the push
            urllib.request.urlopen(urllib.request.Request(
                f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=h), timeout=15).read()
            ok = True
        except Exception as ex:
            print(f"  ntfy failed: {ex}", file=sys.stderr)
    to = cfg.get("imessage_to")
    if to:
        # iMessage to yourself from the Mac: lands on the phone with nothing
        # installed there. Messages must be signed in on this Mac.
        text = title + "\n" + body + ("\n" + url if url else "")
        script = (
            'tell application "Messages"\n'
            '  set acct to 1st account whose service type = iMessage\n'
            f'  send {json.dumps(text, ensure_ascii=False)} to participant {json.dumps(to)} of acct\n'
            'end tell')
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                ok = True
            else:
                print(f"  imessage failed: {r.stderr.strip()}", file=sys.stderr)
        except Exception as ex:
            print(f"  imessage failed: {ex}", file=sys.stderr)
    if cfg.get("macos_notification", True):
        try:
            b = body.replace('"', "'").replace("\\", "")
            t = title.replace('"', "'").replace("\\", "")
            subprocess.run(["osascript", "-e",
                            f'display notification "{b}" with title "{t}" sound name "Glass"'],
                           check=False, capture_output=True, timeout=10)
            ok = True
        except Exception:
            pass
    return ok


def candidates(events, cfg):
    """Only pull listings where a qualifying seat is even possible.

    The cheap events call gives the floor price across all tiers, so if that
    floor is already over budget nothing in the event can qualify.
    """
    max_cents = dollars(cfg, "max_price")
    tiers = cfg.get("tiers", {})
    only = set(cfg.get("event_ids") or [])
    if only:
        # Pinned sessions are always checked, even while their floor is over
        # cap: the point of pinning is to log the trend and catch the drop.
        return [e for e in events if e["id"] in only and e["venue"] in tiers]
    return [e for e in events if e["venue"] in tiers and e["floor"] <= max_cents]


def cmd_run(cfg):
    now = int(time.time())
    stop = cfg.get("stop_after")
    if stop and time.strftime("%Y-%m-%dT%H:%M") >= stop:
        print(f"[{time.strftime('%Y-%m-%d %H:%M')}] past stop_after {stop}, nothing to do")
        return 0
    try:
        events = fetch_events()
    except Exception as ex:
        print(f"events fetch failed: {ex}", file=sys.stderr)
        return 1

    cands = candidates(events, cfg)
    db = open_db(db_path_for(cfg))
    max_cents = dollars(cfg, "max_price")
    instant = dollars(cfg, "instant_buy_price")
    min_drop = dollars(cfg, "min_drop_dollars", 10)
    pinned = bool(cfg.get("event_ids"))
    alerts, floors, checked = [], [], 0

    for ev in cands:
        try:
            buyable = qualifying(fetch_listings(ev["id"]), ev, cfg, cap=False)
        except Exception as ex:
            print(f"  listings failed for {ev['name']}: {ex}", file=sys.stderr)
            continue
        checked += 1
        time.sleep(0.4)
        db.execute("INSERT OR REPLACE INTO event(id,name,round,when_local,url) VALUES(?,?,?,?,?)",
                   (ev["id"], ev["name"], ev["round"], ev["when"], ev["url"]))
        if buyable:
            f = buyable[0]
            db.execute("INSERT INTO floor(event_id,ts,cents,tier,section,row) VALUES(?,?,?,?,?,?)",
                       (ev["id"], now, f["cents"], f["tier"], f["section"], f["row"]))
            floors.append((ev, f, len(buyable)))
        hits = [h for h in buyable if h["cents"] <= max_cents]
        if not hits:
            continue
        best = hits[0]
        db.execute("INSERT INTO seat(event_id,ts,cents,tier,section,row) VALUES(?,?,?,?,?,?)",
                   (ev["id"], now, best["cents"], best["tier"], best["section"], best["row"]))

        prev = db.execute("SELECT cents, key FROM alerted WHERE event_id=?", (ev["id"],)).fetchone()
        last, last_key = (prev[0], prev[1]) if prev else (None, None)
        key = f"{best['section']}|{best['row']}"
        urgent = instant is not None and best["cents"] <= instant
        # Crossing the instant-buy line always pages, even on a tiny drop.
        crossed = urgent and (last is None or last > instant)
        # A different listing at the top means the last one sold: page again,
        # since cheap pairs on the day go in minutes.
        fresh = prev is not None and key != last_key  # None key = alerted before keys existed
        if last is not None and best["cents"] > last - min_drop and not crossed and not fresh:
            continue
        alerts.append((ev, best, len(hits), last, urgent, fresh))

    db.commit()
    print(f"[{time.strftime('%Y-%m-%d %H:%M')}] {len(cands)} candidate sessions, "
          f"{checked} checked, {len(alerts)} alert(s)")
    if pinned:
        for ev, f, n in floors:
            print(f"  floor {money(f['cents'])} {f['tier']} sec {f['section']} row {f['row']} "
                  f"({n} buyable) - {ev['venue']} {ev['when'][5:16].replace('T', ' ')}")

    for ev, best, n, last, urgent, fresh in alerts:
        when = ev["when"][:16].replace("T", " ")
        change = ("new" if last is None
                  else "new listing" if fresh
                  else f"was {money(last)}, now {money(best['cents'])}")
        body = (f"{money(best['cents'])} - {best['tier']}, sec {best['section']} row {best['row']}\n"
                f"{ev['venue']} - {when}\n{ev['round']}\n"
                f"{n} seat(s) under your cap - {change}")
        title = f"{money(best['cents'])} {ev['venue']} {best['tier']}"
        if urgent:
            title = "BUY NOW " + title
        print("  ALERT " + body.replace("\n", " | "))
        notify(cfg, title, body, ev["url"], urgent=urgent, photo=best["photo"] or None)
        db.execute("INSERT OR REPLACE INTO alerted(event_id,cents,ts,key) VALUES(?,?,?,?)",
                   (ev["id"], best["cents"], now, f"{best['section']}|{best['row']}"))
    db.commit()
    db.close()
    return 0


def cmd_scan(cfg):
    events = fetch_events()
    cands = candidates(events, cfg)
    print(f"Budget {money(int(cfg['max_price']*100))}, party of {cfg.get('party_size',1)}. "
          f"{len(cands)} sessions could qualify.\n")
    rows = []
    for ev in cands:
        try:
            hits = qualifying(fetch_listings(ev["id"]), ev, cfg)
        except Exception:
            continue
        time.sleep(0.4)
        if hits:
            rows.append((hits[0], ev, len(hits)))
    if not rows:
        print("Nothing qualifies right now.")
        return
    rows.sort(key=lambda r: r[0]["cents"])
    print(f"{'price':>7}  {'venue':<16} {'tier':<19} {'sec':<6} {'row':<4} {'n':>3}  when")
    print("-" * 92)
    for best, ev, n in rows:
        print(f"{money(best['cents']):>7}  {ev['venue']:<16} {best['tier']:<19} "
              f"{best['section']:<6} {best['row']:<4} {n:>3}  "
              f"{ev['when'][:16].replace('T',' ')}  {ev['round']}")


def cmd_tiers(cfg):
    from collections import defaultdict
    for ev in fetch_events():
        if ev["venue"] == "Grounds":
            continue
        try:
            ls = fetch_listings(ev["id"])
        except Exception:
            continue
        time.sleep(0.4)
        if not ls:
            continue
        g = defaultdict(list)
        for l in ls:
            g[l.get("section_group") or "?"].append((l.get("price") or {}).get("total") or 0)
        cap = int(cfg["max_price"] * 100)
        print(f"\n{ev['name']}  ({ev['when'][:10]}, {len(ls)} listings)")
        for k, ps in sorted(g.items(), key=lambda x: min(x[1])):
            ps = sorted(p for p in ps if p)
            print(f"   {k:<20} n={len(ps):<4} from {money(ps[0]):>7}   "
                  f"under cap: {sum(1 for p in ps if p <= cap)}")


def cmd_history(cfg):
    db = open_db(db_path_for(cfg))
    # Pinned mode logs the floor every run; otherwise only qualifying seats.
    table = "floor" if cfg.get("event_ids") else "seat"
    rows = db.execute(f"""SELECT COALESCE(e.name, s.event_id), s.ts, s.cents, s.tier, s.section
                          FROM {table} s LEFT JOIN event e ON e.id=s.event_id
                          ORDER BY e.when_local, s.ts""").fetchall()
    if not rows:
        print("No history yet. It builds one row per session per run.")
        return
    cur = None
    for name, ts, cents, tier, sec in rows:
        if name != cur:
            print(f"\n{name}")
            cur = name
        print(f"   {time.strftime('%m-%d %H:%M', time.localtime(ts))}  "
              f"{money(cents):>7}  {tier} {sec}")


def main():
    ap = argparse.ArgumentParser(description="US Open good-seat watcher")
    ap.add_argument("--scan", action="store_true", help="what qualifies right now")
    ap.add_argument("--tiers", action="store_true", help="seat tiers per session")
    ap.add_argument("--history", action="store_true", help="best qualifying price over time")
    ap.add_argument("--test", action="store_true", help="test notification")
    ap.add_argument("--config", default=CFG_PATH, help="config file (default config.json)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.test:
        print("sent" if notify(cfg, "US Open watcher", "Test. Wiring works.") else "FAILED")
        return 0
    if a.scan:
        return cmd_scan(cfg)
    if a.tiers:
        return cmd_tiers(cfg)
    if a.history:
        return cmd_history(cfg)
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main() or 0)
