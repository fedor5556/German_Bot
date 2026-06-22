# German Bot 🇩🇪

A personal Telegram bot that is the single daily home for German study: spaced
repetition (SRS, built in), daily writing corrected by Gemini, lesson capture,
and a schedule-aware daily push. Built to the design in
[BOT_SPEC.md](BOT_SPEC.md) / [ROUTINE.md](ROUTINE.md) /
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md), and to the deployment standard
in [remote_host_architecture_guideLAST.md](remote_host_architecture_guideLAST.md).

It runs as a **sibling project under the Admin Hub** on the friend's always-on
Win11 PC — so it implements **only learning commands**; all ops (update, restart,
logs, backup, recreate-venv) come from the Hub.

## What works now

Phases 1–9 of the plan plus seed content (Phase 11) are implemented and tested
locally (11 unit tests green; full import/build/CRUD/SRS/backup smoke green):

| Command | What it does |
|---------|--------------|
| `/start` | Captures your chat id (push target) + shows help |
| `/review` | SRS session: due cards → up to 10 new; rate Again/Hard/Good/Easy (SM-2) |
| `/write` | Writing prompt with target words → your attempt → Gemini correction; mistakes become cards |
| `/lesson` | Paste lesson notes → Gemini extracts vocab + corrections into cards |
| `/schedule` | View/edit lesson days; moving a lesson asks **Permanent vs Just-this-week** ⭐ |
| `/today` | Today's plan (lesson / full / Sunday-review day-type) |
| `/stats` | Streak + deck size |
| `/seed` | Import the Goethe B1 starter deck (40 cards) |
| `/backup` | DM yourself a clean SQLite snapshot |
| `/cancel` | Abort the current step |

Daily pushes (Cyprus time): a morning plan tailored to the day-type and an
evening check-in; a weekly self-backup DMs the SRS DB on Sundays.

## Run it locally

1. **Python 3.11+** (developed on 3.14).
2. Copy `.env.example` → `.env` and fill in:
   - `TELEGRAM_TOKEN` (from @BotFather → `/newbot`)
   - `GEMINI_API_KEY` (from [aistudio.google.com](https://aistudio.google.com); optional — the SRS bot runs without it, you just don't get AI corrections)
   - `OWNER_TELEGRAM_ID` (your numeric id, e.g. from @userinfobot) — **the auth gate; if empty the bot refuses everyone and won't start**
3. Double-click **`COMPLETE_LAUNCH.bat`** — it self-heals the venv, installs deps,
   and launches, teeing output to `logs/german_bot.log`.
4. In Telegram, send `/start`, then `/seed`, then `/review`.

To stop: **`STOP_ALL.bat`** (surgical — only this project's `german_bot.py`).
To rebuild a broken venv: **`FIX_VENV.bat`**.

## Tests

```
venv\Scripts\python.exe -m pytest tests -q
```

Pure logic (`srs.py` SM-2, the `flows/schedule.py` day-type resolver) is unit
tested; chat flows are tested manually in Telegram.

## Layout

```
german_bot.py     entrypoint: fail-closed config, init_db, single-instance guard,
                  run_polling(drop_pending_updates=True), JobQueue registration
config.py         .env loading + owner auth gate (fail-closed) + constants
db.py             SQLite schema + CRUD + online-backup API
srs.py            SM-2 (pure, unit-tested)
gemini.py         Gemini wrapper (lazy import, retry/backoff, async)
auth.py           owner-only decorator (numeric-id gate)
scheduler.py      tz-aware daily pushes + weekly backup (re-registered each boot)
seedloader.py     Goethe B1 starter deck importer
flows/            review · writing · lesson · schedule
seed/             b1_starter.json
tests/            test_srs.py · test_schedule.py
COMPLETE_LAUNCH.bat · STOP_ALL.bat · FIX_VENV.bat · stop_processes.ps1
```

## Deployment (your domain)

Getting the project + `.env` onto the server and registering it in the Hub's
`projects.json` is done out-of-band (whole-project upload via the bus bot /
Backblaze, then a manual registry entry). The server folder + GitHub repo are
named **`GermanBot`**; entrypoint is `german_bot.py` (not `main.py`, to avoid the
transcriber collision). After the folder + `.env` exist on the server, the Hub
manages everything. Remaining server-side step (plan Phase 10): a Task Scheduler
entry running `COMPLETE_LAUNCH.bat` at startup so pushes survive reboots.
```

> One bot token, no crash-alert token — crash visibility comes from the Hub's
> `/logs`. `requirements.txt` is a clean `pip freeze`; never hand-edit it.
