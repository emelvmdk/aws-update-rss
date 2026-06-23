from __future__ import annotations

import copy
import os
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from xml.etree import ElementTree as ET

FEED_PATH = Path("public/feed.xml")
DEFAULT_MAX_REPLAY_ITEMS = 10


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        return 0
    return max(0, min(count, DEFAULT_MAX_REPLAY_ITEMS))


def unique_link(link: str, replay_id: str) -> str:
    link = clean_text(link)
    if not link:
        return f"https://github.com/emelvmdk/aws-update-rss/actions#slack-replay-{replay_id}"

    parsed = urlparse(link)
    fragment = f"slack-replay-{replay_id}"
    return urlunparse(parsed._replace(fragment=fragment))


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


def select_items(channel: ET.Element, count: int, guid_prefix: str) -> list[ET.Element]:
    source_items = source_items_from(channel)
    guid_prefix = clean_text(guid_prefix)

    if guid_prefix:
        matched = [item for item in source_items if child_text(item, "guid").startswith(guid_prefix)]
        limit = count or DEFAULT_MAX_REPLAY_ITEMS
        return matched[:limit]

    if count <= 0:
        return []
    return source_items[:count]


def clone_for_replay(source_item: ET.Element, replay_id: str, index: int, now: datetime) -> ET.Element:
    item = copy.deepcopy(source_item)

    original_title = child_text(source_item, "title") or "Untitled AWS update"
    original_guid = child_text(source_item, "guid") or f"missing-guid-{index}"
    original_link = child_text(source_item, "link")
    replay_guid = f"slack-replay-{replay_id}-{index}-{original_guid[:24]}"

    set_child_text(item, "title", f"[재발송] {original_title}")
    set_child_text(item, "link", unique_link(original_link, f"{replay_id}-{index}"))
    set_child_text(item, "guid", replay_guid, isPermaLink="false")
    set_child_text(item, "pubDate", format_datetime(now))

    return item


def add_replay_items(count: int, guid_prefix: str) -> int:
    tree = ET.parse(FEED_PATH)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS channel element was not found")

    selected = select_items(channel, count, guid_prefix)
    if not selected:
        print("No RSS items matched the Slack replay request.")
        return 0

    now = datetime.now(timezone.utc)
    replay_id = env("GITHUB_RUN_ID") or now.strftime("%Y%m%d%H%M%S")
    replay_items = [clone_for_replay(item, replay_id, index + 1, now) for index, item in enumerate(selected)]

    first_item = channel.find("item")
    insert_index = list(channel).index(first_item) if first_item is not None else len(list(channel))
    for offset, item in enumerate(replay_items):
        channel.insert(insert_index + offset, item)

    FEED_PATH.write_text(
        ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8"),
        encoding="utf-8",
    )
    print(f"Added {len(replay_items)} Slack replay item(s).")
    return len(replay_items)


def main() -> None:
    count = parse_count(env("SLACK_REPLAY_RECENT_ITEMS", "0"))
    guid_prefix = env("SLACK_REPLAY_GUID_PREFIX", "")
    add_replay_items(count, guid_prefix)


if __name__ == "__main__":
    main()
