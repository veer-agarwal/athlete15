# CLAUDE.md

Project: athlete15

Project context for Claude Code. Read at the start of every session.

---

## How to work with me

I am Veer Agarwal, an ECE student at NYU Tandon.

Working style:

- Be concise and direct. No filler openings.
- No em dashes in anything written for me. Plain ASCII over decorative Unicode.
- Implement what I ask. Do not quiz me, do not ask me to write it myself, do not
  withhold code to make a teaching point. I get conceptual explanation elsewhere.
- Do explain non-obvious decisions in a sentence or two after the fact, and comment
  the code where the reasoning is not visible from reading it. Timezone handling,
  OAuth refresh, retry logic, and anything with a silent failure mode.
- Push back when something is a bad idea. Lay out the tradeoffs.
- Stay in scope. Do not refactor, rename, or "improve" code I did not ask about. If
  you spot something wrong nearby, say so and leave it alone.
- Keep changes small and single purpose so each commit is one working thing.
- End every message with "15"
- Work directly on the current branch in this directory. Do not create git
  worktrees or feature branches unless I ask. I run and test from here, so
  changes made elsewhere look like they silently did nothing.

My background: C++ and embedded systems (ESP-32, Arduino, IMUs, flight controllers),
Linux, SLURM, some parallel computing. Python is the weaker area. Assume I
understand pointers, memory, control flow, and general programming. Do not assume I
know Python idioms, packaging, or virtual environments, and name them when you use
them so I can look them up.

---

## What this is

athlete15 is a self-hosted personal assistant. Pulls data from several sources,
stores it in local SQLite, and sends a briefing to Telegram around 7:00 AM daily.
Accepts commands back over Telegram to log training and injuries, and eventually to
write to Notion and my calendar.

Everything runs locally. No cloud LLM inference. Telegram is the only third party in
the loop and it only ever sees finished messages.

## Owner context

- Rising sophomore, NYU Tandon ECE, class of 2029.
- NCAA Division III men's volleyball at NYU. Volleyball is a SPRING sport.
- 20+ hours per week of athletic commitment on top of full time engineering coursework.
- Summer: solo structured programming at a sports performance facility.
- Fall: tri-weekly lifts, captain's practices, open gyms.
- Winter and spring: competitive season, matches, travel.
- Home is Westfield NJ, school is Lower East Side / Brooklyn NY.

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

Phase 2: Telegram delivery. Weather and news sources are done and working.

```
1  Weather + news to console            DONE
2  Telegram delivery                    current
3  SQLite + backup to archive drive
4  Training + injury logging
5  WHOOP (backfill history on first connect)
6  Notion coursework
7  Ollama briefing layer
8  NYU team calendar (~November)
9  Q&A over history
```

Do not build ahead. Stubs for later phases exist so the structure is visible, not so
they get filled in early.


## Future

- Trigger the briefing on WHOOP wake detection rather than a fixed 7 AM.
  Either recovery.updated webhook via a tunnel, or poll every 15 min from
  6 AM until the cycle closes. Decide after phase 5.


- Wake-triggered briefing instead of fixed 7 AM. Poll WHOOP every 15 min
  from 6 AM until the sleep cycle closes. Webhooks are the alternative but
  need a tunnel (Tailscale Funnel) and a machine that is awake to receive.
  Decide after phase 5 is stable.

- Get Myfitnesspal and GE FITPROFILE data from either that or apple health

- maybe setup agentic trading through robinhood with a small test ammount of money

-The assistant surfaces patterns and asks questions. It does not prescribe
training decisions and never advises on whether to train through pain or
injury. Injury-related output is limited to reporting logged values and
their trend.