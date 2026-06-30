# aws-update-rss

AWS update RSS aggregator for Slack.

이 저장소는 AWS What's New, AWS Docs/History RSS, AWS Blog, Security Bulletins를 수집해서 Slack RSS 앱이 구독할 수 있는 통합 RSS를 생성합니다.

## 운영 URL

GitHub Pages 배포 후 운영에서 사용하는 공개 URL입니다.

| 용도 | URL |
| --- | --- |
| Slack RSS 구독용 feed | `https://emelvmdk.github.io/aws-update-rss/feed.xml` |
| 최신 수집 결과 요약 | `https://emelvmdk.github.io/aws-update-rss/index.html` |
| 실행 상태 / 수집 상태 | `https://emelvmdk.github.io/aws-update-rss/status.html` |
| 스킵 / 경계 항목 검토 | `https://emelvmdk.github.io/aws-update-rss/review.html` |
| 스킵 / 경계 항목 JSON | `https://emelvmdk.github.io/aws-update-rss/review.json` |
| Slack RSS 발송 디버그 | `https://emelvmdk.github.io/aws-update-rss/slack-debug.html` |
| Slack RSS 발송 디버그 JSON | `https://emelvmdk.github.io/aws-update-rss/slack-debug.json` |
| Slack 재발송 큐 상태 | `https://emelvmdk.github.io/aws-update-rss/slack-replay-queue.json` |
| 마지막 실행 메타데이터 | `https://emelvmdk.github.io/aws-update-rss/last-run.json` |

Slack 채널에는 아래 URL만 구독합니다.

```text
/feed subscribe https://emelvmdk.github.io/aws-update-rss/feed.xml
```

## 기능

- 여러 AWS RSS feed 수집
- What's New feed는 영어 원본 RSS 기준으로 키워드 필터링
- Docs/History/Blog/Bulletin feed는 기본 전체 수집
- 영어 원본 feed/page를 업데이트 기준으로 먼저 확인
- 한국어 페이지가 있으면 Slack 표시용으로 한국어 title/summary 사용
- 한국어 페이지가 없거나 추출 실패 시 영어 원문 page로 fallback
- Slack에서 읽기 좋은 한글 템플릿 요약 생성
- Slack 메시지에는 중요도를 표시하지 않음
- 중요도, 판단 근거, 카테고리는 디버그/검토용 hidden field로 유지
- Slack RSS 발송 누락 확인용 `slack-debug.html/json` 생성
- Slack RSS 재발송 큐 `slack-replay-queue.json` 생성
- pytest 기반 핵심 로직 테스트

## 사용 흐름

```text
English AWS RSS feeds
  ↓
GitHub Actions
  ↓
pytest로 핵심 로직 검증
  ↓
영어 원본 page 확인
  ↓
한국어 page가 있으면 표시용으로 사용
  ↓
없으면 영어 원문 fallback
  ↓
public/feed.xml 생성
  ↓
Slack 표시용 포맷 정리
  ↓
GitHub Pages 배포
  ↓
Slack RSS 앱에서 feed.xml 구독
```

## 중요한 동작 원칙

이 프로젝트는 한국어 페이지를 기준으로 업데이트를 판단하지 않습니다.

```text
업데이트 감지 기준: 영어 원본 RSS / 영어 원본 page
Slack 표시 내용: 한국어 page가 있으면 한국어 사용
Fallback: 한국어 page가 없으면 영어 원문 사용
```

이렇게 한 이유는 AWS 업데이트가 영어 페이지에 먼저 올라오거나, 영어 페이지에만 존재하는 경우가 있기 때문입니다. 따라서 한국어 페이지가 늦게 생기거나 아예 없어도 업데이트를 놓치지 않도록 설계했습니다.

## GitHub Pages 설정

저장소에서 아래 설정을 한 번만 수행하세요.

```text
Settings → Pages → Build and deployment → Source: GitHub Actions
```

그 후 Actions 탭에서 `Build filtered AWS update RSS` 워크플로를 수동 실행합니다.

## Slack 등록

GitHub Pages 배포 후 생성되는 RSS URL을 Slack 채널에 등록합니다.

```text
/feed subscribe https://emelvmdk.github.io/aws-update-rss/feed.xml
```

기존 원본 AWS RSS들은 테스트 후 제거하는 것을 권장합니다.

```text
/feed list
/feed remove <기존 RSS ID>
```

Slack 메시지가 오지 않을 때는 먼저 아래 순서로 확인합니다.

```text
1. https://emelvmdk.github.io/aws-update-rss/feed.xml 에 새 item이 있는지 확인
2. https://emelvmdk.github.io/aws-update-rss/slack-debug.html 에서 GUID, Link hash, Slack risk 확인
3. GitHub Actions에서 include_test_item=true 로 수동 테스트 실행
4. Slack에서 /feed list 로 구독 URL 확인
5. 필요 시 기존 구독을 제거하고 feed.xml을 다시 구독
```

## Slack 재발송 큐

Slack RSS 앱이 특정 item을 누락했을 때는 GitHub Actions의 `workflow_dispatch` 입력값으로 재발송 대상을 큐에 넣을 수 있습니다.

- `replay_recent_items`: 최신 N개 item을 재발송 큐에 추가합니다. 실제 feed에는 한 번에 1개씩만 공개됩니다.
- `replay_guid_prefix`: `slack-debug.html`에서 확인한 단일 GUID prefix를 재발송 큐에 추가합니다.
- `replay_guid_prefixes`: 여러 GUID prefix를 공백, 쉼표, 줄바꿈으로 입력해서 재발송 큐에 추가합니다.

재발송 큐는 아래 URL에서 확인합니다.

```text
https://emelvmdk.github.io/aws-update-rss/slack-replay-queue.json
```

여러 개를 재발송해야 해도 feed에는 동시에 여러 재발송 item을 넣지 않습니다. Slack RSS 앱이 여러 새 item을 한 메시지로 묶을 수 있기 때문에, 큐에서 하나씩 순차 공개합니다.

## 설정 파일

- `feeds.yaml`, `feeds_blogs.yaml`, `feeds_edge.yaml`, `feeds_network.yaml`: 수집할 RSS 목록
- `config.yaml`: 필터 키워드, 제외 키워드, 출력 설정
- `generate_feed.py`: RSS 생성기
- `scripts/format_slack_rss.py`: Slack 표시용 RSS description 포맷터 및 Slack 디버그 파일 생성
- `scripts/add_slack_replay_items.py`: Slack RSS 재발송 큐 처리
- `scripts/schedule_gate.py`: 정기 실행 슬롯, catch-up, Slack 재발송 큐 실행 판단
- `tests/test_generate_feed.py`: 핵심 로직 테스트
- `tests/test_localization_and_filtering.py`: 영어 원본 기준 localization/fallback 및 필터링 테스트

## 로컬 실행

```bash
python -m pip install -r requirements.txt
pytest -q
python generate_feed.py
python scripts/format_slack_rss.py
```

생성 결과는 `public/feed.xml`, `public/index.html`, `public/status.html`, `public/review.html`, `public/review.json`, `public/slack-debug.html`, `public/slack-debug.json`에 저장됩니다.

## 테스트 범위

테스트는 외부 네트워크에 의존하지 않는 순수 로직 위주로 구성했습니다.

- HTML 정리 및 문자열 축약
- AWS 한국어 URL 후보 생성
- 영어 원본 page를 먼저 조회하는지 검증
- 한국어 page가 있으면 표시용으로 사용하는지 검증
- 한국어 page가 없으면 영어 page로 fallback하는지 검증
- 영어 page 조회 실패 시 RSS 내용으로 fallback하는지 검증
- What's New 포함/제외 키워드 필터링
- Docs/History feed 전체 수집 모드
- 중요도 산출 결과의 hidden field 유지
- HTML 메타 설명 추출
- RSS GUID 안정성
- Slack용 description 생성
- 최종 RSS XML 생성

## 공개 URL / Secret 관리 기준

이 저장소에 박제해도 되는 URL은 이미 GitHub Pages로 공개 배포되는 정적 URL입니다.

```text
https://emelvmdk.github.io/aws-update-rss/feed.xml
https://emelvmdk.github.io/aws-update-rss/slack-debug.html
https://emelvmdk.github.io/aws-update-rss/status.html
https://emelvmdk.github.io/aws-update-rss/review.html
```

아래 항목은 절대 README, 코드, 설정 파일에 직접 저장하지 않습니다.

```text
Slack Webhook URL
GitHub token
AWS Access Key / Secret Access Key
API key
사내 시스템 URL
고객사 도메인 / 내부 IP / VPN URL
AWS 계정 ID가 포함된 콘솔 링크
CloudWatch Logs 직접 링크
S3 presigned URL
쿼리 파라미터에 token, key, signature가 들어간 URL
```

Secret이 필요한 경우 GitHub Actions Secrets에 저장합니다.

```text
Settings → Secrets and variables → Actions → Repository secrets
```

## 운영 주의사항

이 구현은 AI API를 사용하지 않습니다. 영어 원본 RSS/page를 업데이트 기준으로 먼저 확인하고, 한국어 페이지가 있으면 표시용으로 한국어 본문을 추출합니다. 한국어 페이지가 없으면 영어 원문으로 fallback합니다. 따라서 “자연어 AI 번역”이 아니라 “영어 원본 기준 + 한국어 표시 보강 + 영어 fallback” 방식입니다.
