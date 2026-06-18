from __future__ import annotations

from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import pytest

from generate_feed import (
    build_rss,
    clean_text,
    detect_severity,
    detect_severity_with_reasons,
    extract_page_summary,
    item_to_rss_description,
    localized_url_candidate,
    make_guid,
    matched_keywords,
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
        "max_summary_chars": 700,
        "max_detail_chars": 900,
        "include_matched_keywords": True,
        "include_language_status": True,
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
    "category_hints": {
        "whats-new": "신규 기능 영향 여부를 확인하세요.",
        "networking": "네트워크 영향 여부를 확인하세요.",
    },
}


SERVICE_AWARE_CONFIG = {
    **BASE_CONFIG,
    "severity_model": {
        "critical_services": [
            "CloudWatch",
            "Control Tower",
            "Landing Zone",
            "Gateway Load Balancer",
            "GWLB",
            "Gateway Load Balancer Endpoint",
            "GWLBe",
            "VPC Endpoint",
            "Endpoint Service",
        ],
        "important_services": ["Amazon Bedrock", "Bedrock"],
        "high_change_types": [
            "security",
            "policy",
            "landing zone",
            "route table",
            "appliance mode",
            "health check",
            "failover",
            "Bedrock Guardrails",
        ],
        "medium_change_types": ["console", "dashboard", "new capability", "generally available"],
        "low_change_types": ["documentation", "guide"],
    },
}


def test_clean_text_removes_html_and_normalizes_spaces() -> None:
    assert clean_text("<p>Hello&nbsp; <strong>AWS</strong></p>\n") == "Hello AWS"


def test_truncate_adds_ellipsis_when_too_long() -> None:
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 4) == "abc"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://aws.amazon.com/about-aws/whats-new/2026/06/example/",
            "https://aws.amazon.com/ko/about-aws/whats-new/2026/06/example/",
        ),
        (
            "https://aws.amazon.com/ko/about-aws/whats-new/2026/06/example/",
            "https://aws.amazon.com/ko/about-aws/whats-new/2026/06/example/",
        ),
        (
            "https://docs.aws.amazon.com/vpc/latest/tgw/transit-gateway-release-notes.html",
            "https://docs.aws.amazon.com/ko_kr/vpc/latest/tgw/transit-gateway-release-notes.html",
        ),
        (
            "https://docs.aws.amazon.com/ko_kr/vpc/latest/tgw/transit-gateway-release-notes.html",
            "https://docs.aws.amazon.com/ko_kr/vpc/latest/tgw/transit-gateway-release-notes.html",
        ),
    ],
)
def test_localized_url_candidate(url: str, expected: str) -> None:
    assert localized_url_candidate(url, BASE_CONFIG) == expected


def test_localized_url_candidate_returns_none_for_unknown_domain() -> None:
    assert localized_url_candidate("https://example.com/page", BASE_CONFIG) is None


def test_keyword_filter_includes_matching_whats_new_entry() -> None:
    entry = {
        "title": "Amazon CloudWatch announces Log Analytics",
        "summary": "CloudWatch Logs Insights update",
        "link": "https://aws.amazon.com/about-aws/whats-new/example/",
    }
    feed = {"filter_mode": "keyword"}

    included, matches = should_include(entry, feed, BASE_CONFIG)

    assert included is True
    assert "CloudWatch" in matches


def test_keyword_filter_excludes_noise_entry() -> None:
    entry = {
        "title": "AWS HealthOmics supports CloudWatch logs",
        "summary": "CloudWatch is present but excluded service should win",
    }
    feed = {"filter_mode": "keyword"}

    included, matches = should_include(entry, feed, BASE_CONFIG)

    assert included is False
    assert matches == []


def test_all_filter_mode_includes_entry_without_keywords() -> None:
    entry = {"title": "AWS Backup documentation update"}
    feed = {"filter_mode": "all"}

    included, matches = should_include(entry, feed, BASE_CONFIG)

    assert included is True
    assert matches == []


def test_detect_severity_uses_high_before_medium() -> None:
    assert detect_severity("Transit Gateway CloudWatch update", BASE_CONFIG) == "High"
    assert detect_severity("CloudWatch dashboard update", BASE_CONFIG) == "Medium"
    assert detect_severity("console guide update", BASE_CONFIG) == "Low"


def test_service_aware_severity_keeps_critical_console_updates_medium() -> None:
    severity, reasons = detect_severity_with_reasons(
        "Amazon CloudWatch console experience update",
        SERVICE_AWARE_CONFIG,
    )

    assert severity == "Medium"
    assert "critical service: CloudWatch" in reasons
    assert "medium change: console" in reasons


def test_service_aware_severity_elevates_control_tower_and_gwlb_path_changes() -> None:
    control_tower_severity, control_tower_reasons = detect_severity_with_reasons(
        "AWS Control Tower landing zone and account factory policy update",
        SERVICE_AWARE_CONFIG,
    )
    gwlb_severity, gwlb_reasons = detect_severity_with_reasons(
        "Gateway Load Balancer Endpoint route table and appliance mode update",
        SERVICE_AWARE_CONFIG,
    )

    assert control_tower_severity == "High"
    assert any("Control Tower" in reason for reason in control_tower_reasons)
    assert gwlb_severity == "High"
    assert any("Gateway Load Balancer" in reason for reason in gwlb_reasons)


def test_matched_keywords_is_case_insensitive() -> None:
    assert matched_keywords("amazon cloudwatch update", ["CloudWatch"]) == ["CloudWatch"]


def test_extract_page_summary_prefers_meta_description() -> None:
    html_doc = """
    <html>
      <head>
        <meta name="description" content="한국어 요약 설명입니다.">
      </head>
      <body>
        <h1>테스트 제목</h1>
        <p>본문 설명입니다. 충분히 긴 문단입니다. AWS 업데이트 설명입니다.</p>
      </body>
    </html>
    """

    title, summary = extract_page_summary(html_doc)

    assert title == "테스트 제목"
    assert summary == "한국어 요약 설명입니다."


def test_make_guid_is_stable_for_same_entry() -> None:
    entry = {
        "id": "item-1",
        "link": "https://aws.amazon.com/example",
        "title": "Example update",
        "published": "Mon, 15 Jun 2026 00:00:00 GMT",
    }

    assert make_guid("feed", entry) == make_guid("feed", entry)


def test_item_to_rss_description_contains_fallback_language_and_link() -> None:
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

    assert "en fallback" in description
    assert "CloudWatch" in description
    assert "https://aws.amazon.com/example" in description
    assert "요약" in description


def test_item_to_rss_description_can_include_severity_reasons() -> None:
    description = item_to_rss_description(
        title="CloudWatch console update",
        source_name="AWS What's New Filtered",
        category="whats-new",
        severity="Medium",
        language="en fallback",
        matched=["CloudWatch"],
        page_summary="",
        rss_summary="CloudWatch console update",
        link="https://aws.amazon.com/example",
        config={**BASE_CONFIG, "summary": {**BASE_CONFIG["summary"], "include_severity_reasons": True}},
        severity_reasons=["critical service: CloudWatch", "medium change: console"],
    )

    assert "판단 근거" in description
    assert "critical service: CloudWatch" in description


def test_build_rss_outputs_valid_xml() -> None:
    items = [
        {
            "title": "[whats-new] Example update",
            "link": "https://aws.amazon.com/example",
            "guid": "abc123",
            "published_at": datetime(2026, 6, 15, tzinfo=timezone.utc),
            "description": "<p>요약: 테스트</p>",
            "category": "whats-new",
        }
    ]

    xml_text = build_rss(BASE_CONFIG, items)
    root = ET.fromstring(xml_text)

    assert root.tag == "rss"
    assert root.find("channel/title").text == "AWS Update RSS for SA"
    assert root.find("channel/item/guid").text == "abc123"
