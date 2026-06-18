from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
from xml.etree import ElementTree as ET

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup


ROOT = Path(__file__).parent
PUBLIC_DIR = ROOT / "public"
OUTPUT_FILE = PUBLIC_DIR / "feed.xml"
STATUS_FILE = PUBLIC_DIR / "status.html"
INDEX_FILE = PUBLIC_DIR / "index.html"


def clean_text(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def truncate(value: str, limit: int) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> dict[str, Any]:
    return load_yaml(ROOT / "config.yaml")


def load_feeds() -> list[dict[str, Any]]:
    feeds: list[dict[str, Any]] = []
    for path in sorted(ROOT.glob("feeds*.yaml")):
        data = load_yaml(path)
        feeds.extend(data.get("feeds", []))
    return [feed for feed in feeds if feed.get("enabled", True)]


def make_session(config: dict[str, Any]) -> requests.Session:
    session = requests.Session()
    user_agent = config.get("fetch", {}).get(
        "user_agent",
        "aws-update-rss/1.0",
    )
    session.headers.update({"User-Agent": user_agent})
    return session


def parse_entry_datetime(entry: Any) -> datetime:
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass

    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass

    return datetime.now(timezone.utc)


def entry_text(entry: Any) -> str:
    parts = [
        entry.get("title", ""),
        entry.get("summary", ""),
        entry.get("description", ""),
        entry.get("link", ""),
    ]
    tags = entry.get("tags") or []
    for tag in tags:
        parts.append(tag.get("term", ""))
    return clean_text(" ".join(str(part) for part in parts)).lower()


def matched_keywords(text: str, keywords: list[str]) -> list[str]:
    lower_text = text.lower()
    return [keyword for keyword in keywords if keyword.lower() in lower_text]


def should_include(entry: Any, feed: dict[str, Any], config: dict[str, Any]) -> tuple[bool, list[str]]:
    mode = feed.get("filter_mode", "all")
    text = entry_text(entry)

    if mode == "all":
        return True, []

    include_keywords = config.get("what_new_filter", {}).get("include_keywords", [])
    exclude_keywords = config.get("what_new_filter", {}).get("exclude_keywords", [])

    if matched_keywords(text, exclude_keywords):
        return False, []

    matches = matched_keywords(text, include_keywords)
    return bool(matches), matches


def detect_severity(text: str, config: dict[str, Any]) -> str:
    rules = config.get("severity_rules", {})
    lower_text = text.lower()
    for severity in ("high", "medium", "low"):
        for keyword in rules.get(severity, []):
            if keyword.lower() in lower_text:
                return severity.capitalize()
    return "Low"


def localized_url_candidate(url: str, config: dict[str, Any]) -> str | None:
    if not url:
        return None

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or "/"

    if host == "docs.aws.amazon.com":
        locale = config.get("localization", {}).get("preferred_docs_locale", "ko_kr")
        parts = [part for part in path.split("/") if part]
        if parts and parts[0] == locale:
            return url
        new_path = "/" + "/".join([locale] + parts)
        if path.endswith("/") and not new_path.endswith("/"):
            new_path += "/"
        return urlunparse(parsed._replace(path=new_path))

    if host == "aws.amazon.com":
        locale = config.get("localization", {}).get("preferred_aws_site_locale", "ko")
        parts = [part for part in path.split("/") if part]
        if parts and parts[0] == locale:
            return url
        new_path = "/" + "/".join([locale] + parts)
        if path.endswith("/") and not new_path.endswith("/"):
            new_path += "/"
        return urlunparse(parsed._replace(path=new_path))

    return None


def fetch_html(session: requests.Session, url: str, timeout: int) -> tuple[str, str] | None:
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        if response.status_code >= 400:
            return None
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower() and len(response.text) < 500:
            return None
        if "Page Not Found" in response.text or "404 -" in response.text:
            return None
        return response.url, response.text
    except Exception:
        return None


def extract_page_summary(html_doc: str) -> tuple[str, str]:
    soup = BeautifulSoup(html_doc, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    title = ""
    for selector in [
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
        "h1",
        "title",
    ]:
        node = soup.select_one(selector)
        if not node:
            continue
        title = clean_text(node.get("content") if node.name == "meta" else node.get_text(" "))
        if title:
            break

    description = ""
    for selector in [
        'meta[name="description"]',
        'meta[property="og:description"]',
        'meta[name="twitter:description"]',
    ]:
        node = soup.select_one(selector)
        if node:
            description = clean_text(node.get("content"))
            if description:
                break

    paragraphs: list[str] = []
    for node in soup.select("main p, article p, #main p, .lb-txt p, p"):
        text = clean_text(node.get_text(" "))
        if len(text) < 45:
            continue
        lowered = text.lower()
        if "cookie" in lowered or "privacy" in lowered:
            continue
        if text not in paragraphs:
            paragraphs.append(text)
        if len(paragraphs) >= 2:
            break

    summary = description or " ".join(paragraphs)
    return title, summary


def enrich_entry(
    session: requests.Session,
    entry: Any,
    feed: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, str]:
    timeout = int(config.get("fetch", {}).get("timeout_seconds", 20))
    original_link = entry.get("link", "")
    rss_title = clean_text(entry.get("title", "Untitled"))
    rss_summary = clean_text(entry.get("summary", entry.get("description", "")))

    candidates: list[tuple[str, str]] = []
    if config.get("localization", {}).get("enabled", True):
        localized = localized_url_candidate(original_link, config)
        if localized:
            lang = "ko" if "aws.amazon.com/ko/" in localized else "ko_kr"
            candidates.append((localized, lang))

    if config.get("localization", {}).get("fallback_to_original", True):
        candidates.append((original_link, "en fallback"))

    seen_urls: set[str] = set()
    for candidate_url, language in candidates:
        if not candidate_url or candidate_url in seen_urls:
            continue
        seen_urls.add(candidate_url)
        fetched = fetch_html(session, candidate_url, timeout)
        if not fetched:
            continue
        final_url, html_doc = fetched
        page_title, page_summary = extract_page_summary(html_doc)
        if page_title or page_summary:
            return {
                "link": final_url or candidate_url,
                "language": language,
                "title": page_title or rss_title,
                "page_summary": page_summary,
                "rss_summary": rss_summary,
            }

    return {
        "link": original_link,
        "language": "rss-only fallback",
        "title": rss_title,
        "page_summary": "",
        "rss_summary": rss_summary,
    }


def build_korean_summary(
    title: str,
    source_name: str,
    category: str,
    severity: str,
    detail: str,
    config: dict[str, Any],
) -> str:
    category_hints = config.get("category_hints", {})
    hint = category_hints.get(category, "서비스 변경 내용과 운영 영향 여부를 확인하세요.")
    title_part = f"제목은 '{title}'입니다." if title else "새 업데이트가 감지되었습니다."
    detail_part = f" 주요 내용: {truncate(detail, 220)}" if detail else ""
    return f"{source_name}에 새 AWS 업데이트가 있습니다. {title_part} 중요도는 {severity}로 분류했습니다. {hint}{detail_part}"


def make_guid(feed_name: str, entry: Any) -> str:
    raw = "|".join(
        [
            feed_name,
            clean_text(entry.get("id", "")),
            clean_text(entry.get("guid", "")),
            clean_text(entry.get("link", "")),
            clean_text(entry.get("title", "")),
            clean_text(entry.get("published", entry.get("updated", ""))),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def item_to_rss_description(
    title: str,
    source_name: str,
    category: str,
    severity: str,
    language: str,
    matched: list[str],
    page_summary: str,
    rss_summary: str,
    link: str,
    config: dict[str, Any],
) -> str:
    summary_config = config.get("summary", {})
    max_detail_chars = int(summary_config.get("max_detail_chars", 900))
    detail = page_summary or rss_summary
    korean_summary = build_korean_summary(title, source_name, category, severity, detail, config)

    lines = [
        f"<p><strong>요약</strong>: {html.escape(truncate(korean_summary, int(summary_config.get('max_summary_chars', 700))))}</p>",
        f"<p><strong>출처</strong>: {html.escape(source_name)}</p>",
        f"<p><strong>분류</strong>: {html.escape(category)}</p>",
    ]

    if summary_config.get("include_severity", True):
        lines.append(f"<p><strong>중요도</strong>: {html.escape(severity)}</p>")
    if summary_config.get("include_language_status", True):
        lines.append(f"<p><strong>언어</strong>: {html.escape(language)}</p>")
    if summary_config.get("include_matched_keywords", True) and matched:
        lines.append(f"<p><strong>매칭 키워드</strong>: {html.escape(', '.join(matched))}</p>")

    if detail:
        lines.append(f"<p><strong>상세</strong>: {html.escape(truncate(detail, max_detail_chars))}</p>")
    if rss_summary and rss_summary != detail:
        lines.append(f"<p><strong>RSS 원문 설명</strong>: {html.escape(truncate(rss_summary, max_detail_chars))}</p>")

    if link:
        lines.append(f"<p><strong>원문</strong>: <a href=\"{html.escape(link)}\">{html.escape(link)}</a></p>")

    return "\n".join(lines)


def collect_items(config: dict[str, Any], feeds: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    session = make_session(config)
    timeout = int(config.get("fetch", {}).get("timeout_seconds", 20))
    failures: list[str] = []
    items: list[dict[str, Any]] = []
    max_age_days = int(config.get("output", {}).get("max_age_days", 30))
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    for feed in feeds:
        name = feed.get("name", "Unnamed feed")
        url = feed.get("url")
        if not url:
            failures.append(f"{name}: missing URL")
            continue

        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
        except Exception as exc:
            failures.append(f"{name}: feed fetch failed: {exc}")
            continue

        parsed = feedparser.parse(response.text)
        if parsed.bozo:
            failures.append(f"{name}: feed parse warning: {parsed.bozo_exception}")

        for entry in parsed.entries:
            published_at = parse_entry_datetime(entry)
            if published_at < cutoff:
                continue

            included, matches = should_include(entry, feed, config)
            if not included:
                continue

            enriched = enrich_entry(session, entry, feed, config)
            source_name = name
            category = feed.get("category", "general")
            title = enriched["title"] or clean_text(entry.get("title", "Untitled"))
            combined_text = " ".join(
                [
                    title,
                    enriched.get("page_summary", ""),
                    enriched.get("rss_summary", ""),
                    source_name,
                    category,
                ]
            )
            severity = detect_severity(combined_text, config)
            description = item_to_rss_description(
                title=title,
                source_name=source_name,
                category=category,
                severity=severity,
                language=enriched["language"],
                matched=matches,
                page_summary=enriched.get("page_summary", ""),
                rss_summary=enriched.get("rss_summary", ""),
                link=enriched["link"],
                config=config,
            )

            items.append(
                {
                    "title": f"[{category}] {title}",
                    "link": enriched["link"] or entry.get("link", ""),
                    "guid": make_guid(source_name, entry),
                    "published_at": published_at,
                    "description": description,
                    "source": source_name,
                    "category": category,
                    "severity": severity,
                }
            )

    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        deduped[item["guid"]] = item

    sorted_items = sorted(
        deduped.values(),
        key=lambda item: item["published_at"],
        reverse=True,
    )
    max_items = int(config.get("output", {}).get("max_items", 100))
    return sorted_items[:max_items], failures


def build_rss(config: dict[str, Any], items: list[dict[str, Any]]) -> str:
    output = config.get("output", {})
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = output.get("title", "AWS Update RSS")
    ET.SubElement(channel, "link").text = output.get("link", "https://aws.amazon.com/new/")
    ET.SubElement(channel, "description").text = output.get("description", "Filtered AWS updates")
    ET.SubElement(channel, "language").text = output.get("language", "ko-KR")
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    for item_data in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = item_data["title"]
        ET.SubElement(item, "link").text = item_data["link"]
        ET.SubElement(item, "guid", isPermaLink="false").text = item_data["guid"]
        ET.SubElement(item, "pubDate").text = format_datetime(item_data["published_at"])
        ET.SubElement(item, "description").text = item_data["description"]
        ET.SubElement(item, "category").text = item_data["category"]

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True).decode("utf-8")


def write_index(items: list[dict[str, Any]]) -> None:
    latest = ""
    for item in items[:20]:
        latest += (
            f"<li><strong>{html.escape(item['severity'])}</strong> "
            f"<a href=\"{html.escape(item['link'])}\">{html.escape(item['title'])}</a> "
            f"<small>{html.escape(item['published_at'].isoformat())}</small></li>"
        )
    INDEX_FILE.write_text(
        f"""<!doctype html>
<html lang=\"ko\">
<head><meta charset=\"utf-8\"><title>AWS Update RSS</title></head>
<body>
  <h1>AWS Update RSS</h1>
  <p>Filtered RSS: <a href=\"./feed.xml\">feed.xml</a></p>
  <h2>Latest items</h2>
  <ol>{latest}</ol>
  <p>Status: <a href=\"./status.html\">status.html</a></p>
</body>
</html>
""",
        encoding="utf-8",
    )


def write_status(items: list[dict[str, Any]], failures: list[str], feeds: list[dict[str, Any]]) -> None:
    failure_html = "".join(f"<li>{html.escape(failure)}</li>" for failure in failures) or "<li>No failures</li>"
    feed_html = "".join(
        f"<li>{html.escape(feed.get('name', 'Unnamed'))} - {html.escape(feed.get('category', 'general'))}</li>"
        for feed in feeds
    )
    STATUS_FILE.write_text(
        f"""<!doctype html>
<html lang=\"ko\">
<head><meta charset=\"utf-8\"><title>AWS Update RSS Status</title></head>
<body>
  <h1>Status</h1>
  <p>Generated at: {html.escape(datetime.now(timezone.utc).isoformat())}</p>
  <p>Item count: {len(items)}</p>
  <p>Feed count: {len(feeds)}</p>
  <h2>Failures / warnings</h2>
  <ul>{failure_html}</ul>
  <h2>Feeds</h2>
  <ul>{feed_html}</ul>
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> None:
    config = load_config()
    feeds = load_feeds()
    items, failures = collect_items(config, feeds)

    PUBLIC_DIR.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(build_rss(config, items), encoding="utf-8")
    write_index(items)
    write_status(items, failures, feeds)

    print(f"Generated {OUTPUT_FILE} with {len(items)} items from {len(feeds)} feeds.")
    if failures:
        print("Warnings:")
        for failure in failures:
            print(f"- {failure}")


if __name__ == "__main__":
    main()
