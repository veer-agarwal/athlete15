"""Notion coursework and tasks. Phase 6.

Setup:
    1. Create an internal integration at notion.so/my-integrations, copy the secret.
    2. CRITICAL AND EASY TO MISS: open the database in Notion, use the ... menu ->
       Connections, and add your integration. The token by itself grants access to
       nothing. Every database must be shared explicitly.
    3. The database id is the 32 character string in the database URL.

The Notion API returns deeply nested property objects. A title property is not a
string, it is a list of rich text objects each with plain_text. Write a small
extractor per property type rather than reaching through the nesting inline at every
call site.
"""

from src import config

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def fetch() -> list[dict]:
    """Return upcoming coursework.

    Query the database with a filter for incomplete items due within about a week,
    sorted by due date.

    Returns dicts with keys: id, title, course, due_date, status.
    """
    raise NotImplementedError("phase 6")


def create_task(title: str, due_date: str, course: str | None = None) -> str:
    """Create a page in the tasks database. Returns the new page id.

    This is the first WRITE action in the project and the template for every later
    one. Confirm before writing when the request came from natural language rather
    than an explicit command.
    """
    raise NotImplementedError("phase 6+")
