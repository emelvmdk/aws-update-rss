# aws-update-rss

AWS update RSS aggregator for Slack.

이 저장소는 AWS What's New, AWS Docs/History RSS, AWS Blog, Security Bulletins를 수집해서 Slack RSS 앱이 구독할 수 있는 통합 RSS를 생성합니다.

## 기능

- 여러 AWS RSS feed 수집
- What's New feed는 키워드 기반 필터링
- Docs/History/Blog/Bulletin feed는 기본 전체 수집
- 한국어 URL 우선 시도
- 한국어 페이지가 없거나 추출 실패 시 영어 원문 fallback
- Slack에서 읽기 좋은 한글 템플릿 요약 생성
- 중요도/카테고리 태그 부여
- GitHub Actions + GitHub Pages로 무료 운영

## 사용 흐름

```text
AWS RSS feeds
  ↓
GitHub Actions
  ↓
public/feed.xml 생성
  ↓
GitHub Pages 배포
  ↓
Slack RSS 앱에서 feed.xml 구독
```

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

- `feeds.yaml`: 수집할 RSS 목록
- `config.yaml`: 필터 키워드, 제외 키워드, 중요도 규칙, 출력 설정
- `generate_feed.py`: RSS 생성기

## 무료 운영 주의사항

이 구현은 AI API를 사용하지 않습니다. 한국어 페이지가 있으면 한국어 본문을 추출하고, 없으면 영어 원문으로 fallback합니다. 따라서 “자연어 AI 번역”이 아니라 “한국어 우선 추출 + 한글 템플릿 요약” 방식입니다.
