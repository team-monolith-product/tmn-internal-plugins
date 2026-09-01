#!/usr/bin/env python3
"""Codex·Claude 작업 구간의 메인/서브에이전트 사용량을 완료 도구에 붙입니다.

프롬프트나 응답 내용은 저장하지 않습니다. 상태 파일에는 실행 식별자, transcript
경로와 작업 시작 시점의 바이트 offset만 남깁니다. 두 클라이언트 모두 transcript
형식을 안정 API로 보장하지 않으므로 파싱 누락은 collection_status로 드러냅니다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple

COLLECTOR_VERSION = "tmn-operating/0.1.3"
START_TOOL = "start_slack_list_task"
PUBLISH_TOOL = "publish_slack_task_result"
# ponytail: 범주별 토큰은 의사결정에 실제 필요해질 때만 수집한다.


class UsageKey(NamedTuple):
    model: str | None
    effort: str | None
    is_subagent: bool


UsageGroups = dict[UsageKey, int]


def tool_is(name: str, expected: str) -> bool:
    return name.lower().replace("-", "_").endswith(expected)


def state_root() -> Path:
    configured = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    root = (
        Path(configured)
        if configured
        else Path.home() / ".cache" / "tmn-operating" / "execution-metrics"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def digest(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def detect_service(payload: dict[str, Any]) -> str:
    transcript = str(payload.get("transcript_path") or "")
    if "/.claude/" in transcript or "\\.claude\\" in transcript:
        return "Claude Code"
    return "Codex"


def prompt_state_path(payload: dict[str, Any]) -> Path:
    key = digest(detect_service(payload), str(payload["session_id"]))
    return state_root() / "prompt" / f"{key}.json"


def task_state_path(payload: dict[str, Any], list_url: str) -> Path:
    key = digest(detect_service(payload), str(payload["session_id"]), list_url)
    return state_root() / "task" / f"{key}.json"


def iter_json_lines(path: Path, offset: int = 0) -> Iterable[dict[str, Any] | None]:
    """큰 transcript를 메모리에 쌓지 않고 한 줄씩 읽습니다.

    파싱할 수 없는 줄이나 파일 오류는 None으로 알려 수집 상태를 partial로 남깁니다.
    """
    try:
        with path.open("rb") as stream:
            size = path.stat().st_size
            actual_offset = offset if 0 <= offset <= size else 0
            if actual_offset != offset:
                yield None
            stream.seek(actual_offset)
            for raw_line in stream:
                try:
                    value = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    yield None
                    continue
                if isinstance(value, dict):
                    yield value
                else:
                    yield None
    except OSError:
        yield None


def first_codex_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            for index, raw_line in enumerate(stream):
                if index >= 80:
                    break
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if record.get("type") == "session_meta":
                    value = record.get("payload")
                    return value if isinstance(value, dict) else None
    except OSError:
        pass
    return None


def codex_sessions_root(transcript: Path) -> Path:
    for parent in transcript.parents:
        if parent.name == "sessions":
            return parent
    return transcript.parent


def discover_codex_files(
    transcript: Path,
    session_id: str,
    *,
    since: float | None = None,
    known: Iterable[str] = (),
) -> list[Path]:
    candidates = {transcript, *(Path(value) for value in known)}
    if since is not None:
        sessions_root = codex_sessions_root(transcript)
        day = datetime.fromtimestamp(since, timezone.utc).date() - timedelta(days=1)
        last_day = datetime.now(timezone.utc).date() + timedelta(days=1)
        while day <= last_day:
            directory = sessions_root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
            try:
                for path in directory.glob("*.jsonl"):
                    if path.stat().st_mtime >= since - 1:
                        candidates.add(path)
            except OSError:
                pass
            day += timedelta(days=1)

    matches = []
    for path in candidates:
        meta = first_codex_meta(path)
        if meta and str(meta.get("session_id")) == session_id:
            matches.append(path)
    return sorted(set(matches))


def discover_claude_files(transcript: Path) -> list[Path]:
    files = [transcript]
    subagent_dir = transcript.with_suffix("") / "subagents"
    if subagent_dir.is_dir():
        files.extend(subagent_dir.glob("*.jsonl"))
    return sorted(set(files))


def discover_files(
    payload: dict[str, Any], *, since: float | None = None, known: Iterable[str] = ()
) -> list[Path]:
    transcript = Path(str(payload.get("transcript_path") or ""))
    if not transcript.is_file():
        return []
    if detect_service(payload) == "Claude Code":
        return discover_claude_files(transcript)
    return discover_codex_files(
        transcript, str(payload["session_id"]), since=since, known=known
    )


def capture_baseline(payload: dict[str, Any]) -> dict[str, Any]:
    captured_at = time.time()
    offsets = {}
    for path in discover_files(payload):
        try:
            offsets[str(path)] = path.stat().st_size
        except OSError:
            continue
    return {
        "captured_at": captured_at,
        "offsets": offsets,
        "model": payload.get("model"),
    }


def task_execution_id(
    payload: dict[str, Any], list_url: str, baseline: dict[str, Any]
) -> str:
    """같은 게시 재시도는 묶고, 같은 세션의 재개 실행은 분리합니다."""
    value = digest(
        detect_service(payload),
        str(payload["session_id"]),
        list_url,
        str(baseline.get("captured_at") or 0),
    )
    return f"run-{value}"


def bind_task(payload: dict[str, Any]) -> None:
    list_url = str((payload.get("tool_input") or {}).get("list_url") or "")
    if not list_url:
        return
    path = task_state_path(payload, list_url)
    if path.exists():
        return
    baseline = read_json(prompt_state_path(payload)) or capture_baseline(payload)
    baseline |= {
        "execution_id": task_execution_id(payload, list_url, baseline),
    }
    write_json(path, baseline)


def collect_codex(
    paths: list[Path], offsets: dict[str, int], fallback_model: str | None
) -> tuple[UsageGroups, bool]:
    groups: UsageGroups = {}
    malformed = False
    for path in paths:
        is_subagent = (first_codex_meta(path) or {}).get("thread_source") != "user"
        model = fallback_model if not is_subagent else None
        effort = None
        for record in iter_json_lines(path, int(offsets.get(str(path), 0))):
            if record is None:
                malformed = True
                continue
            if record.get("type") == "turn_context":
                context = record.get("payload") or {}
                model = context.get("model") or model
                effort = context.get("effort") or effort
                continue
            if record.get("type") != "event_msg":
                continue
            event = record.get("payload") or {}
            if event.get("type") != "token_count":
                continue
            raw = (event.get("info") or {}).get("last_token_usage") or {}
            if raw.get("total_tokens") is None:
                malformed = True
                continue
            try:
                total_tokens = int(raw["total_tokens"])
            except (TypeError, ValueError):
                malformed = True
                continue
            key = UsageKey(model, effort, is_subagent)
            groups[key] = groups.get(key, 0) + total_tokens
    return groups, malformed


def claude_total_tokens(raw: dict[str, Any]) -> int | None:
    values = [
        raw.get(field)
        for field in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    ]
    if not any(value is not None for value in values):
        return None
    try:
        return sum(int(value or 0) for value in values)
    except (TypeError, ValueError):
        return None


def collect_claude(
    paths: list[Path], offsets: dict[str, int], fallback_model: str | None
) -> tuple[UsageGroups, bool]:
    groups: UsageGroups = {}
    seen_messages: set[tuple[str, str]] = set()
    malformed = False
    for path in paths:
        is_subagent = path.parent.name == "subagents"
        for record in iter_json_lines(path, int(offsets.get(str(path), 0))):
            if record is None:
                malformed = True
                continue
            if record.get("type") != "assistant":
                continue
            message = record.get("message") or {}
            raw_usage = message.get("usage")
            message_id = str(message.get("id") or record.get("uuid") or "")
            identity = (str(path), message_id)
            if not isinstance(raw_usage, dict) or identity in seen_messages:
                continue
            seen_messages.add(identity)
            model = message.get("model") or fallback_model
            total_tokens = claude_total_tokens(raw_usage)
            if total_tokens is None:
                malformed = True
                continue
            if model == "<synthetic>" and total_tokens == 0:
                continue
            key = UsageKey(model, record.get("effort"), is_subagent)
            groups[key] = groups.get(key, 0) + total_tokens
    return groups, malformed


def collect_publish_metadata(payload: dict[str, Any]) -> dict[str, Any] | None:
    tool_input = payload.get("tool_input") or {}
    if tool_input.get("collector_version") == COLLECTOR_VERSION and tool_input.get(
        "execution_id"
    ):
        return None
    list_url = str(tool_input.get("list_url") or "")
    if not list_url:
        return None
    state = read_json(task_state_path(payload, list_url))
    if state is None:
        state = read_json(prompt_state_path(payload)) or capture_baseline(payload)
        state |= {
            "execution_id": task_execution_id(payload, list_url, state),
        }
        write_json(task_state_path(payload, list_url), state)

    offsets = {str(key): int(value) for key, value in state.get("offsets", {}).items()}
    paths = discover_files(
        payload,
        since=float(state.get("captured_at") or 0),
        known=offsets,
    )
    fallback_model = payload.get("model") or state.get("model")
    if detect_service(payload) == "Claude Code":
        groups, malformed = collect_claude(paths, offsets, fallback_model)
    else:
        groups, malformed = collect_codex(paths, offsets, fallback_model)

    root_groups = {key: total for key, total in groups.items() if not key.is_subagent}
    candidates = root_groups or groups
    primary = max(candidates, key=candidates.get, default=None)
    status = "unavailable" if not groups else "partial" if malformed else "complete"
    return {
        "execution_id": state["execution_id"],
        "model": primary.model if primary and primary.model else fallback_model,
        "reasoning_effort": primary.effort if primary else None,
        "total_tokens": sum(groups.values()) if groups else None,
        "collector_version": COLLECTOR_VERSION,
        "collection_status": status,
    }


def tool_failed(payload: dict[str, Any]) -> bool:
    response = payload.get("tool_response")
    return isinstance(response, dict) and bool(
        response.get("isError") or response.get("is_error")
    )


def complete_task(payload: dict[str, Any]) -> None:
    if tool_failed(payload):
        return
    list_url = str((payload.get("tool_input") or {}).get("list_url") or "")
    if not list_url:
        return
    try:
        task_state_path(payload, list_url).unlink()
    except FileNotFoundError:
        pass


def deny_for_retry(metadata: dict[str, Any]) -> dict[str, Any]:
    fields = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    reason = (
        "실행 메타데이터 수집이 완료되었습니다. 같은 publish_slack_task_result 호출을 "
        "즉시 다시 실행하되 다른 입력은 유지하고 다음 훅 전용 필드를 그대로 추가하세요: "
        f"{fields}"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main() -> None:
    payload = json.load(sys.stdin)
    event = str(payload.get("hook_event_name") or "")
    tool_name = str(payload.get("tool_name") or "")

    if event == "UserPromptSubmit":
        write_json(prompt_state_path(payload), capture_baseline(payload))
        return
    if event == "PreToolUse" and tool_is(tool_name, START_TOOL):
        bind_task(payload)
        return
    if event == "PreToolUse" and tool_is(tool_name, PUBLISH_TOOL):
        metadata = collect_publish_metadata(payload)
        if metadata is not None:
            print(json.dumps(deny_for_retry(metadata), ensure_ascii=False))
        return
    if event == "PostToolUse" and tool_is(tool_name, PUBLISH_TOOL):
        complete_task(payload)


if __name__ == "__main__":
    main()
