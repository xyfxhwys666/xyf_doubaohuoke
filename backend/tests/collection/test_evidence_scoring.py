from app.collection.evidence import (
    EvidenceGrade,
    choose_best_contact,
    grade_contact_evidence,
)
from app.collection.normalization import deduplicate_leads, normalize_lead


def test_authoritative_same_page_company_and_mobile_is_grade_a() -> None:
    result = grade_contact_evidence(
        "深圳市甲软装设计有限公司",
        "13800138000",
        [
            {
                "url": "https://jia-design.cn/contact",
                "quote": "深圳市甲软装设计有限公司业务咨询：13800138000",
                "stage": "legal_name",
                "authoritative": True,
            }
        ],
    )

    assert result.grade is EvidenceGrade.A
    assert result.auto_valid is True


def test_independent_forward_and_reverse_sources_are_grade_b() -> None:
    result = grade_contact_evidence(
        "深圳市乙空间设计有限公司",
        "13900139000",
        [
            {
                "url": "https://design-directory.example/yi",
                "quote": "乙空间是深圳市乙空间设计有限公司旗下品牌，联系电话13900139000",
                "stage": "alias",
            },
            {
                "url": "https://project-news.example/villa",
                "quote": "13900139000 深圳乙空间别墅全案设计",
                "stage": "reverse_mobile",
                "matched_alias": "深圳乙空间",
            },
        ],
        aliases=["深圳乙空间", "乙空间"],
    )

    assert result.grade is EvidenceGrade.B
    assert result.auto_valid is True
    assert result.independent_source_count == 2


def test_model_only_mobile_is_manual_review_grade_c() -> None:
    result = grade_contact_evidence(
        "深圳市丙设计有限公司",
        "13700137000",
        [{"url": "", "quote": "模型回答该号码", "stage": "model"}],
    )

    assert result.grade is EvidenceGrade.C
    assert result.auto_valid is False


def test_reverse_source_for_another_company_is_rejected() -> None:
    result = grade_contact_evidence(
        "深圳市丁设计有限公司",
        "13600136000",
        [
            {
                "url": "https://directory.example/ding",
                "quote": "深圳市丁设计有限公司联系电话13600136000",
                "stage": "legal_name",
            },
            {
                "url": "https://other.example/contact",
                "quote": "深圳市其他设计有限公司联系电话13600136000",
                "stage": "reverse_mobile",
                "conflicting_company": True,
            },
        ],
    )

    assert result.grade is EvidenceGrade.REJECTED
    assert result.auto_valid is False


def test_choose_best_contact_prefers_bidirectional_evidence() -> None:
    weak = grade_contact_evidence(
        "深圳市戊设计有限公司",
        "13500135000",
        [
            {
                "url": "https://wu-design.cn/contact",
                "quote": "深圳市戊设计有限公司13500135000",
                "stage": "legal_name",
            }
        ],
    )
    strong = grade_contact_evidence(
        "深圳市戊设计有限公司",
        "13400134000",
        [
            {
                "url": "https://directory.example/wu",
                "quote": "深圳市戊设计有限公司13400134000",
                "stage": "legal_name",
            },
            {
                "url": "https://news.example/wu-project",
                "quote": "13400134000 深圳市戊设计有限公司",
                "stage": "reverse_mobile",
            },
        ],
    )

    assert choose_best_contact([weak, strong]).mobile == "13400134000"


def test_deduplicate_same_company_keeps_stronger_mobile_evidence() -> None:
    leads = [
        {
            "city": "深圳",
            "company_name": "深圳市己软装设计有限公司",
            "mobile": "13300133000",
            "contact_evidence_score": 55,
            "contact_evidence_url": "https://weak.example/contact",
        },
        {
            "city": "深圳",
            "company_name": "深圳市己软装设计有限公司",
            "mobile": "13200132000",
            "contact_evidence_score": 88,
            "contact_evidence_url": "https://strong.example/contact",
            "contact_verification_status": "verified_cross_source",
        },
    ]

    result = deduplicate_leads(leads)

    assert len(result) == 1
    assert result[0]["mobile"] == "13200132000"
    assert result[0]["contact_evidence_url"] == "https://strong.example/contact"


def test_normalize_lead_preserves_evidence_grade_and_score() -> None:
    result = normalize_lead(
        {
            "company_name": "深圳市庚设计有限公司",
            "mobile": "13100131000",
            "contact_evidence_grade": "B",
            "contact_evidence_score": 90,
        },
        city="深圳",
        provider="doubao",
    )

    assert result["contact_evidence_grade"] == "B"
    assert result["contact_evidence_score"] == 90
