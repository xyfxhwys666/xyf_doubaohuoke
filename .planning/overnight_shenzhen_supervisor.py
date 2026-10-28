from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_ROOT = "http://127.0.0.1:8010/api/v1"
CURRENT_TASK_ID = "941ae701-7f71-42af-8b86-a97a9048ed7b"
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / ".planning" / "overnight-shenzhen-supervisor.json"
LOG_PATH = ROOT / ".planning" / "overnight-shenzhen-supervisor.log"
POLL_SECONDS = 30
ERP_URL_KEY = "ERP_DELIVERY_URL"
MOBILE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)

NEW_REQUEST = (
    "持续查找深圳市内与别墅软装、全案设计业务相近的真实企业，包括高端住宅设计、私宅设计、"
    "大宅设计、豪宅设计、室内设计、空间设计、软装陈设、整装设计和全案落地公司。"
    "目标100条有效信息；每条必须是深圳实际经营且未在历史采集任务出现过的公司，必须有公开来源"
    "能对应到公司的中国大陆11位手机号或公开邮箱，手机号或邮箱任一即可；联系人姓名和职务只有"
    "公开证据明确出现时才填写，没有就留空，禁止编造。使用已登录的豆包聊天网页，单账号串行、"
    "低频查询；遇到安全验证立即暂停并等待人工处理，不自动重试验证。"
)


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "version": 1,
            "current_task_id": CURRENT_TASK_ID,
            "current_paused": False,
            "current_pushed": False,
            "new_task_id": None,
            "recovery_attempts": 0,
            "last_metrics": None,
        }


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STATE_PATH)


def request_json(path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{API_ROOT}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed localhost API
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"API unavailable {path}: {exc}") from exc
    return json.loads(raw) if raw else {}


def env_value(key: str) -> str | None:
    try:
        lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            value = line[len(prefix) :].strip().strip('"').strip("'")
            return value or None
    return None


def has_contact(lead: dict) -> bool:
    mobile = re.sub(r"\D", "", str(lead.get("mobile") or ""))
    if MOBILE_PATTERN.fullmatch(mobile):
        return True
    return bool(EMAIL_PATTERN.search(str(lead.get("email") or "")))


def metrics(task: dict) -> dict:
    return {
        "status": task.get("status"),
        "round_phase": task.get("round_phase"),
        "collection_phase": task.get("collection_phase"),
        "candidate_count": task.get("candidate_count"),
        "enrichment_completed": task.get("enrichment_completed"),
        "enrichment_total": task.get("enrichment_total"),
        "valid_count": task.get("contact_lead_count"),
        "round_number": task.get("round_number"),
        "updated_at": task.get("updated_at"),
    }


def browser_requires_attention(browser: dict) -> bool:
    return browser.get("state") in {"waiting_for_login", "challenge", "captcha"}


def wait_for_current_task(state: dict) -> bool:
    task_id = str(state.get("current_task_id") or CURRENT_TASK_ID)
    while True:
        task = request_json(f"/collection-tasks/{task_id}")
        browser = request_json("/collection-tasks/doubao-web/status")
        current = metrics(task)
        if current != state.get("last_metrics"):
            log(f"current task metrics={json.dumps(current, ensure_ascii=False)}")
            state["last_metrics"] = current
            save_state(state)
        if task.get("status") in {"waiting_for_login", "paused", "terminated", "completed", "completed_with_errors"}:
            return task.get("status") != "waiting_for_login"
        if browser_requires_attention(browser) or task.get("web_chat_status") == "waiting_for_login":
            log("paused: Doubao requires manual login or safety verification")
            return False
        total = int(task.get("enrichment_total") or 0)
        completed = int(task.get("enrichment_completed") or 0)
        phase = str(task.get("round_phase") or "")
        if total and completed >= total and phase in {"verifying", "delivering", "cooldown", "completed"}:
            if not state.get("current_paused"):
                log("requesting pause after current candidate pool finished")
                request_json(f"/collection-tasks/{task_id}/pause", method="POST")
            state["current_paused"] = True
            save_state(state)
        time.sleep(POLL_SECONDS)


def wait_until_paused(task_id: str) -> dict:
    for _ in range(60):
        task = request_json(f"/collection-tasks/{task_id}")
        if task.get("status") in {"paused", "completed", "completed_with_errors", "terminated"}:
            return task
        if task.get("status") == "waiting_for_login":
            raise RuntimeError("current task is waiting for manual Doubao verification")
        time.sleep(2)
    raise RuntimeError("current task did not acknowledge pause within 120 seconds")


def push_current_task(state: dict) -> None:
    if state.get("current_pushed"):
        return
    task_id = str(state.get("current_task_id") or CURRENT_TASK_ID)
    task = wait_until_paused(task_id)
    leads = request_json(f"/collection-tasks/{task_id}/leads?limit=1000&offset=0").get("items", [])
    eligible = sum(has_contact(lead) for lead in leads)
    log(f"current task ready for ERP: candidates={len(leads)} eligible_contact={eligible}")
    target_url = env_value(ERP_URL_KEY)
    if not target_url:
        raise RuntimeError("ERP_DELIVERY_URL is not configured")
    result = request_json(
        f"/collection-tasks/{task_id}/deliver",
        method="POST",
        payload={"target_url": target_url, "batch_size": 100},
    )
    log(f"ERP delivery result={json.dumps(result, ensure_ascii=False)}")
    final_delivery: dict = {}
    for attempt in range(6):
        delivery = request_json(f"/collection-tasks/{task_id}/delivery-status")
        final_delivery = delivery
        failed = int(delivery.get("failed") or 0)
        pending = int(delivery.get("pending") or 0)
        if not failed and not pending:
            break
        if attempt == 5:
            raise RuntimeError(
                "ERP delivery did not settle: "
                + json.dumps(final_delivery, ensure_ascii=False)
            )
        if failed:
            retry = request_json(f"/collection-tasks/{task_id}/retry-delivery", method="POST")
            log(f"ERP retry {attempt + 1} result={json.dumps(retry, ensure_ascii=False)}")
        time.sleep(60)
    state["current_pushed"] = True
    save_state(state)


def create_new_task(state: dict) -> str:
    existing_id = state.get("new_task_id")
    if existing_id:
        return str(existing_id)
    created = request_json(
        "/collection-tasks/from-natural-language",
        method="POST",
        payload={
            "request_text": NEW_REQUEST,
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
    state["new_task_id"] = task_id
    state["new_request"] = NEW_REQUEST
    save_state(state)
    log(f"created and started new Shenzhen task={task_id}")
    return task_id


def monitor_new_task(state: dict) -> None:
    task_id = create_new_task(state)
    while True:
        task = request_json(f"/collection-tasks/{task_id}")
        browser = request_json("/collection-tasks/doubao-web/status")
        current = metrics(task)
        if current != state.get("new_metrics"):
            log(f"new task metrics={json.dumps(current, ensure_ascii=False)}")
            state["new_metrics"] = current
            save_state(state)
        if browser_requires_attention(browser) or task.get("web_chat_status") == "waiting_for_login":
            log("stopped: new task needs manual Doubao verification")
            return
        valid = int(task.get("contact_lead_count") or 0)
        if valid >= 100:
            log(f"new Shenzhen task reached valid target: {valid}")
            return
        if task.get("status") == "completed_with_errors":
            attempts = int(state.get("recovery_attempts") or 0)
            if attempts >= 3:
                log("stopped: non-captcha task errors exceeded three recovery attempts")
                return
            state["recovery_attempts"] = attempts + 1
            save_state(state)
            log(f"retrying non-captcha task errors attempt={attempts + 1}")
            request_json(f"/collection-tasks/{task_id}/retry-errors", method="POST")
        time.sleep(POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="print one status snapshot and exit")
    args = parser.parse_args()
    state = load_state()
    log("overnight Shenzhen supervisor started")
    if args.once:
        task = request_json(f"/collection-tasks/{state.get('current_task_id') or CURRENT_TASK_ID}")
        print(json.dumps(metrics(task), ensure_ascii=False, indent=2))
        return
    while not state.get("current_pushed"):
        try:
            if not wait_for_current_task(state):
                return
            push_current_task(state)
        except RuntimeError as exc:
            log(f"supervisor retryable error before new task: {exc}")
            time.sleep(300)
    monitor_new_task(state)


if __name__ == "__main__":
    main()
