from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = ROOT / "public"
FEED_FILE = PUBLIC_DIR / "feed.xml"
REVIEW_JSON_FILE = PUBLIC_DIR / "review.json"
NOTION_SYNC_JSON_FILE = PUBLIC_DIR / "notion-sync.json"
NOTION_SYNC_HTML_FILE = PUBLIC_DIR / "notion-sync.html"

NOTION_VERSION = "2022-06-28"
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {
    "sc_channel",
    "sc_campaign",
    "sc_medium",
    "sc_publisher",
    "sc_content",
    "trk",
    "trkcampaign",
    "ref",
}

PROPERTY_TITLE = os.getenv("NOTION_PROP_TITLE", "제목")
PROPERTY_URL = os.getenv("NOTION_PROP_URL", "URL")
PROPERTY_SOURCE = os.getenv("NOTION_PROP_SOURCE", "출처")
PROPERTY_SERVICE = os.getenv("NOTION_PROP_SERVICE", "서비스")
PROPERTY_SEVERITY = os.getenv("NOTION_PROP_SEVERITY", "중요도")
PROPERTY_STATUS = os.getenv("NOTION_PROP_STATUS", "상태")
PROPERTY_MATCHED_KEYWORDS = os.getenv("NOTION_PROP_MATCHED_KEYWORDS", "매칭 키워드")
PROPERTY_REVIEW_REASON = os.getenv("NOTION_PROP_REVIEW_REASON", "검토 사유")
PROPERTY_PUBLISHED_AT = os.getenv("NOTION_PROP_PUBLISHED_AT", "게시일")
PROPERTY_COLLECTED_AT = os.getenv("NOTION_PROP_COLLECTED_AT", "수집일")
PROPERTY_SLACK_SENT = os.getenv("NOTION_PROP_SLACK_SENT", "Slack 전송")
PROPERTY_GUID = os.getenv("NOTION_PROP_GUID", "GUID")
PROPERTY_DEDUPE_KEY = os.getenv("NOTION_PROP_DEDUPE_KEY", "중복 키")


def clean_text(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def truncate(value: str, limit: int) -> str:
    value = clean_text(value)
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def canonical_url_for_dedupe(url: str) -> str:
    url = clean_text(url)
    if not url:
        return ""
    parsed = urlparse(url)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key in TRACKING_QUERY_KEYS or any(lower_key.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query.append((key, value))
    normalized_path = re.sub(r"/+$", "", parsed.path or "/") or "/"
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=normalized_path,
            query=urlencode(query, doseq=True),
            fragment="",
        )
    )


def parse_rfc2822_datetime(value: str) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def parse_iso_datetime(value: str) -> str | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def parse_category_and_title(raw_title: str) -> tuple[str, str]:
    title = clean_text(raw_title)
    match = re.fullmatch(r"\[([^\]]+)]\s*(.+)", title)
    if match:
        return clean_text(match.group(1)), clean_text(match.group(2))
    return "general", title


def extract_description_field(description: str, label: str) -> str:
    pattern = rf"<strong>\s*{re.escape(label)}\s*</strong>\s*:\s*(.*?)</p>"
    match = re.search(pattern, description or "", flags=re.IGNORECASE | re.DOTALL)
    return clean_text(match.group(1)) if match else ""


def load_feed_items() -> list[dict[str, Any]]:
    if not FEED_FILE.exists():
        return []

    tree = ET.parse(FEED_FILE)
    channel = tree.getroot().find("channel")
    if channel is None:
        return []

    items: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        raw_title = item.findtext("title", default="")
        category, title = parse_category_and_title(raw_title)
        link = clean_text(item.findtext("link", default=""))
        description = item.findtext("description", default="") or ""
        severity = extract_description_field(description, "중요도") or "Low"
        summary = extract_description_field(description, "요약")
        guid = clean_text(item.findtext("guid", default=""))
        published_at = parse_rfc2822_datetime(item.findtext("pubDate", default=""))
        dedupe_key = canonical_url_for_dedupe(link) or guid

        if not link and not guid:
            continue

        items.append(
            {
                "kind": "feed",
                "title": title or raw_title or "Untitled",
                "url": link,
                "source": "feed.xml",
                "service": [category] if category else [],
                "severity": severity,
                "status": "미확인",
                "matched_keywords": [],
                "review_reason": "",
                "published_at": published_at,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "slack_sent": True,
                "guid": guid,
                "dedupe_key": dedupe_key,
                "summary": summary,
            }
        )
    return items


def load_review_items() -> list[dict[str, Any]]:
    if os.getenv("NOTION_SYNC_REVIEW_ITEMS", "true").lower() not in {"1", "true", "yes", "y"}:
        return []
    if not REVIEW_JSON_FILE.exists():
        return []

    payload = json.loads(REVIEW_JSON_FILE.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = []
    for item in payload.get("items", []):
        link = clean_text(item.get("link", ""))
        title = clean_text(item.get("title", "Untitled"))
        reason = clean_text(item.get("review_reason", ""))
        dedupe_key = "review|" + (canonical_url_for_dedupe(link) or title)
        published_at = parse_iso_datetime(clean_text(item.get("published_at", "")))
        items.append(
            {
                "kind": "review",
                "title": title,
                "url": link,
                "source": clean_text(item.get("source", "review.json")),
                "service": [clean_text(item.get("category", "general"))],
                "severity": "Low",
                "status": "Boundary",
                "matched_keywords": [clean_text(keyword) for keyword in item.get("matched_keywords", []) if clean_text(keyword)],
                "review_reason": reason,
                "published_at": published_at,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "slack_sent": False,
                "guid": "",
                "dedupe_key": dedupe_key,
                "summary": f"검토 사유: {reason}" if reason else "",
            }
        )
    return items


class NotionClient:
    def __init__(self, token: str, database_id: str) -> None:
        self.database_id = database_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(5):
            response = self.session.request(method, url, timeout=30, **kwargs)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            retry_after = int(response.headers.get("Retry-After", "1"))
            time.sleep(max(retry_after, 1))
        response.raise_for_status()
        return response

    def find_page_by_dedupe_key(self, dedupe_key: str) -> str | None:
        if not dedupe_key:
            return None
        payload = {
            "filter": {
                "property": PROPERTY_DEDUPE_KEY,
                "rich_text": {"equals": dedupe_key},
            },
            "page_size": 1,
        }
        response = self.request("POST", f"https://api.notion.com/v1/databases/{self.database_id}/query", json=payload)
        results = response.json().get("results", [])
        return results[0].get("id") if results else None

    def create_page(self, item: dict[str, Any]) -> None:
        payload = {
            "parent": {"database_id": self.database_id},
            "properties": notion_properties(item),
            "children": notion_children(item),
        }
        self.request("POST", "https://api.notion.com/v1/pages", json=payload)

    def update_page(self, page_id: str, item: dict[str, Any]) -> None:
        # Do not overwrite manual review state. Only refresh machine-owned metadata.
        properties = notion_properties(item, include_status=False)
        self.request("PATCH", f"https://api.notion.com/v1/pages/{page_id}", json={"properties": properties})


def title_property(value: str) -> dict[str, Any]:
    return {"title": [{"text": {"content": truncate(value, 180)}}]}


def rich_text_property(value: str) -> dict[str, Any]:
    return {"rich_text": [{"text": {"content": truncate(value, 1900)}}]} if value else {"rich_text": []}


def url_property(value: str) -> dict[str, Any]:
    return {"url": value or None}


def select_property(value: str) -> dict[str, Any]:
    return {"select": {"name": truncate(value, 90)} if value else None}


def multi_select_property(values: list[str]) -> dict[str, Any]:
    names = []
    seen: set[str] = set()
    for value in values:
        name = truncate(value, 90)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append({"name": name})
    return {"multi_select": names}


def date_property(value: str | None) -> dict[str, Any]:
    return {"date": {"start": value} if value else None}


def checkbox_property(value: bool) -> dict[str, Any]:
    return {"checkbox": bool(value)}


def notion_properties(item: dict[str, Any], include_status: bool = True) -> dict[str, Any]:
    properties = {
        PROPERTY_TITLE: title_property(item["title"]),
        PROPERTY_URL: url_property(item.get("url", "")),
        PROPERTY_SOURCE: select_property(item.get("source", "")),
        PROPERTY_SERVICE: multi_select_property(item.get("service", [])),
        PROPERTY_SEVERITY: select_property(item.get("severity", "")),
        PROPERTY_MATCHED_KEYWORDS: multi_select_property(item.get("matched_keywords", [])),
        PROPERTY_REVIEW_REASON: rich_text_property(item.get("review_reason", "")),
        PROPERTY_PUBLISHED_AT: date_property(item.get("published_at")),
        PROPERTY_COLLECTED_AT: date_property(item.get("collected_at")),
        PROPERTY_SLACK_SENT: checkbox_property(item.get("slack_sent", False)),
        PROPERTY_GUID: rich_text_property(item.get("guid", "")),
        PROPERTY_DEDUPE_KEY: rich_text_property(item.get("dedupe_key", "")),
    }
    if include_status:
        properties[PROPERTY_STATUS] = select_property(item.get("status", "미확인"))
    return properties


def notion_children(item: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    summary = clean_text(item.get("summary", ""))
    if summary:
        lines.append(f"요약: {summary}")
    if item.get("url"):
        lines.append(f"링크: {item['url']}")
    if item.get("review_reason"):
        lines.append(f"검토 사유: {item['review_reason']}")
    if item.get("matched_keywords"):
        lines.append(f"매칭 키워드: {', '.join(item['matched_keywords'])}")
    if not lines:
        lines.append("자동 수집된 AWS 업데이트 항목입니다.")

    text = "\n".join(lines)
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": truncate(text, 1900)}}],
            },
        }
    ]


def write_sync_report(report: dict[str, Any]) -> None:
    PUBLIC_DIR.mkdir(exist_ok=True)
    NOTION_SYNC_JSON_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    errors = "".join(f"<li>{html.escape(error)}</li>" for error in report.get("errors", [])) or "<li>No errors</li>"
    NOTION_SYNC_HTML_FILE.write_text(
        f"""<!doctype html>
<html lang=\"ko\">
<head><meta charset=\"utf-8\"><title>Notion Sync Status</title></head>
<body>
  <h1>Notion Sync Status</h1>
  <p>Generated at: {html.escape(report['generated_at'])}</p>
  <p>Enabled: {html.escape(str(report['enabled']))}</p>
  <p>Loaded items: {report['loaded_items']}</p>
  <p>Created: {report['created']}</p>
  <p>Updated: {report['updated']}</p>
  <p>Skipped: {report['skipped']}</p>
  <h2>Errors</h2>
  <ul>{errors}</ul>
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> None:
    token = os.getenv("NOTION_TOKEN", "").strip()
    database_id = os.getenv("NOTION_DATABASE_ID", "").strip()
    strict = os.getenv("NOTION_SYNC_STRICT", "false").lower() in {"1", "true", "yes", "y"}

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": bool(token and database_id),
        "loaded_items": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }

    try:
        items = load_feed_items() + load_review_items()
        report["loaded_items"] = len(items)

        if not token or not database_id:
            report["skipped"] = len(items)
            report["errors"].append("NOTION_TOKEN or NOTION_DATABASE_ID is not configured. Notion sync was skipped.")
            write_sync_report(report)
            print("Notion sync skipped because NOTION_TOKEN or NOTION_DATABASE_ID is missing.")
            return

        client = NotionClient(token, database_id)
        for item in items:
            try:
                existing_page_id = client.find_page_by_dedupe_key(item.get("dedupe_key", ""))
                if existing_page_id:
                    client.update_page(existing_page_id, item)
                    report["updated"] += 1
                else:
                    client.create_page(item)
                    report["created"] += 1
                time.sleep(float(os.getenv("NOTION_SYNC_SLEEP_SECONDS", "0.4")))
            except Exception as exc:
                report["errors"].append(f"{item.get('title', 'Untitled')}: {exc}")
                if strict:
                    raise
    except Exception as exc:
        report["errors"].append(str(exc))
        if strict:
            raise
    finally:
        write_sync_report(report)

    print(
        "Notion sync completed: "
        f"created={report['created']}, updated={report['updated']}, "
        f"skipped={report['skipped']}, errors={len(report['errors'])}"
    )


if __name__ == "__main__":
    main()
