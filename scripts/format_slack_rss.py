from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from xml.etree import ElementTree as ET

FEED_PATH = Path("public/feed.xml")
SLACK_DEBUG_JSON_PATH = Path("public/slack-debug.json")
SLACK_DEBUG_HTML_PATH = Path("public/slack-debug.html")
MAX_SUMMARY_CHARS = 220
AWSUPDATE_NS = "https://github.com/emelvmdk/aws-update-rss/ns"
ET.register_namespace("awsupdate", AWSUPDATE_NS)


SEVERITY_EMOJI = {
    "Critical": "🔴",
    "High": "🔴",
    "Medium": "🟠",
    "Low": "🟢",
}


def clean_html_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def truncate(value: str, limit: int) -> str:
    value = clean_html_text(value)
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def extract_field(description: str, label: str) -> str:
    pattern = rf"<(?:strong|b)>{re.escape(label)}</(?:strong|b)>:\s*(.*?)(?:</p>|$)"
    match = re.search(pattern, description or "", flags=re.IGNORECASE | re.DOTALL)
    return clean_html_text(match.group(1)) if match else ""


def extract_href_for_label(description: str, label: str) -> str:
    pattern = rf"<(?:strong|b)>{re.escape(label)}</(?:strong|b)>:\s*<a\s+href=\"([^\"]+)\""
    match = re.search(pattern, description or "", flags=re.IGNORECASE | re.DOTALL)
    return html.unescape(match.group(1)).strip() if match else ""


def category_from_item(item: ET.Element) -> str:
    category = clean_html_text(item.findtext("category") or "")
    if category:
        return category
    title = item.findtext("title") or ""
    match = re.match(r"^\[([^\]]+)\]", title)
    return clean_html_text(match.group(1)) if match else "general"


def set_hidden_field(item: ET.Element, name: str, value: str) -> None:
    tag = f"{{{AWSUPDATE_NS}}}{name}"
    existing = item.find(tag)
    if not value:
        if existing is not None:
            item.remove(existing)
        return
    node = existing if existing is not None else ET.SubElement(item, tag)
    node.text = value


def hidden_field(item: ET.Element, name: str) -> str:
    return clean_html_text(item.findtext(f"{{{AWSUPDATE_NS}}}{name}") or "")


def canonical_link_for_debug(link: str) -> str:
    link = clean_html_text(link)
    if not link:
        return ""
    parsed = urlparse(link)
    # Ignore fragment because we may later add guid fragments to make Slack links unique.
    return urlunparse(parsed._replace(fragment=""))


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12] if value else ""


def slack_risk_label(duplicate_link_count: int, guid: str, link: str) -> str:
    risks: list[str] = []
    if duplicate_link_count > 1:
        risks.append("duplicate_link")
    if not guid:
        risks.append("missing_guid")
    if not link:
        risks.append("missing_link")
    return ", ".join(risks) if risks else "ok"


def format_description(item: ET.Element) -> str:
    description_node = item.find("description")
    old_description = description_node.text if description_node is not None and description_node.text else ""

    severity = extract_field(old_description, "중요도") or "Low"
    reason = extract_field(old_description, "판단 근거")
    summary = extract_field(old_description, "요약")
    link = clean_html_text(item.findtext("link") or "") or extract_href_for_label(old_description, "링크")
    source_link = extract_href_for_label(old_description, "영어 원문 링크")
    category = category_from_item(item)
    emoji = SEVERITY_EMOJI.get(severity, "⚪")

    set_hidden_field(item, "severity", severity)
    set_hidden_field(item, "severityReason", reason)
    set_hidden_field(item, "category", category)
    set_hidden_field(item, "sourceLink", source_link)
    set_hidden_field(item, "displayLink", link)

    lines: list[str] = [f"<p>{emoji} {html.escape(severity)}</p>"]

    if summary:
        lines.append(f"<p><b>핵심</b>: {html.escape(truncate(summary, MAX_SUMMARY_CHARS))}</p>")

    return "\n".join(lines)


def item_debug_record(item: ET.Element, duplicate_link_count: int) -> dict[str, str | int]:
    title = clean_html_text(item.findtext("title") or "")
    link = clean_html_text(item.findtext("link") or "")
    canonical_link = canonical_link_for_debug(link)
    guid = clean_html_text(item.findtext("guid") or "")
    pub_date = clean_html_text(item.findtext("pubDate") or "")
    category = clean_html_text(item.findtext("category") or hidden_field(item, "category"))
    severity = hidden_field(item, "severity")

    return {
        "title": title,
        "pubDate": pub_date,
        "guid": guid,
        "guid_prefix": guid[:12],
        "link": link,
        "canonical_link": canonical_link,
        "link_hash": short_hash(canonical_link),
        "duplicate_link_count": duplicate_link_count,
        "category": category,
        "severity": severity,
        "slack_risk": slack_risk_label(duplicate_link_count, guid, link),
    }


def write_slack_debug(channel: ET.Element) -> None:
    items = list(channel.findall("item"))
    canonical_links = [canonical_link_for_debug(item.findtext("link") or "") for item in items]
    link_counts = Counter(link for link in canonical_links if link)

    records = [
        item_debug_record(item, link_counts.get(canonical_link_for_debug(item.findtext("link") or ""), 0))
        for item in items[:50]
    ]
    duplicate_records = [record for record in records if int(record["duplicate_link_count"]) > 1]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "feed_title": clean_html_text(channel.findtext("title") or ""),
        "item_count": len(items),
        "debug_item_count": len(records),
        "duplicate_link_count": len(duplicate_records),
        "notes": [
            "Slack RSS may suppress new items when multiple RSS items reuse the same link.",
            "Check duplicate_link_count and slack_risk for items that appear in feed.xml but did not arrive in Slack.",
        ],
        "items": records,
    }

    SLACK_DEBUG_JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rows = ""
    for record in records:
        risk = str(record["slack_risk"])
        rows += (
            "<tr>"
            f"<td>{html.escape(str(record['pubDate']))}</td>"
            f"<td>{html.escape(str(record['severity']))}</td>"
            f"<td>{html.escape(str(record['category']))}</td>"
            f"<td><a href=\"{html.escape(str(record['link']))}\">{html.escape(str(record['title']))}</a></td>"
            f"<td><code>{html.escape(str(record['guid_prefix']))}</code></td>"
            f"<td><code>{html.escape(str(record['link_hash']))}</code></td>"
            f"<td>{html.escape(str(record['duplicate_link_count']))}</td>"
            f"<td>{html.escape(risk)}</td>"
            "</tr>"
        )

    SLACK_DEBUG_HTML_PATH.write_text(
        f"""<!doctype html>
<html lang=\"ko\">
<head><meta charset=\"utf-8\"><title>Slack RSS Debug</title></head>
<body>
  <h1>Slack RSS Debug</h1>
  <p>Generated at: {html.escape(str(payload['generated_at']))}</p>
  <p>Feed title: {html.escape(str(payload['feed_title']))}</p>
  <p>Total items: {len(items)} / Debugged recent items: {len(records)}</p>
  <p>Duplicate-link items in recent list: {len(duplicate_records)}</p>
  <p>JSON: <a href=\"./slack-debug.json\">slack-debug.json</a></p>
  <p><strong>Tip:</strong> If a feed item exists here but Slack did not post it, check whether <code>Duplicate link count</code> is greater than 1.</p>
  <table border=\"1\" cellpadding=\"6\" cellspacing=\"0\">
    <thead>
      <tr><th>PubDate</th><th>Severity</th><th>Category</th><th>Title</th><th>GUID</th><th>Link hash</th><th>Duplicate link count</th><th>Slack risk</th></tr>
    </thead>
    <tbody>{rows or '<tr><td colspan=\"8\">No items.</td></tr>'}</tbody>
  </table>
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> None:
    tree = ET.parse(FEED_PATH)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("RSS channel element was not found")

    count = 0
    for item in channel.findall("item"):
        description_node = item.find("description")
        if description_node is None:
            description_node = ET.SubElement(item, "description")
        description_node.text = format_description(item)
        count += 1

    write_slack_debug(channel)

    FEED_PATH.write_text(
        ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8"),
        encoding="utf-8",
    )
    print(f"Formatted {count} RSS items for Slack readability.")
    print(f"Wrote {SLACK_DEBUG_JSON_PATH} and {SLACK_DEBUG_HTML_PATH}.")


if __name__ == "__main__":
    main()
