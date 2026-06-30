from __future__ import annotations

import copy
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

FEED_PATH = Path("public/feed.xml")
QUEUE_PATH = Path("public/slack-replay-queue.json")
DEFAULT_MAX_REQUEST_ITEMS = 10
DEFAULT_RELEASE_INTERVAL_MINUTES = 15


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_count(value: str) -> int:
    try:
        count = int(value or "0")
    except ValueError:
        return 0
    return max(0, min(count, DEFAULT_MAX_REQUEST_ITEMS))


def split_prefixes(*values: str) -> list[str]:
    prefixes: list[str] = []
    for value in values:
        for part in re.split(r"[\s,]+", value or ""):
            part = clean_text(part)
            if part and part not in prefixes:
                prefixes.append(part)
    return prefixes


def pages_base_url() -> str:
    configured = env("PAGES_BASE_URL")
    if configured:
        return configured.rstrip("/")

    repository = env("GITHUB_REPOSITORY")
    if "/" not in repository:
        return ""
    owner, repo = repository.split("/", 1)
    return f"https://{owner}.github.io/{repo}"


def queue_url() -> str:
    base = pages_base_url()
    return f"{base}/slack-replay-queue.json" if base else ""


def with_cache_bust(url: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}t={int(time.time())}"


def fetch_existing_queue() -> dict[str, Any]:
    url = queue_url()
    if not url:
        return {}

    request = Request(
        with_cache_bust(url),
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "aws-update-rss-slack-replay-queue",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
    except HTTPError as exc:
        if exc.code == 404:
            return {}
        print(f"Could not fetch slack replay queue: HTTP {exc.code}")
        return {}
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Could not fetch slack replay queue: {exc}")
        return {}


def empty_queue() -> dict[str, Any]:
    return {
        "generated_at": iso_now(),
        "release_interval_minutes": DEFAULT_RELEASE_INTERVAL_MINUTES,
        "pending": [],
        "released": [],
    }


def normalize_queue(data: dict[str, Any]) -> dict[str, Any]:
    queue = empty_queue()
    queue.update({k: v for k, v in data.items() if k in queue or k in {"last_released_at", "last_release_run_id"}})
    if not isinstance(queue.get("pending"), list):
        queue["pending"] = []
    if not isinstance(queue.get("released"), list):
        queue["released"] = []
    try:
        queue["release_interval_minutes"] = int(queue.get("release_interval_minutes") or DEFAULT_RELEASE_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        queue["release_interval_minutes"] = DEFAULT_RELEASE_INTERVAL_MINUTES
    return queue


def unique_link(link: str, replay_id: str) -> str:
    link = clean_text(link)
    if not link:
        return f"https://github.com/emelvmdk/aws-update-rss/actions/runs/{replay_id}"

    parsed = urlparse(link)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("aws-update-rss-replay", replay_id))
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


def child_text(item: ET.Element, name: str) -> str:
    return clean_text(item.findtext(name))


def set_child_text(item: ET.Element, name: str, value: str, **attrib: str) -> None:
    child = item.find(name)
    if child is None:
        child = ET.SubElement(item, name, attrib)
    else:
        child.attrib.update(attrib)
    child.text = value


def source_items_from(channel: ET.Element) -> list[ET.Element]:
    return [item for item in channel.findall("item") if not child_text(item, "guid").startswith("slack-replay-")]


def item_summary(item: ET.Element, request_source: str) -> dict[str, str]:
    return {
        "guid": child_text(item, "guid"),
        "guid_prefix": child_text(item, "guid")[:12],
        "title": child_text(item, "title"),
        "link": child_text(item, "link"),
        "category": child_text(item, "category"),
        "requested_at": iso_now(),
        "request_source": request_source,
    }


def queue_has_guid(queue: dict[str, Any], guid: str) -> bool:
    return any(clean_text(item.get("guid")) == guid for item in queue.get("pending", []))


def add_recent_requests(queue: dict[str, Any], source_items: list[ET.Element], count: int) -> int:
    added = 0
    for item in source_items[:count]:
        guid = child_text(item, "guid")
        if not guid or queue_has_guid(queue, guid):
            continue
        queue["pending"].append(item_summary(item, "recent"))
        added += 1
    return added


def add_guid_prefix_requests(queue: dict[str, Any], source_items: list[ET.Element], prefixes: list[str]) -> int:
    added = 0
    for prefix in prefixes:
        matched = next((item for item in source_items if child_text(item, "guid").startswith(prefix)), None)
        if matched is None:
            print(f"No source item matched GUID prefix: {prefix}")
            continue
        guid = child_text(matched, "guid")
        if not guid or queue_has_guid(queue, guid):
            continue
        queue["pending"].append(item_summary(matched, f"guid_prefix:{prefix}"))
        added += 1
    return added


def parse_utc(value: str) -> datetime | None:
    value = clean_text(value)
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except ValueError:
        return None


def release_due(queue: dict[str, Any], force: bool) -> bool:
    if force:
        return True
    last_released = parse_utc(str(queue.get("last_released_at") or ""))
    if last_released is None:
        return True
    interval = timedelta(minutes=int(queue.get("release_interval_minutes") or DEFAULT_RELEASE_INTERVAL_MINUTES))
    return utc_now() - last_released >= interval


def find_source_by_guid(source_items: list[ET.Element], guid: str) -> ET.Element | None:
    return next((item for item in source_items if child_text(item, "guid") == guid), None)


def clone_for_replay(source_item: ET.Element, replay_id: str, now: datetime) -> ET.Element:
    item = copy.deepcopy(source_item)

    original_title = child_text(source_item, "title") or "Untitled AWS update"
    original_guid = child_text(source_item, "guid") or "missing-guid"
    original_link = child_text(source_item, "link")
    replay_guid = f"slack-replay-{replay_id}-{original_guid[:24]}"

    set_child_text(item, "title", f"[재발송] {original_title}")
    set_child_text(item, "link", unique_link(original_link, replay_id))
    set_child_text(item, "guid", replay_guid, isPermaLink="false")
    set_child_text(item, "pubDate", format_datetime(now))

    return item


def insert_replay_item(channel: ET.Element, source_item: ET.Element) -> dict[str, str]:
    now = utc_now()
    replay_id = env("GITHUB_RUN_ID") or now.strftime("%Y%m%d%H%M%S")
    replay_item = clone_for_replay(source_item, replay_id, now)

    first_item = channel.find("item")
    insert_index = list(channel).index(first_item) if first_item is not None else len(list(channel))
    channel.insert(insert_index, replay_item)

    return {
        "guid": child_text(source_item, "guid"),
        "replay_guid": child_text(replay_item, "guid"),
        "title": child_text(source_item, "title"),
        "released_at": now.isoformat(),
        "run_id": env("GITHUB_RUN_ID"),
    }


def process_replay_queue() -> None:
    tree = ET.parse(FEED_PATH)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS channel element was not found")

    source_items = source_items_from(channel)
    queue = normalize_queue(fetch_existing_queue())

    requested_recent_count = parse_count(env("SLACK_REPLAY_RECENT_ITEMS", "0"))
    requested_prefixes = split_prefixes(env("SLACK_REPLAY_GUID_PREFIX", ""), env("SLACK_REPLAY_GUID_PREFIXES", ""))
    has_manual_request = requested_recent_count > 0 or bool(requested_prefixes)

    added_recent = add_recent_requests(queue, source_items, requested_recent_count)
    added_prefixes = add_guid_prefix_requests(queue, source_items, requested_prefixes)
    added_total = added_recent + added_prefixes

    released_record: dict[str, str] | None = None
    pending = queue.get("pending", [])
    if pending and release_due(queue, force=has_manual_request):
        next_request = pending.pop(0)
        source_item = find_source_by_guid(source_items, clean_text(next_request.get("guid")))
        if source_item is None:
            print(f"Queued source item no longer exists in current feed: {next_request.get('guid')}")
        else:
            released_record = insert_replay_item(channel, source_item)
            released_record.update({"request_source": clean_text(next_request.get("request_source"))})
            queue.setdefault("released", []).insert(0, released_record)
            queue["released"] = queue["released"][:50]
            queue["last_released_at"] = released_record["released_at"]
            queue["last_release_run_id"] = env("GITHUB_RUN_ID")

    queue["generated_at"] = iso_now()
    queue["pending_count"] = len(queue.get("pending", []))
    queue["released_count"] = len(queue.get("released", []))
    queue["last_added_count"] = added_total
    queue["last_released_title"] = released_record.get("title") if released_record else ""

    QUEUE_PATH.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    FEED_PATH.write_text(
        ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8"),
        encoding="utf-8",
    )

    if released_record:
        print(f"Released 1 Slack replay item: {released_record['title']}")
    else:
        print("No Slack replay item released in this run.")
    print(f"Slack replay queue pending: {queue['pending_count']} item(s).")


def main() -> None:
    process_replay_queue()


if __name__ == "__main__":
    main()
