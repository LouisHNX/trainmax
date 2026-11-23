# tgvmax-alert

Web page that lists MAX JEUNE (TGVmax) seats on the routes you watch. The check runs every 6 h
(00/06/12/18h UTC) and posts each new seat as an alert on the page. **Vérifier maintenant** runs a
check right away and lists every seat currently available.

## Environment variables
| Variable | Meaning |
|---|---|
| `WEB_PASSWORD` | the page asks for it (any username, this password) |
| `DB_PATH` | SQLite file, default `./alert.db`; use `/data/alert.db` on Railway |

## Local run
```
pip install -r requirements.txt
python -m unittest discover -s tests -t .        # tests
python run.py --dry-run                           # prints the alert, touches nothing
WEB_PASSWORD=x python web.py                      # http://localhost:8000
```

## Railway (one service)
The web service owns the SQLite volume (Railway volumes can't be shared), so `web.py` also runs
`run.py` itself at 00/06/12/18h UTC. Use **one** service, not a separate cron.
1. Push this folder to a GitHub repo, then New Project > Deploy from GitHub repo.
2. Service > Settings: add a **Volume** mounted at `/data`, and Networking > **Generate Domain**.
3. Variables: `DB_PATH=/data/alert.db`, `WEB_PASSWORD`.
4. Start command is `python web.py` (from `railway.json`). Open the domain: add and delete routes, read the alerts.
   Logs show each check.

## What the sources rely on (checked 2026-11-02)
**Planner** (`sources/planner.py`): `GET .../search-freeplaces-proposals?origin=&destination=&departureDateTime=`,
one call per station pair and per day, over the next 30 days. It answers per station, so `paris` and
`lyon` are resolved to real pairs through the open data. The fixture is a real browser capture.

**Blocked from Railway:** the planner answers HTTP 403 there (seen 2026-11-02). The site uses DataDome and
the page sends `x-client-*` headers that this code does not send. The open data takes over, and the logs show it.

**Open data** (`sources/opendata.py`): refreshed about once a day, so it misses seats freed during the
day. Raw station codes (e.g. FRPLY) work on the planner only.

## Behaviour
The day before a trip, the noon UTC run posts one reminder to check the Max Planner before 17:00.

A seat that disappears and comes back alerts again. Routes are deleted once their last date has passed.
Known short station names are in `stations.py`.
