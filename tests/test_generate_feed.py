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


STRICT_FILTER_CONFIG = {
    **BASE_CONFIG,
    "what_new_filter": {
        "always_include_keywords": ["Transit Gateway", "AWS WAF"],
        "contextual_keywords": ["VPC", "IAM", "WAF"],
        "relevance_keywords": ["console", "security", "policy", "endpoint"],
        "exclude_keywords": ["HealthOmics", "Bedrock", "Amazon Bedrock", "Bedrock Guardrails"],
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
            "IAM Access Analyzer",
            "AWS Secrets Manager",
            "AWS Certificate Manager",
            "Amazon Macie",
            "VPC Flow Logs",
            "Reachability Analyzer",
            "Network Access Analyzer",
            "Traffic Mirroring",
            "VPC IPAM",
            "NAT Gateway",
            "Internet Gateway",
            "AWS Health",
            "Service Quotas",
            "AWS CloudFormation",
            "AWS Service Catalog",
        ],
        "important_services": [
            "Amazon Bedrock",
            "Bedrock",
            "VPC Lattice",
            "AWS Detective",
            "AWS Trusted Advisor",
        ],
        "high_change_types": [
            "console",
            "dashboard",
            "console experience",
            "new console",
            "security",
            "policy",
            "landing zone",
            "route table",
            "appliance mode",
            "health check",
            "failover",
            "Bedrock Guardrails",
        ],
        "medium_change_types": ["new capability", "generally available", "region expansion"],
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


def test_strict_filter_requires_context_for_broad_keywords() -> None:
    feed = {"filter_mode": "keyword"}
    broad_only = {"title": "Amazon VPC adds a minor integration"}
    broad_with_context = {"title": "Amazon VPC console update for endpoint workflows"}
    precise = {"title": "AWS Transit Gateway announces route table improvements"}

    included_broad_only, matches_broad_only = should_include(broad_only, feed, STRICT_FILTER_CONFIG)
    included_broad_with_context, matches_broad_with_context = should_include(broad_with_context, feed, STRICT_FILTER_CONFIG)
    included_precise, matches_precise = should_include(precise, feed, STRICT_FILTER_CONFIG)

    assert included_broad_only is False
    assert matches_broad_only == []
    assert included_broad_with_context is True
    assert "VPC" in matches_broad_with_context
    assert "console" in matches_broad_with_context
    assert included_precise is True
    assert matches_precise == ["Transit Gateway"]


def test_strict_filter_excludes_bedrock_even_with_context() -> None:
    feed = {"filter_mode": "keyword"}
    entry = {"title": "Amazon Bedrock console security update for guardrails"}

    included, matches = should_include(entry, feed, STRICT_FILTER_CONFIG)

    assert included is False
    assert matches == []


def test_short_acronyms_use_alphanumeric_boundaries() -> None:
    assert matched_keywords("AWS WAF policy update", ["WAF"]) == ["WAF"]
    assert matched_keywords("The wafer service update", ["WAF"]) == []
    assert matched_keywords("IAM Identity Center update", ["IAM"]) == ["IAM"]
    assert matched_keywords("premium feature update", ["IAM"]) == []


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


def test_service_aware_severity_elevates_console_updates_to_high() -> None:
    severity, reasons = detect_severity_with_reasons(
        "Amazon CloudWatch console experience update",
        SERVICE_AWARE_CONFIG,
    )

    assert severity == "High"
    assert "critical service: CloudWatch" in reasons
    assert "high change: console" in reasons


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


def test_service_aware_severity_handles_added_sa_services() -> None:
    flow_logs_severity, flow_logs_reasons = detect_severity_with_reasons(
        "VPC Flow Logs documentation update",
        SERVICE_AWARE_CONFIG,
    )
    reachability_severity, reachability_reasons = detect_severity_with_reasons(
        "Reachability Analyzer console update",
        SERVICE_AWARE_CONFIG,
    )
    secrets_manager_severity, secrets_manager_reasons = detect_severity_with_reasons(
        "AWS Secrets Manager announces a new capability",
        SERVICE_AWARE_CONFIG,
    )

    assert flow_logs_severity == "Medium"
    assert any("VPC Flow Logs" in reason for reason in flow_logs_reasons)
    assert reachability_severity == "High"
    assert any("Reachability Analyzer" in reason for reason in reachability_reasons)
    assert secrets_manager_severity == "Medium"
    assert any("AWS Secrets Manager" in reason for reason in secrets_manager_reasons)


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


def test_extract_page_summary_prefers_korean_text_for_localized_pages() -> None:
    html_doc = """
    <html>
      <head>
        <meta property="og:title" content="Amazon VPC console update">
        <meta name="description" content="Amazon VPC now supports a redesigned console workflow.">
      </head>
      <body>
        <h1>Amazon VPC 콘솔 업데이트</h1>
        <main>
          <p>Amazon VPC 콘솔에서 엔드포인트와 라우팅 관련 워크플로를 더 쉽게 확인할 수 있도록 화면 구성이 업데이트되었습니다.</p>
          <p>This English paragraph should not be preferred when Korean content exists.</p>
        </main>
      </body>
    </html>
    """

    title, summary = extract_page_summary(html_doc, prefer_korean=True)

    assert title == "Amazon VPC 콘솔 업데이트"
    assert "Amazon VPC 콘솔" in summary
    assert "redesigned console workflow" not in summary


def test_make_guid_is_stable_for_same_entry() -> None:
    entry = {
        "id": "item-1",
        "link": "https://aws.amazon.com/example",
        "title": "Example update",
        "published": "Mon, 15 Jun 2026 00:00:00 GMT",
    }

    assert make_guid("feed", entry) == make_guid("feed", entry)


def test_item_to_rss_description_is_compact_for_slack() -> None:
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
    assert "https://aws.amazon.com/example" in description
    assert "운영 판단" not in description
    assert "확인할 것" not in description
    assert "표시 언어" not in description
    assert "매칭 키워드" not in description
    assert "출처" not in description
    assert "분류" not in description


def test_item_to_rss_description_can_include_severity_reasons() -> None:
    description = item_to_rss_description(
        title="CloudWatch console update",
        source_name="AWS What's New Filtered",
        category="whats-new",
        severity="High",
        language="en fallback",
        matched=["CloudWatch"],
        page_summary="",
        rss_summary="CloudWatch console update",
        link="https://aws.amazon.com/example",
        config={**BASE_CONFIG, "summary": {**BASE_CONFIG["summary"], "include_severity_reasons": True}},
        severity_reasons=["critical service: CloudWatch", "high change: console"],
    )

    assert "판단 근거" in description
    assert "critical service: CloudWatch" in description
    assert "high change: console" in description
    assert "운영 판단" not in description
    assert "확인할 것" not in description


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
