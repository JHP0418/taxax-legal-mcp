from __future__ import annotations

LAW_GROUPS = (
    {"law_id": "civil_act", "law_name": "민법", "articles": (("제162조", ("limitation",)), ("제163조", ("limitation",)), ("제164조", ("limitation",)), ("제165조", ("limitation",)), ("제166조", ("limitation",)), ("제449조", ("assignment",)), ("제450조", ("assignment",)), ("제451조", ("assignment",)), ("제476조", ("appropriation",)), ("제477조", ("appropriation",)), ("제478조", ("appropriation",)), ("제479조", ("appropriation",)), ("제492조", ("setoff",)), ("제493조", ("setoff",)), ("제496조", ("setoff",)), ("제498조", ("setoff",)), ("제499조", ("setoff",)))},
    {"law_id": "commercial_act", "law_name": "상법", "articles": (("제64조", ("limitation",)),)},
    {"law_id": "corporate_tax_act", "law_name": "법인세법", "articles": (("제19조의2", ("bad_debt",)), ("제40조", ("attribution_year",)), ("제112조", ("books_retention",)), ("제116조", ("evidence",)))},
    {"law_id": "corporate_tax_decree", "law_name": "법인세법 시행령", "articles": (("제19조의2", ("bad_debt", "attribution_year")), ("제68조", ("attribution_year",)), ("제69조", ("attribution_year",)), ("제71조", ("attribution_year",)), ("제76조", ("foreign_currency",)), ("제158조", ("evidence",)))},
    {"law_id": "vat_act", "law_name": "부가가치세법", "articles": (("제32조", ("tax_invoice",)), ("제34조", ("tax_invoice",)), ("제45조", ("vat_bad_debt",)))},
    {"law_id": "vat_decree", "law_name": "부가가치세법 시행령", "articles": (("제70조", ("corrected_tax_invoice",)), ("제87조", ("vat_bad_debt",)))},
    {"law_id": "national_tax_basic_act", "law_name": "국세기본법", "articles": (("제45조", ("amended_return",)), ("제45조의2", ("correction_claim",)), ("제85조의3", ("books_retention",)))},
    {"law_id": "income_tax_act", "law_name": "소득세법", "articles": (("제39조", ("sole_proprietor", "attribution_year")), ("제160조", ("sole_proprietor", "books_retention")))},
    {"law_id": "income_tax_decree", "law_name": "소득세법 시행령", "articles": (("제55조", ("sole_proprietor", "bad_debt")),)},
    {"law_id": "bills_act", "law_name": "어음법", "articles": (("제43조", ("bills",)), ("제44조", ("bills",)), ("제70조", ("bills", "limitation")))},
    {"law_id": "corporate_tax_rule", "law_name": "법인세법 시행규칙", "articles": ()},
    {"law_id": "vat_rule", "law_name": "부가가치세법 시행규칙", "articles": ()},
    {"law_id": "national_tax_basic_rule", "law_name": "국세기본법 시행규칙", "articles": ()},
)

FORM_SPECS = (
    {"source_id": "form_corporate_bad_debt_schedule", "law_id": "corporate_tax_rule", "title_marker": "대손충당금 및 대손금조정명세서", "issues": ("bad_debt",)},
    {"source_id": "form_vat_bad_debt_report", "law_id": "vat_rule", "title_marker": "대손세액 공제(변제)신고서", "issues": ("vat_bad_debt",)},
    {"source_id": "form_correction_claim", "law_id": "national_tax_basic_rule", "title_marker": "과세표준 및 세액의 결정(경정) 청구서", "issues": ("correction_claim",)},
)

NTS_SPECS = (
    {"source_id": "nts_receivable_due_date_two_years", "document_id": "010000000000455288", "issues": ("bad_debt",)},
    {"source_id": "nts_changed_receivable_due_date", "document_id": "010000000000455127", "issues": ("bad_debt",)},
    {"source_id": "nts_terminated_contract_deposit_vat_bad_debt", "document_id": "200000000000018515", "issues": ("vat_bad_debt",)},
    {"source_id": "nts_return_and_termination_corrected_invoice", "document_id": "010000000000147201", "issues": ("corrected_tax_invoice",)},
    {"source_id": "nts_contract_termination_supply_value_change", "document_id": "010000000000045480", "issues": ("corrected_tax_invoice",)},
    {"source_id": "nts_judgment_contract_termination", "document_id": "200000000000015459", "issues": ("corrected_tax_invoice",)},
)


def seed_profile() -> dict[str, object]:
    return {
        "law_groups": LAW_GROUPS,
        "form_specs": FORM_SPECS,
        "nts_specs": NTS_SPECS,
        "nts_status": "L2_NOT_STARTED",
    }
