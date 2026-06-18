# aws-update-rss

AWS update RSS aggregator for Slack.

이 저장소는 AWS What's New, AWS Docs/History RSS, AWS Blog, Security Bulletins를 수집해서 Slack RSS 앱이 구독할 수 있는 통합 RSS를 생성합니다.

## 기능

- 여러 AWS RSS feed 수집
- What's New feed는 영어 원본 RSS 기준으로 키워드 필터링
- Docs/History/Blog/Bulletin feed는 기본 전체 수집
- 영어 원본 feed/page를 업데이트 기준으로 먼저 확인
- 한국어 페이지가 있으면 Slack 표시용으로 한국어 title/summary 사용
- 한국어 페이지가 없거나 추출 실패 시 영어 원문 page로 fallback
- Slack에서 읽기 좋은 한글 템플릿 요약 생성
- 중요도/카테고리 태그 부여
- GitHub Actions + GitHub Pages로 무료 운영
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
/feed subscribe https://<github-username>.github.io/aws-update-rss/feed.xml
```

기존 원본 AWS RSS들은 테스트 후 제거하는 것을 권장합니다.

```text
/feed list
/feed remove <기존 RSS ID>
```

## 설정 파일

- `feeds.yaml`, `feeds_blogs.yaml`, `feeds_edge.yaml`, `feeds_network.yaml`: 수집할 RSS 목록
- `config.yaml`: 필터 키워드, 제외 키워드, 중요도 규칙, 출력 설정
- `generate_feed.py`: RSS 생성기
- `tests/test_generate_feed.py`: 핵심 로직 테스트
- `tests/test_localization_and_filtering.py`: 영어 원본 기준 localization/fallback 및 필터링 테스트

## 로컬 실행

```bash
python -m pip install -r requirements.txt
pytest -q
python generate_feed.py
```

생성 결과는 `public/feed.xml`, `public/index.html`, `public/status.html`에 저장됩니다.

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
- 중요도 분류
- HTML 메타 설명 추출
- RSS GUID 안정성
- Slack용 description 생성
- 최종 RSS XML 생성

## 무료 운영 주의사항

이 구현은 AI API를 사용하지 않습니다. 영어 원본 RSS/page를 업데이트 기준으로 먼저 확인하고, 한국어 페이지가 있으면 표시용으로 한국어 본문을 추출합니다. 한국어 페이지가 없으면 영어 원문으로 fallback합니다. 따라서 “자연어 AI 번역”이 아니라 “영어 원본 기준 + 한국어 표시 보강 + 영어 fallback” 방식입니다.
