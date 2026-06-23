from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
TARGET_SLOTS = ((8, 13), (12, 13), (16, 13))
DEFAULT_CATCH_UP_MINUTES = 180
DEFAULT_REPLAY_RELEASE_INTERVAL_MINUTES = 15


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def now_kst() -> datetime:
    return datetime.now(KST).replace(microsecond=0)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def pages_base_url() -> str:
    configured = env("PAGES_BASE_URL")
    if configured:
        return configured.rstrip("/")

    repository = env("GITHUB_REPOSITORY")
    if "/" not in repository:
        return ""

    owner, repo = repository.split("/", 1)
    return f"https://{owner}.github.io/{repo}"


def last_run_url() -> str:
    configured = env("LAST_RUN_URL")
    if configured:
        return configured

    base_url = pages_base_url()
    if not base_url:
        return ""

    return f"{base_url}/last-run.json"


def replay_queue_url() -> str:
    configured = env("SLACK_REPLAY_QUEUE_URL")
    if configured:
        return configured

    base_url = pages_base_url()
    if not base_url:
        return ""

    return f"{base_url}/slack-replay-queue.json"


def with_cache_bust(url: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}t={int(time.time())}"


def fetch_json_url(url: str, label: str) -> dict[str, Any]:
    if not url:
        return {}

    request = Request(
        with_cache_bust(url),
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "aws-update-rss-schedule-gate",
        },
    )

    try:
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
    except HTTPError as exc:
        if exc.code == 404:
            return {}
        print(f"Could not fetch {label}: HTTP {exc.code}")
        return {}
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Could not fetch {label}: {exc}")
        return {}


def load_event_schedule() -> str:
    event_path = env("GITHUB_EVENT_PATH")
    if not event_path:
        return ""

    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""

    return str(event.get("schedule") or "")


def fetch_last_run() -> dict[str, Any]:
    return fetch_json_url(last_run_url(), "last-run.json")


def fetch_replay_queue() -> dict[str, Any]:
    return fetch_json_url(replay_queue_url(), "slack-replay-queue.json")


def slot_at(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)


def latest_due_slot(reference: datetime) -> datetime | None:
    candidates: list[datetime] = []
    for offset_days in (0, 1):
        day = (reference - timedelta(days=offset_days)).date()
        for hour, minute in TARGET_SLOTS:
            candidate = slot_at(day, hour, minute)
            if candidate <= reference:
                candidates.append(candidate)

    return max(candidates) if candidates else None


def parse_utc(value: str) -> datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except ValueError:
        return None


def replay_queue_due(queue: dict[str, Any], reference_utc: datetime) -> tuple[bool, str, str]:
    pending = queue.get("pending")
    if not isinstance(pending, list) or not pending:
        return False, "", "no queued Slack replay items"

    try:
        interval_minutes = int(queue.get("release_interval_minutes") or DEFAULT_REPLAY_RELEASE_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        interval_minutes = DEFAULT_REPLAY_RELEASE_INTERVAL_MINUTES

    last_released = parse_utc(str(queue.get("last_released_at") or ""))
    if last_released is None:
        return True, f"slack-replay/{reference_utc.isoformat()}", f"queued Slack replay item is pending; no previous release; pending={len(pending)}"

    age = reference_utc - last_released
    if age >= timedelta(minutes=interval_minutes):
        return True, f"slack-replay/{reference_utc.isoformat()}", f"queued Slack replay item is due; age={int(age.total_seconds() // 60)}m; pending={len(pending)}"

    remaining = interval_minutes - int(age.total_seconds() // 60)
    return False, "", f"queued Slack replay item is waiting for release interval; pending={len(pending)}; remaining≈{remaining}m"


def append_step_summary(lines: list[str]) -> None:
    summary_path = env("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines))
        summary.write("\n")


def write_outputs(values: dict[str, str]) -> None:
    output_path = env("GITHUB_OUTPUT")
    if not output_path:
        for key, value in values.items():
            print(f"{key}={value}")
        return

    with Path(output_path).open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def decide() -> None:
    event_name = env("GITHUB_EVENT_NAME")
    event_schedule = load_event_schedule()
    run_id = env("GITHUB_RUN_ID")
    run_attempt = env("GITHUB_RUN_ATTEMPT")
    sha = env("GITHUB_SHA")
    current_kst = now_kst()
    current_utc = utc_now()

    if event_name != "schedule":
        reason = f"manual trigger via {event_name or 'unknown'}"
        outputs = {
            "should_run": "true",
            "target_type": "manual",
            "target_slot_kst": f"manual/{run_id or current_utc.isoformat()}",
            "reason": reason,
            "event_schedule": event_schedule,
            "now_kst": current_kst.isoformat(),
        }
        write_outputs(outputs)
        append_step_summary(
            [
                "### Schedule gate",
                "",
                "- decision: run",
                "- type: manual",
                f"- reason: {reason}",
                f"- kst_now: {current_kst.isoformat()}",
                f"- utc_now: {current_utc.isoformat()}",
                f"- run_id: {run_id}",
                f"- run_attempt: {run_attempt}",
                f"- sha: {sha}",
            ]
        )
        return

    catch_up_minutes = int(env("SCHEDULE_CATCH_UP_MINUTES", str(DEFAULT_CATCH_UP_MINUTES)))
    due_slot = latest_due_slot(current_kst)
    last_run = fetch_last_run()
    replay_queue = fetch_replay_queue()
    replay_due, replay_target, replay_reason = replay_queue_due(replay_queue, current_utc)
    last_scheduled_slot = str(last_run.get("last_scheduled_slot_kst") or last_run.get("last_slot_kst") or "")
    last_run_state_url = last_run_url()
    replay_queue_state_url = replay_queue_url()

    should_run = False
    target_type = "schedule"
    target_slot = ""
    reason = "no due slot"
    age_minutes = ""

    if replay_due:
        should_run = True
        target_type = "slack_replay"
        target_slot = replay_target
        reason = replay_reason
    elif due_slot is not None:
        target_slot = due_slot.isoformat()
        age = current_kst - due_slot
        age_minutes = str(int(age.total_seconds() // 60))

        if age > timedelta(minutes=catch_up_minutes):
            reason = f"latest due slot is outside catch-up window ({age_minutes}m > {catch_up_minutes}m)"
        elif last_scheduled_slot == target_slot:
            reason = f"slot already processed: {target_slot}"
        else:
            should_run = True
            reason = f"slot needs processing: {target_slot}"
    else:
        reason = replay_reason or reason

    outputs = {
        "should_run": "true" if should_run else "false",
        "target_type": target_type,
        "target_slot_kst": target_slot,
        "reason": reason,
        "event_schedule": event_schedule,
        "now_kst": current_kst.isoformat(),
        "last_scheduled_slot_kst": last_scheduled_slot,
    }
    write_outputs(outputs)

    append_step_summary(
        [
            "### Schedule gate",
            "",
            f"- decision: {'run' if should_run else 'skip'}",
            f"- type: {target_type}",
            f"- event_schedule: {event_schedule}",
            f"- target_slot_kst: {target_slot or '(none)'}",
            f"- age_minutes: {age_minutes or '(none)'}",
            f"- catch_up_minutes: {catch_up_minutes}",
            f"- last_scheduled_slot_kst: {last_scheduled_slot or '(none)'}",
            f"- last_run_url: {last_run_state_url or '(none)'}",
            f"- replay_queue_url: {replay_queue_state_url or '(none)'}",
            f"- replay_queue_reason: {replay_reason or '(none)'}",
            f"- reason: {reason}",
            f"- kst_now: {current_kst.isoformat()}",
            f"- utc_now: {current_utc.isoformat()}",
            f"- run_id: {run_id}",
            f"- run_attempt: {run_attempt}",
            f"- sha: {sha}",
        ]
    )


def record() -> None:
    target_type = env("TARGET_TYPE", "schedule")
    target_slot = env("TARGET_SLOT_KST")
    decision_reason = env("DECISION_REASON")
    event_schedule = env("EVENT_SCHEDULE") or load_event_schedule()

    data = fetch_last_run()
    if not isinstance(data, dict):
        data = {}

    current_kst = now_kst()
    current_utc = utc_now()

    data.update(
        {
            "last_run_kst": current_kst.isoformat(),
            "last_run_utc": current_utc.isoformat(),
            "last_run_id": env("GITHUB_RUN_ID"),
            "last_run_attempt": env("GITHUB_RUN_ATTEMPT"),
            "last_event_name": env("GITHUB_EVENT_NAME"),
            "last_event_schedule": event_schedule,
            "last_sha": env("GITHUB_SHA"),
            "last_actor": env("GITHUB_ACTOR"),
            "last_decision_reason": decision_reason,
            "last_target_type": target_type,
            "last_target_slot_kst": target_slot,
        }
    )

    if target_type == "schedule" and target_slot:
        data.update(
            {
                "last_scheduled_slot_kst": target_slot,
                "last_scheduled_run_kst": current_kst.isoformat(),
                "last_scheduled_run_utc": current_utc.isoformat(),
                "last_scheduled_run_id": env("GITHUB_RUN_ID"),
                "last_scheduled_event_schedule": event_schedule,
            }
        )
    elif target_type == "manual":
        data.update(
            {
                "last_manual_run_kst": current_kst.isoformat(),
                "last_manual_run_utc": current_utc.isoformat(),
                "last_manual_run_id": env("GITHUB_RUN_ID"),
            }
        )
    elif target_type == "slack_replay":
        data.update(
            {
                "last_slack_replay_run_kst": current_kst.isoformat(),
                "last_slack_replay_run_utc": current_utc.isoformat(),
                "last_slack_replay_run_id": env("GITHUB_RUN_ID"),
            }
        )

    output_path = Path("public/last-run.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    append_step_summary(
        [
            "### Last-run record",
            "",
            f"- wrote: {output_path}",
            f"- target_type: {target_type}",
            f"- target_slot_kst: {target_slot or '(none)'}",
            f"- last_scheduled_slot_kst: {data.get('last_scheduled_slot_kst', '(none)')}",
            f"- kst_now: {current_kst.isoformat()}",
            f"- utc_now: {current_utc.isoformat()}",
        ]
    )
    print(f"Wrote {output_path}")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"decide", "record"}:
        raise SystemExit("Usage: python scripts/schedule_gate.py [decide|record]")

    if sys.argv[1] == "decide":
        decide()
    else:
        record()


if __name__ == "__main__":
    main()
