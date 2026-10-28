from __future__ import annotations

import json
import math
from collections.abc import Iterable
from tempfile import SpooledTemporaryFile
from typing import Any

import httpx


def deliver_leads(
    task: dict[str, Any],
    *,
    leads: Iterable[dict[str, Any]] | None = None,
    target_url: str,
    bearer_token: str | None,
    batch_size: int,
    client: httpx.Client | None = None,
) -> dict[str, int | str]:
    source = task.get("leads", []) if leads is None else leads
    headers = {"Content-Type": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    http_client = client or httpx.Client(timeout=30, follow_redirects=True)
    owns_client = client is None
    sent = 0
    batches = 0
    try:
        with SpooledTemporaryFile(
            max_size=1024 * 1024,
            mode="w+",
            encoding="utf-8",
            newline="\n",
        ) as spool:
            for lead in source:
                if not lead.get("source_url") or not (lead.get("mobile") or lead.get("email") or lead.get("wechat")):
                    continue
                spool.write(json.dumps(lead, ensure_ascii=False) + "\n")
                sent += 1

            total_batches = math.ceil(sent / batch_size) if sent else 0
            spool.seek(0)
            batch: list[dict[str, Any]] = []
            for line in spool:
                batch.append(json.loads(line))
                if len(batch) < batch_size:
                    continue
                batches += 1
                response = http_client.post(
                    target_url,
                    headers=headers,
                    json={
                        "task_id": task["id"],
                        "task_name": task["name"],
                        "batch_index": batches,
                        "total_batches": total_batches,
                        "items": batch,
                    },
                )
                response.raise_for_status()
                batch = []
            if batch:
                batches += 1
                response = http_client.post(
                    target_url,
                    headers=headers,
                    json={
                        "task_id": task["id"],
                        "task_name": task["name"],
                        "batch_index": batches,
                        "total_batches": total_batches,
                        "items": batch,
                    },
                )
                response.raise_for_status()
    finally:
        if owns_client:
            http_client.close()
    return {"status": "completed", "sent": sent, "batches": batches}
