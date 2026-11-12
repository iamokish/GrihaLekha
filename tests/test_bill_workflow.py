from datetime import date, timedelta

import pytest

from grihalekha.ledger import Ledger


@pytest.fixture
def account(tmp_path):
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "1", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
    yield ledger, tenant
    ledger.close()


def test_monthly_electricity_is_unknown_until_review_and_never_duplicated(account):
    ledger, tenant = account
    assert ledger.prepare_monthly_drafts("2024-01-15") == 2
    assert ledger.prepare_monthly_drafts("2024-01-31") == 0
    electricity = next(row for row in ledger.list_charges(tenant) if row["kind"] == "electricity")
    assert electricity["amount_paise"] is None and electricity["needs_amount"]
    assert ledger.balance(tenant)["outstanding_paise"] == 0
    with pytest.raises(ValueError, match="amount"):
        ledger.post_charge(electricity["id"])
    ledger.post_charge(electricity["id"], 0)
    assert ledger.prepare_monthly_drafts("2024-03-01") == 4
    assert ledger.prepare_monthly_drafts("2024-03-01") == 0
    assert len(ledger.list_charges(tenant)) == 6


def test_existing_manual_electricity_is_not_billed_twice(account):
    ledger, tenant = account
    original = ledger.add_charge(tenant, "electricity", 12_300, "2024-01-01", "2024-01-31", "2024-01-31")
    assert ledger.prepare_monthly_drafts("2024-01-15") == 1
    assert [row["id"] for row in ledger.list_charges(tenant) if row["kind"] == "electricity"] == [original]


def test_legacy_electricity_with_different_service_dates_counts_for_its_cycle(account):
    ledger, tenant = account
    original = ledger.add_charge(tenant, "electricity", 12_300, "2024-01-01", "2024-01-15", "2024-01-15")
    assert ledger.prepare_monthly_drafts("2024-01-20") == 1
    assert [row["id"] for row in ledger.list_charges(tenant) if row["kind"] == "electricity"] == [original]


def test_paid_and_unpaid_keep_effective_date_history(account):
    ledger, tenant = account
    bill = ledger.add_charge(tenant, "rent", 100_000, "2024-01-01", "2024-01-31", "2024-01-31")
    payment = ledger.pay_bill(bill, 100_000, "2024-01-10", "cash")
    assert ledger.get_bill(bill)["outstanding_paise"] == 0
    with pytest.raises(ValueError, match="Partial payments"):
        ledger.pay_bill(bill, 30_000, "2024-01-10", "cash")
    assert len(ledger.list_payments(tenant)) == 1
    ledger.mark_bill_unpaid(bill, "Receipt entered by mistake", "2024-01-20")
    assert ledger.get_bill(bill)["outstanding_paise"] == 100_000
    assert ledger.get_bill(bill, "2024-01-15")["paid_paise"] == 100_000
    assert ledger.list_payments(tenant)[0]["id"] == payment
    assert ledger.list_payments(tenant)[0]["reversed_on"] is None
    assert ledger.balance(tenant)["credit_paise"] == 100_000
    ledger.mark_bill_paid(bill, "2024-01-21")
    assert ledger.get_bill(bill)["outstanding_paise"] == 0


def test_mark_unpaid_can_keep_received_money_as_advance(account):
    ledger, tenant = account
    bill = ledger.add_charge(tenant, "rent", 100_000, "2024-01-01", "2024-01-31", "2024-01-31")
    ledger.pay_bill(bill, 100_000, "2024-01-10", "upi")
    ledger.mark_bill_unpaid(bill, "Use this receipt later", "2024-01-20", keep_credit=True)
    assert ledger.get_bill(bill)["outstanding_paise"] == 100_000
    balance = ledger.balance(tenant)
    assert balance["credit_paise"] == 100_000 and balance["net_paise"] == 0
    assert ledger.get_bill(bill, "2024-01-19")["outstanding_paise"] == 0
    assert not ledger.list_payments(tenant)[0]["reversed_on"]


def test_legacy_combined_receipts_cannot_accidentally_unpay_other_bills(account):
    ledger, tenant = account
    rent = ledger.add_charge(tenant, "rent", 100_000, "2024-01-01", "2024-01-31", "2024-01-31")
    power = ledger.add_charge(tenant, "electricity", 10_000, "2024-01-01", "2024-01-31", "2024-01-31")
    ledger.record_payment(tenant, 110_000, "2024-02-01", "cash", allocations={rent: 100_000, power: 10_000})
    ledger.mark_bill_unpaid(rent, "Wrong allocation", "2024-02-02")
    assert ledger.get_bill(power)["outstanding_paise"] == 0
    assert ledger.get_bill(rent)["outstanding_paise"] == 100_000
    assert ledger.balance(tenant)["credit_paise"] == 100_000


def test_future_payment_and_payment_to_unreviewed_bill_are_rejected(account):
    ledger, tenant = account
    ledger.prepare_monthly_drafts("2024-01-01")
    bill = ledger.list_charges(tenant)[0]
    with pytest.raises(ValueError, match="Review"):
        ledger.pay_bill(bill["id"], 100, "2024-01-02", "cash")
    ledger.post_charge(bill["id"], 100)
    with pytest.raises(ValueError, match="future"):
        ledger.pay_bill(bill["id"], 100, (date.today() + timedelta(days=1)).isoformat(), "cash")


def test_electricity_drafts_follow_shorter_and_extended_departure(account):
    ledger, tenant = account
    ledger.prepare_monthly_drafts("2024-03-01")
    ledger.move_out(tenant, "2024-02-15", 50_000, reason="Leave earlier")
    power = {row["electricity_cycle_start"]: row for row in ledger.list_charges(tenant) if row["kind"] == "electricity"}
    assert power["2024-02-01"]["period_end"] == "2024-02-15"
    assert power["2024-03-01"]["status"] == "void"
    ledger.move_out(tenant, "2024-03-20", 60_000, reason="Stay longer")
    power = {row["electricity_cycle_start"]: row for row in ledger.list_charges(tenant) if row["kind"] == "electricity"}
    assert power["2024-02-01"]["period_end"] == "2024-02-29"
    assert power["2024-03-01"]["period_end"] == "2024-03-20"
    assert power["2024-03-01"]["status"] == "draft"
    assert power["2024-03-01"]["amount_paise"] is None


def test_posted_electricity_is_preserved_when_departure_changes(account):
    ledger, tenant = account
    ledger.prepare_monthly_drafts("2024-02-01")
    bill = next(row for row in ledger.list_charges(tenant) if row["kind"] == "electricity" and row["period_start"] == "2024-02-01")
    ledger.post_charge(bill["id"], 15_000)
    ledger.move_out(tenant, "2024-02-15", 50_000, reason="Leave earlier")
    after = ledger.get_bill(bill["id"])
    assert after["period_end"] == "2024-02-29" and after["amount_paise"] == 15_000


def test_archived_accounts_reject_financial_mutations_and_new_drafts(account):
    ledger, tenant = account
    ledger.move_out(tenant, "2024-01-01", 0)
    for bill in ledger.list_charges(tenant):
        if bill["status"] == "draft":
            ledger.post_charge(bill["id"], 0)
    ledger.archive_tenancy(tenant)
    bill = ledger.list_charges(tenant)[0]
    revision = ledger.get_revision()
    operations = [
        lambda: ledger.record_payment(tenant, 100, "2024-01-01", "cash"),
        lambda: ledger.record_deposit(tenant, 100, "2024-01-01", "receive"),
        lambda: ledger.apply_credit(tenant),
        lambda: ledger.void_charge(bill["id"], "Correction"),
        lambda: ledger.post_charge(bill["id"], 0),
        lambda: ledger.add_charge(tenant, "electricity", 100, "2024-01-01", "2024-01-01", "2024-01-01"),
        lambda: ledger.pay_bill(bill["id"], 1, "2024-01-01", "cash"),
    ]
    for operation in operations:
        with pytest.raises(ValueError, match="archived"):
            operation()
    assert ledger.prepare_monthly_drafts() == 0
    assert ledger.prepare_rent_drafts() == 0
    assert ledger.get_revision() == revision
