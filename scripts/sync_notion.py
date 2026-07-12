from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import generate_feed as gf

PUBLIC_DIR = ROOT / "public"
REVIEW_JSON_FILE = PUBLIC_DIR / "review.json"
NOTION_SYNC_JSON_FILE = PUBLIC_DIR / "notion-sync.json"
NOTION_SYNC_HTML_FILE = PUBLIC_DIR / "notion-sync.html"
NOTION_ITEMS_JSON_FILE = PUBLIC_DIR / "items.json"
NOTION_VERSION = "2022-06-28"

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
    text = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def truncate(value: str, limit: int) -> str:
    value = clean_text(value)
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def canonical_url(url: str) -> str:
    parsed = urlparse(clean_text(url))
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in gf.TRACKING_QUERY_KEYS
    ]
    path = re.sub(r"/+$", "", parsed.path or "/") or "/"
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=path,
            query=urlencode(query),
            fragment="",
        )
    )


def description_field(description: str, label: str) -> str:
    match = re.search(
        rf"<strong>\s*{re.escape(label)}\s*</strong>\s*:\s*(.*?)</p>",
        description or "",
        re.I | re.S,
    )
    return clean_text(match.group(1)) if match else ""


def match_fields(entry: Any, keywords: list[str]) -> list[str]:
    fields: list[str] = []
    if gf.matched_keywords(clean_text(entry.get("title", "")).lower(), keywords):
        fields.append("title")
    if gf.matched_keywords(gf.entry_body_text(entry), keywords):
        fields.append("body")
    if gf.matched_keywords(gf.url_match_text(entry.get("link", "")), keywords):
        fields.append("url")
    return fields


def collect_enriched_items() -> tuple[list[dict[str, Any]], list[str]]:
    config = gf.load_config()
    feeds = gf.load_feeds()
    metadata_by_entry: dict[int, dict[str, Any]] = {}
    metadata_by_guid: dict[str, dict[str, Any]] = {}
    original_evaluate = gf.evaluate_include
    original_make_guid = gf.make_guid

    def wrapped_evaluate(entry: Any, feed: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, list[str], str]:
        included, matches, reason = original_evaluate(entry, feed, cfg)
        score_reasons: list[str] = []
        if matches and gf.feed_requires_url_hint(feed):
            contextual = gf.matched_keywords(
                gf.entry_text(entry),
                cfg.get("what_new_filter", {}).get("contextual_keywords", []),
            )
            relevance = gf.matched_keywords(
                gf.entry_text(entry),
                cfg.get("what_new_filter", {}).get("relevance_keywords", []),
            )
            if contextual and relevance:
                _, score_reasons, _ = gf.score_broad_keyword_match(entry, contextual, relevance)
        metadata_by_entry[id(entry)] = {
            "matched_keywords": matches,
            "matched_fields": match_fields(entry, matches),
            "filter_reason": reason,
            "filter_reasons": score_reasons,
        }
        return included, matches, reason

    def wrapped_make_guid(feed_name: str, entry: Any) -> str:
        guid = original_make_guid(feed_name, entry)
        metadata_by_guid[guid] = metadata_by_entry.get(id(entry), {})
        return guid

    gf.evaluate_include = wrapped_evaluate
    gf.make_guid = wrapped_make_guid
    try:
        items, failures, review_items = gf.collect_items(config, feeds)
    finally:
        gf.evaluate_include = original_evaluate
        gf.make_guid = original_make_guid

    now = datetime.now(timezone.utc).isoformat()
    notion_items: list[dict[str, Any]] = []
    for item in items:
        meta = metadata_by_guid.get(item.get("guid", ""), {})
        title = re.sub(r"^\[[^]]+]\s*", "", item.get("title", ""))
        reasons = list(meta.get("filter_reasons", [])) + list(item.get("severity_reasons", []))
        fields = meta.get("matched_fields", [])
        if fields:
            reasons.insert(0, "matched fields: " + ", ".join(fields))
        notion_items.append(
            {
                "title": clean_text(title) or "Untitled",
                "url": clean_text(item.get("link", "")),
                "source": "feed.xml",
                "service": [clean_text(item.get("category", "general"))],
                "severity": clean_text(item.get("severity", "Low")),
                "status": "미확인",
                "matched_keywords": meta.get("matched_keywords", []),
                "review_reason": "; ".join(reasons),
                "published_at": item["published_at"].isoformat(),
                "collected_at": now,
                "slack_sent": True,
                "guid": clean_text(item.get("guid", "")),
                "dedupe_key": canonical_url(item.get("link", "")) or clean_text(item.get("guid", "")),
                "summary": description_field(item.get("description", ""), "요약"),
                "matched_fields": fields,
            }
        )

    for item in review_items:
        link = clean_text(item.get("link", ""))
        reason = clean_text(item.get("review_reason", ""))
        notion_items.append(
            {
                "title": clean_text(item.get("title", "Untitled")),
                "url": link,
                "source": "review.json",
                "service": [clean_text(item.get("category", "general"))],
                "severity": "Low",
                "status": "Boundary",
                "matched_keywords": [clean_text(x) for x in item.get("matched_keywords", []) if clean_text(x)],
                "review_reason": reason,
                "published_at": clean_text(item.get("published_at", "")),
                "collected_at": now,
                "slack_sent": False,
                "guid": "",
                "dedupe_key": "review|"
                + (
                    canonical_url(link)
                    or hashlib.sha1(clean_text(item.get("title", "")).encode()).hexdigest()
                ),
                "summary": f"검토 사유: {reason}" if reason else "",
                "matched_fields": [],
            }
        )

    PUBLIC_DIR.mkdir(exist_ok=True)
    NOTION_ITEMS_JSON_FILE.write_text(
        json.dumps(
            {"generated_at": now, "count": len(notion_items), "items": notion_items},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return notion_items, failures


def title_prop(value: str) -> dict[str, Any]:
    return {"title": [{"text": {"content": truncate(value, 180)}}]}


def text_prop(value: str) -> dict[str, Any]:
    return {"rich_text": [{"text": {"content": truncate(value, 1900)}}]} if value else {"rich_text": []}


def select_prop(value: str) -> dict[str, Any]:
    return {"select": {"name": truncate(value, 90)} if value else None}


def multi_prop(values: list[str]) -> dict[str, Any]:
    return {
        "multi_select": [
            {"name": truncate(v, 90)}
            for v in dict.fromkeys(clean_text(x) for x in values)
            if v
        ]
    }


def properties(item: dict[str, Any], include_status: bool = True) -> dict[str, Any]:
    result = {
        PROPERTY_TITLE: title_prop(item["title"]),
        PROPERTY_URL: {"url": item.get("url") or None},
        PROPERTY_SOURCE: select_prop(item.get("source", "")),
        PROPERTY_SERVICE: multi_prop(item.get("service", [])),
        PROPERTY_SEVERITY: select_prop(item.get("severity", "")),
        PROPERTY_MATCHED_KEYWORDS: multi_prop(item.get("matched_keywords", [])),
        PROPERTY_REVIEW_REASON: text_prop(item.get("review_reason", "")),
        PROPERTY_PUBLISHED_AT: {"date": {"start": item["published_at"]} if item.get("published_at") else None},
        PROPERTY_COLLECTED_AT: {"date": {"start": item["collected_at"]} if item.get("collected_at") else None},
        PROPERTY_SLACK_SENT: {"checkbox": bool(item.get("slack_sent"))},
        PROPERTY_GUID: text_prop(item.get("guid", "")),
        PROPERTY_DEDUPE_KEY: text_prop(item.get("dedupe_key", "")),
    }
    if include_status:
        result[PROPERTY_STATUS] = select_prop(item.get("status", "미확인"))
    return result


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
        for _ in range(5):
            response = self.session.request(method, url, timeout=30, **kwargs)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            time.sleep(max(int(response.headers.get("Retry-After", "1")), 1))
        response.raise_for_status()
        return response

    def find(self, dedupe_key: str) -> str | None:
        payload = {
            "filter": {
                "property": PROPERTY_DEDUPE_KEY,
                "rich_text": {"equals": dedupe_key},
            },
            "page_size": 1,
        }
        results = self.request(
            "POST",
            f"https://api.notion.com/v1/databases/{self.database_id}/query",
            json=payload,
        ).json().get("results", [])
        return results[0]["id"] if results else None

    def create(self, item: dict[str, Any]) -> None:
        details = [
            f"요약: {item['summary']}" if item.get("summary") else "",
            f"링크: {item['url']}" if item.get("url") else "",
            f"매칭 위치: {', '.join(item.get('matched_fields', []))}" if item.get("matched_fields") else "",
            f"매칭 키워드: {', '.join(item.get('matched_keywords', []))}" if item.get("matched_keywords") else "",
            f"판단 근거: {item['review_reason']}" if item.get("review_reason") else "",
        ]
        text = "\n".join(x for x in details if x) or "자동 수집된 AWS 업데이트 항목입니다."
        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties(item),
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {"type": "text", "text": {"content": truncate(text, 1900)}}
                        ]
                    },
                }
            ],
        }
        self.request("POST", "https://api.notion.com/v1/pages", json=payload)

    def update(self, page_id: str, item: dict[str, Any]) -> None:
        self.request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            json={"properties": properties(item, include_status=False)},
        )


def write_report(report: dict[str, Any]) -> None:
    PUBLIC_DIR.mkdir(exist_ok=True)
    NOTION_SYNC_JSON_FILE.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    errors = "".join(f"<li>{html.escape(x)}</li>" for x in report["errors"]) or "<li>No errors</li>"
    NOTION_SYNC_HTML_FILE.write_text(
        f"<!doctype html><html lang='ko'><meta charset='utf-8'><title>Notion Sync Status</title><h1>Notion Sync Status</h1><p>Generated: {report['generated_at']}</p><p>Enabled: {report['enabled']}</p><p>Loaded: {report['loaded_items']}</p><p>Created: {report['created']}</p><p>Updated: {report['updated']}</p><p>Skipped: {report['skipped']}</p><h2>Errors</h2><ul>{errors}</ul></html>",
        encoding="utf-8",
    )


def main() -> None:
    token = os.getenv("NOTION_TOKEN", "").strip()
    database_id = os.getenv("NOTION_DATABASE_ID", "").strip()
    strict = os.getenv("NOTION_SYNC_STRICT", "false").lower() in {"1", "true", "yes"}
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": bool(token and database_id),
        "loaded_items": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
    }
    try:
        items, failures = collect_enriched_items()
        report["loaded_items"] = len(items)
        report["errors"].extend(failures)
        if not token or not database_id:
            report["skipped"] = len(items)
            report["errors"].append("NOTION_TOKEN or NOTION_DATABASE_ID is not configured.")
            return
        client = NotionClient(token, database_id)
        for item in items:
            try:
                page_id = client.find(item["dedupe_key"])
                if page_id:
                    client.update(page_id, item)
                    report["updated"] += 1
                else:
                    client.create(item)
                    report["created"] += 1
                time.sleep(float(os.getenv("NOTION_SYNC_SLEEP_SECONDS", "0.4")))
            except Exception as exc:
                report["errors"].append(f"{item['title']}: {exc}")
                if strict:
                    raise
    except Exception as exc:
        report["errors"].append(str(exc))
        if strict:
            raise
    finally:
        write_report(report)
    print(
        "Notion sync completed: "
        f"created={report['created']}, updated={report['updated']}, "
        f"skipped={report['skipped']}, errors={len(report['errors'])}"
    )


if __name__ == "__main__":
    main()
