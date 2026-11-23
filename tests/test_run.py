import unittest
from datetime import date

import db
import run
from models import SourceError, Train
from commands import parse_command

TODAY = date(2026, 11, 2)


class Inbox(list):
    """Collects posted messages; `post` is what run() calls."""

    def post(self, text):
        self.append(text)


def train(no="6603", day="2026-11-15", dep="07:56", arr="09:52"):
    return Train(date=day, dep=dep, arr=arr, train_no=no, origin="Paris Gare de Lyon", dest="Lyon Part-Dieu")


class FakeSource:
    def __init__(self, trains=None, error=None):
        self.trains = trains or []
        self.error = error
        self.calls = 0

    def __call__(self, route):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.trains)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.route = db.add_route(self.conn, "paris", "lyon", "2026-11-15", "2026-11-16")

    def go(self, inbox, *sources, **kw):
        run.run(self.conn, inbox.post, list(sources) or [("p", FakeSource())], TODAY, **kw)

    def test_alert_once_then_silent(self):
        src = ("p", FakeSource([train()]))
        tg = Inbox()
        self.go(tg, src)
        self.assertEqual(len(tg), 1)
        self.assertTrue(tg[0].startswith(
            "🚄 Paris Gare de Lyon → Lyon Part-Dieu · dimanche 15/11 · 07:56→09:52 · TGV 6603 — place MAX dispo\n\n📋"))
        self.go(tg, src)
        self.assertEqual(len(tg), 1)  # nothing new

    def test_disappear_then_return_realerts(self):
        s = FakeSource([train()])
        tg = Inbox()
        self.go(tg, ("p", s))
        s.trains = []
        self.go(tg, ("p", s))
        self.assertEqual(len(tg), 1)
        s.trains = [train()]
        self.go(tg, ("p", s))
        self.assertEqual(len(tg), 2)

    def test_only_new_trains_listed(self):
        s = FakeSource([train("1", dep="08:00")])
        tg = Inbox()
        self.go(tg, ("p", s))
        s.trains = [train("1", dep="08:00"), train("2", dep="09:00")]
        self.go(tg, ("p", s))
        self.assertIn("TGV 2", tg[1])
        self.assertNotIn("TGV 1", tg[1])

    def test_route_filters_applied(self):
        db.delete_route(self.conn, self.route.id)
        db.add_route(self.conn, "paris", "lyon", "2026-11-15", "2026-11-15", "08:00", "12:00")
        s = FakeSource([train("1", dep="07:00"), train("2", dep="09:00"), train("3", day="2026-11-16", dep="09:00")])
        tg = Inbox()
        self.go(tg, ("p", s))
        self.assertIn("TGV 2", tg[0])
        self.assertNotIn("TGV 1", tg[0])
        self.assertNotIn("TGV 3", tg[0])

    def test_grouped_by_route(self):
        db.add_route(self.conn, "lille", "nantes", "2026-11-15", "2026-11-15")
        tg = Inbox()
        self.go(tg, ("p", FakeSource([train()])))
        self.assertEqual(len(tg), 1)  # ONE message
        self.assertEqual(len(tg[0].split("\n\n")), 3)  # train block, other route has none, recap

    def test_recap_lists_every_route_with_zero(self):
        db.add_route(self.conn, "lille", "nantes", "2026-11-15", "2026-11-15", "20:00", "22:00")
        tg = Inbox()
        self.go(tg, ("p", FakeSource([train()])))
        recap = tg[0].split("📋")[1]
        self.assertIn("1 train dispo", recap)
        self.assertIn("0 train pour l'instant", recap)

    def test_fallback_on_source_error(self):
        bad, good = FakeSource(error=SourceError("403")), FakeSource([train()])
        tg = Inbox()
        db.add_route(self.conn, "lille", "nantes", "2026-11-15", "2026-11-15")
        self.go(tg, ("planner", bad), ("opendata", good))
        self.assertEqual(len(tg), 1)
        self.assertEqual(bad.calls, 1)   # circuit breaker: not retried for the 2nd route
        self.assertEqual(good.calls, 2)

    def test_all_sources_fail_keeps_notified(self):
        s = FakeSource([train()])
        tg = Inbox()
        self.go(tg, ("p", s))
        before = db.get_notified(self.conn, self.route.id)
        self.go(tg, ("p", FakeSource(error=SourceError("down"))))
        self.assertEqual(db.get_notified(self.conn, self.route.id), before)
        self.assertIn("⚠️", tg[1])
        self.go(tg, ("p", s))
        self.assertEqual(len(tg), 2)  # no re-alert after the outage

    def test_now_sends_everything(self):
        s = ("p", FakeSource([train()]))
        tg = Inbox()
        self.go(tg, s)
        self.go(tg, s, want_now=True)
        self.assertEqual(len(tg), 2)  # alert, then full availability again
        self.assertIn("TGV 6603", tg[1])

    def test_now_with_nothing(self):
        tg = Inbox()
        self.go(tg, ("p", FakeSource([])), want_now=True)
        self.assertIn("Aucune place", tg[-1])

    def test_expired_routes_purged(self):
        old = db.add_route(self.conn, "lille", "nantes", "2026-10-01", "2026-11-01")
        src = FakeSource([])
        self.go(Inbox(), ("p", src))
        self.assertEqual([r.id for r in db.list_routes(self.conn)], [self.route.id])
        self.assertEqual(src.calls, 1)
        self.assertNotIn(old.id, [r.id for r in db.list_routes(self.conn)])

    def test_route_list_groups_round_trips(self):
        conn = db.connect(":memory:")
        db.add_route(conn, "montpellier", "lyon", "2026-11-05", "2026-11-05", "04:00", "08:00")
        db.add_route(conn, "montpellier", "lyon", "2026-11-04", "2026-11-04", "17:00", "20:00")
        db.add_route(conn, "lyon", "montpellier", "2026-11-05", "2026-11-05", "17:00", "22:00")
        db.add_route(conn, "montpellier", "lyon", "2026-11-12", "2026-11-12")
        self.assertEqual(run.format_route_list(db.list_routes(conn)), "\n".join([
            "🔁 Montpellier Saint-Roch ↔ Lyon",
            "",
            "Semaine du 02/11",
            "Aller :",
            "  #2 · mercredi 04/11 · 17:00-20:00",
            "  #1 · jeudi 05/11 · 04:00-08:00",
            "Retour :",
            "  #3 · jeudi 05/11 · 17:00-22:00",
            "",
            "Semaine du 09/11",
            "Aller :",
            "  #4 · jeudi 12/11",
        ]))
        conn.close()

    def test_weekly_route_from_command_filters_and_lists(self):
        tg = Inbox()
        s = FakeSource([train("1", day="2026-11-12", dep="06:29"), train("2", day="2026-11-11", dep="06:29")])
        db.delete_route(self.conn, self.route.id)
        reply, _ = run.apply_command(self.conn, parse_command("/add montpellier lyon jeudi 04:00-08:00", TODAY))
        self.assertIn("chaque jeudi", reply)
        self.assertIn("Chaque semaine", run.apply_command(self.conn, parse_command("/list", TODAY))[0])
        self.go(tg, ("p", s))
        self.assertIn("TGV 1", tg[0])
        self.assertNotIn("TGV 2", tg[0])

    def test_route_list_orders_weekly_routes_by_day(self):
        conn = db.connect(":memory:")
        db.add_route(conn, "lyon", "montpellier", "2026-11-02", "2099-12-31", "17:00", "22:00", weekday=3)
        db.add_route(conn, "lyon", "montpellier", "2026-11-02", "2099-12-31", "11:00", "14:00", weekday=4)
        db.add_route(conn, "lyon", "montpellier", "2026-11-02", "2099-12-31", "04:00", "08:00", weekday=3)
        text = run.format_route_list(db.list_routes(conn))
        self.assertLess(text.index("#3 · chaque jeudi"), text.index("#1 · chaque jeudi"))
        self.assertLess(text.index("#1 · chaque jeudi"), text.index("#2 · chaque vendredi"))
        conn.close()

    def weekly_trips(self):
        db.delete_route(self.conn, self.route.id)
        db.add_route(self.conn, "montpellier", "lyon", "2026-11-02", "2099-12-31", "17:00", "20:00", weekday=2)
        db.add_route(self.conn, "lyon", "montpellier", "2026-11-02", "2099-12-31", "11:00", "14:00", weekday=4)

    def test_reminder_the_day_before_only_once(self):
        self.weekly_trips()
        tg = Inbox()
        tuesday = date(2026, 11, 3)
        run.run(self.conn, tg.post, [("p", FakeSource())], tuesday, hour=13)
        self.assertEqual(len(tg), 1)
        self.assertEqual(tg[0].split("\n"), [
            "⏰ Vérifie le Max Planner avant 17h pour demain (mercredi 04/11) :",
            "Montpellier Saint-Roch → Lyon · 17:00-20:00",
            run.PLANNER_PAGE,
        ])
        run.run(self.conn, tg.post, [("p", FakeSource())], tuesday, hour=14)
        self.assertEqual(len(tg), 1)

    def test_no_reminder_outside_hours_or_without_trip(self):
        self.weekly_trips()
        tg = Inbox()
        run.run(self.conn, tg.post, [("p", FakeSource())], date(2026, 11, 3), hour=19)
        run.run(self.conn, tg.post, [("p", FakeSource())], date(2026, 11, 2), hour=13)  # nothing on Tuesday
        self.assertEqual(tg, [])

    def test_dry_run_mode_does_not_persist(self):
        self.go(Inbox(), ("p", FakeSource([train()])), persist=False)
        self.assertEqual(db.get_notified(self.conn, self.route.id), set())


if __name__ == "__main__":
    unittest.main()
