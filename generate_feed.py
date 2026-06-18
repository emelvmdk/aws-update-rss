from __future__ import annotations

import hashlib
import html
import json
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
REVIEW_FILE = PUBLIC_DIR / "review.html"
REVIEW_JSON_FILE = PUBLIC_DIR / "review.json"
URL_HINT_SKIP_REASON = "url_service_hint_missing"
LOW_SCORE_SKIP_REASON = "low_keyword_score"
BORDERLINE_SCORE_REASON = "borderline_keyword_score"
PASSED_WITHOUT_URL_HINT_REASON = "passed_without_url_hint"
URL_HINT_DEFAULT_CATEGORIES = {"whats-new", "operations"}
BROAD_KEYWORD_REVIEW_SCORE = 4
BROAD_KEYWORD_PASS_SCORE = 5

DOC_SERVICE_NAMES = {
    "aws-backup": "AWS Backup",
    "config": "AWS Config",
    "controltower": "AWS Control Tower",
    "organizations": "AWS Organizations",
    "ram": "AWS RAM",
    "iam": "IAM",
    "singlesignon": "IAM Identity Center",
    "vpc": "Amazon VPC",
    "directconnect": "AWS Direct Connect",
    "route53": "Amazon Route 53",
    "cloudwatch": "Amazon CloudWatch",
    "cloudtrail": "AWS CloudTrail",
    "securityhub": "AWS Security Hub",
    "waf": "AWS WAF",
    "network-firewall": "AWS Network Firewall",
}

URL_ALIAS_OVERRIDES = {
    "transit gateway": ["tgw"],
    "gateway load balancer": ["gwlb"],
    "gateway load balancer endpoint": ["gwlbe"],
    "vpc endpoint": ["endpoint"],
    "interface endpoint": ["endpoint"],
    "gateway endpoint": ["endpoint"],
    "aws resource access manager": ["ram"],
    "resource access manager": ["ram"],
    "iam identity center": ["singlesignon", "single sign on", "sso"],
    "aws certificate manager": ["acm"],
    "network firewall": ["network firewall"],
}


def clean_text(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def has_korean(value: Any) -> bool:
    return re.search(r"[가-힣]", clean_text(value)) is not None


def truncate(value: str, limit: int) -> str:
    value = clean_text(value)
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def normalize_for_match(value: str) -> str:
    value = clean_text(value).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> dict[str, Any]:
    return load_yaml(ROOT / "config.yaml")


def load_feeds() -> list[dict[str, Any]]:
    feeds: list[dict[str, Any]] = []
    for path in sorted(ROOT.glob("feeds*.yaml")):
        feeds.extend(load_yaml(path).get("feeds", []))
    return [feed for feed in feeds if feed.get("enabled", True)]


def make_session(config: dict[str, Any]) -> requests.Session:
    session = requests.Session()
    user_agent = config.get("fetch", {}).get("user_agent", "aws-update-rss/1.0")
    session.headers.update({"User-Agent": user_agent})
    return session


def decode_response_text(response: Any) -> str:
    content = getattr(response, "content", None)
    if content is None:
        return getattr(response, "text", "") or ""
    if isinstance(content, str):
        return content

    headers = getattr(response, "headers", {}) or {}
    header_encoding = requests.utils.get_encoding_from_headers(headers)
    response_encoding = getattr(response, "encoding", None)
    apparent_encoding = getattr(response, "apparent_encoding", None)
    weak = {"iso-8859-1", "latin-1", "windows-1252"}

    candidates: list[str] = []
    if header_encoding and header_encoding.lower() not in weak:
        candidates.append(header_encoding)
    candidates.append("utf-8")
    candidates.extend(x for x in [apparent_encoding, header_encoding, response_encoding] if x)

    seen: set[str] = set()
    for encoding in candidates:
        key = encoding.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def response_feed_content(response: Any) -> bytes | str:
    content = getattr(response, "content", None)
    return content if content is not None else (getattr(response, "text", "") or "")


def docs_service_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "docs.aws.amazon.com":
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if parts and re.fullmatch(r"[a-z]{2}(?:_[a-z]{2})?", parts[0]):
        parts = parts[1:]
    if not parts:
        return ""
    service_key = parts[0]
    if service_key in DOC_SERVICE_NAMES:
        return DOC_SERVICE_NAMES[service_key]
    if service_key.startswith("aws-"):
        return "AWS " + " ".join(word.capitalize() for word in service_key[4:].split("-"))
    return " ".join(word.upper() if len(word) <= 3 else word.capitalize() for word in service_key.split("-"))


def url_match_text(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    pieces = [parsed.netloc, parsed.path, parsed.query, docs_service_name_from_url(url)]
    return normalize_for_match(" ".join(pieces))


def keyword_url_aliases(keyword: str) -> list[str]:
    normalized = normalize_for_match(keyword)
    if not normalized:
        return []
    aliases = {normalized}
    for prefix in ("aws ", "amazon "):
        if normalized.startswith(prefix):
            aliases.add(normalized[len(prefix) :])
    aliases.update(URL_ALIAS_OVERRIDES.get(normalized, []))
    return sorted(alias for alias in aliases if alias)


def keyword_matches(text: str, keyword: str) -> bool:
    keyword = keyword.strip()
    if not keyword:
        return False
    lower_text = text.lower()
    lower_keyword = keyword.lower()
    if len(lower_keyword) <= 4 and re.fullmatch(r"[a-z0-9]+", lower_keyword):
        pattern = rf"(?<![a-z0-9]){re.escape(lower_keyword)}(?![a-z0-9])"
        return re.search(pattern, lower_text) is not None
    return lower_keyword in lower_text


def matched_keywords(text: str, keywords: list[str]) -> list[str]:
    return [keyword for keyword in keywords if keyword_matches(text, keyword)]


def matched_keywords_have_url_hint(keywords: list[str], url: str) -> bool:
    if not keywords:
        return True
    url_text = url_match_text(url)
    if not url_text:
        return True
    return any(keyword_matches(url_text, alias) for keyword in keywords for alias in keyword_url_aliases(keyword))


def feed_requires_url_hint(feed: dict[str, Any]) -> bool:
    explicit = feed.get("require_url_hint")
    if explicit is not None:
        return bool(explicit)
    return str(feed.get("category", "")).lower() in URL_HINT_DEFAULT_CATEGORIES


def parse_entry_datetime(entry: Any) -> datetime:
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            try:
                dt = parsedate_to_datetime(value)
                return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
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
        url_match_text(entry.get("link", "")),
    ]
    parts.extend(tag.get("term", "") for tag in (entry.get("tags") or []))
    return clean_text(" ".join(str(part) for part in parts)).lower()


def entry_body_text(entry: Any) -> str:
    parts = [entry.get("summary", ""), entry.get("description", "")]
    parts.extend(tag.get("term", "") for tag in (entry.get("tags") or []))
    return clean_text(" ".join(str(part) for part in parts)).lower()


def score_broad_keyword_match(entry: Any, contextual_matches: list[str], relevance_matches: list[str]) -> tuple[int, list[str], bool]:
    title_text = clean_text(entry.get("title", "")).lower()
    body_text = entry_body_text(entry)
    link = entry.get("link", "")
    has_url_hint = matched_keywords_have_url_hint(contextual_matches, link)

    score = 0
    reasons: list[str] = []
    title_context = matched_keywords(title_text, contextual_matches)
    body_context = matched_keywords(body_text, contextual_matches)

    if title_context:
        score += 3
        reasons.append(f"title service keyword: {', '.join(title_context)}")
    elif body_context:
        score += 2
        reasons.append(f"body service keyword: {', '.join(body_context)}")
    elif has_url_hint:
        score += 1
        reasons.append("url service hint")

    if relevance_matches:
        score += 2
        reasons.append(f"relevance keyword: {', '.join(relevance_matches)}")
    if has_url_hint:
        score += 1
        reasons.append("url hint bonus")
    return score, reasons, has_url_hint


def evaluate_include(entry: Any, feed: dict[str, Any], config: dict[str, Any]) -> tuple[bool, list[str], str]:
    mode = feed.get("filter_mode", "all")
    text = entry_text(entry)
    require_scoring = feed_requires_url_hint(feed)

    if mode == "all":
        return True, [], ""

    filter_config = config.get("what_new_filter", {})
    if matched_keywords(text, filter_config.get("exclude_keywords", [])):
        return False, [], "excluded_keyword"

    always_keywords = filter_config.get("always_include_keywords")
    contextual_keywords = filter_config.get("contextual_keywords")
    relevance_keywords = filter_config.get("relevance_keywords")

    if always_keywords is not None or contextual_keywords is not None:
        always_matches = matched_keywords(text, always_keywords or [])
        if always_matches:
            return True, always_matches, ""

        contextual_matches = matched_keywords(text, contextual_keywords or [])
        relevance_matches = matched_keywords(text, relevance_keywords or [])
        if contextual_matches and relevance_matches:
            matches = contextual_matches + relevance_matches
            if require_scoring:
                score, _, has_url_hint = score_broad_keyword_match(entry, contextual_matches, relevance_matches)
                if score < BROAD_KEYWORD_REVIEW_SCORE:
                    return False, matches, LOW_SCORE_SKIP_REASON
                if score < BROAD_KEYWORD_PASS_SCORE:
                    return True, matches, BORDERLINE_SCORE_REASON
                if not has_url_hint:
                    return True, matches, PASSED_WITHOUT_URL_HINT_REASON
            return True, matches, ""
        return False, [], "no_keyword_match"

    matches = matched_keywords(text, filter_config.get("include_keywords", []))
    if matches and require_scoring and not matched_keywords_have_url_hint(matches, entry.get("link", "")):
        return False, matches, URL_HINT_SKIP_REASON
    return bool(matches), matches, "" if matches else "no_keyword_match"


def should_include(entry: Any, feed: dict[str, Any], config: dict[str, Any]) -> tuple[bool, list[str]]:
    included, matches, _ = evaluate_include(entry, feed, config)
    return included, matches if included else []


def review_candidate_from_entry(entry: Any, feed: dict[str, Any], matches: list[str], published_at: datetime, reason: str) -> dict[str, Any]:
    link = entry.get("link", "")
    return {
        "title": clean_text(entry.get("title", "Untitled")),
        "link": link,
        "source": feed.get("name", "Unnamed feed"),
        "category": feed.get("category", "general"),
        "published_at": published_at.isoformat(),
        "matched_keywords": matches,
        "url_text": url_match_text(link),
        "review_reason": reason,
    }


def _legacy_detect_severity_with_reasons(text: str, config: dict[str, Any]) -> tuple[str, list[str]]:
    for severity in ("high", "medium", "low"):
        for keyword in config.get("severity_rules", {}).get(severity, []):
            if keyword_matches(text, keyword):
                return severity.capitalize(), [f"legacy {severity}: {keyword}"]
    return "Low", ["default: no severity keyword matched"]


def detect_severity_with_reasons(text: str, config: dict[str, Any]) -> tuple[str, list[str]]:
    model = config.get("severity_model", {})
    if not model:
        return _legacy_detect_severity_with_reasons(text, config)

    critical_services = matched_keywords(text, model.get("critical_services", []))
    important_services = matched_keywords(text, model.get("important_services", []))
    high_changes = matched_keywords(text, model.get("high_change_types", []))
    medium_changes = matched_keywords(text, model.get("medium_change_types", []))
    low_changes = matched_keywords(text, model.get("low_change_types", []))

    reasons: list[str] = []
    reasons.extend(f"critical service: {keyword}" for keyword in critical_services[:3])
    reasons.extend(f"important service: {keyword}" for keyword in important_services[:3])
    reasons.extend(f"high change: {keyword}" for keyword in high_changes[:3])
    reasons.extend(f"medium change: {keyword}" for keyword in medium_changes[:3])
    reasons.extend(f"low signal: {keyword}" for keyword in low_changes[:3])

    if high_changes:
        return "High", reasons or ["high change type matched"]
    if critical_services and medium_changes:
        return "Medium", reasons or ["critical service with medium change type"]
    if critical_services:
        return "Medium", reasons or ["critical service matched"]
    if important_services and medium_changes:
        return "Medium", reasons or ["important service with medium change type"]
    if important_services:
        return "Medium", reasons or ["important service matched"]
    if medium_changes:
        return "Medium", reasons or ["medium change type matched"]
    if low_changes:
        return "Low", reasons or ["low signal matched"]
    return _legacy_detect_severity_with_reasons(text, config)


def detect_severity(text: str, config: dict[str, Any]) -> str:
    severity, _ = detect_severity_with_reasons(text, config)
    return severity


def normalize_display_title(title: str, source_title: str = "", link: str = "") -> str:
    title = clean_text(title)
    source_title = clean_text(source_title)
    service = docs_service_name_from_url(link)

    misplaced = re.fullmatch(r"에 대한 문서 기록\s+(.+)", title)
    if misplaced:
        subject = clean_text(misplaced.group(1))
        return f"{subject} 문서 기록" if subject else title

    regular = re.fullmatch(r"(.+?)에 대한 문서 기록", title)
    if regular:
        subject = clean_text(regular.group(1))
        return f"{subject} 문서 기록" if subject else title

    generic_titles = {"문서 기록", "문서 기록 페이지", "document history", "document history page"}
    if service and title.lower() in generic_titles:
        return f"{service} 문서 기록"
    if service and "documenthistory" in urlparse(link).path.lower() and title == "문서 기록":
        return f"{service} 문서 기록"
    if service and "doc-history" in urlparse(link).path.lower() and title == "문서 기록":
        return f"{service} 문서 기록"
    if service and source_title.lower().startswith("document history") and title.lower().startswith("document history"):
        return f"{service} 문서 기록"
    return title


def localized_url_candidate(url: str, config: dict[str, Any]) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path or "/"
    if host == "docs.aws.amazon.com":
        locale = config.get("localization", {}).get("preferred_docs_locale", "ko_kr")
    elif host == "aws.amazon.com":
        locale = config.get("localization", {}).get("preferred_aws_site_locale", "ko")
    else:
        return None
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] == locale:
        return url
    new_path = "/" + "/".join([locale] + parts)
    if path.endswith("/") and not new_path.endswith("/"):
        new_path += "/"
    return urlunparse(parsed._replace(path=new_path))


def fetch_html(session: requests.Session, url: str, timeout: int) -> tuple[str, str] | None:
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        if response.status_code >= 400:
            return None
        headers = getattr(response, "headers", {}) or {}
        content_type = headers.get("content-type", "")
        html_text = decode_response_text(response)
        if "html" not in content_type.lower() and len(html_text) < 500:
            return None
        if "Page Not Found" in html_text or "404 -" in html_text:
            return None
        return response.url, html_text
    except Exception:
        return None


def extract_page_summary(html_doc: str, prefer_korean: bool = False) -> tuple[str, str]:
    soup = BeautifulSoup(html_doc, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    title_candidates: list[str] = []
    for selector in ['meta[property="og:title"]', 'meta[name="twitter:title"]', "h1", "title"]:
        node = soup.select_one(selector)
        if not node:
            continue
        title = clean_text(node.get("content") if node.name == "meta" else node.get_text(" "))
        if title and title not in title_candidates:
            title_candidates.append(title)

    title = ""
    if prefer_korean:
        title = next((candidate for candidate in title_candidates if has_korean(candidate)), "")
    title = title or (title_candidates[0] if title_candidates else "")

    description_candidates: list[str] = []
    for selector in ['meta[name="description"]', 'meta[property="og:description"]', 'meta[name="twitter:description"]']:
        node = soup.select_one(selector)
        if node:
            description = clean_text(node.get("content"))
            if description and description not in description_candidates:
                description_candidates.append(description)

    paragraphs: list[str] = []
    for node in soup.select("main p, article p, #main p, .lb-txt p, p"):
        text = clean_text(node.get_text(" "))
        if len(text) < 45:
            continue
        if "cookie" in text.lower() or "privacy" in text.lower():
            continue
        if text not in paragraphs:
            paragraphs.append(text)
        if len(paragraphs) >= 4:
            break

    if prefer_korean:
        korean_description = next((candidate for candidate in description_candidates if has_korean(candidate)), "")
        korean_paragraphs = [paragraph for paragraph in paragraphs if has_korean(paragraph)]
        summary = korean_description or " ".join(korean_paragraphs[:2])
        if not summary:
            summary = description_candidates[0] if description_candidates else " ".join(paragraphs[:2])
    else:
        summary = description_candidates[0] if description_candidates else " ".join(paragraphs[:2])
    return title, summary


def _page_payload(url: str, language: str, title: str, summary: str) -> dict[str, str]:
    return {"url": url, "language": language, "title": title, "summary": summary}


def fetch_page_payload(session: requests.Session, url: str, language: str, timeout: int) -> dict[str, str] | None:
    fetched = fetch_html(session, url, timeout)
    if not fetched:
        return None
    final_url, html_doc = fetched
    prefer_korean = language.startswith("ko")
    page_title, page_summary = extract_page_summary(html_doc, prefer_korean=prefer_korean)
    if not page_title and not page_summary:
        return None
    if prefer_korean and not has_korean(" ".join([page_title, page_summary])):
        return None
    return _page_payload(final_url or url, language, page_title, page_summary)


def enrich_entry(session: requests.Session, entry: Any, feed: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    timeout = int(config.get("fetch", {}).get("timeout_seconds", 20))
    original_link = entry.get("link", "")
    rss_title = clean_text(entry.get("title", "Untitled"))
    rss_summary = clean_text(entry.get("summary", entry.get("description", "")))

    english_payload = fetch_page_payload(session, original_link, "en source", timeout) if original_link else None
    source_link = english_payload["url"] if english_payload else original_link
    source_title = english_payload["title"] if english_payload and english_payload["title"] else rss_title
    source_summary = english_payload["summary"] if english_payload and english_payload["summary"] else rss_summary

    if config.get("localization", {}).get("enabled", True):
        localized = localized_url_candidate(original_link, config)
        if localized and localized != original_link:
            language = "ko" if "aws.amazon.com/ko/" in localized else "ko_kr"
            localized_payload = fetch_page_payload(session, localized, language, timeout)
            if localized_payload:
                title = normalize_display_title(localized_payload["title"] or source_title, source_title, localized_payload["url"])
                return {
                    "link": localized_payload["url"],
                    "language": language,
                    "title": title,
                    "page_summary": localized_payload["summary"],
                    "rss_summary": rss_summary,
                    "source_link": source_link,
                    "source_title": source_title,
                    "source_summary": source_summary,
                    "source_language": "en source",
                }

    if english_payload:
        title = normalize_display_title(english_payload["title"] or rss_title, source_title, english_payload["url"])
        return {
            "link": english_payload["url"],
            "language": "en fallback",
            "title": title,
            "page_summary": english_payload["summary"],
            "rss_summary": rss_summary,
            "source_link": source_link,
            "source_title": source_title,
            "source_summary": source_summary,
            "source_language": "en source",
        }

    title = normalize_display_title(rss_title, rss_title, original_link)
    return {
        "link": original_link,
        "language": "rss-only fallback",
        "title": title,
        "page_summary": "",
        "rss_summary": rss_summary,
        "source_link": original_link,
        "source_title": rss_title,
        "source_summary": source_summary,
        "source_language": "en rss source",
    }


def build_korean_summary(title: str, source_name: str, category: str, severity: str, detail: str, config: dict[str, Any]) -> str:
    hint = config.get("category_hints", {}).get(category, "서비스 변경 내용과 운영 영향 여부를 확인하세요.")
    title_part = f"제목은 '{title}'입니다." if title else "새 업데이트가 감지되었습니다."
    detail_part = f" 주요 내용: {truncate(detail, 220)}" if detail else ""
    return f"{source_name}에 새 AWS 업데이트가 있습니다. {title_part} 중요도는 {severity}로 분류했습니다. {hint}{detail_part}"


def make_guid(feed_name: str, entry: Any) -> str:
    raw = "|".join([
        feed_name,
        clean_text(entry.get("id", "")),
        clean_text(entry.get("guid", "")),
        clean_text(entry.get("link", "")),
        clean_text(entry.get("title", "")),
        clean_text(entry.get("published", entry.get("updated", ""))),
    ])
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
    source_link: str | None = None,
    source_summary: str | None = None,
    severity_reasons: list[str] | None = None,
) -> str:
    max_detail_chars = int(config.get("summary", {}).get("max_detail_chars", 900))
    detail = page_summary or source_summary or rss_summary
    lines = []
    if config.get("summary", {}).get("include_severity", True):
        lines.append(f"<p><strong>중요도</strong>: {html.escape(severity)}</p>")
    if config.get("summary", {}).get("include_severity_reasons", True) and severity_reasons:
        lines.append(f"<p><strong>판단 근거</strong>: {html.escape(', '.join(severity_reasons))}</p>")
    if detail:
        lines.append(f"<p><strong>요약</strong>: {html.escape(truncate(detail, max_detail_chars))}</p>")
    if link:
        lines.append(f"<p><strong>링크</strong>: <a href=\"{html.escape(link)}\">{html.escape(link)}</a></p>")
    if source_link and source_link != link:
        lines.append(f"<p><strong>영어 원문 링크</strong>: <a href=\"{html.escape(source_link)}\">{html.escape(source_link)}</a></p>")
    return "\n".join(lines)


def collect_items(config: dict[str, Any], feeds: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    session = make_session(config)
    timeout = int(config.get("fetch", {}).get("timeout_seconds", 20))
    failures: list[str] = []
    items: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(config.get("output", {}).get("max_age_days", 30)))

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

        parsed = feedparser.parse(response_feed_content(response))
        if parsed.bozo:
            failures.append(f"{name}: feed parse warning: {parsed.bozo_exception}")

        for entry in parsed.entries:
            published_at = parse_entry_datetime(entry)
            if published_at < cutoff:
                continue
            included, matches, review_reason = evaluate_include(entry, feed, config)
            if review_reason in {URL_HINT_SKIP_REASON, LOW_SCORE_SKIP_REASON, BORDERLINE_SCORE_REASON, PASSED_WITHOUT_URL_HINT_REASON}:
                review_items.append(review_candidate_from_entry(entry, feed, matches, published_at, review_reason))
            if not included:
                continue

            enriched = enrich_entry(session, entry, feed, config)
            source_name = name
            category = feed.get("category", "general")
            title = enriched["title"] or clean_text(entry.get("title", "Untitled"))
            combined_text = " ".join([
                title,
                enriched.get("page_summary", ""),
                enriched.get("rss_summary", ""),
                enriched.get("source_summary", ""),
                source_name,
                category,
            ])
            severity, severity_reasons = detect_severity_with_reasons(combined_text, config)
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
                source_link=enriched.get("source_link"),
                source_summary=enriched.get("source_summary"),
                severity_reasons=severity_reasons,
            )
            items.append({
                "title": f"[{category}] {title}",
                "link": enriched["link"] or entry.get("link", ""),
                "guid": make_guid(source_name, entry),
                "published_at": published_at,
                "description": description,
                "source": source_name,
                "category": category,
                "severity": severity,
                "severity_reasons": severity_reasons,
            })

    deduped = {item["guid"]: item for item in items}
    sorted_items = sorted(deduped.values(), key=lambda item: item["published_at"], reverse=True)
    sorted_review = sorted(review_items, key=lambda item: item["published_at"], reverse=True)
    max_review_items = int(config.get("output", {}).get("max_review_items", 100))
    return sorted_items[: int(config.get("output", {}).get("max_items", 100))], failures, sorted_review[:max_review_items]


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
        latest += f"<li><strong>{html.escape(item['severity'])}</strong> <a href=\"{html.escape(item['link'])}\">{html.escape(item['title'])}</a> <small>{html.escape(item['published_at'].isoformat())}</small></li>"
    INDEX_FILE.write_text(f"""<!doctype html>
<html lang=\"ko\">
<head><meta charset=\"utf-8\"><title>AWS Update RSS</title></head>
<body>
  <h1>AWS Update RSS</h1>
  <p>Filtered RSS: <a href=\"./feed.xml\">feed.xml</a></p>
  <h2>Latest items</h2>
  <ol>{latest}</ol>
  <p>Status: <a href=\"./status.html\">status.html</a></p>
  <p>Review candidates: <a href=\"./review.html\">review.html</a></p>
</body>
</html>
""", encoding="utf-8")


def write_review(review_items: list[dict[str, Any]]) -> None:
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "count": len(review_items), "items": review_items}
    REVIEW_JSON_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = ""
    for item in review_items:
        rows += (
            "<tr>"
            f"<td>{html.escape(item['published_at'])}</td>"
            f"<td>{html.escape(item['source'])}</td>"
            f"<td>{html.escape(item['category'])}</td>"
            f"<td><a href=\"{html.escape(item['link'])}\">{html.escape(item['title'])}</a></td>"
            f"<td>{html.escape(', '.join(item['matched_keywords']))}</td>"
            f"<td>{html.escape(item['review_reason'])}</td>"
            f"<td>{html.escape(item['url_text'])}</td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan=\"7\">No review candidates.</td></tr>"

    REVIEW_FILE.write_text(f"""<!doctype html>
<html lang=\"ko\">
<head><meta charset=\"utf-8\"><title>AWS Update RSS Review</title></head>
<body>
  <h1>Review candidates</h1>
  <p>넓은 키워드 점수가 낮거나, 경계 점수이거나, 통과했지만 URL 힌트가 약한 항목을 표시합니다.</p>
  <p>JSON: <a href=\"./review.json\">review.json</a></p>
  <p>Count: {len(review_items)}</p>
  <table border=\"1\" cellpadding=\"6\" cellspacing=\"0\">
    <thead><tr><th>Published</th><th>Source</th><th>Category</th><th>Title</th><th>Matched keywords</th><th>Review reason</th><th>URL text</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>
""", encoding="utf-8")


def write_status(items: list[dict[str, Any]], failures: list[str], feeds: list[dict[str, Any]], review_items: list[dict[str, Any]]) -> None:
    failure_html = "".join(f"<li>{html.escape(failure)}</li>" for failure in failures) or "<li>No failures</li>"
    feed_html = "".join(f"<li>{html.escape(feed.get('name', 'Unnamed'))} - {html.escape(feed.get('category', 'general'))}</li>" for feed in feeds)
    STATUS_FILE.write_text(f"""<!doctype html>
<html lang=\"ko\">
<head><meta charset=\"utf-8\"><title>AWS Update RSS Status</title></head>
<body>
  <h1>Status</h1>
  <p>Generated at: {html.escape(datetime.now(timezone.utc).isoformat())}</p>
  <p>Item count: {len(items)}</p>
  <p>Feed count: {len(feeds)}</p>
  <p>Review candidate count: {len(review_items)} (<a href=\"./review.html\">review.html</a>, <a href=\"./review.json\">review.json</a>)</p>
  <h2>Failures / warnings</h2>
  <ul>{failure_html}</ul>
  <h2>Feeds</h2>
  <ul>{feed_html}</ul>
</body>
</html>
""", encoding="utf-8")


def main() -> None:
    config = load_config()
    feeds = load_feeds()
    items, failures, review_items = collect_items(config, feeds)
    PUBLIC_DIR.mkdir(exist_ok=True)
    OUTPUT_FILE.write_text(build_rss(config, items), encoding="utf-8")
    write_index(items)
    write_review(review_items)
    write_status(items, failures, feeds, review_items)
    print(f"Generated {OUTPUT_FILE} with {len(items)} items from {len(feeds)} feeds.")
    print(f"Generated {REVIEW_FILE} with {len(review_items)} review candidates.")
    if failures:
        print("Warnings:")
        for failure in failures:
            print(f"- {failure}")


if __name__ == "__main__":
    main()
