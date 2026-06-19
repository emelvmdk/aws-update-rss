from __future__ import annotations

import html
import re
from pathlib import Path
from xml.etree import ElementTree as ET

FEED_PATH = Path("public/feed.xml")
MAX_SUMMARY_CHARS = 420
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
    pattern = rf"<strong>{re.escape(label)}</strong>:\s*(.*?)(?:</p>|$)"
    match = re.search(pattern, description or "", flags=re.IGNORECASE | re.DOTALL)
    return clean_html_text(match.group(1)) if match else ""


def extract_href_for_label(description: str, label: str) -> str:
    pattern = rf"<strong>{re.escape(label)}</strong>:\s*<a\s+href=\"([^\"]+)\""
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

    lines: list[str] = []
    lines.append(
        f"<p>{emoji} <strong>중요도</strong>: {html.escape(severity)} "
        f"· <strong>분류</strong>: {html.escape(category)}</p>"
    )

    if summary:
        lines.append(f"<p>🧭 <strong>요약</strong>: {html.escape(truncate(summary, MAX_SUMMARY_CHARS))}</p>")

    if link:
        escaped_link = html.escape(link)
        lines.append(f"<p>🔗 <a href=\"{escaped_link}\">업데이트 원문 보기</a></p>")

    if source_link and source_link != link:
        escaped_source_link = html.escape(source_link)
        lines.append(f"<p>🌐 <a href=\"{escaped_source_link}\">영어 원문 보기</a></p>")

    return "\n".join(lines)


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

    FEED_PATH.write_text(
        ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8"),
        encoding="utf-8",
    )
    print(f"Formatted {count} RSS items for Slack readability.")


if __name__ == "__main__":
    main()
