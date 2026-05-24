"""Core daemon logic for the Viabe Team agent-loop daemon.

Single Python process that:
  1. Polls `.running/to-claudecode/` (signals from Cowork) and
     `.viabe/queue/*/status` (queued tasks) every 30 s.
  2. Dispatches the highest-priority work via `claude_agent_sdk.query()` while
     preserving a single Claude Code session_id across all signals.
  3. Enforces the brief's parallelism + priority policy strictly.
  4. Auto-detects merged PRs and flips `in-pr → merged → done`, unblocking
     dependent tasks.

Idempotency note
----------------
- `type: task` signals (one-shot bash, e.g. `gh pr merge`) are moved to
  processed/ BEFORE the query() call so a crash mid-run cannot re-fire the
  same destructive action. Auto-merge-detection self-heals the queue state
  if the daemon crashes after the merge but before signalling task-result.
- All other signal types (brief-ready, review, plan-ready, answer, guidance,
  pre-merge-check) move to processed/ AFTER successful query() — re-running
  them on restart is a planning rewrite, not a destructive op.
- `notify` signals never invoke query() (Cowork review condition #2); they
  echo to daemon log + cost log and move to processed/.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

import hooks

POLL_SECONDS = 30
# Per-call ceiling — applies to a single query() invocation regardless of
# signal type. Sized to comfortably cover a brief-ready dispatch for a
# 3-hour task. notify is short-circuited so this cap is irrelevant for it.
PER_CALL_BUDGET_USD = 5.0
_BACKOFF_SLEEPS = [5, 30]
LLM_REQUIRED_TYPES = {
    "brief-ready",
    "review",
    "answer",
    "guidance",
    "task",
    "pre-merge-check",
    "plan-ready",
}
MERGE_DETECT_TYPES = ("in-pr",)
SKIP_TASK_STATUSES = {"blocked", "deferred", "done", "merged"}
BUSY_TASK_STATUSES = {"planning", "implementing"}


@dataclass(frozen=True)
class DaemonPaths:
    repo: Path
    inbox: Path
    outbox: Path
    processed: Path
    queue: Path
    cost_log: Path
    session_state: Path
    daemon_log: Path
    transcripts: Path
    stop_file: Path
    telegram_env: Path
    notifications_log: Path


def default_paths(repo: Path) -> DaemonPaths:
    return DaemonPaths(
        repo=repo,
        inbox=repo / ".running/to-claudecode",
        outbox=repo / ".running/to-cowork",
        processed=repo / ".running/processed",
        queue=repo / ".viabe/queue",
        cost_log=repo / ".viabe/daemon/cost.log",
        session_state=repo / ".viabe/daemon/session.state",
        daemon_log=repo / ".viabe/daemon/agent-loop.log",
        transcripts=repo / ".viabe/daemon/transcripts",
        stop_file=repo / ".viabe/daemon/STOP",
        telegram_env=repo / ".viabe/secrets/telegram.env",
        notifications_log=repo / ".viabe/notifications/log",
    )


@dataclass(frozen=True)
class ProcessSignal:
    path: Path
    task: Optional[str]
    sig_type: str


@dataclass(frozen=True)
class StartTask:
    task_id: str


Action = Union[ProcessSignal, StartTask]
QueryFn = Callable[..., Any]


def load_session_id(state_file: Path) -> Optional[str]:
    if not state_file.exists():
        return None
    try:
        text = state_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def save_session_id(state_file: Path, session_id: str) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(session_id, encoding="utf-8")
    os.replace(tmp, state_file)


def read_queue_state(queue_dir: Path) -> dict[str, str]:
    """Return {task_id: status} for all queue dirs except `done/`."""
    state: dict[str, str] = {}
    if not queue_dir.exists():
        return state
    for child in sorted(queue_dir.iterdir()):
        if not child.is_dir() or child.name == "done":
            continue
        status_file = child / "status"
        if not status_file.exists():
            continue
        try:
            state[child.name] = status_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return state


def parse_signal_frontmatter(path: Path) -> dict:
    """Tiny YAML-frontmatter parser. Flat key: value pairs only — block
    scalars (| and >) are skipped (their continuation lines are dropped)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end_idx = text.find("\n---", 4)
    if end_idx == -1:
        return {}
    block = text[4:end_idx]
    result: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip()
        if value in ("|", ">"):
            continue
        result[key.strip()] = value
    return result


def read_signal_body(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return text.strip()
    end_idx = text.find("\n---", 4)
    if end_idx == -1:
        return ""
    return text[end_idx + 4:].strip()


def scan_inbox(inbox: Path) -> list[dict]:
    """Return parsed signal frontmatter for each *.md in INBOX, oldest first."""
    if not inbox.exists():
        return []
    out: list[dict] = []
    for path in sorted(inbox.glob("*.md")):
        front = parse_signal_frontmatter(path)
        out.append(
            {
                "path": path,
                "task": front.get("task"),
                "type": front.get("type", "unknown"),
                "authorized_by": front.get("authorized_by"),
                "frontmatter": front,
            }
        )
    return out


def pick_next_action(state: dict[str, str], inbox: list[dict]) -> Optional[Action]:
    """Encode the brief's policy verbatim.

    1. If any task is in {planning, implementing}, only signals for that task
       are processable; nothing else.
    2. Otherwise: process the oldest signal in INBOX first (FIFO by filename
       timestamp). If no signals, dispatch the oldest `queued` task.
    3. Tasks in {blocked, deferred, done, merged} are skipped for dispatch
       (they still receive signals in step 2 if any arrive — e.g. an `answer`
       signal for a `blocked` task remains processable so Cowork can lift
       the block).
    """
    busy = [t for t, s in state.items() if s in BUSY_TASK_STATUSES]
    if busy:
        active = busy[0]
        for sig in inbox:
            if sig.get("task") == active:
                return ProcessSignal(path=sig["path"], task=sig.get("task"), sig_type=sig["type"])
        return None
    if inbox:
        sig = inbox[0]
        return ProcessSignal(path=sig["path"], task=sig.get("task"), sig_type=sig["type"])
    for tid, status in state.items():
        if status == "queued":
            return StartTask(task_id=tid)
    return None


def read_brief_budget(brief_path: Path) -> tuple[Optional[int], Optional[int]]:
    """Return (budget_tokens, budget_minutes) from brief frontmatter."""
    front = parse_signal_frontmatter(brief_path)

    def _maybe_int(value: Optional[str]) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return _maybe_int(front.get("budget_tokens")), _maybe_int(front.get("budget_minutes"))


def task_token_spend(cost_log: Path, task_id: str) -> int:
    """Sum tokens=N over cost_log lines for the given task_id."""
    if not cost_log.exists():
        return 0
    total = 0
    pattern = re.compile(r"\btokens=(\d+)\b")
    for line in cost_log.read_text(encoding="utf-8").splitlines():
        if f" {task_id} " not in line:
            continue
        m = pattern.search(line)
        if m:
            total += int(m.group(1))
    return total


def record_cost(
    cost_log: Path,
    task_id: str,
    sig_type: str,
    *,
    cost_usd: float = 0.0,
    tokens: int = 0,
    turns: int = 0,
    session_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    cost_log.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"{_iso_now()} {task_id} {sig_type} "
        f"cost=${cost_usd:.4f} turns={turns} tokens={tokens} "
        f"session={session_id or 'none'} error={error or 'None'}\n"
    )
    with cost_log.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def move_to_processed(path: Path, processed: Path) -> Path:
    processed.mkdir(parents=True, exist_ok=True)
    dest = processed / path.name
    counter = 1
    while dest.exists():
        dest = processed / f"{path.stem}-{counter}{path.suffix}"
        counter += 1
    path.rename(dest)
    return dest


def write_outbox_signal(
    outbox: Path,
    *,
    task: str,
    sig_type: str,
    body: str,
    extra_frontmatter: Optional[dict] = None,
) -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    ts_utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"{ts_utc}-{sig_type}-{task}.md"
    front_lines = [
        "---",
        "from: claudecode",
        "to: cowork",
        f"task: {task}",
        f"type: {sig_type}",
        f"ts: {_iso_now()}",
    ]
    if extra_frontmatter:
        for key, value in extra_frontmatter.items():
            front_lines.append(f"{key}: {value}")
    front_lines.append("---")
    front_lines.append("")
    front_lines.append(body.rstrip())
    front_lines.append("")
    path = outbox / name
    path.write_text("\n".join(front_lines), encoding="utf-8")
    return path


def append_daemon_log(daemon_log: Path, line: str) -> None:
    daemon_log.parent.mkdir(parents=True, exist_ok=True)
    with daemon_log.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def dispatch_macos_notification(body: str, task_id: Optional[str], *, paths: DaemonPaths) -> bool:
    """Fire `osascript display notification`. Best-effort; never raises."""
    title = "Cowork"
    subtitle = task_id or ""
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    safe_sub = subtitle.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_body}" with title "{title}" subtitle "{safe_sub}"'
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            _log_notification(paths, f"osascript exit={result.returncode}: {result.stderr.strip()}")
            return False
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        _log_notification(paths, f"osascript exception: {type(exc).__name__}: {exc}")
        return False


def dispatch_telegram(body: str, task_id: Optional[str], *, paths: DaemonPaths) -> bool:
    """POST to the Telegram Bot API. No-op if `.viabe/secrets/telegram.env`
    is missing. Best-effort; never raises into caller."""
    if not paths.telegram_env.exists():
        return False
    token, chat_id = _read_telegram_env(paths.telegram_env)
    if not token or not chat_id:
        _log_notification(paths, "telegram_env present but missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    import urllib.parse
    import urllib.request

    text = f"[Cowork{(' ' + task_id) if task_id else ''}] {body}"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"), timeout=10
        ) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        _log_notification(paths, f"telegram exception: {type(exc).__name__}: {exc}")
        return False
    success = '"ok":true' in payload
    _log_notification(paths, f"telegram {'sent' if success else 'failed'}: {payload[:200]}")
    return success


def _read_telegram_env(env_path: Path) -> tuple[Optional[str], Optional[str]]:
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values.get("TELEGRAM_BOT_TOKEN"), values.get("TELEGRAM_CHAT_ID")


def _log_notification(paths: DaemonPaths, line: str) -> None:
    try:
        paths.notifications_log.parent.mkdir(parents=True, exist_ok=True)
        with paths.notifications_log.open("a", encoding="utf-8") as fh:
            fh.write(f"[{_iso_now()}] {line}\n")
    except OSError:
        pass


async def process_signal(
    path: Path,
    session_id: Optional[str],
    options: Any,
    *,
    paths: DaemonPaths,
    query_fn: Optional[QueryFn] = None,
) -> Optional[str]:
    """Dispatch one signal to the agent loop.

    Returns the new session_id (or the prior one for short-circuits / errors).
    """
    front = parse_signal_frontmatter(path)
    sig_type = front.get("type", "unknown")
    task = front.get("task", "unknown")

    if sig_type == "notify":
        body = read_signal_body(path)
        priority = (front.get("priority") or "normal").lower()
        append_daemon_log(paths.daemon_log, f"[{_iso_now()}] notify {task} priority={priority}: {body}")
        if priority == "high":
            dispatch_macos_notification(body, task, paths=paths)
            dispatch_telegram(body, task, paths=paths)
        record_cost(paths.cost_log, task, "notify", cost_usd=0.0, tokens=0, session_id=session_id)
        move_to_processed(path, paths.processed)
        return session_id

    if sig_type not in LLM_REQUIRED_TYPES:
        append_daemon_log(
            paths.daemon_log,
            f"[{_iso_now()}] unknown-type {task} type={sig_type}; archiving without LLM dispatch.",
        )
        record_cost(paths.cost_log, task, sig_type, cost_usd=0.0, tokens=0, session_id=session_id)
        move_to_processed(path, paths.processed)
        return session_id

    over_budget_reason = _check_task_budget(paths, task)
    if over_budget_reason is not None:
        body = (
            f"Budget gate triggered while dispatching `{sig_type}` for `{task}`.\n\n"
            f"{over_budget_reason}\n\n"
            "Daemon left the signal in `.running/to-claudecode/` for re-trigger after Cowork either "
            "raises `budget_tokens` in the brief or splits the task."
        )
        write_outbox_signal(paths.outbox, task=task, sig_type="blocked", body=body)
        append_daemon_log(
            paths.daemon_log,
            f"[{_iso_now()}] BUDGET-BLOCK task={task} sig={sig_type} ({over_budget_reason})",
        )
        return session_id

    is_task_signal = sig_type == "task"
    if is_task_signal:
        path = move_to_processed(path, paths.processed)

    hooks._active_signal_context = {
        "type": sig_type,
        "task": task,
        "authorized_by": front.get("authorized_by"),
        "signal_path": str(path),
    }
    hooks._active_task_log_path = paths.queue / task / "task_log.md"
    hooks._active_status_path = paths.queue / task / "status"
    hooks._active_task_id = task
    hooks._daemon_log_path = paths.daemon_log
    hooks._transcripts_dir = paths.transcripts
    hooks._active_session_jsonl = _resolve_session_jsonl(paths.repo, session_id)

    prompt = _render_prompt(path, sig_type, task, paths)
    result_message = None
    last_error: Optional[BaseException] = None
    new_session = session_id

    if query_fn is None:
        from claude_agent_sdk import query as sdk_query  # type: ignore[import-not-found]

        query_fn = sdk_query

    attempts = 1 + len(_BACKOFF_SLEEPS)
    for attempt in range(attempts):
        try:
            async for message in query_fn(prompt=prompt, options=options):  # type: ignore[misc]
                if _is_result_message(message):
                    result_message = message
            last_error = None
            break
        except BaseException as exc:  # noqa: BLE001 — surface any SDK failure to backoff
            last_error = exc
            if attempt < len(_BACKOFF_SLEEPS):
                await asyncio.sleep(_BACKOFF_SLEEPS[attempt])
            continue
        finally:
            hooks._active_signal_context = None

    hooks._active_task_log_path = None
    hooks._active_status_path = None
    hooks._active_task_id = None
    hooks._transcripts_dir = None
    hooks._active_session_jsonl = None
    hooks._daemon_log_path = None

    if last_error is not None:
        exc_class = type(last_error).__name__
        body = (
            f"`claude_agent_sdk.query()` failed during dispatch of `{sig_type}` for `{task}`.\n\n"
            f"Exception class: `{exc_class}`\n"
            f"Message: {last_error!r}\n\n"
            "Daemon attempted 1 + 2 retries (sleep 5 s, sleep 30 s) before giving up. "
            "Signal left in `.running/to-claudecode/` so Cowork can re-trigger after the underlying "
            "issue is fixed (auth, rate-limit, network)."
        )
        write_outbox_signal(paths.outbox, task=task, sig_type="blocked", body=body)
        record_cost(
            paths.cost_log,
            task,
            sig_type,
            cost_usd=0.0,
            tokens=0,
            session_id=session_id,
            error=exc_class,
        )
        return session_id

    cost_usd = float(getattr(result_message, "total_cost_usd", 0.0) or 0.0)
    usage = getattr(result_message, "usage", None) or {}
    tokens = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
    turns = int(getattr(result_message, "num_turns", 0) or 0)
    new_session = getattr(result_message, "session_id", None) or session_id
    if new_session and new_session != session_id:
        save_session_id(paths.session_state, new_session)

    record_cost(
        paths.cost_log,
        task,
        sig_type,
        cost_usd=cost_usd,
        tokens=tokens,
        turns=turns,
        session_id=new_session,
    )

    if not is_task_signal and path.exists():
        move_to_processed(path, paths.processed)

    return new_session


def _check_task_budget(paths: DaemonPaths, task: str) -> Optional[str]:
    brief_path = paths.queue / task / "brief.md"
    if not brief_path.exists():
        return None
    budget_tokens, _ = read_brief_budget(brief_path)
    if not budget_tokens:
        return None
    spend = task_token_spend(paths.cost_log, task)
    if spend >= int(budget_tokens * 0.8):
        return (
            f"Token spend `{spend}` ≥ 80% of brief budget `{budget_tokens}`. "
            "Dispatch refused until budget is raised or task is split."
        )
    return None


def _render_prompt(path: Path, sig_type: str, task: str, paths: DaemonPaths) -> str:
    return (
        "You are Claude Code, operating inside the agent-loop daemon for the Viabe Team protocol.\n\n"
        f"Read /Users/fazalkhan/development/viabe-team/.viabe/protocol.md first.\n\n"
        f"Then process this Cowork signal at: {path}\n"
        f"Signal type: {sig_type}\n"
        f"Task: {task}\n\n"
        "Dispatch by type per protocol. After acting, MOVE the signal to "
        f"{paths.processed} unless the protocol says otherwise. Append every decision to "
        f"{paths.queue}/{task}/task_log.md. Surface clarifications via "
        f"{paths.outbox} only — never to Fazal directly.\n"
    )


def _is_result_message(message: Any) -> bool:
    return type(message).__name__ == "ResultMessage" or hasattr(message, "session_id")


def _resolve_session_jsonl(repo: Path, session_id: Optional[str]) -> Optional[Path]:
    if not session_id:
        return None
    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    candidate = Path.home() / ".claude/projects" / encoded / f"{session_id}.jsonl"
    if candidate.exists():
        return candidate
    projects_root = Path.home() / ".claude/projects"
    if not projects_root.exists():
        return None
    prefix = encoded.split("-")[0]
    for proj in projects_root.iterdir():
        if not proj.is_dir() or not proj.name.startswith(prefix):
            continue
        candidate = proj / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


async def dispatch_queued_task(
    task_id: str,
    session_id: Optional[str],
    options: Any,
    *,
    paths: DaemonPaths,
    query_fn: Optional[QueryFn] = None,
) -> Optional[str]:
    """Synthesize a brief-ready signal for a queued task and route it through
    `process_signal` so hooks fire and accounting is uniform."""
    ts_utc = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    sig_path = paths.inbox / f"{ts_utc}-brief-ready-{task_id}.md"
    body = (
        f"Synthetic brief-ready for `{task_id}` (status: queued).\n"
        f"See `.viabe/queue/{task_id}/brief.md` for the brief.\n"
    )
    front = (
        "---\n"
        "from: daemon\n"
        "to: claudecode\n"
        f"task: {task_id}\n"
        "type: brief-ready\n"
        f"ts: {_iso_now()}\n"
        "---\n\n"
    )
    sig_path.parent.mkdir(parents=True, exist_ok=True)
    sig_path.write_text(front + body, encoding="utf-8")
    return await process_signal(
        sig_path,
        session_id,
        options,
        paths=paths,
        query_fn=query_fn,
    )


def detect_pr_merges(state: dict[str, str], paths: DaemonPaths) -> list[tuple[str, str]]:
    """For every in-pr task, ask `gh pr view <N>` whether it merged.

    Returns [(task_id, merge_sha), ...] for newly-merged PRs. Tasks whose
    pr.md cannot be parsed or whose `gh` call fails are skipped silently
    (re-checked next iteration)."""
    merged: list[tuple[str, str]] = []
    for task, status in state.items():
        if status not in MERGE_DETECT_TYPES:
            continue
        pr_md = paths.queue / task / "pr.md"
        pr_number = _parse_pr_number(pr_md)
        if pr_number is None:
            continue
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--json",
                    "mergedAt,mergeCommit,state",
                    "--jq",
                    "{state: .state, merged: .mergedAt, sha: .mergeCommit.oid}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
        if proc.returncode != 0:
            continue
        try:
            payload = json.loads(proc.stdout.strip() or "{}")
        except json.JSONDecodeError:
            continue
        if payload.get("merged") and payload.get("sha"):
            merged.append((task, str(payload["sha"])))
    return merged


def _parse_pr_number(pr_md: Path) -> Optional[int]:
    if not pr_md.exists():
        return None
    try:
        text = pr_md.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"pull/(\d+)", text) or re.search(r"pr_url:\s*\S*?/pull/(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def apply_merge_cleanup(task_id: str, merge_sha: str, *, paths: DaemonPaths) -> None:
    """Flip status in-pr → done, move dir to done/, unblock dependents.

    Mirrors what we did manually for VT-OIV at PR #53 merge time.
    """
    task_dir = paths.queue / task_id
    if not task_dir.exists():
        return
    atomic_write(task_dir / "status", "done\n")
    log = task_dir / "task_log.md"
    if log.exists():
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"[{_iso_now()}] MERGE detected via gh pr view; sha={merge_sha}; status→done.\n")
    dest = paths.queue / "done" / task_id
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    task_dir.rename(dest)
    _unblock_dependents(task_id, paths)


def _unblock_dependents(merged_task: str, paths: DaemonPaths) -> None:
    for child in sorted(paths.queue.iterdir()):
        if not child.is_dir() or child.name == "done":
            continue
        status_path = child / "status"
        brief_path = child / "brief.md"
        if not status_path.exists() or not brief_path.exists():
            continue
        try:
            status = status_path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if status != "blocked":
            continue
        front = parse_signal_frontmatter(brief_path)
        depends = front.get("depends_on", "")
        if merged_task in depends:
            atomic_write(status_path, "queued\n")


def should_stop(stop_file: Path) -> bool:
    return stop_file.exists()


async def main_loop(
    paths: DaemonPaths,
    *,
    options_builder: Callable[[], Any],
    query_fn: Optional[QueryFn] = None,
    poll_seconds: int = POLL_SECONDS,
    iteration_callback: Optional[Callable[[int], Awaitable[None]]] = None,
) -> None:
    """Forever-loop until STOP file appears or SIGINT/SIGTERM."""
    session_id = load_session_id(paths.session_state)
    append_daemon_log(
        paths.daemon_log,
        f"[{_iso_now()}] START agent-loop; session={session_id or 'fresh'}; poll={poll_seconds}s",
    )
    iteration = 0
    while not should_stop(paths.stop_file):
        iteration += 1
        try:
            state = read_queue_state(paths.queue)
            for task_id, sha in detect_pr_merges(state, paths):
                apply_merge_cleanup(task_id, sha, paths=paths)
                append_daemon_log(
                    paths.daemon_log,
                    f"[{_iso_now()}] AUTO-MERGE-DETECT task={task_id} sha={sha} → done + unblock deps",
                )
            state = read_queue_state(paths.queue)  # re-read after possible cleanup
            inbox = scan_inbox(paths.inbox)
            action = pick_next_action(state, inbox)
            if isinstance(action, ProcessSignal):
                session_id = await process_signal(
                    action.path, session_id, options_builder(), paths=paths, query_fn=query_fn
                ) or session_id
            elif isinstance(action, StartTask):
                session_id = await dispatch_queued_task(
                    action.task_id, session_id, options_builder(), paths=paths, query_fn=query_fn
                ) or session_id
        except BaseException as exc:  # noqa: BLE001 — daemon never dies on inner errors
            append_daemon_log(
                paths.daemon_log,
                f"[{_iso_now()}] LOOP-ERROR {type(exc).__name__}: {exc!r}",
            )
        if iteration_callback is not None:
            await iteration_callback(iteration)
        await asyncio.sleep(poll_seconds)
    append_daemon_log(paths.daemon_log, f"[{_iso_now()}] STOP file present; exiting cleanly.")


__all__ = [
    "Action",
    "DaemonPaths",
    "POLL_SECONDS",
    "PER_CALL_BUDGET_USD",
    "LLM_REQUIRED_TYPES",
    "ProcessSignal",
    "StartTask",
    "apply_merge_cleanup",
    "default_paths",
    "detect_pr_merges",
    "dispatch_macos_notification",
    "dispatch_queued_task",
    "dispatch_telegram",
    "load_session_id",
    "main_loop",
    "parse_signal_frontmatter",
    "pick_next_action",
    "process_signal",
    "read_queue_state",
    "read_signal_body",
    "record_cost",
    "save_session_id",
    "scan_inbox",
    "should_stop",
    "task_token_spend",
    "write_outbox_signal",
]
