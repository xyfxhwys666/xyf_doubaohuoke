from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = "http://127.0.0.1:8010/api/v1"
ORIGINAL_TASK_ID = "6ad6b653-c6d7-4f88-852b-ce8d259c3151"
ERP_URL = "https://qingflow.com/api/qsource/eab3897c-4748-47b5-9121-96683b36cf0c"
STATE_PATH = ROOT / ".planning" / "overnight-goal-supervisor.json"
LOG_PATH = ROOT / ".planning" / "overnight-goal-supervisor.log"
PACING_PATH = ROOT / "data" / "doubao-web-pacing.json"
POLL_SECONDS = 30
RETRY_ERRORS_INTERVAL = 300
STALE_TASK_SECONDS = 20 * 60
TERMINAL_STATES = {"completed", "completed_with_errors", "terminated"}
LOW_FREQUENCY_ENV = {
    "DOUBAO_WEB_MIN_INTERVAL_SECONDS": "90",
    "DOUBAO_WEB_MAX_INTERVAL_SECONDS": "150",
    "DOUBAO_WEB_BATCH_MIN_SIZE": "5",
    "DOUBAO_WEB_BATCH_MAX_SIZE": "8",
    "DOUBAO_WEB_COOLDOWN_MIN_SECONDS": "3600",
    "DOUBAO_WEB_COOLDOWN_MAX_SECONDS": "5400",
}
NEXT_TASK_MARKER = "低频深圳100条续采"
NEXT_TASK_REQUEST = (
    f"{NEXT_TASK_MARKER}：持续查找深圳市内与别墅软装、全案设计业务相近的真实企业，"
    "包括高端住宅设计、私宅设计、大宅设计、豪宅设计、室内设计、空间设计、软装陈设、"
    "整装设计和全案落地公司。目标100条严格有效信息；每条必须是深圳实际经营且未在任何"
    "历史采集或已推送记录出现过的公司，至少有公开手机号或公开邮箱之一。联系人姓名和职务"
    "只有公开证据明确出现时填写，没有就留空；禁止编造任何公司、电话、邮箱、联系人或职务。"
    "使用已登录豆包聊天网页，单账号串行、低频查询。只有真实可见验证码才暂停等待人工；"
    "普通超时、连接异常、解析异常记录原因后自动重连、等待并继续。"
)


def log(message: str, **fields: object) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "message": message,
        **fields,
    }
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_state() -> dict:
    default = {
        "version": 1,
        "origin_task_id": ORIGINAL_TASK_ID,
        "active_task_id": ORIGINAL_TASK_ID,
        "cycle": 0,
        "phase": "monitoring",
        "low_frequency_restarted": False,
        "delivery_done_for": [],
        "last_snapshot": None,
        "last_retry_at": 0.0,
        "last_error": None,
    }
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    for key, fallback in default.items():
        value.setdefault(key, fallback)
    return value


def save_state(value: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def request_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout_seconds: float = 30,
) -> dict:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{API_ROOT}{path}", data=body, headers=headers, method=method)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def iso_age_seconds(value: object) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return max(0.0, time.time() - parsed.timestamp())
    except ValueError:
        return None


def pacing_blocks_prompt() -> bool:
    try:
        value = json.loads(PACING_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    now = time.time()
    return any(float(value.get(key) or 0) > now for key in ("next_prompt_at", "cooldown_until"))


def snapshot(task: dict, browser: dict) -> dict:
    return {
        "task_id": task.get("id"),
        "status": task.get("status"),
        "phase": task.get("collection_phase"),
        "round_phase": task.get("round_phase"),
        "candidate_count": task.get("candidate_count"),
        "enrichment": [task.get("enrichment_completed"), task.get("enrichment_total")],
        "raw_contacts": task.get("raw_contact_lead_count"),
        "verified_contacts": task.get("contact_lead_count"),
        "unit_errors": task.get("unit_errors"),
        "web_state": browser.get("state"),
        "web_detail": browser.get("detail"),
        "current_company": browser.get("current_company") or task.get("web_chat_current_company"),
    }


def set_low_frequency_env() -> None:
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    positions = {line.split("=", 1)[0]: index for index, line in enumerate(lines) if "=" in line}
    for key, value in LOW_FREQUENCY_ENV.items():
        line = f"{key}={value}"
        if key in positions:
            lines[positions[key]] = line
        else:
            lines.append(line)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)


def restart_backend() -> None:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    script = ROOT / ".planning" / "restart_backend_only.py"
    subprocess.run([str(python), str(script)], cwd=ROOT, check=True, timeout=90)


def deliver_all(task_id: str) -> tuple[bool, dict[str, int]]:
    totals = {"selected": 0, "sent": 0, "failed": 0, "attempts": 0}
    stagnant = 0
    for _ in range(12):
        result = request_json(
            f"/collection-tasks/{task_id}/deliver",
            method="POST",
            payload={"target_url": ERP_URL, "batch_size": 100},
            timeout_seconds=900,
        )
        selected = int(result.get("selected") or 0)
        sent = int(result.get("sent") or 0)
        failed = int(result.get("failed") or 0)
        totals["selected"] += selected
        totals["sent"] += sent
        totals["failed"] += failed
        totals["attempts"] += 1
        log("delivery batch", task_id=task_id, selected=selected, sent=sent, failed=failed)
        if selected == 0:
            return True, totals
        if sent == 0:
            stagnant += 1
            if stagnant >= 2:
                break
        else:
            stagnant = 0
        time.sleep(2)
    return False, totals


def find_existing_next_task() -> dict | None:
    items = request_json("/collection-tasks").get("items", [])
    for item in items:
        text = f"{item.get('name', '')} {item.get('request_text', '')}"
        if NEXT_TASK_MARKER in text and item.get("status") not in TERMINAL_STATES:
            return item
    return None


def create_or_start_next_task() -> str:
    existing = find_existing_next_task()
    if existing is not None:
        task_id = str(existing["id"])
        if existing.get("status") in {"draft", "queued"}:
            request_json(f"/collection-tasks/{task_id}/start", method="POST")
        elif existing.get("status") == "paused":
            request_json(f"/collection-tasks/{task_id}/resume", method="POST")
        return task_id
    created = request_json(
        "/collection-tasks/from-natural-language",
        method="POST",
        payload={
            "request_text": NEXT_TASK_REQUEST,
            "models": ["doubao"],
            "auto_deliver": False,
            "continuous_mode": True,
            "verified_auto_deliver": False,
            "use_doubao_web_chat": True,
            "web_chat_discovery": True,
        },
    )
    task_id = str(created["id"])
    request_json(f"/collection-tasks/{task_id}/start", method="POST")
    log("created next low-frequency Shenzhen task", task_id=task_id)
    return task_id


def recover_task(task: dict, browser: dict, state: dict) -> bool:
    task_id = str(task["id"])
    if task.get("status") == "waiting_for_login" and browser.get("state") in {"ready", "working"}:
        request_json(f"/collection-tasks/{task_id}/resume", method="POST")
        log("resumed task after browser became ready", task_id=task_id)
        return True
    if task.get("status") == "completed_with_errors" and task.get("continuous_mode"):
        target = int(task.get("valid_lead_target") or 100)
        valid = int(task.get("contact_lead_count") or 0)
        if (
            valid < target
            and time.time() - float(state.get("last_retry_at") or 0) >= RETRY_ERRORS_INTERVAL
        ):
            request_json(f"/collection-tasks/{task_id}/retry-errors", method="POST")
            state["last_retry_at"] = time.time()
            log("requeued task errors", task_id=task_id, valid=valid, target=target)
            return True
    return False


def watchdog(task: dict, browser: dict) -> None:
    if task.get("status") not in {"running", "queued"} or pacing_blocks_prompt():
        return
    age = iso_age_seconds(task.get("updated_at"))
    if age is None or age < STALE_TASK_SECONDS:
        return
    state = browser.get("state")
    if state in {"unavailable", "timeout", "stopped"}:
        request_json("/collection-tasks/doubao-web/reconnect", method="POST")
        log("reconnected stale Doubao browser", task_id=task.get("id"), age=round(age), state=state)
        return
    if state == "working":
        detail = str(browser.get("detail") or "")
        if detail.startswith("等待豆包网页查询间隔"):
            return
        restart_backend()
        log(
            "restarted stale backend worker",
            task_id=task.get("id"),
            age=round(age),
            state=state,
            detail=detail[:160],
        )
        return
    if state != "waiting_for_login":
        restart_backend()
        log("restarted stale backend worker", task_id=task.get("id"), age=round(age), state=state)


def main() -> None:
    state = load_state()
    log("supervisor started", active_task_id=state.get("active_task_id"))
    while True:
        try:
            task_id = str(state["active_task_id"])
            task = request_json(f"/collection-tasks/{task_id}")
            browser = request_json("/collection-tasks/doubao-web/status")
            current_snapshot = snapshot(task, browser)
            if current_snapshot != state.get("last_snapshot"):
                log("progress", **current_snapshot)
                state["last_snapshot"] = current_snapshot
            if recover_task(task, browser, state):
                save_state(state)
                time.sleep(POLL_SECONDS)
                continue
            watchdog(task, browser)

            status = str(task.get("status") or "")
            valid = int(task.get("contact_lead_count") or 0)
            target = int(task.get("valid_lead_target") or 100)
            terminal = status in TERMINAL_STATES
            if status == "terminated":
                log("supervisor stopped because task was terminated", task_id=task_id)
                state["phase"] = "terminated"
                save_state(state)
                return
            if terminal and task_id not in set(state.get("delivery_done_for") or []):
                delivered, totals = deliver_all(task_id)
                if delivered:
                    state.setdefault("delivery_done_for", []).append(task_id)
                    log("delivery completed", task_id=task_id, **totals)
                else:
                    log("delivery still has failures; will retry", task_id=task_id, **totals)
                save_state(state)
                time.sleep(POLL_SECONDS)
                continue
            if terminal and task_id in set(state.get("delivery_done_for") or []):
                set_low_frequency_env()
                if not state.get("low_frequency_restarted"):
                    restart_backend()
                    state["low_frequency_restarted"] = True
                    log("restarted backend with low-frequency Doubao settings")
                    save_state(state)
                    time.sleep(3)
                    continue
                next_id = create_or_start_next_task()
                state["active_task_id"] = next_id
                state["cycle"] = int(state.get("cycle") or 0) + 1
                state["phase"] = "monitoring_next"
                state["last_snapshot"] = None
                state["last_retry_at"] = 0.0
                save_state(state)
                continue
            if valid >= target and status == "running":
                log("target reached; waiting for task terminal state", task_id=task_id, valid=valid, target=target)
            state["last_error"] = None
            save_state(state)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            state["last_error"] = str(exc)
            save_state(state)
            log("supervisor iteration failed", error=str(exc))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
