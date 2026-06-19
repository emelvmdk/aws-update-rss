from __future__ import annotations

from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from requests import Response

from generate_feed import (
    BORDERLINE_SCORE_REASON,
    HIGH_SCORE_MISSING_URL_HINT_REASON,
    LOW_SCORE_SKIP_REASON,
    build_rss,
    clean_text,
    decode_response_text,
    detect_severity,
    evaluate_include,
    extract_page_summary,
    item_to_rss_description,
    localized_url_candidate,
    make_guid,
    matched_keywords,
    normalize_display_title,
    should_include,
    truncate,
)


BASE_CONFIG = {
    "output": {
        "title": "AWS Update RSS for SA",
        "link": "https://aws.amazon.com/new/",
        "description": "Filtered AWS updates",
        "language": "ko-KR",
    },
    "localization": {
        "preferred_docs_locale": "ko_kr",
        "preferred_aws_site_locale": "ko",
    },
    "summary": {
        "max_detail_chars": 900,
        "include_severity": True,
    },
    "what_new_filter": {
        "include_keywords": ["CloudWatch", "Transit Gateway"],
        "exclude_keywords": ["HealthOmics"],
    },
    "severity_rules": {
        "high": ["CVE", "Transit Gateway"],
        "medium": ["CloudWatch"],
        "low": ["console"],
    },
    "category_hints": {"whats-new": "check new features"},
}


STRICT_FILTER_CONFIG = {
    **BASE_CONFIG,
    "what_new_filter": {
        "always_include_keywords": ["Transit Gateway", "AWS WAF"],
        "contextual_keywords": ["VPC", "IAM", "WAF"],
        "relevance_keywords": ["console", "security", "policy", "endpoint"],
        "exclude_keywords": ["HealthOmics", "Bedrock", "Amazon Bedrock", "Bedrock Guardrails"],
    },
}


def response_with_content(content: bytes, content_type: str = "text/html") -> Response:
    response = Response()
    response.status_code = 200
    response._content = content
    response.headers["content-type"] = content_type
    return response


def test_clean_text_and_truncate() -> None:
    assert clean_text("<p>Hello&nbsp;<strong>AWS</strong></p>") == "Hello AWS"
    assert truncate("abcdef", 4) == "abc…"


def test_decode_response_text_prefers_utf8() -> None:
    expected = "AWS Backup 개발자 가이드"
    response = response_with_content(expected.encode("utf-8"))
    response.encoding = "ISO-8859-1"
    assert decode_response_text(response) == expected


def test_localized_url_candidate() -> None:
    assert localized_url_candidate("https://aws.amazon.com/about-aws/whats-new/2026/06/example/", BASE_CONFIG) == "https://aws.amazon.com/ko/about-aws/whats-new/2026/06/example/"
    assert localized_url_candidate("https://docs.aws.amazon.com/vpc/latest/tgw/example.html", BASE_CONFIG) == "https://docs.aws.amazon.com/ko_kr/vpc/latest/tgw/example.html"
    assert localized_url_candidate("https://example.com/page", BASE_CONFIG) is None


def test_normalize_display_title_fixes_docs_history_titles() -> None:
    backup_url = "https://docs.aws.amazon.com/ko_kr/aws-backup/latest/devguide/doc-history.html"
    config_url = "https://docs.aws.amazon.com/ko_kr/config/latest/developerguide/DocumentHistory.html"
    assert normalize_display_title("에 대한 문서 기록 AWS Backup", "Document history for AWS Backup", backup_url) == "AWS Backup 문서 기록"
    assert normalize_display_title("문서 기록", "Document history", config_url) == "AWS Config 문서 기록"


def test_legacy_keyword_filter_uses_url_hint_for_whats_new() -> None:
    included, matches = should_include(
        {
            "title": "Amazon CloudWatch announces Log Analytics",
            "summary": "CloudWatch Logs Insights update",
            "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/console-experience-update/",
        },
        {"filter_mode": "keyword", "category": "whats-new"},
        BASE_CONFIG,
    )
    assert included is False
    assert matches == []


def test_precise_keyword_passes_without_url_hint() -> None:
    included, matches = should_include(
        {
            "title": "AWS Transit Gateway announces route table improvements",
            "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/improved-networking-console-experience/",
        },
        {"filter_mode": "keyword", "category": "whats-new"},
        STRICT_FILTER_CONFIG,
    )
    assert included is True
    assert matches == ["Transit Gateway"]


def test_broad_keyword_requires_strong_score_and_url_hint() -> None:
    feed = {"filter_mode": "keyword", "category": "whats-new"}

    included_ok, matches_ok, reason_ok = evaluate_include(
        {
            "title": "Amazon VPC console update for endpoint workflows",
            "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-vpc-console-endpoint-workflows/",
        },
        feed,
        STRICT_FILTER_CONFIG,
    )
    assert included_ok is True
    assert "VPC" in matches_ok
    assert reason_ok == ""

    included_borderline, matches_borderline, reason_borderline = evaluate_include(
        {
            "title": "New console experience",
            "summary": "VPC endpoint workflow improvements are available.",
            "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/console-experience-update/",
        },
        feed,
        STRICT_FILTER_CONFIG,
    )
    assert included_borderline is False
    assert "VPC" in matches_borderline
    assert reason_borderline == BORDERLINE_SCORE_REASON

    included_high_no_url, matches_high_no_url, reason_high_no_url = evaluate_include(
        {
            "title": "Amazon VPC console update for endpoint workflows",
            "summary": "Endpoint workflow improvements are available.",
            "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/console-experience-update/",
        },
        feed,
        STRICT_FILTER_CONFIG,
    )
    assert included_high_no_url is False
    assert "VPC" in matches_high_no_url
    assert reason_high_no_url == HIGH_SCORE_MISSING_URL_HINT_REASON

    included_low, matches_low, reason_low = evaluate_include(
        {
            "title": "Console update",
            "summary": "VPC workflow notes are available.",
            "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/console-update/",
        },
        feed,
        STRICT_FILTER_CONFIG,
    )
    assert included_low is False
    assert "VPC" in matches_low
    assert reason_low == LOW_SCORE_SKIP_REASON


def test_exclude_keywords_win() -> None:
    included, matches = should_include(
        {
            "title": "AWS HealthOmics supports CloudWatch logs",
            "summary": "CloudWatch is present but excluded service should win",
            "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/aws-healthomics-cloudwatch-logs/",
        },
        {"filter_mode": "keyword", "category": "whats-new"},
        BASE_CONFIG,
    )
    assert included is False
    assert matches == []


def test_all_filter_mode_includes_entry_without_keywords() -> None:
    included, matches = should_include({"title": "AWS Backup documentation update"}, {"filter_mode": "all"}, BASE_CONFIG)
    assert included is True
    assert matches == []


def test_acronym_boundaries() -> None:
    assert matched_keywords("AWS WAF policy update", ["WAF"]) == ["WAF"]
    assert matched_keywords("The wafer service update", ["WAF"]) == []
    assert matched_keywords("IAM Identity Center update", ["IAM"]) == ["IAM"]
    assert matched_keywords("premium feature update", ["IAM"]) == []


def test_detect_severity() -> None:
    assert detect_severity("Transit Gateway CloudWatch update", BASE_CONFIG) == "High"
    assert detect_severity("CloudWatch dashboard update", BASE_CONFIG) == "Medium"
    assert detect_severity("console guide update", BASE_CONFIG) == "Low"


def test_extract_page_summary_prefers_korean_text_for_localized_pages() -> None:
    html_doc = """
    <html><head>
      <meta name="description" content="Amazon VPC now supports a redesigned console workflow.">
      <title>Amazon VPC console update</title>
    </head><body><main>
      <h1>Amazon VPC 콘솔 업데이트</h1>
      <p>Amazon VPC 콘솔에서 엔드포인트와 라우팅 관련 워크플로를 더 쉽게 확인할 수 있도록 화면 구성이 업데이트되었습니다.</p>
    </main></body></html>
    """
    title, summary = extract_page_summary(html_doc, prefer_korean=True)
    assert title == "Amazon VPC 콘솔 업데이트"
    assert "Amazon VPC 콘솔" in summary


def test_make_guid_is_stable_for_same_entry() -> None:
    entry = {
        "id": "item-1",
        "link": "https://aws.amazon.com/example",
        "title": "Example update",
        "published": "Mon, 15 Jun 2026 00:00:00 GMT",
    }
    assert make_guid("feed", entry) == make_guid("feed", entry)


def test_item_to_rss_description_is_compact() -> None:
    description = item_to_rss_description(
        title="Example update",
        source_name="AWS What's New Filtered",
        category="whats-new",
        severity="Medium",
        language="en fallback",
        matched=["CloudWatch"],
        page_summary="",
        rss_summary="English fallback summary",
        link="https://aws.amazon.com/example",
        config=BASE_CONFIG,
    )
    assert "중요도" in description
    assert "요약" in description
    assert "English fallback summary" in description
    assert "운영 판단" not in description
    assert "확인할 것" not in description


def test_build_rss_outputs_valid_xml() -> None:
    items = [
        {
            "title": "[whats-new] Example update",
            "link": "https://aws.amazon.com/example",
            "guid": "abc123",
            "published_at": datetime(2026, 6, 15, tzinfo=timezone.utc),
            "description": "<p>summary</p>",
            "category": "whats-new",
        }
    ]
    root = ET.fromstring(build_rss(BASE_CONFIG, items))
    assert root.tag == "rss"
    assert root.find("channel/title").text == "AWS Update RSS for SA"
    assert root.find("channel/item/guid").text == "abc123"
