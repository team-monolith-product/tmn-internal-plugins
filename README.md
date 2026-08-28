# Team Monolith Internal Plugins

Team Monolith 구성원에게만 배포하는 Claude·Codex 플러그인 Marketplace입니다.
저장소 접근 권한이 있는 사내 GitHub 계정으로만 설치할 수 있습니다.

## 설치 주소

```text
https://wfa.codle.io/plugins/tmn-operating.git
```

### Codex

최초 한 번 Marketplace를 추가합니다.

```bash
codex plugin marketplace add https://wfa.codle.io/plugins/tmn-operating.git
```

Codex 앱을 다시 열고 **Plugins → Team Monolith → TMN Operating**에서 설치합니다.

### Claude Code

Claude Code 안에서 최초 한 번 Marketplace를 추가하고 플러그인을 설치합니다.

```text
/plugin marketplace add https://wfa.codle.io/plugins/tmn-operating.git
/plugin install tmn-operating@team-monolith
```

설치 후 `/reload-plugins`를 실행합니다.

## 제공 기능

- `query_knowledge`: 사내 과거 업무와 Slack 공개 채널 지식 검색
- `start-slack-list-task`: Slack List 작업 시작·재개와 요청 맥락·기존 작업 기록 조회
- `publish_slack_task_result`: 사용자 승인을 받은 짧은 최종 결과를 게시하고 완료 처리
- `publish-operational-postmortem`: 실패를 만든 결정의 스레드에 포스트모템을 남기고 개선 작업 연결
- `start-operate-task`: 착수 조건을 확인하고 리뷰와 포스트모템 검토 뒤 완료하는 운영 절차
- `operational-postmortem`: 사실과 가설을 구분해 원인을 조사하고 자기 개선 작업을 만드는 분석 절차

MCP 도구를 처음 사용할 때 admin-rails OAuth로 사내 계정을 인증합니다.

## MCP 연결 주소

플러그인 설정에는 아래 두 주소가 포함됩니다. 둘은 같은 `wfa.codle.io` FastAPI
서비스에서 제공되며, 경로별로 노출 도구만 분리합니다.

| 용도 | 주소 | 노출 도구 |
|---|---|---|
| 전사 지식 검색 | `https://wfa.codle.io/mcp` | `query_knowledge` |
| 운영 Slack List 작업 | `https://wfa.codle.io/mcp/operate` | `start-slack-list-task`, `publish-operational-postmortem`, `publish_slack_task_result` |

두 주소는 플러그인에 이미 설정되어 있어 사용자가 별도로 MCP 주소를 추가하거나 바꿀 필요가 없습니다.

## 업데이트

Codex:

```bash
codex plugin marketplace upgrade team-monolith
codex plugin add tmn-operating@team-monolith
```

업데이트 후 Codex 앱을 다시 실행하고 새 작업을 엽니다.

Claude Code는 Marketplace 자동 업데이트를 켜거나 다음 명령으로 갱신합니다.

```text
/plugin marketplace update team-monolith
/reload-plugins
```
