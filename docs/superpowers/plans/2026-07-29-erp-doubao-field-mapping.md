# ERP Doubao Field Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every newly generated ERP lead payload identify the source as `豆包` and the customer type as `B端客资` while preserving the documented JSON structure.

**Architecture:** Keep the mapping in the existing `build_erp_payload` boundary so manual delivery, automatic delivery, fingerprinting, and retry all share one payload contract. Extend the existing payload unit test before changing production code.

**Tech Stack:** Python 3, FastAPI backend, pytest, httpx mock transport

## Global Constraints

- `sourceAccountName` must equal `豆包`.
- `customerSourceChannel` must equal `豆包`.
- `customerType` must equal `B端客资`.
- Preserve the ERP document's English field names and nested `contactInfo` object.
- Do not call the real ERP endpoint during tests.

---

### Task 1: ERP payload Chinese source mapping

**Files:**
- Modify: `backend/tests/collection/test_erp_delivery.py`
- Modify: `backend/app/collection/erp_delivery.py`

**Interfaces:**
- Consumes: `build_erp_payload(task: dict[str, Any], lead: dict[str, Any]) -> dict[str, Any]`
- Produces: the same function signature with the confirmed ERP field values

- [ ] **Step 1: Write the failing test**

Add this assertion to `test_build_erp_payload_uses_stable_guid_and_nested_contacts`:

```python
assert first["sourceAccountName"] == "豆包"
```

Keep the existing assertions:

```python
assert first["customerSourceChannel"] == "豆包"
assert first["customerType"] == "B端客资"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```text
C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_erp_delivery.py::test_build_erp_payload_uses_stable_guid_and_nested_contacts -q
```

Expected: FAIL because `sourceAccountName` is currently `AI客户采集工具`.

- [ ] **Step 3: Implement the minimal mapping change**

In `build_erp_payload`, change only the source account value:

```python
"sourceAccountName": "豆包",
```

- [ ] **Step 4: Run the ERP delivery test file**

Run:

```text
C:\ProgramData\miniforge3\Scripts\conda.exe run -n doubao-leads --no-capture-output python -m pytest backend/tests/collection/test_erp_delivery.py -q
```

Expected: all tests pass; tests use `httpx.MockTransport` and do not contact the real ERP.

- [ ] **Step 5: Inspect one generated payload and validate the documented contract**

Call `build_erp_payload(TASK, LEAD)` locally and assert the keys include:

```python
{
    "sourceAccountName",
    "customerAccountGuid",
    "customerSourceChannel",
    "customerCapturedAt",
    "customerAccountName",
    "customerProfileBio",
    "customerIpRegion",
    "customerType",
    "cEndCustomerRequirement",
    "sourceMethod",
    "contactInfo",
}
```

Expected values: `豆包`, `豆包`, and `B端客资` for the three confirmed fields.

- [ ] **Step 6: Run formatting checks**

Run:

```text
git diff --check
```

Expected: exit code 0.
