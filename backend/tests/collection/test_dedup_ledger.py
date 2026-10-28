from app.collection.dedup_ledger import DedupLedger


def test_delivered_mobile_is_skipped_after_restart(tmp_path) -> None:
    path = tmp_path / "dedup-ledger.json"
    first = DedupLedger(path)
    decision = first.evaluate(
        city="深圳",
        company_name="深圳市甲设计有限公司",
        mobile="13800138000",
        evidence_score=85,
    )
    first.mark_delivered(decision)

    restarted = DedupLedger(path)
    duplicate = restarted.evaluate(
        city="深圳",
        company_name="深圳市其他设计有限公司",
        mobile="13800138000",
        evidence_score=95,
    )

    assert duplicate.action == "skip_duplicate_mobile"


def test_same_company_with_weaker_number_is_skipped(tmp_path) -> None:
    ledger = DedupLedger(tmp_path / "dedup-ledger.json")
    original = ledger.evaluate(
        city="深圳",
        company_name="深圳市乙设计有限公司",
        mobile="13900139000",
        evidence_score=90,
    )
    ledger.mark_delivered(original)

    weaker = ledger.evaluate(
        city="深圳",
        company_name="深圳市乙设计有限公司",
        mobile="13700137000",
        evidence_score=70,
    )

    assert weaker.action == "skip_duplicate_company"
    assert weaker.customer_guid == original.customer_guid


def test_same_company_with_stronger_number_updates_original_guid(tmp_path) -> None:
    ledger = DedupLedger(tmp_path / "dedup-ledger.json")
    original = ledger.evaluate(
        city="深圳",
        company_name="深圳市丙设计有限公司",
        mobile="13600136000",
        evidence_score=80,
    )
    ledger.mark_delivered(original)

    stronger = ledger.evaluate(
        city="深圳",
        company_name="深圳市丙设计有限公司",
        mobile="13500135000",
        evidence_score=95,
    )

    assert stronger.action == "update"
    assert stronger.customer_guid == original.customer_guid


def test_failed_delivery_does_not_claim_mobile(tmp_path) -> None:
    ledger = DedupLedger(tmp_path / "dedup-ledger.json")
    failed = ledger.evaluate(
        city="深圳",
        company_name="深圳市丁设计有限公司",
        mobile="13400134000",
        evidence_score=88,
    )

    retry = DedupLedger(tmp_path / "dedup-ledger.json").evaluate(
        city="深圳",
        company_name="深圳市丁设计有限公司",
        mobile="13400134000",
        evidence_score=88,
    )

    assert failed.action == "create"
    assert retry.action == "create"
    assert retry.customer_guid == failed.customer_guid


def test_delivered_email_is_skipped_after_restart(tmp_path) -> None:
    path = tmp_path / "dedup-ledger.json"
    first = DedupLedger(path)
    decision = first.evaluate(
        city="深圳",
        company_name="深圳市邮箱设计有限公司",
        email="sales@example.com",
        evidence_score=85,
    )
    first.mark_delivered(decision)

    restarted = DedupLedger(path)
    duplicate = restarted.evaluate(
        city="广州",
        company_name="广州其他设计有限公司",
        email="SALES@example.com",
        evidence_score=95,
    )

    assert duplicate.action == "skip_duplicate_email"


def test_delivered_company_can_be_excluded_before_collection(tmp_path) -> None:
    ledger = DedupLedger(tmp_path / "dedup-ledger.json")
    decision = ledger.evaluate(
        city="深圳市",
        company_name="深圳市已推送设计有限公司",
        mobile="13800138001",
        evidence_score=90,
    )
    ledger.mark_delivered(decision)

    assert ledger.is_company_delivered("深圳市", "深圳市已推送设计有限公司") is True
    assert ledger.is_company_delivered("深圳市", "深圳市新设计有限公司") is False
