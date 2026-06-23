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
MAX_REPLAY_ITEMS_PER_RUN = 1


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def wants_recent_replay(value: str) -> bool:
    try:
        return int(value) > 0
    except ValueError:
        return False


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


def select_one_item(channel: ET.Element, replay_recent: bool, guid_prefix: str) -> ET.Element | None:
    source_items = source_items_from(channel)
    guid_prefix = clean_text(guid_prefix)

    if guid_prefix:
        for item in source_items:
            if child_text(item, "guid").startswith(guid_prefix):
                return item
        return None

    if replay_recent:
        return source_items[0] if source_items else None

    return None


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


def add_replay_item(replay_recent: bool, guid_prefix: str) -> int:
    tree = ET.parse(FEED_PATH)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS channel element was not found")

    selected = select_one_item(channel, replay_recent, guid_prefix)
    if selected is None:
        print("No RSS item matched the Slack replay request.")
        return 0

    now = datetime.now(timezone.utc)
    replay_id = env("GITHUB_RUN_ID") or now.strftime("%Y%m%d%H%M%S")
    replay_item = clone_for_replay(selected, replay_id, now)

    first_item = channel.find("item")
    insert_index = list(channel).index(first_item) if first_item is not None else len(list(channel))
    channel.insert(insert_index, replay_item)

    FEED_PATH.write_text(
        ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8"),
        encoding="utf-8",
    )
    print(f"Added {MAX_REPLAY_ITEMS_PER_RUN} Slack replay item. Run again for another missed item.")
    return MAX_REPLAY_ITEMS_PER_RUN


def main() -> None:
    replay_recent = wants_recent_replay(env("SLACK_REPLAY_RECENT_ITEMS", "0"))
    guid_prefix = env("SLACK_REPLAY_GUID_PREFIX", "")
    add_replay_item(replay_recent, guid_prefix)


if __name__ == "__main__":
    main()
