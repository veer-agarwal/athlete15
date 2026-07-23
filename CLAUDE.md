# CLAUDE.md

Project: athlete15

Project context for Claude Code. Read at the start of every session.

---

## How to work with me

I am Veer Agarwal, an ECE student at NYU Tandon. I am learning Python while building
this, and that goal carries equal weight to shipping the product.

Working style:

- Be concise and direct. No filler openings.
- No em dashes in anything written for me. Plain ASCII over decorative Unicode.
- Explain your approach before writing code, then write it. Do not lead with a wall
  of code.
- One function at a time. Do not generate whole files unprompted.
- When a concept is teachable, ask me to try first. Especially loops, comprehensions,
  error handling, decorators, async, and context managers. Give me the signature and
  docstring and let me attempt the body.
- Explain why, not just what. "A session object reuses the TCP connection across
  requests" is useful. "Added a session" is not.
- Give me something to react to rather than asking a lot of clarifying questions
  upfront. I will edit and send back revisions.
- Push back when something is a bad idea. Lay out the tradeoffs.
- Do not silently fix my code. Tell me what is wrong and let me fix it.

If I say "just write it," override the above for that task.

My background: C++ and embedded systems (ESP-32, Arduino, IMUs, flight controllers),
Linux, SLURM, some parallel computing. Python is the weaker area and the reason for
this project. Assume I understand pointers, memory, and control flow. Do not assume
I know Python idioms, packaging, or virtual environments.

---

## What this is

athlete15 is a self-hosted personal assistant. Pulls data from several sources, stores it in local
SQLite, and sends a briefing to Telegram around 6:30 AM daily. Accepts commands back
over Telegram to log training and injuries, and eventually to write to Notion and my
calendar.

Everything runs locally. No cloud LLM inference. Telegram is the only third party in
the loop and it only ever sees finished messages.

## Owner context

- Rising sophomore, NYU Tandon ECE, class of 2029.
- NCAA Division III men's volleyball at NYU. Volleyball is a SPRING sport.
- 20+ hours per week of athletic commitment on top of full time engineering coursework.
- Summer: solo structured programming at a sports performance facility.
- Fall: tri-weekly lifts, captain's practices, open gyms.
- Winter and spring: competitive season, matches, travel.
- Home is Westfield NJ, school is Brooklyn NY.

Implication: the NYU team athletics .ics feed has no useful data until roughly
December. Do not prioritize it. Facility training, injuries, and offseason work are
live right now and are logged manually.

## Target output

One Telegram message each morning containing, in order:

1. Recovery and sleep numbers (WHOOP)
2. Short narrative summary of last night's sleep
3. Today's calendar
4. Homework and assignments due (Notion)
5. Training status: yesterday's session, current load trend, active injuries
6. Weather, then a brief news roundup

It reports. It does not coach. Do not add training advice or recommendations unless
explicitly asked.

---

## Architecture

Single long running Python process. APScheduler fires the morning job. The Telegram
bot polls in the same process and handles logging commands.

```
src/
  main.py          entry point: scheduler + bot loop
  config.py        env vars and constants, no logic
  db.py            SQLite schema and query helpers
  brief.py         assembles the final message
  llm.py           Ollama calls
  notify.py        Telegram send and command handlers
  training.py      training and injury logging + load calculations
  sources/
    weather.py     Open-Meteo, no API key. REFERENCE IMPLEMENTATION.
    news.py        RSS feeds
    whoop.py       OAuth 2.0
    notion.py      internal integration token
    athletics.py   .ics parsing, build last
```

### Source module contract

Every module in `sources/` exposes one function returning a list of plain dicts, or
raises. No printing, no formatting, no Telegram. That separation is what lets a
seventh source get added without touching anything else.

```python
def fetch() -> list[dict]:
    ...
```

`training.py` is not a source. It reads and writes local data rather than calling an
external API, so it sits at the top level.

### Conventions

- Type hints on every function signature.
- Store the raw API response alongside parsed fields. When you later want a field you
  did not parse, the history will still have it.
- All timestamps stored as UTC ISO-8601. Convert to America/New_York at display time
  only.
- Repo lives at D:\athlete15 on the M.2. Paths in config.py are relative to the repo
  root, so nothing hardcodes a drive letter except BACKUP_DIR in .env.
- Secrets live in `.env`, loaded through `config.py`. Nothing else reads os.environ.
- One source failing must not kill the briefing. Wrap each fetch and degrade to
  "WHOOP unavailable" rather than crashing.

---

## Training and injury logging

The hard design constraint: logging a session must take under 15 seconds on a phone,
or the habit dies inside two weeks. Terse commands only. No forms, no multi step
prompts, no confirmation dialogs for routine logging.

Target command shapes:

```
/lift squat 5x3 225, bench 4x5 155, rpe 7
/court 90 rpe 6 open gym
/cond 30 rpe 8 bike intervals
/pain shoulder 3
/injury new right shoulder tendinitis
/injury resolve right shoulder
```

Parsing should be forgiving. Accept missing RPE, accept lbs or kg, accept "5x3" and
"5 x 3". If a command cannot be parsed, reply with what was understood and what was
not, and do not discard the message.

### Load calculation

Session load = duration_min * session_rpe. Acute load is the 7 day rolling sum,
chronic load is the 28 day rolling average scaled to 7 days. The ratio between them
describes whether load is spiking relative to baseline.

Report this as a descriptive trend only. The research on acute:chronic workload ratio
as an injury predictor is contested and it should not be presented as a warning
system. State the numbers, not a verdict.

---

## Storage and backups

The M.2 (D:) has limited free space. Ollama models are the only large artifacts and
are not needed until phase 7.

assistant.db holds training and injury history that exists nowhere else. WHOOP
backfills from its API and Notion lives in the cloud, but a logged session that was
never backed up dies with the drive. The morning job copies the database to
BACKUP_DIR on the 7 TB drive. Build that in phase 3, not later.

## Current phase

Phase 1: weather and news to console. No database, no Telegram, no LLM.

```
1  Weather + news to console
2  Telegram delivery
3  SQLite
4  Training + injury logging
5  WHOOP (backfill history on first connect)
6  Notion coursework
7  Ollama briefing layer
8  NYU team calendar (~November)
9  Q&A over history
```

Do not build ahead. Stubs for later phases exist so the structure is visible, not so
they get filled in early.
