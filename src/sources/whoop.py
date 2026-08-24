"""WHOOP recovery, sleep and strain. Phase 5.

The hardest integration in the project, and the one worth understanding properly.

OAuth 2.0 authorization code flow:
    1. Register an app at developer-dashboard.whoop.com. Set the redirect URI to
       match config.WHOOP_REDIRECT_URI.
    2. Send yourself to the authorize URL in a browser. You approve, WHOOP redirects
       to your redirect URI with a ?code= parameter.
    3. Exchange that code for an access token and a refresh token.
    4. Access tokens expire. Refresh tokens let you get a new one without repeating
       the browser step. Store both, persist them to disk, and refresh on 401.

Two traps specific to WHOOP:

    - Recovery data does not exist until the preceding sleep cycle closes. If the
      7:00 AM job runs before you wake up, you get nothing for that day. Handle the
      empty case rather than crashing, and consider scheduling around your actual
      wake time.
    - The API is on v2. Endpoints are /v2/activity/sleep, /v2/activity/workout, and
      recovery comes through the v2 cycle endpoints. v1 webhooks were removed.

BACKFILL: on first successful connection, pull your full history with a date range
query rather than only fetching today. Unlike training data, this history exists
already and is free to retrieve.

Run the one-time browser step with:  python -m src.main --auth
Then pull history with:              python -m src.sources.whoop backfill 2024-09-01
"""

import json
import logging
import shutil
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests
from authlib.integrations.base_client.errors import OAuthError
from whoop import WhoopClient

from src import config, db

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


class WhoopAuthError(RuntimeError):
    """A refresh or access token WHOOP rejected, distinct from a network fault.

    Raised when the OAuth grant itself failed (invalid_grant / invalid_request),
    which replaying cannot fix: the refresh token was consumed or revoked, and
    the only cure is re-running the browser authorization. Callers must treat
    this as fatal and NOT retry, unlike a requests.RequestException, which is a
    transient network condition worth retrying.
    """

SCOPES = [
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "read:body_measurement",
    "offline",
]

# How far back fetch() looks for a finished cycle. One day covers the normal case;
# three tolerates the strap sitting on the charger over a weekend without turning a
# gap into an error. Anything older than this is backfill's problem, not the
# morning briefing's.
FETCH_LOOKBACK_DAYS = 3

# Keys fetch() attaches on top of the daily_metrics columns. See db_row().
CYCLE_TAG_KEYS = ("cycle_kind", "cycle_id")

# (connect, read) rather than one scalar. The read leg is generous because the
# real failure this fixes was a token refresh whose response arrived just after a
# 15s cap on a cold connection right after S3 wake: WHOOP had already rotated the
# refresh token server-side, so timing out before reading the response threw away
# the new token and locked out every call after. 45s lets that response land. The
# connect leg stays short so a genuinely dead adapter fails fast. default_timeout
# applies this to EVERY session request, including authlib's token refresh.
REQUEST_TIMEOUT = (10, 45)

# The `state` value from the most recent build_authorize_url() call in this process.
# See exchange_code() for why it is a module global rather than an argument.
_auth_state: str | None = None


# ----------------------------------------------------------------------------------
# token persistence


def _load_token() -> dict | None:
    """Read the stored token, or None if there is not one yet."""
    if not config.WHOOP_TOKEN_PATH.exists():
        return None
    return json.loads(config.WHOOP_TOKEN_PATH.read_text(encoding="utf-8"))


def _save_token(token: dict) -> None:
    """Write the token to disk, replacing whatever was there.

    This runs on every refresh, not only at initial authorization. WHOOP rotates
    the refresh token on each use and invalidates the one you traded in, so a file
    written once goes stale the first time the access token expires. Nothing fails
    at that moment; it fails weeks later with an invalid_grant that looks unrelated
    to anything you did.

    Written to a temp file and renamed rather than truncating in place. A partial
    write here is not a lost line of data, it is being locked out of the API until
    the browser step is repeated.

    Registered as authlib's update_token callback (via the whoop library's
    on_token_refresh, see _get_client), so a rotated token hits disk the instant
    the session issues it, before the API call that triggered the refresh
    proceeds, rather than only after a call succeeds.

    The previous file is kept as .bak, written atomically (copy to .bak.tmp then
    replace) so the backup itself is never a half-written file. Its value is
    narrow but real: it recovers a corrupted or truncated write of the live file.
    It does NOT reliably let you "go back a rotation" once a refresh has
    succeeded, because WHOOP invalidates the old refresh token the moment it
    issues a new one, so .bak's refresh token is usually already dead server-side.
    """
    target = config.WHOOP_TOKEN_PATH
    temp_path = target.with_name(target.name + ".tmp")
    # dict() because authlib hands back an OAuth2Token, which json.dumps will only
    # serialize by accident of it subclassing dict.
    temp_path.write_text(json.dumps(dict(token), indent=2), encoding="utf-8")
    if target.exists():
        bak_temp = target.with_name(target.name + ".bak.tmp")
        shutil.copyfile(target, bak_temp)
        bak_temp.replace(target.with_name(target.name + ".bak"))
    temp_path.replace(target)  # atomic within the same directory
    logging.info("whoop token written to %s", target)


def _get_client(token: dict | None = None) -> WhoopClient:
    """Build a client. token=None is only for the authorization flow.

    on_token_refresh is the reason every client goes through this helper. authlib
    refreshes an expired access token transparently mid-request, and without the
    callback the newly rotated refresh token would exist only in memory and be gone
    when the process exits.
    """
    client = WhoopClient(
        config.WHOOP_CLIENT_ID,
        config.WHOOP_CLIENT_SECRET,
        config.WHOOP_REDIRECT_URI,
        scopes=SCOPES,
        token=token,
        on_token_refresh=_save_token,
    )
    client.session.default_timeout = REQUEST_TIMEOUT
    return client


def _authorized_client() -> WhoopClient:
    """Client built from the stored token.

    Raises:
        RuntimeError: if no token has been stored yet.
    """
    token = _load_token()
    if token is None:
        raise RuntimeError(
            f"no WHOOP token at {config.WHOOP_TOKEN_PATH}, "
            "run: python -m src.main --auth"
        )
    return _get_client(token)


# ----------------------------------------------------------------------------------
# OAuth flow


def build_authorize_url() -> str:
    """Return the URL to open in a browser to start the OAuth flow."""
    global _auth_state

    client = _get_client()
    try:
        url, _auth_state = client.authorization_url()
    finally:
        client.close()
    return url


def exchange_code(code: str) -> dict:
    """Trade an authorization code for access and refresh tokens.

    Accepts either the bare code or the entire redirect URL you landed on. The URL
    is what the browser actually gives you, and picking the code out of a query
    string by hand is a good way to drop a character off the end.

    The token is persisted before this returns, so a crash immediately afterward
    does not cost you the browser step.

    Raises:
        authlib.integrations.base_client.errors.OAuthError: if WHOOP rejects the
            code, usually because it was already used or is older than ten minutes.
    """
    client = _get_client()
    try:
        if "code=" in code:
            # state is only meaningful when build_authorize_url() ran in this same
            # process, which is what --auth does. After a restart there is nothing
            # to compare against, and authlib skips the check on state=None rather
            # than failing. Passing it along is free CSRF cover for the common path
            # without making the uncommon one impossible.
            token = client.fetch_token(authorization_response=code, state=_auth_state)
        else:
            token = client.fetch_token(code=code)
    finally:
        client.close()

    _save_token(token)
    return dict(token)


def refresh_access_token() -> dict:
    """Use the stored refresh token to get a fresh access token.

    Not needed on the normal path: any request made through _authorized_client()
    refreshes itself when the access token has expired. This exists to check that
    the stored credentials still work without pulling data, and to force the
    rotation on demand.

    Raises:
        RuntimeError: if no token has been stored yet.
    """
    client = _authorized_client()
    try:
        token = client.session.refresh_token(TOKEN_URL)
    finally:
        client.close()

    # on_token_refresh already wrote this. Writing again costs one file rename and
    # means persistence does not silently depend on an authlib callback continuing
    # to fire across library versions.
    _save_token(token)
    return dict(token)


# ----------------------------------------------------------------------------------
# parsing


def _to_local(timestamp: str, offset: str | None) -> datetime:
    """A WHOOP timestamp as an aware datetime in the record's own local zone.

    Uses the UTC offset WHOOP recorded for the record ('-05:00') rather than
    assuming America/New_York, so a road trip to a match in a different zone still
    lands in the right local wall-clock time. Falls back to the configured
    timezone if the offset is missing or malformed.
    """
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

    tzinfo: Any = ZoneInfo(config.TIMEZONE)
    if offset:
        try:
            # strptime %z parses the colon form since 3.7; this is just the least
            # error prone way to turn '-05:00' into a tzinfo.
            tzinfo = datetime.strptime(offset, "%z").tzinfo
        except ValueError:
            logging.warning(
                "whoop: unparseable timezone_offset %r, falling back to %s",
                offset,
                config.TIMEZONE,
            )

    return moment.astimezone(tzinfo)


def _local_date(timestamp: str, offset: str | None) -> str:
    """Local calendar date of a WHOOP timestamp as 'YYYY-MM-DD'."""
    return _to_local(timestamp, offset).strftime("%Y-%m-%d")


def _local_dt_str(timestamp: str | None, offset: str | None) -> str:
    """Local 'YYYY-MM-DD HH:MM' for a WHOOP timestamp, or '-' when absent.

    Used by the bucketing log and the audit table, where seeing the wall-clock
    start and end next to the assigned date is the whole point of the diff.
    """
    if not timestamp:
        return "-"
    return _to_local(timestamp, offset).strftime("%Y-%m-%d %H:%M")


def _cycle_date(cycle: dict) -> str:
    """The local calendar date a cycle belongs to: the date its START falls on.

    Always start, never end, and never the attached sleep record. A WHOOP cycle
    runs from the moment you wake to the moment you fall asleep again, so `start`
    IS the wake instant and is on the correct day by construction. `end` is sleep
    onset, which crosses midnight on every night you get to bed after 12: a cycle
    that began Saturday morning and ended 01:01 Sunday is Saturday's day, and
    labeling it by `end` pushes a whole day of strain and workouts onto Sunday.

    Deriving it from sleep['end'] has the same failure in a subtler form (it is
    the wake instant only for the sleep at the head of the cycle, and naps and
    late-scored records break that assumption), so the cycle's own start is the
    single rule with no fallbacks.
    """
    return _local_date(cycle["start"], cycle.get("timezone_offset"))


def _utc(timestamp: str) -> datetime:
    """A WHOOP timestamp as an aware UTC datetime, for interval comparisons."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _in_cycle(moment: datetime, cycle: dict) -> bool:
    """Whether an instant falls in a cycle's half-open window.

    cycle.start <= moment < cycle.end. Half-open so the instant a cycle ends,
    which is the instant the next one starts, belongs to exactly one of them.
    A cycle with no `end` is the one currently open and has no upper bound.
    """
    if not cycle.get("start"):
        return False
    if moment < _utc(cycle["start"]):
        return False
    end = cycle.get("end")
    return end is None or moment < _utc(end)


def _match_workouts_to_cycles(
    cycles: list[dict], workouts: list[dict]
) -> dict[str, list[dict]]:
    """Bucket workouts onto cycles by the workout's START instant only.

    A workout belongs to the cycle it BEGAN in: cycle.start <= workout.start <
    cycle.end. Its end is never consulted, so a lift that runs past sleep onset
    stays on the day it started rather than splitting across two days or jumping
    forward one.

    Cycles are meant to tile the timeline without overlapping. If they ever do,
    a workout matches more than one; that is a data problem worth seeing, so it
    is logged as a warning and the workout is attributed to the first match only
    (the collection sorts newest first) rather than being counted twice.

    Returns {cycle_id: [workout, ...]} with an entry for every cycle, empty list
    included. Workouts matching no cycle in the list are dropped and counted in
    one info line: they started before the oldest cycle in the window.
    """
    by_cycle: dict[str, list[dict]] = {cycle["id"]: [] for cycle in cycles}
    unmatched = 0

    for workout in workouts:
        start = workout.get("start_utc")
        if not start:
            logging.warning(
                "whoop: workout %s has no start, not attributed to any cycle",
                workout.get("id"),
            )
            continue

        matches = [cycle for cycle in cycles if _in_cycle(_utc(start), cycle)]
        if not matches:
            unmatched += 1
            continue
        if len(matches) > 1:
            logging.warning(
                "whoop: workout %s starting %s falls in %d cycles (%s), "
                "attributing it to %s only",
                workout.get("id"),
                start,
                len(matches),
                ", ".join(str(cycle.get("id")) for cycle in matches),
                matches[0].get("id"),
            )
        by_cycle[matches[0]["id"]].append(workout)

    if unmatched:
        logging.info(
            "whoop: %d workout(s) started outside every cycle in the window",
            unmatched,
        )
    return by_cycle


def _sleep_hours(sleep: dict | None) -> float | None:
    """Hours actually asleep, which is time in bed minus time awake."""
    stages = ((sleep or {}).get("score") or {}).get("stage_summary") or {}
    in_bed_milli = stages.get("total_in_bed_time_milli")
    if in_bed_milli is None:
        return None
    awake_milli = stages.get("total_awake_time_milli") or 0
    return round((in_bed_milli - awake_milli) / 3_600_000, 2)


def _row(cycle: dict, recovery: dict | None, sleep: dict | None) -> dict:
    """Flatten one cycle plus its recovery and sleep into a daily_metrics row.

    The keys are exactly what db.upsert_daily_metrics accepts, so backfill can hand
    the dict straight over without a translation step in between.
    """
    strain_score = cycle.get("score") or {}
    recovery_score = (recovery or {}).get("score") or {}
    sleep_score = (sleep or {}).get("score") or {}

    return {
        "date": _cycle_date(cycle),
        "recovery_score": recovery_score.get("recovery_score"),
        "hrv_ms": recovery_score.get("hrv_rmssd_milli"),
        "resting_hr": recovery_score.get("resting_heart_rate"),
        "sleep_hours": _sleep_hours(sleep),
        "sleep_performance": sleep_score.get("sleep_performance_percentage"),
        "strain": strain_score.get("strain"),
        # Whole payloads, per the storage convention in CLAUDE.md. Everything not
        # parsed above (spo2, skin temp, respiratory rate, the sleep stage
        # breakdown, sleep debt) is already in hand, and WHOOP history is not
        # guaranteed to still be re-fetchable when you decide you want it.
        "raw": {"cycle": cycle, "recovery": recovery, "sleep": sleep},
    }


def _is_finished(cycle: dict) -> bool:
    """Whether WHOOP has both closed and scored this cycle.

    `end` is null for the cycle you are currently living in. score_state stays
    PENDING_SCORE until the strap syncs after you wake and the numbers settle, so a
    closed cycle is still not a usable one.
    """
    return bool(cycle.get("end")) and cycle.get("score_state") == "SCORED"


def _scored(record: dict | None) -> dict | None:
    """The record if WHOOP finished scoring it, otherwise None."""
    if record and record.get("score_state") == "SCORED":
        return record
    return None


def _get_or_none(call: Callable[..., dict], *args: Any) -> dict | None:
    """Call a client getter, turning a 404 into None.

    WHOOP 404s the recovery and sleep sub-resources of a cycle that does not have
    one rather than returning an empty body, and "you have not slept yet" is not an
    error condition here. Every other status still raises.
    """
    try:
        return call(*args)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise


# ----------------------------------------------------------------------------------
# public source contract


def _auth_failed(exc: OAuthError) -> WhoopAuthError:
    """Log the one actionable line and wrap an OAuth rejection for the caller.

    Kept as one helper so the exact remediation string is identical everywhere a
    token gets rejected, and so callers can catch WhoopAuthError without importing
    authlib.
    """
    logging.error("WHOOP auth failed, run: python -m src.main --auth")
    return WhoopAuthError(f"WHOOP rejected the stored token: {exc}")


def fetch() -> list[dict]:
    """Both cycles the morning briefing needs, each tagged with its kind.

    Returns up to two rows, current first:

        {"cycle_kind": "current",   ...}  the OPEN cycle. It began when you woke
                                          this morning, so it carries LAST
                                          NIGHT'S SLEEP and TODAY'S RECOVERY.
                                          Its strain is still climbing and means
                                          nothing until it closes tonight.
        {"cycle_kind": "completed", ...}  the newest CLOSED and scored cycle,
                                          which is yesterday: its day strain is
                                          final and its workouts are all in.

    Returning only the completed cycle, which is what this used to do, makes the
    briefing a night stale: the header would show the recovery and sleep from
    the morning BEFORE. Both are needed because they answer different questions
    and no single cycle holds both.

    Each row also carries "cycle_id" so the briefing can log which cycle every
    field came from. Those two tag keys are not daily_metrics columns; strip
    them with db_row() before handing a row to db.upsert_daily_metrics.

    A cycle is only returned when its recovery AND sleep are both SCORED. Before
    the strap syncs after you wake there is no open cycle with a scored recovery
    yet, so the list comes back with the completed cycle alone, or empty. Both
    are normal conditions, not errors.

    Raises:
        RuntimeError: if no token has been stored yet.
        WhoopAuthError: if WHOOP rejected the token (dead refresh token). Fatal,
            do not retry; the fix is re-running --auth.
        requests.HTTPError: for any API failure other than a 404 on a
            sub-resource that does not exist.
    """
    start = datetime.now(timezone.utc).date() - timedelta(days=FETCH_LOOKBACK_DAYS)

    rows: list[dict] = []
    client = _authorized_client()
    try:
        # The collection endpoint sorts newest first.
        cycles = client.get_cycle_collection(start_date=start.isoformat())

        # There is at most one open cycle and it is the newest. `end` is null
        # exactly while you are living in it.
        open_cycle = next((cycle for cycle in cycles if not cycle.get("end")), None)
        if open_cycle is not None:
            row = _cycle_row(client, open_cycle, "current")
            if row is not None:
                rows.append(row)
        else:
            logging.info("whoop: no open cycle yet, header will use yesterday only")

        # The first cycle that is closed, scored, and has scored recovery and
        # sleep. Walking past the ones that are not lets a single unsynced day
        # fall back to the one before it rather than dropping the section.
        for cycle in cycles:
            if not _is_finished(cycle):
                continue
            row = _cycle_row(client, cycle, "completed")
            if row is not None:
                rows.append(row)
                break
        else:
            logging.info(
                "whoop: no complete cycle in the last %d days", FETCH_LOOKBACK_DAYS
            )
    except OAuthError as exc:
        # The auto-refresh inside a data call failed at the grant level. This is
        # never worth retrying: the refresh token was consumed or revoked, and
        # every replay just re-fails. Fail fast and loud instead.
        raise _auth_failed(exc) from exc
    finally:
        client.close()

    return rows


def _cycle_row(client: WhoopClient, cycle: dict, kind: str) -> dict | None:
    """One cycle plus its recovery and sleep as a tagged row, or None.

    None means WHOOP has not scored the recovery or the sleep for this cycle
    yet, which is the normal state of the open cycle until the strap syncs after
    you wake. Logs the date-bucketing audit trail: local start, local end, and
    the date _cycle_date assigned, so a wrong bucket can be caught by reading
    brief.log against the WHOOP app rather than guessed at.
    """
    recovery = _scored(_get_or_none(client.get_recovery_for_cycle, cycle["id"]))
    if recovery is None:
        logging.info("whoop: %s cycle %s has no scored recovery", kind, cycle["id"])
        return None

    sleep = _sleep_for(client, cycle, recovery)
    if sleep is None:
        logging.info("whoop: %s cycle %s has no scored sleep", kind, cycle["id"])
        return None

    row = _row(cycle, recovery, sleep)
    row["cycle_kind"] = kind
    row["cycle_id"] = cycle.get("id")

    logging.info(
        "whoop %s cycle %s: local start %s, end %s -> assigned date %s",
        kind,
        cycle.get("id"),
        _local_dt_str(cycle.get("start"), cycle.get("timezone_offset")),
        _local_dt_str(cycle.get("end"), cycle.get("timezone_offset")),
        row["date"],
    )
    return row


def db_row(metrics: dict) -> dict:
    """A fetch() row with the cycle tags stripped, ready for daily_metrics.

    db.upsert_daily_metrics rejects unknown keys on purpose, so cycle_kind and
    cycle_id have to come off before a write. They are routing and logging
    information about where a number came from, not health data.
    """
    return {k: v for k, v in metrics.items() if k not in CYCLE_TAG_KEYS}


def _sleep_for(client: WhoopClient, cycle: dict, recovery: dict) -> dict | None:
    """The sleep that produced this recovery.

    Goes through recovery['sleep_id'] rather than the cycle's own sleep endpoint.
    Recovery is scored from one specific sleep, and reporting a recovery number
    next to a different night's sleep is the kind of quiet mismatch you would never
    catch by reading the briefing. The cycle lookup is a fallback for older records
    with no sleep_id, and for naps confusing the association.
    """
    sleep_id = recovery.get("sleep_id")
    if sleep_id:
        sleep = _scored(_get_or_none(client.get_sleep_by_id, sleep_id))
        if sleep is not None:
            return sleep
    return _scored(_get_or_none(client.get_sleep_for_cycle, cycle["id"]))


def fetch_workouts(start_date: str | None = None) -> list[dict]:
    """Workouts from the v2 workout endpoint, newest first.

    These are WHOOP's own recorded activities and are never entered by hand. They
    are deliberately kept apart from the `sessions` table, which holds what you
    type into Telegram: a lift WHOOP saw as 48 minutes of elevated heart rate and
    the same lift you logged as "60 min rpe 8" are two different measurements of
    one event, and collapsing them would destroy the ability to compare them.

    sport_id is kept as WHOOP's integer, and sport_name as the label WHOOP puts
    in the payload alongside it ('volleyball', 'weightlifting'). The name is taken
    from the response rather than mapped from the id locally, so a WHOOP sport-id
    reshuffle never silently mislabels an activity.

    Args:
        start_date: 'YYYY-MM-DD'. Defaults to the endpoint's own trailing
            seven-day window.

    Raises:
        RuntimeError: if no token has been stored yet.
        WhoopAuthError: if WHOOP rejected the token. Fatal, do not retry.
        requests.HTTPError: for any API failure.
    """
    client = _authorized_client()
    try:
        workouts = client.get_workout_collection(start_date=start_date)
    except OAuthError as exc:
        raise _auth_failed(exc) from exc
    finally:
        client.close()

    rows = []
    for workout in workouts:
        # Unscored workouts still have start, end and sport. Keep them: an
        # activity that happened is worth recording even before WHOOP finishes
        # putting a strain number on it.
        score = workout.get("score") or {}
        rows.append({
            "id": workout["id"],
            "date": _local_date(workout["start"], workout.get("timezone_offset")),
            "sport_id": workout.get("sport_id"),
            "sport_name": workout.get("sport_name"),
            "start_utc": workout["start"],
            "end_utc": workout.get("end"),
            "duration_min": _duration_min(workout.get("start"), workout.get("end")),
            "strain": score.get("strain"),
            "average_hr": score.get("average_heart_rate"),
            "max_hr": score.get("max_heart_rate"),
            "kilojoule": score.get("kilojoule"),
            "scored": workout.get("score_state") == "SCORED",
            "raw": workout,
        })

    logging.info("whoop: %d workout(s) from %s", len(rows), start_date or "last 7 days")
    return rows


def store_workouts(workouts: list[dict]) -> int:
    """Write fetched workouts into whoop_workouts. Returns the number stored.

    Kept out of fetch_workouts() so the source contract holds: modules under
    sources/ return data and do not touch the database. Keyed on WHOOP's UUID, so
    calling this on overlapping windows updates rows instead of duplicating them.
    """
    for workout in workouts:
        db.upsert_whoop_workout(workout)
    logging.info("whoop: %d workout(s) stored", len(workouts))
    return len(workouts)


def _duration_min(start: str | None, end: str | None) -> int | None:
    """Whole minutes between two WHOOP timestamps, or None if either is missing."""
    if not start or not end:
        return None
    started = datetime.fromisoformat(start.replace("Z", "+00:00"))
    ended = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return round((ended - started).total_seconds() / 60)


def audit(days: int = 7) -> list[dict]:
    """Per-cycle audit rows for the last `days` days, for diffing against the app.

    Joins cycles with their recovery and sleep locally (one collection call each,
    the same approach as backfill), attaches every workout to the cycle its START
    instant falls inside (see _match_workouts_to_cycles), and reports the cycle's
    own local start and end. The point is to verify date bucketing by eye: if a workout or a day
    strain lands under the wrong date here, it lands under the wrong date in the
    briefing too. Newest cycle first. Unfinished cycles are included on purpose so
    the current day shows too; day strain on those is still climbing.

    Each row: date, cycle_id, start_local, end_local, strain, recovery_score,
    sleep_hours, sleep_performance, workouts[{sport, duration_min, strain}].

    Raises:
        RuntimeError: if no token has been stored yet.
        WhoopAuthError: if WHOOP rejected the token.
    """
    start = (date.today() - timedelta(days=days)).isoformat()

    client = _authorized_client()
    try:
        cycles = client.get_cycle_collection(start_date=start)
        recoveries = client.get_recovery_collection(start_date=start)
        sleeps = client.get_sleep_collection(start_date=start)
    except OAuthError as exc:
        raise _auth_failed(exc) from exc
    finally:
        client.close()

    # fetch_workouts manages its own client and already parses and labels rows.
    workouts = fetch_workouts(start_date=start)

    recovery_by_cycle = {r["cycle_id"]: r for r in recoveries if _scored(r)}
    sleep_by_id = {s["id"]: s for s in sleeps if _scored(s)}

    # Interval matching on the workout's start instant, not a date-string join:
    # a workout that began at 23:40 belongs to the cycle that was open at 23:40,
    # which is the previous day's cycle once you are past midnight.
    workouts_by_cycle = _match_workouts_to_cycles(cycles, workouts)

    rows = []
    for cycle in cycles:
        recovery = recovery_by_cycle.get(cycle["id"])
        sleep = sleep_by_id.get(recovery.get("sleep_id")) if recovery else None
        assigned = _cycle_date(cycle)

        recovery_score = (recovery or {}).get("score") or {}
        sleep_score = (sleep or {}).get("score") or {}
        cycle_score = cycle.get("score") or {}

        rows.append({
            "date": assigned,
            "cycle_id": cycle.get("id"),
            "start_local": _local_dt_str(cycle.get("start"), cycle.get("timezone_offset")),
            "end_local": _local_dt_str(cycle.get("end"), cycle.get("timezone_offset")),
            "strain": cycle_score.get("strain"),
            "recovery_score": recovery_score.get("recovery_score"),
            "sleep_hours": _sleep_hours(sleep),
            "sleep_performance": sleep_score.get("sleep_performance_percentage"),
            "workouts": [
                {
                    "sport": workout.get("sport_name") or f"sport {workout.get('sport_id')}",
                    "duration_min": workout.get("duration_min"),
                    "strain": workout.get("strain"),
                }
                for workout in sorted(
                    workouts_by_cycle.get(cycle["id"], []),
                    key=lambda w: w.get("start_utc") or "",
                )
            ],
        })

    return rows


def format_lines(metrics: dict) -> list[str]:
    """Recovery and sleep numbers for the briefing.

    Dated explicitly, and that is not decoration. Day strain only finishes
    accumulating when the cycle closes, so until tonight's bedtime the newest
    complete day is yesterday. Printing yesterday's recovery score bare, directly
    under a header stamped with today's date, would read as today's number.

    Fields that WHOOP did not score are dropped rather than printed as a dash, so
    a partial day stays readable.

    Everything is formatted through an explicit precision. WHOOP sends
    recovery_score, resting_heart_rate and sleep_performance_percentage as floats
    even though they are whole numbers and the column types are INTEGER, so
    interpolating them directly prints "recovery 72.0%". SQLite quietly converts
    them on the way into daily_metrics; this is only a display concern.
    """
    parts = []
    if metrics.get("recovery_score") is not None:
        parts.append(f"recovery {metrics['recovery_score']:.0f}%")
    if metrics.get("sleep_hours") is not None:
        parts.append(f"sleep {metrics['sleep_hours']:.1f}h")
    if metrics.get("sleep_performance") is not None:
        parts.append(f"sleep performance {metrics['sleep_performance']:.0f}%")
    if metrics.get("hrv_ms") is not None:
        parts.append(f"HRV {metrics['hrv_ms']:.0f}ms")
    if metrics.get("resting_hr") is not None:
        parts.append(f"RHR {metrics['resting_hr']:.0f}")

    if not parts:
        return ["WHOOP: cycle recorded but no scored metrics"]

    day = datetime.strptime(metrics["date"], "%Y-%m-%d").strftime("%a %b %d")
    return [f"{', '.join(parts)} ({day})"]


def backfill(start_date: str) -> int:
    """Pull all history from start_date to now into daily_metrics.

    Uses the three collection endpoints and joins them locally rather than making
    two extra calls per cycle. Over a year of history that is roughly 45 requests
    instead of 730, which matters against WHOOP's rate limit and is the difference
    between this taking seconds and taking minutes.

    More forgiving than fetch(): a day with a scored recovery but no scored sleep is
    written with the sleep columns left NULL. db.upsert_daily_metrics COALESCEs on
    conflict, so re-running this later fills those in rather than overwriting good
    values with nulls.

    Args:
        start_date: 'YYYY-MM-DD'. Interpreted as the start of that day in UTC.

    Returns the number of days written.

    Raises:
        RuntimeError: if no token has been stored yet.
        ValueError: if start_date is not a parseable date, or is in the future.
    """
    # Widen the sleep window by a day. The sleep that scored the earliest cycle in
    # range began the night before that cycle's start and would otherwise fall
    # outside the query, silently costing that one day its sleep columns.
    try:
        sleep_start = (date.fromisoformat(start_date[:10]) - timedelta(days=1)).isoformat()
    except ValueError:
        sleep_start = start_date

    client = _authorized_client()
    try:
        cycles = client.get_cycle_collection(start_date=start_date)
        recoveries = client.get_recovery_collection(start_date=start_date)
        sleeps = client.get_sleep_collection(start_date=sleep_start)
    finally:
        client.close()

    recovery_by_cycle = {r["cycle_id"]: r for r in recoveries if _scored(r)}
    sleep_by_id = {s["id"]: s for s in sleeps if _scored(s)}

    written = 0
    for cycle in cycles:
        # Skips the cycle currently in progress, whose strain is still climbing.
        if not _is_finished(cycle):
            continue

        recovery = recovery_by_cycle.get(cycle["id"])
        sleep = sleep_by_id.get(recovery.get("sleep_id")) if recovery else None

        db.upsert_daily_metrics(_row(cycle, recovery, sleep))
        written += 1

    logging.info(
        "whoop backfill: %d day(s) written from %s, %d cycle(s) seen",
        written,
        start_date,
        len(cycles),
    )
    return written


if __name__ == "__main__":
    # Test this module alone:
    #   python -m src.sources.whoop
    #   python -m src.sources.whoop backfill 2024-09-01
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        if len(sys.argv) < 3:
            sys.exit("usage: python -m src.sources.whoop backfill YYYY-MM-DD")
        db.init_db()
        print(f"{backfill(sys.argv[2])} day(s) written")
    else:
        rows = fetch()
        if not rows:
            print("no complete cycle yet")
        for row in rows:
            print({k: v for k, v in row.items() if k != "raw"})
