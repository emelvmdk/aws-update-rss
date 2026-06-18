from __future__ import annotations

from generate_feed import URL_HINT_SKIP_REASON, enrich_entry, evaluate_include, should_include


CONFIG = {
    "fetch": {
        "timeout_seconds": 3,
        "user_agent": "test-agent",
    },
    "localization": {
        "enabled": True,
        "preferred_docs_locale": "ko_kr",
        "preferred_aws_site_locale": "ko",
        "fallback_to_original": True,
    },
    "summary": {
        "max_summary_chars": 700,
        "max_detail_chars": 900,
        "include_matched_keywords": True,
        "include_language_status": True,
        "include_severity": True,
    },
    "what_new_filter": {
        "include_keywords": [
            "CloudWatch",
            "Transit Gateway",
            "Network Firewall",
            "PrivateLink",
        ],
        "exclude_keywords": [
            "HealthOmics",
            "IoT Core",
            "SageMaker",
        ],
    },
    "category_hints": {
        "whats-new": "신규 기능, 콘솔 경험, 리전 확장 또는 서비스 동작 변경 여부를 확인하세요.",
        "networking": "라우팅, 연결성, 하이브리드 네트워크, 보안 검사 경로에 영향이 있는지 확인하세요.",
    },
}


class FakeResponse:
    def __init__(
        self,
        url: str,
        text: str,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class FakeSession:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.requested_urls: list[str] = []

    def get(self, url: str, timeout: int, allow_redirects: bool = True) -> FakeResponse:
        self.requested_urls.append(url)
        return self.responses.get(
            url,
            FakeResponse(url=url, text="Page Not Found", status_code=404),
        )


def html_page(title: str, description: str) -> str:
    return f"""
    <!doctype html>
    <html>
      <head>
        <meta name="description" content="{description}">
        <title>{title}</title>
      </head>
      <body>
        <main>
          <h1>{title}</h1>
          <p>{description} 이 문장은 테스트 추출을 위해 충분한 길이를 가집니다.</p>
        </main>
      </body>
    </html>
    """


def test_enrich_entry_checks_english_source_before_korean_display_page() -> None:
    original_url = "https://aws.amazon.com/about-aws/whats-new/2026/06/cloudwatch-example/"
    korean_url = "https://aws.amazon.com/ko/about-aws/whats-new/2026/06/cloudwatch-example/"
    session = FakeSession(
        {
            original_url: FakeResponse(
                url=original_url,
                text=html_page(
                    "Amazon CloudWatch announces Log Analytics",
                    "CloudWatch Logs Insights, Live Tail, and Contributor Insights are unified.",
                ),
            ),
            korean_url: FakeResponse(
                url=korean_url,
                text=html_page(
                    "Amazon CloudWatch Log Analytics 발표",
                    "CloudWatch 로그 분석 기능이 통합 콘솔 경험으로 제공됩니다.",
                ),
            ),
        }
    )
    entry = {
        "title": "Amazon CloudWatch announces Log Analytics",
        "summary": "English RSS summary",
        "link": original_url,
    }

    result = enrich_entry(session, entry, {"name": "AWS What's New Filtered"}, CONFIG)

    assert session.requested_urls == [original_url, korean_url]
    assert result["language"] == "ko"
    assert result["link"] == korean_url
    assert result["source_link"] == original_url
    assert result["source_language"] == "en source"
    assert "통합 콘솔" in result["page_summary"]
    assert "Live Tail" in result["source_summary"]


def test_enrich_entry_falls_back_to_english_when_korean_page_is_missing() -> None:
    original_url = "https://aws.amazon.com/about-aws/whats-new/2026/06/cloudwatch-example/"
    korean_url = "https://aws.amazon.com/ko/about-aws/whats-new/2026/06/cloudwatch-example/"
    session = FakeSession(
        {
            original_url: FakeResponse(
                url=original_url,
                text=html_page(
                    "Amazon CloudWatch announces Log Analytics",
                    "CloudWatch English-only update details are available here.",
                ),
            ),
            korean_url: FakeResponse(url=korean_url, text="Page Not Found", status_code=404),
        }
    )
    entry = {
        "title": "Amazon CloudWatch announces Log Analytics",
        "summary": "English RSS summary",
        "link": original_url,
    }

    result = enrich_entry(session, entry, {"name": "AWS What's New Filtered"}, CONFIG)

    assert session.requested_urls == [original_url, korean_url]
    assert result["language"] == "en fallback"
    assert result["link"] == original_url
    assert result["source_link"] == original_url
    assert "English-only update" in result["page_summary"]


def test_enrich_entry_uses_rss_only_when_english_page_cannot_be_checked() -> None:
    original_url = "https://aws.amazon.com/about-aws/whats-new/2026/06/cloudwatch-example/"
    session = FakeSession({})
    entry = {
        "title": "Amazon CloudWatch announces Log Analytics",
        "summary": "English RSS summary is still available.",
        "link": original_url,
    }

    result = enrich_entry(session, entry, {"name": "AWS What's New Filtered"}, CONFIG)

    assert result["language"] == "rss-only fallback"
    assert result["link"] == original_url
    assert result["source_language"] == "en rss source"
    assert result["source_summary"] == "English RSS summary is still available."


def test_whats_new_filter_uses_english_source_entry_text() -> None:
    entry = {
        "title": "Amazon CloudWatch announces Log Analytics",
        "summary": "Logs Insights and Live Tail are unified.",
        "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/cloudwatch-example/",
    }

    included, matches = should_include(entry, {"filter_mode": "keyword", "category": "whats-new"}, CONFIG)

    assert included is True
    assert matches == ["CloudWatch"]


def test_url_hint_skipped_candidate_keeps_review_reason_for_whats_new() -> None:
    entry = {
        "title": "Amazon CloudWatch announces Log Analytics",
        "summary": "CloudWatch Logs Insights and Live Tail are unified.",
        "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/console-experience-update/",
    }

    included, matches, reason = evaluate_include(entry, {"filter_mode": "keyword", "category": "whats-new"}, CONFIG)

    assert included is False
    assert matches == ["CloudWatch"]
    assert reason == URL_HINT_SKIP_REASON


def test_fsi_and_architecture_do_not_require_url_hint_by_default() -> None:
    entry = {
        "title": "Amazon CloudWatch announces Log Analytics",
        "summary": "CloudWatch Logs Insights and Live Tail are unified.",
        "link": "https://aws.amazon.com/blogs/architecture/centralized-observability-pattern/",
    }

    fsi_included, fsi_matches, fsi_reason = evaluate_include(entry, {"filter_mode": "keyword", "category": "fsi"}, CONFIG)
    arch_included, arch_matches, arch_reason = evaluate_include(entry, {"filter_mode": "keyword", "category": "architecture"}, CONFIG)

    assert fsi_included is True
    assert fsi_matches == ["CloudWatch"]
    assert fsi_reason == ""
    assert arch_included is True
    assert arch_matches == ["CloudWatch"]
    assert arch_reason == ""


def test_whats_new_exclude_keywords_win_even_when_included_keyword_exists() -> None:
    entry = {
        "title": "AWS HealthOmics streams engine logs to CloudWatch",
        "summary": "CloudWatch appears, but HealthOmics should be filtered out.",
        "link": "https://aws.amazon.com/about-aws/whats-new/2026/06/healthomics-example/",
    }

    included, matches = should_include(entry, {"filter_mode": "keyword", "category": "whats-new"}, CONFIG)

    assert included is False
    assert matches == []


def test_docs_history_feed_is_not_keyword_filtered() -> None:
    entry = {
        "title": "AWS Backup documentation update",
        "summary": "No What’s New keyword is required for curated docs feeds.",
        "link": "https://docs.aws.amazon.com/aws-backup/latest/devguide/example.html",
    }

    included, matches = should_include(entry, {"filter_mode": "all"}, CONFIG)

    assert included is True
    assert matches == []
