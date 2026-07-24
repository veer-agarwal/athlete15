"""Create the coursework database in Notion. One time setup for phase 6.

Run once:  python scripts/create_notion_db.py

Prints the new database id. Put it in .env as NOTION_TASKS_DB_ID.

Before running:
    1. NOTION_TOKEN and NOTION_PARENT_PAGE_ID must be set in .env.
    2. The parent page must be shared with the integration. Open the page in
       Notion, ... menu -> Connections -> add your integration. The token alone
       grants access to nothing, and the API reports a missing share as a 404
       rather than a permission error, which reads like a wrong page id.

A NOTE ON THE STATUS PROPERTY: `status` was read-only in this API for years,
creatable only in the Notion UI. Notion lifted that in March 2026, and the run
that created the live database got a real status property with exactly the three
options asked for.

This passes an empty `{"status": {}}`, which takes Notion's defaults. Those
defaults are Not started / In progress / Done, which is precisely the spec, so
there was nothing to customize. Custom option names are supported too, but each
option then needs a `group` of To-do, In progress, or Complete. Groups themselves
are still UI only: they cannot be renamed or reordered through the API.

The select fallback below stays as insurance, since this is the part of the
schema most likely to start rejecting again. If it ever fires, the script says so
and the only loss is the grouped board UI; querying is identical either way.

This is a setup script, not part of the running assistant. It lives in scripts/
because it runs once by hand and nothing imports it.
"""

import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Running a file inside scripts/ puts scripts/ on the module search path
# (sys.path), not the repo root, so "from src import config" would not resolve.
# Adding the repo root explicitly makes `python scripts/create_notion_db.py`
# work from anywhere without needing to be run as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402  (import must follow the sys.path line above)

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

DATABASE_TITLE = "Coursework"

STATUS_OPTIONS = ("Not started", "In progress", "Done")
TYPE_OPTIONS = ("pset", "exam", "lab", "reading", "project", "quiz")

# Course has no options in the spec, but a select whose options do not exist yet
# is a coin flip on whether the API creates them implicitly. Seeding the ones the
# test rows use removes that question. Adding more later in the Notion UI is
# free.
COURSE_OPTIONS = (
    "ECE-UY 2004",
    "ECE-UY 2233",
    "MA-UY 2034",
    "PH-UY 2033",
    "EXPOS-UA 1",
)

# Due dates are spread over the next 14 days so a "due within a week" query has
# both hits and misses to work against.
TEST_ROWS = (
    {"name": "Pset 3, op-amp circuits", "course": "ECE-UY 2004",
     "days": 1, "type": "pset", "status": "In progress"},
    {"name": "Lab 2 writeup, sampling", "course": "ECE-UY 2233",
     "days": 4, "type": "lab", "status": "Not started"},
    {"name": "Linear algebra midterm", "course": "MA-UY 2034",
     "days": 6, "type": "exam", "status": "Not started"},
    {"name": "Reading response, week 3", "course": "EXPOS-UA 1",
     "days": 9, "type": "reading", "status": "Done"},
    {"name": "Final project proposal", "course": "PH-UY 2033",
     "days": 13, "type": "project", "status": "In progress"},
)


def _request(method: str, path: str, payload: dict) -> dict:
    """Call the Notion API and return the parsed body.

    Notion answers a rejected request with a JSON body carrying `code` and
    `message` that say exactly what was wrong ("body.properties.Status.status
    should be not present"). raise_for_status() alone throws that away and leaves
    a bare 400, so the body is read first and surfaced in the error.

    Raises:
        RuntimeError: if Notion rejects the request.
        requests.Timeout: if the request exceeds 15 seconds.
    """
    response = requests.request(
        method,
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {config.NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )

    try:
        body = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError(f"notion returned unparseable body: {response.text}")

    if response.status_code >= 400:
        raise RuntimeError(
            f"notion {response.status_code} {body.get('code')}: {body.get('message')}"
        )
    return body


def _select(options: tuple[str, ...]) -> dict:
    return {"select": {"options": [{"name": name} for name in options]}}


def build_properties(status_as_select: bool) -> dict:
    """The property schema, with Status as either a real status or a select."""
    status = (
        _select(STATUS_OPTIONS)
        if status_as_select
        # An empty status config asks Notion for its defaults, which are exactly
        # Not started / In progress / Done. The API does not accept custom status
        # options, so there is nothing else to pass here.
        else {"status": {}}
    )

    return {
        "Name": {"title": {}},
        "Course": _select(COURSE_OPTIONS),
        "Due": {"date": {}},
        "Status": status,
        "Type": _select(TYPE_OPTIONS),
    }


def create_database() -> tuple[str, bool]:
    """Create the database. Returns (database_id, status_is_select).

    Tries a real status property first and falls back to a select, so this keeps
    working unchanged if Notion ever allows creating one.
    """
    payload = {
        "parent": {"type": "page_id", "page_id": config.NOTION_PARENT_PAGE_ID},
        "title": [{"type": "text", "text": {"content": DATABASE_TITLE}}],
        "properties": build_properties(status_as_select=False),
    }

    try:
        body = _request("POST", "/databases", payload)
        print("created with a real status property")
        return body["id"], False
    except RuntimeError as exc:
        if "status" not in str(exc).lower():
            raise
        print(f"notion refused the status property, falling back to select\n  {exc}")

    payload["properties"] = build_properties(status_as_select=True)
    body = _request("POST", "/databases", payload)
    return body["id"], True


def insert_test_rows(database_id: str, status_is_select: bool) -> list[str]:
    """Add the sample coursework rows. Returns the new page ids."""
    today = _today_local()
    status_key = "select" if status_is_select else "status"

    page_ids = []
    for row in TEST_ROWS:
        due = (today + timedelta(days=row["days"])).isoformat()
        body = _request("POST", "/pages", {
            "parent": {"database_id": database_id},
            "properties": {
                "Name": {"title": [{"text": {"content": row["name"]}}]},
                "Course": {"select": {"name": row["course"]}},
                "Due": {"date": {"start": due}},
                "Status": {status_key: {"name": row["status"]}},
                "Type": {"select": {"name": row["type"]}},
            },
        })
        page_ids.append(body["id"])
        print(f"  {due}  {row['type']:<8} {row['status']:<12} {row['name']}")

    return page_ids


def _today_local() -> date:
    """Local date. Due dates are academic deadlines, which are local by nature."""
    from datetime import datetime

    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def main() -> int:
    if not config.NOTION_TOKEN:
        print("NOTION_TOKEN is not set in .env")
        return 1
    if not config.NOTION_PARENT_PAGE_ID:
        print("NOTION_PARENT_PAGE_ID is not set in .env")
        return 1

    database_id, status_is_select = create_database()

    print(f"\ninserting {len(TEST_ROWS)} test rows")
    insert_test_rows(database_id, status_is_select)

    print("\ndatabase created")
    print(f"NOTION_TASKS_DB_ID={database_id}")
    if status_is_select:
        print(
            "\nStatus is a select, not a status property. Change the property type"
            "\nin the Notion UI if you want the grouped board view."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
