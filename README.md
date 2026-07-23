# athlete15

Self-hosted daily briefing assistant for a student-athlete. Pulls WHOOP recovery
data, Notion coursework, training and injury logs, athletics schedules, weather and
news into one Telegram message each morning. Runs entirely on local hardware,
including LLM inference via Ollama.

## Location

Lives on `D:\athlete15` (M.2). Claude Code does constant small random reads across
the repo, so it belongs on the NVMe, not a spinning disk.

## Setup

```powershell
cd D:\athlete15
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Run:

```powershell
python -m src.main
```

Test a single source in isolation:

```powershell
python -m src.sources.weather
```

## Phases

| Phase | Status | Scope |
|-------|--------|-------|
| 1 | in progress | Weather + news to console |
| 2 | | Telegram delivery |
| 3 | | SQLite persistence + backup |
| 4 | | Training + injury logging (write path) |
| 5 | | WHOOP OAuth, with history backfill |
| 6 | | Notion coursework |
| 7 | | Ollama briefing generation |
| 8 | | NYU athletics .ics, build ~November |
| 9 | | Reply-to-bot Q&A over history |

Phase 4 is deliberately early. Training and injury data cannot be recovered
retroactively, unlike WHOOP history which backfills with one API call.

## Storage

| Item | Size | Drive |
|------|------|-------|
| Repo + venv | ~200 MB | D: (M.2) |
| assistant.db | ~3 MB per year | D: (M.2) |
| Ollama models | ~4.7 GB each | D: or archive drive |
| Database backups | trivial | 7 TB drive |

The M.2 has limited free space and NVMe write performance degrades past roughly
80 percent full. Keep one or two active models locally, archive the rest.

Redirect Ollama's model store with a system environment variable if C: is a
separate, smaller drive:

```
OLLAMA_MODELS = D:\ollama\models
```

## Backups

`assistant.db` holds training and injury history that exists nowhere else. WHOOP
backfills from the API, Notion lives in the cloud, but a session you logged and
never backed up is gone with the drive. Set `BACKUP_DIR` in `.env` to a path on the
7 TB drive and the morning job copies the database there.

## Notes

- `.env` is gitignored and holds live credentials. Never commit it.
- Raw API payloads are stored alongside parsed fields so history can be re-parsed
  if the schema changes.
