#!/usr/bin/env python3
"""Watch soldout.ua (myr.com.ua) events for free seats in the centre of the front rows
and send a Telegram notification when such seats appear or change price.

Only the standard library is used, so it runs anywhere with Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

API = "https://z.soldout.com.ua"
READ = f"{API}/sold-out-widget-read-service"
BOOKING = f"{API}/sold-out-widget-booking-service"
WIDGET_KEY = "myr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://myr.widget.soldout.ua/",
    "Origin": "https://myr.widget.soldout.ua",
}
# Main-floor sector keys, in order of preference, used when the config does not name one.
DEFAULT_SECTORS = ("parter", "zal")
ROW_ID = re.compile(r"^(?P<sector>.+):row-(?P<row>\d+)$")


# --------------------------------------------------------------------------- HTTP

def http_get(url: str, *, as_json: bool = True, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body) if as_json else body
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed: {last}")


def api(path: str):
    sep = "&" if "?" in path else "?"
    return http_get(f"{path}{sep}widgetKey={WIDGET_KEY}")


def resolve_event_id(page_url: str) -> int:
    """myr.com.ua event pages embed the soldout widget; the event id is in its URL."""
    html = http_get(page_url, as_json=False)
    m = re.search(r"widget\.soldout\.ua/event/(\d+)", html)
    if not m:
        raise RuntimeError(f"no soldout event id found on {page_url}")
    return int(m.group(1))


# --------------------------------------------------------------------------- hall layout

@dataclass(frozen=True)
class Seat:
    key: str      # e.g. "parter:row-1:seat-12" -- the same key the tickets API uses
    sector: str
    row: int
    label: str
    x: float


def parse_hall_svg(svg: str) -> dict[tuple[str, int], list[Seat]]:
    """Return {(sector, row): [Seat, ...]} from the hall SVG.

    Rows are <g id="sector:row-N"> groups; each seat inside is a <g> holding a <circle>
    and a <text> label. The widget builds the seat key as "<row group id>:seat-<label>".
    """
    root = ET.fromstring(svg)
    rows: dict[tuple[str, int], list[Seat]] = {}
    for g in root.iter("{http://www.w3.org/2000/svg}g"):
        m = ROW_ID.match(g.get("id", ""))
        if not m:
            continue
        sector, row = m.group("sector"), int(m.group("row"))
        seats = rows.setdefault((sector, row), [])
        for seat_g in g.iter("{http://www.w3.org/2000/svg}g"):
            circle = seat_g.find("{http://www.w3.org/2000/svg}circle")
            text = seat_g.find("{http://www.w3.org/2000/svg}text")
            if circle is None or text is None or not (text.text or "").strip():
                continue
            label = text.text.strip()
            seats.append(Seat(f"{g.get('id')}:seat-{label}", sector, row, label, float(circle.get("cx"))))
    return rows


def central_zone(seats: list[Seat], width: int) -> list[Seat]:
    """The `width` seats closest to the middle of the row (by x coordinate).

    Using geometry rather than seat numbers keeps this correct for rows with an aisle,
    extra seats like "1a"/"1b", or curved rows.
    """
    if not seats:
        return []
    xs = [s.x for s in seats]
    mid = (min(xs) + max(xs)) / 2
    return sorted(sorted(seats, key=lambda s: abs(s.x - mid))[:width], key=lambda s: s.x)


def pick_sector(rows: dict[tuple[str, int], list[Seat]], wanted: str | None) -> str:
    sectors = {s for s, _ in rows}
    if wanted:
        if wanted not in sectors:
            raise RuntimeError(f"sector {wanted!r} not in hall, available: {sorted(sectors)}")
        return wanted
    for s in DEFAULT_SECTORS:
        if s in sectors:
            return s
    raise RuntimeError(f"cannot guess main-floor sector, set 'sector' in config; available: {sorted(sectors)}")


# --------------------------------------------------------------------------- event check

@dataclass
class EventInfo:
    event_id: int
    title: str
    city: str
    date: str
    hall: str
    url: str


@dataclass
class Hit:
    key: str
    row: int
    label: str
    price: str


def ticket_price(t: dict) -> str:
    # "price" is often null/0 in this API; the colour category carries the real price.
    cp = t.get("colorPrice") or {}
    p = cp.get("price") or t.get("price")
    try:
        return f"{float(p):.0f}"
    except (TypeError, ValueError):
        return "?"


def load_event(event_id: int, url: str) -> EventInfo:
    e = api(f"{READ}/event/client/find-one-public-event-parse-client-event-one-base-data-dto/{event_id}")

    def ua(v):
        return (v.get("valueUA") or v.get("valueEN") or "") if isinstance(v, dict) else (v or "")

    place = e.get("platformHall") or {}
    city = ua((place.get("city") or {}).get("dictionary"))
    return EventInfo(event_id, ua(e.get("name")), city, (e.get("dataBegin") or "")[:16].replace("T", " "),
                     ua(place.get("name")), url)


@dataclass
class Watch:
    """Everything about an event that does not change between polls."""
    info: EventInfo
    zone: dict[str, Seat]
    sector: str
    zone_desc: dict[int, list[str]]
    max_price: float | None
    loaded_at: float


# Event details and the hall layout are static; reload them once an hour just in case.
WATCH_TTL = 3600


def prepare_event(cfg: dict, defaults: dict, hall_cache: dict) -> Watch:
    event_id = cfg.get("event_id") or resolve_event_id(cfg["url"])
    url = cfg.get("url") or f"https://myr.widget.soldout.ua/event/{event_id}"
    info = load_event(event_id, url)

    hall_id = api(f"{READ}/hall/client/find-one-by-event/{event_id}")["id"]
    if hall_id not in hall_cache:
        hall_cache[hall_id] = parse_hall_svg(api(f"{BOOKING}/hall/client/find-first-part-hall/{hall_id}")["pattern"])
    rows = hall_cache[hall_id]

    sector = pick_sector(rows, cfg.get("sector", defaults.get("sector")))
    want_rows = cfg.get("rows", defaults.get("rows", [1, 2, 3]))
    width = int(cfg.get("center_seats", defaults.get("center_seats", 20)))
    max_price = cfg.get("max_price", defaults.get("max_price"))

    zones = {r: central_zone(rows.get((sector, r), []), width) for r in want_rows}
    zone = {s.key: s for seats in zones.values() for s in seats}
    zone_desc = {r: [s.label for s in seats] for r, seats in zones.items()}
    return Watch(info, zone, sector, zone_desc, max_price, time.time())


def check_event(w: Watch) -> list[Hit]:
    free = api(f"{READ}/event/client/online/find-all-free-tickets-by-event-id/{w.info.event_id}")
    hits = []
    for t in free:
        seat = w.zone.get((t.get("seat") or {}).get("key"))
        if not seat:
            continue
        cp = t.get("colorPrice") or {}
        if cp and cp.get("available") is False:
            continue
        price = ticket_price(t)
        if w.max_price is not None and price != "?" and float(price) > float(w.max_price):
            continue
        hits.append(Hit(seat.key, seat.row, seat.label, price))
    hits.sort(key=lambda h: (h.row, w.zone[h.key].x))
    return hits


# --------------------------------------------------------------------------- telegram

def tg_send(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        if not json.load(resp).get("ok"):
            raise RuntimeError("telegram sendMessage failed")


def tg_print_chat_ids(token: str) -> None:
    upd = http_get(f"https://api.telegram.org/bot{token}/getUpdates")
    chats = {}
    for u in upd.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        c = msg.get("chat")
        if c:
            chats[c["id"]] = c.get("title") or c.get("username") or c.get("first_name")
    if not chats:
        print("No messages yet: write anything to your bot in Telegram and run this again.")
    for cid, name in chats.items():
        print(f"chat_id={cid}  ({name})")


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(info: EventInfo, new: list[Hit], changed: list[tuple[Hit, str]], total: list[Hit]) -> str:
    lines = [f"🎟 <b>{esc(info.title)}</b>",
             f"📍 {esc(info.city + ', ' if info.city else '')}{esc(info.hall)}",
             f"🗓 {esc(info.date)}", ""]
    if new:
        lines.append("<b>Появились места в центре первых рядов:</b>")
        lines += _by_row(new)
    if changed:
        lines.append("<b>Изменилась цена:</b>")
        lines += [f"ряд {h.row}, место {esc(h.label)}: {old} → {h.price} грн" for h, old in changed]
    lines += ["", f"Всего свободно в зоне сейчас: {len(total)}", f'<a href="{esc(info.url)}">Купить билеты</a>']
    return "\n".join(lines)


def _by_row(hits: list[Hit]) -> list[str]:
    out = []
    for row in sorted({h.row for h in hits}):
        seats = ", ".join(f"{esc(h.label)} ({h.price} грн)" for h in hits if h.row == row)
        out.append(f"ряд {row}: {seats}")
    return out


# --------------------------------------------------------------------------- main loop

class Monitor:
    def __init__(self, config: dict, state_path: Path, *, dry_run: bool, verbose: bool):
        self.events = config["events"]
        self.defaults = config.get("defaults", {})
        self.state_path = state_path
        self.state = json.loads(state_path.read_text()) if state_path.exists() else {}
        self.dry_run, self.verbose = dry_run, verbose
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not dry_run and not (self.token and self.chat_id):
            sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use --dry-run)")
        self.hall_cache: dict = {}
        self.watches: dict[int, Watch] = {}  # index in config["events"] -> Watch

    def watch(self, i: int) -> Watch:
        w = self.watches.get(i)
        if w is None or time.time() - w.loaded_at > WATCH_TTL:
            w = self.watches[i] = prepare_event(self.events[i], self.defaults, self.hall_cache)
            if self.verbose:
                print(f"{w.info.city or w.info.hall} {w.info.date} (event {w.info.event_id}), sector={w.sector}")
                for r, labels in w.zone_desc.items():
                    print(f"    row {r} zone: {', '.join(labels)}")
        return w

    def poll(self, *, quiet: bool) -> int:
        """Check every event once. Returns the number of events that failed."""
        errors = 0
        dirty = False
        for i, ev in enumerate(self.events):
            try:
                w = self.watch(i)
                hits = check_event(w)
            except Exception as e:  # keep watching the other events
                errors += 1
                print(f"[{now()}] error {ev.get('url') or ev.get('event_id')}: {e}", file=sys.stderr)
                continue

            info = w.info
            seen = self.state.get(str(info.event_id), {})
            current = {h.key: h.price for h in hits}
            new = [h for h in hits if h.key not in seen]
            changed = [(h, seen[h.key]) for h in hits if h.key in seen and seen[h.key] != h.price]

            if not quiet or new or changed or current != seen:
                print(f"[{now()}] {info.city or info.hall} {info.date}: "
                      f"{len(hits)} free in zone, {len(new)} new, {len(changed)} price changes")
                for line in _by_row(hits) if self.verbose else []:
                    print(f"    free -> {line}")

            if new or changed:
                msg = format_message(info, new, changed, hits)
                if self.dry_run:
                    print("---- telegram (dry run) ----\n" + msg + "\n---------------------------")
                else:
                    try:
                        tg_send(self.token, self.chat_id, msg)
                    except Exception as e:
                        # Keep the old state so the same seats are reported on the next poll.
                        errors += 1
                        print(f"[{now()}] error telegram: {e}", file=sys.stderr)
                        continue
            # Seats that disappeared are forgotten, so they are reported again if they come back.
            if current != seen:
                self.state[str(info.event_id)] = current
                dirty = True

        if dirty or not self.state_path.exists():
            self.state_path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False))
        return errors


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--state", default="state.json", help="remembers already reported seats")
    ap.add_argument("--loop", type=float, metavar="SECONDS", help="keep running, checking every N seconds")
    ap.add_argument("--duration", type=float, metavar="SECONDS", help="with --loop: stop after this long")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending to Telegram")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the centre zone and free seats")
    ap.add_argument("--get-chat-id", action="store_true", help="print chat ids that wrote to the bot")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    args = ap.parse_args()

    if args.get_chat_id:
        tg_print_chat_ids(os.environ["TELEGRAM_BOT_TOKEN"])
        return
    if args.test_telegram:
        tg_send(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], "✅ Бот мониторинга билетов подключён")
        print("sent")
        return

    config = json.loads(Path(args.config).read_text())
    mon = Monitor(config, Path(args.state), dry_run=args.dry_run, verbose=args.verbose)
    if not args.loop:
        errors = mon.poll(quiet=False)
        sys.exit(1 if errors == len(config["events"]) else 0)

    deadline = time.time() + args.duration if args.duration else None
    failures = 0       # consecutive polls where every event failed
    last_report = 0.0
    while deadline is None or time.time() < deadline:
        # Print a status line every 10 minutes; otherwise only when something changes.
        report = time.time() - last_report > 600
        errors = mon.poll(quiet=not report)
        if report:
            last_report = time.time()
        failures = failures + 1 if errors == len(config["events"]) else 0
        # Back off when the site is down or rate-limiting us, up to 5 minutes.
        delay = min(args.loop * 2 ** failures, 300) if failures else args.loop
        # A little jitter so requests do not land on an exact beat.
        time.sleep(delay * random.uniform(0.85, 1.15))
    print(f"[{now()}] --duration reached, exiting")


if __name__ == "__main__":
    main()
