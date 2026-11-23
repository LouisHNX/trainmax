"""MAX JEUNE seat alert: one run = check routes, post new seats to the web page, exit."""
import argparse
import logging
import signal
import sys
from datetime import date, datetime, timedelta, timezone

import db
from commands import HELP, Command
from models import Route, SourceError, Train, format_day
from stations import route_station

log = logging.getLogger("run")

WATCHDOG_SECONDS = 600
PLANNER_PAGE = "https://www.maxjeune-tgvinoui.sncf/sncf-connect/max-planner"
REMINDER_HOURS = range(12, 17)  # Paris time: the noon UTC run, before the 17:00 confirmation deadline


def now_paris() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris"))
    except Exception:  # tzdata missing in a slim image
        return datetime.now(timezone.utc)


# --- formatting ------------------------------------------------------------

def format_train(t: Train) -> str:
    times = f"{t.dep}→{t.arr}" if t.arr else t.dep
    seats = f"{t.seats} places MAX dispo" if t.seats and t.seats > 1 else "place MAX dispo"
    return (f"🚄 {t.origin} → {t.dest} · {format_day(t.date)} · "
            f"{times} · TGV {t.train_no.lstrip('0') or t.train_no} — {seats}")


def format_message(groups: list[tuple[Route, list[Train]]], failed: list[Route]) -> str:
    blocks = ["\n".join(format_train(t) for t in trains) for r, trains in groups if trains]
    text = "\n\n".join(blocks)
    if failed:
        warn = "⚠️ Sources indisponibles pour : " + ", ".join(f"#{r.id}" for r in failed)
        text = f"{text}\n\n{warn}" if text else warn
    return text


def format_recap(routes: list[Route], counts: dict[int, int]) -> str:
    """One line per watched route, even when nothing is available."""
    lines = ["📋 Récap des lignes surveillées :"]
    for r in routes:
        n = counts.get(r.id)
        state = "source indisponible" if n is None else (
            "0 train pour l'instant" if n == 0 else f"{n} train{'s' if n > 1 else ''} dispo")
        lines.append(f"• {route_station(r.origin)} → {route_station(r.dest)} · {r.slot()} : {state}")
    return "\n".join(lines)


def format_route_list(routes: list[Route]) -> str:
    """Routes by station pair, then week, then direction. The first route added sets the outward way."""
    pairs: dict[frozenset, list[Route]] = {}
    for r in routes:
        pairs.setdefault(frozenset((r.origin, r.dest)), []).append(r)

    def monday(r: Route) -> date:
        if r.weekday is not None:
            return date.min  # weekly routes come first, under their own title
        d = date.fromisoformat(r.date_from)
        return d - timedelta(days=d.weekday())

    blocks = []
    for group in pairs.values():
        out, back = group[0].origin, group[0].dest
        lines = [f"🔁 {route_station(out)} ↔ {route_station(back)}"]
        for week in sorted({monday(r) for r in group}):
            lines.append("\nChaque semaine" if week == date.min else f"\nSemaine du {week.day:02d}/{week.month:02d}")
            for title, origin in (("Aller", out), ("Retour", back)):
                legs = sorted((r for r in group if monday(r) == week and r.origin == origin),
                              key=lambda r: (r.weekday or 0, r.date_from, r.time_from or ""))
                if legs:
                    lines.append(f"{title} :")
                    lines.extend(f"  {r.slot()}" for r in legs)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_reminder(routes: list[Route], day: date) -> str:
    lines = [f"⏰ Vérifie le Max Planner avant 17h pour demain ({format_day(day.isoformat())}) :"]
    for r in sorted(routes, key=lambda r: r.time_from or ""):
        window = f" · {r.time_from}-{r.time_to}" if r.time_from else ""
        lines.append(f"{route_station(r.origin)} → {route_station(r.dest)}{window}")
    lines.append(PLANNER_PAGE)
    return "\n".join(lines)


def send_reminder(conn, post, today: date, hour: int) -> None:
    """At most one message a day, listing the routes that have a trip tomorrow."""
    if hour not in REMINDER_HOURS or db.get_reminder_day(conn) == today.isoformat():
        return
    tomorrow = today + timedelta(days=1)
    routes = [r for r in db.list_routes(conn) if r.covers(tomorrow.isoformat())]
    if routes:
        post(format_reminder(routes, tomorrow))
        db.set_reminder_day(conn, today.isoformat())


# --- commands --------------------------------------------------------------

def apply_command(conn, cmd: Command) -> tuple[str, bool]:
    """Apply one command. Returns (reply text, wants_full_availability)."""
    if cmd.name == "help":
        return HELP, False
    if cmd.name == "list":
        routes = db.list_routes(conn)
        if not routes:
            return "Aucune route surveillée. Ajoute-en une avec /add.", False
        return "Routes surveillées :\n\n" + format_route_list(routes), False
    if cmd.name == "del":
        ok = db.delete_route(conn, cmd.route_id)
        return (f"Route #{cmd.route_id} supprimée." if ok else f"Route #{cmd.route_id} introuvable."), False
    if cmd.name == "now":
        return "OK, je vérifie tout de suite et j'envoie les dispos actuelles.", True
    if cmd.name == "add":
        r = db.add_route(conn, cmd.origin, cmd.dest, cmd.date_from, cmd.date_to,
                         cmd.time_from, cmd.time_to, cmd.weekday)
        return f"Route ajoutée : {r.label()}", False
    return HELP, False


# --- checking routes -------------------------------------------------------

def nkey(t: Train) -> tuple[str, str]:
    return (t.date, t.train_no.lstrip("0") or t.train_no)


def check_route(route: Route, sources, down: set[str]) -> list[Train] | None:
    """Try each source in order. Returns accepted trains, or None if all failed."""
    for name, fetch in sources:
        if name in down:
            continue
        try:
            trains = fetch(route)
        except SourceError as e:
            log.warning("source %s failed for route #%s: %s", name, route.id, e)
            down.add(name)  # don't hammer a blocked source for the other routes
            continue
        except Exception:
            log.exception("source %s crashed for route #%s", name, route.id)
            down.add(name)
            continue
        log.info("route #%s answered by source %s (%d trains)", route.id, name, len(trains))
        seen, out = set(), []
        for t in sorted(trains, key=lambda t: (t.date, t.dep)):
            if route.accepts(t) and nkey(t) not in seen:
                seen.add(nkey(t))
                out.append(t)
        return out
    return None


def run(conn, post, sources, today: date, persist: bool = True,
        want_now: bool = False, hour: int | None = None) -> None:
    """`post(text)` publishes one message; want_now lists every seat, not only the new ones."""
    if persist:
        purged = db.purge_expired(conn, today.isoformat())
        if purged:
            log.info("purged expired routes: %s", purged)
        if hour is not None:
            send_reminder(conn, post, today, hour)

    down: set[str] = set()
    groups, failed, current = [], [], {}
    routes = db.list_routes(conn)
    for route in routes:
        avail = check_route(route, sources, down)
        if avail is None:
            failed.append(route)
            continue
        current[route.id] = {nkey(t) for t in avail}
        already = db.get_notified(conn, route.id)
        shown = avail if want_now else [t for t in avail if nkey(t) not in already]
        if shown:
            groups.append((route, shown))

    text = format_message(groups, failed)
    if want_now and not groups:
        text = "Aucune place MAX dispo pour le moment." + (f"\n\n{text}" if text else "")
    elif want_now:
        text = "Disponibilités actuelles :\n\n" + text

    if text:
        text += "\n\n" + format_recap(routes, {rid: len(k) for rid, k in current.items()})
        post(text)

    if persist:
        for rid, keys in current.items():
            db.replace_notified(conn, rid, keys)


# --- entry point -----------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print instead of posting; state is not modified")
    ap.add_argument("--now", action="store_true", help="post every available seat, not only the new ones")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_: sys.exit("watchdog: run took too long"))
        signal.alarm(WATCHDOG_SECONDS)

    from sources import opendata, planner
    sources = [("planner", planner.fetch_available), ("opendata", opendata.fetch_available)]

    conn = db.connect()
    try:
        now = now_paris()
        post = print if args.dry_run else (lambda text: db.add_message(conn, text))
        run(conn, post, sources, now.date(), persist=not args.dry_run, want_now=args.now, hour=now.hour)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
