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
- Work directly on the current branch in this directory. Do not create git
  worktrees or feature branches unless I ask. I run and test from here, so
  changes made elsewhere look like they silently did nothing.
- End every message with "15"

My background: C++ and embedded systems (ESP-32, Arduino, IMUs, flight controllers),
Linux, SLURM, some parallel computing. Python is the weaker area. Assume I
understand pointers, memory, control flow, and general programming. Do not assume I
know Python idioms, packaging, or virtual environments, and name them when you use
them so I can look them up.

---

## What this is

athlete15 is a self-hosted personal assistant for a Division III student-athlete.
It pulls from health, academic, and schedule sources into local SQLite, sends a
briefing to Telegram each morning, and accepts free-text logging back over Telegram.

The long-term goal is one system covering four roles:

- **Secretary**: knows what is on today, what is due, what is coming.
- **Assistant**: takes actions on request. Creates Notion tasks, adds calendar
  events, answers questions about my own history.
- **Coach**: surfaces patterns across training load, recovery, and academic
  workload. See Scope and limits below, this one has hard boundaries.
- **Manager**: tracks longer arcs. Injury duration, training blocks, semester
  workload.

Everything runs locally. No cloud LLM inference. Telegram is the only third party
in the loop and it only ever sees finished messages.

## Owner context

- Rising sophomore, NYU Tandon ECE, class of 2029.
- NCAA Division III men's volleyball at NYU. Volleyball is a SPRING sport.
- 20+ hours per week of athletic commitment on top of full time engineering
  coursework.
- Summer: solo structured programming at a sports performance facility.
- Fall: tri-weekly lifts, captain's practices, open gyms.
- Winter and spring: competitive season, matches, travel.
- Home is Westfield NJ, school is Lower East Side / Brooklyn NY.

Implication: the NYU team athletics .ics feed has no useful data until roughly
December. Facility training and injuries are live now and logged manually.

---

## Scope and limits

The assistant surfaces patterns and asks questions. It does not prescribe.

"Your last three sessions above RPE 8 were each followed by a sub-50 recovery day"
is good output: verifiable, traceable to logged data. "Take today off" is not.

Hard rule on injuries: never advise on whether to train through pain or injury,
under any framing, including when asked directly. Injury output is limited to
reporting logged values and their trend over time. That judgment belongs to me and
my athletic trainer.

A local 8B model handed numbers will confabulate causation. It cannot distinguish
"recovery is low because of Tuesday's lift" from illness, alcohol, or bad sleep,
and it will sound equally confident either way. Design output so every claim points
at a specific logged value, never at an inferred cause.

## Target output

One Telegram message each morning containing, in order:

1. Recovery and sleep numbers (WHOOP)
2. Short narrative summary of last night's sleep
3. Today's calendar
4. Homework and assignments due (Notion)
5. Training status: yesterday's session, load trend, active injuries
6. Weather, then a brief news roundup

---

## Architecture

Two independent Windows scheduled tasks. There is no single always-on process, and
APScheduler is not used despite still being in requirements.txt.

**athlete15 brief** - daily 7:00 AM, WakeToRun enabled, StartWhenAvailable,
RunOnlyIfNetworkAvailable. Runs `python -m src.main --now` as a one-shot. Stdout
redirects to brief.log. The machine sleeps (S3) and this wakes it. There is a 30
second network delay on startup because the adapter is not up immediately on wake.

**athlete15 bot** - at logon, restart 3x on failure, no execution time limit. Runs
`python -m src.main --bot`, a blocking python-telegram-bot polling loop. Suspends
during sleep. Telegram queues updates about 24 hours, so messages sent while the
machine sleeps get processed on wake.

Schedule changes go in Task Scheduler, not config. BRIEF_HOUR and BRIEF_MINUTE in
.env are vestigial.

```
src/
  main.py          entry point: --now, --bot, --auth
  config.py        env vars and constants, no logic
  db.py            SQLite schema, query helpers, backup
  brief.py         assembles the morning message
  llm.py           Ollama calls (phase 7)
  notify.py        Telegram send + bot polling + handlers
  training.py      logging, parsing, load calculations
  sources/
    weather.py     Open-Meteo, no API key. REFERENCE IMPLEMENTATION.
    news.py        RSS feeds
    whoop.py       OAuth 2.0 via the `whoop` library
    notion.py      internal integration token (phase 6)
    athletics.py   .ics parsing (phase 8)
save.ps1           git add + commit + push
```
## Subagents

Defined in .claude/agents/. Delegate to them rather than doing their work inline.

- **reviewer** (opus, read-only) - run on the diff before every commit. Not
  optional. This is the main check on code I did not write myself.
- **debugger** (opus, read-only) - any failure, traceback, or missing 7 AM
  briefing. Diagnose before proposing a fix.
- **tester** (sonnet) - after any parser, calculation, or data transformation.
  Writes tests/ only, never source.
- **source-builder** (sonnet) - new or modified modules in src/sources/.

Do not delegate single-line edits, config changes, or anything under about
20 lines. Subagent overhead exceeds the benefit and each one carries its own
context window.

After completing any implementation task, invoke reviewer before telling me it
is done. Report what it found, including when it found nothing.

### Source module contract

Every module in `sources/` exposes functions returning lists of plain dicts, or
raises. No printing, no formatting, no Telegram. Third-party library types never
appear in the return value: `whoop.py` uses the `whoop` package internally but
returns plain dicts, so replacing the library touches one file.

`training.py` is not a source. It reads and writes local data.

### Conventions

- Type hints on every function signature.
- Store the raw API response alongside parsed fields. When you later want a field
  you did not parse, the history will still have it.
- All timestamps stored as UTC ISO-8601. Convert to America/New_York at display
  time only.
- Repo lives at D:\athlete15 on the M.2. Paths in config.py are relative to the
  repo root, so nothing hardcodes a drive letter except BACKUP_DIR in .env.
- Secrets live in `.env`, loaded through `config.py`. Nothing else reads os.environ.
- WHOOP tokens persist to whoop_token.json and are rewritten after every refresh,
  not only at initial authorization. Refresh tokens rotate, and replaying a stale
  one locks the app out weeks later.
- One source failing must not kill the briefing. Wrap each fetch and degrade to
  "WHOOP unavailable" rather than crashing.
- Gitignored: .env, whoop_token.json, *.db, brief.log, .claude/

---

## Logging design

**Workout data comes from WHOOP, never manual entry.** Sport, duration, start and
end time, heart rate zones, and strain are fetched via `fetch_workouts()`. Do not
build UI for typing any of that.

**Manual entry is limited to what WHOOP cannot know**: pain and soreness, injury
open/resolve, and session RPE. Strain is heart-rate derived and systematically
undervalues resistance training, so a heavy lift with long rests looks easy. RPE is
what makes the load math meaningful.

**Free text, not commands.** Any non-slash message to the bot is a log entry.
"low back tight 2/10" and "left shoulder was rough today, maybe a 4" both work.

**Raw first, always.** Write `raw_text` to `log_entries` BEFORE attempting to parse.
Parsing is a separate step that is allowed to fail. This is the same convention as
storing raw API payloads, applied to my own input, and it is what makes the LLM
re-parse in phase 7b possible.

```
log_entries: id, received_at, raw_text, parsed_json, parse_status, parse_method
```

`parse_entry(raw_text)` currently uses regex and keywords with
`parse_method='simple'`. The signature must allow swapping in `'llm'` later without
touching callers.

**Always echo what was understood.** On partial parse failure, save what was
extracted and say plainly what was not. Never silently discard an entry. The
confirmation reply is not overhead, it is what makes an unreliable parser
survivable.

Logging must stay under 15 seconds on a phone. If LLM parsing pushes round-trip
past a few seconds, fall back to simple parsing for the live reply and re-parse
in the background.

### Load calculation

Session load = WHOOP workout duration * my logged RPE for that date. Acute load is
the 7 day rolling sum, chronic is the 28 day rolling average scaled to 7 days.

Report the ratio as a descriptive trend only. The research on acute:chronic
workload ratio as an injury predictor is contested. State the numbers, not a
verdict.

---

## Storage and backups

C: had 0.7 GB free at one point and is still tight. D: is the M.2 with the repo.
F: is the 7 TB archive drive. TMP and TEMP redirect to D:\temp. OLLAMA_MODELS must
point at D: before phase 7 pulls a ~5 GB model.

assistant.db holds logged pain, injury, and RPE data that exists nowhere else.
WHOOP backfills from its API and Notion lives in the cloud, but a logged entry that
was never backed up dies with the drive. The morning job copies the database to
BACKUP_DIR on F: using sqlite3's backup API, not a file copy.

---

## Model constraints (phase 7)

RTX 2070 Super, 8GB VRAM. An 8-9B model at Q4_K_M fits with usable context. 12-14B
at Q4 spills and gets slow. Nothing larger runs usefully. This is a hardware
ceiling, not a configuration problem.

Exploit the asymmetry: the 7 AM briefing is unattended, so a slow model is fine
there. Interactive chat is not. Config should support two model names, a larger one
for the scheduled job and a small fast one for conversation.

For phase 9 Q&A, the data is tabular numbers, not documents. Use text-to-SQL, not
embeddings: semantic search over "recovery 41, HRV 62" loses numeric relationships.
Small models are unreliable at free-form SQL, so give the model a fixed set of
parameterized query functions to select from rather than letting it write arbitrary
queries.

---

## Current phase

Phase 6: Notion coursework.

```
1   Weather + news to console                          DONE
2   Telegram delivery + Task Scheduler wake            DONE
3   SQLite + backup to F:                              DONE
4   Free-text training and injury logging              DONE
5   WHOOP OAuth + backfill                             DONE
6   Notion coursework                                  current
7   Ollama briefing layer
7b  Re-parse all historical log_entries with the LLM
8   NYU team calendar (~November)
9   Q&A over history via text-to-SQL
10  Write actions: create Notion tasks, calendar events
11  Proactive: evening check-in, unlogged-session nudge
```

Phase 7b is the real self-improvement mechanism. Because every entry is stored raw,
the entire history can be re-parsed with a better method and the structured data
upgraded retroactively. Fine-tuning is not the answer on this hardware and would
teach writing style, not history.

Do not build ahead. Stubs for later phases exist so the structure is visible, not
so they get filled in early.

---

## Future

Not scheduled. Do not start these without being asked.

- **Wake-triggered briefing** instead of fixed 7 AM. Poll WHOOP every 15 min from
  6 AM until the sleep cycle closes. Webhooks are the alternative but need a public
  endpoint via Tailscale Funnel and a machine awake to receive. Polling is probably
  right for a single user. Decide after phase 7.

- **Nutrition data** from MyFitnessPal or GE Fit Profile. Note that WHOOP writes to
  Apple Health but the WHOOP API does not read from it, and Apple Health has no
  cloud API. Would need an iOS export app pushing files. Not currently covered by
  any existing integration.

- **Algorithmic trading experiment.** Use Alpaca's paper trading API, not Robinhood.
  Robinhood has no official public trading API and the reverse-engineered libraries
  violate terms and risk account restriction. Alpaca is official, free, built for
  this, and uses fake money. Build order management, position tracking, and
  backtesting there. A local 8B model has no market edge, so treat this as a
  systems engineering exercise, not a strategy. Real money is not on the table.