from datetime import datetime, timedelta

import pytest

from grihalekha import clock
from grihalekha.clock import IST, today_ist
from grihalekha.backups import BackupManager
from grihalekha.ledger import Ledger
from legacy_ledger import cancel_historical_bill


@pytest.fixture
def account(tmp_path):
    ledger = Ledger(tmp_path / "account")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "1", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
    ledger.prepare_monthly_drafts("2024-01-01")
    bills = {row["kind"]: row["id"] for row in ledger.list_charges(tenant)}
    yield ledger, tenant, bills
    ledger.close()


def test_receipts_build_balance_and_full_bill_payment_consumes_it(account):
    ledger, tenant, bills = account
    ledger.deposit_payment(tenant, 40_000, "2024-01-02", "cash")
    ledger.deposit_payment(tenant, 80_000, "2024-01-03", "upi")
    ledger.post_charge(bills["rent"])
    assert ledger.get_bill(bills["rent"])["paid_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == 120_000
    assert ledger.mark_bill_paid(bills["rent"], "2024-01-03") == 100_000
    assert ledger.get_bill(bills["rent"])["outstanding_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == 20_000
    assert ledger.get_bill(bills["rent"], "2024-01-02")["paid_paise"] == 0
    assert len(ledger.list_payments(tenant)) == 2


def test_insufficient_balance_rolls_back_all_allocations(account):
    ledger, tenant, bills = account
    ledger.post_charge(bills["rent"])
    ledger.deposit_payment(tenant, 40_000, "2024-01-02", "cash")
    ledger.deposit_payment(tenant, 50_000, "2024-01-02", "cash")
    before = ledger.get_revision()
    with pytest.raises(ValueError, match="full bill"):
        ledger.mark_bill_paid(bills["rent"], "2024-01-02")
    assert ledger.get_bill(bills["rent"])["payments"] == []
    assert ledger.balance(tenant)["credit_paise"] == 90_000
    assert ledger.get_revision() == before


def test_unpaid_releases_all_sources_without_changing_receipts(account):
    ledger, tenant, bills = account
    ledger.post_charge(bills["rent"])
    ledger.post_charge(bills["electricity"], 20_000)
    ledger.deposit_payment(tenant, 40_000, "2024-01-02", "cash")
    ledger.deposit_payment(tenant, 80_000, "2024-01-02", "cash")
    ledger.mark_bill_paid(bills["rent"], "2024-01-02")
    ledger.mark_bill_paid(bills["electricity"], "2024-01-02")
    ledger.mark_bill_unpaid(bills["rent"], "Use balance later", "2024-01-03")
    assert ledger.get_bill(bills["rent"])["outstanding_paise"] == 100_000
    assert ledger.get_bill(bills["rent"], "2024-01-02")["outstanding_paise"] == 0
    assert ledger.get_bill(bills["electricity"])["outstanding_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == 100_000
    assert all(not row["reversed_on"] for row in ledger.list_payments(tenant))


def test_cycle_lock_blocks_direct_and_indirect_changes_but_allows_receipts(account):
    ledger, tenant, bills = account
    ledger.post_charge(bills["rent"])
    payment = ledger.deposit_payment(tenant, 100_000, "2024-01-02", "cash")
    ledger.mark_bill_paid(bills["rent"], "2024-01-02")
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    operations = [
        lambda: ledger.mark_bill_unpaid(bills["rent"], "Correction"),
        lambda: ledger.mark_bill_paid(bills["rent"]),
        lambda: ledger.reverse_payment(payment, "Correction"),
        lambda: ledger.void_charge(bills["rent"], "Correction"),
        lambda: ledger.post_charge(bills["electricity"], 0),
        lambda: ledger.add_charge(tenant, "misc", 100, "2024-01-01", "2024-01-31", "2024-01-31", "Fee"),
        lambda: ledger.move_out(tenant, "2024-01-15", 50_000),
    ]
    for operation in operations:
        with pytest.raises(ValueError, match="locked"):
            operation()
    ledger.deposit_payment(tenant, 1_000, "2024-01-03", "cash")
    path = ledger.data_dir
    ledger.close()
    reopened = Ledger(path)
    try:
        assert reopened.list_bill_cycles(tenant)[0]["locked"]
        assert reopened.get_bill(bills["rent"])["payment_locked"]
        reopened.set_cycle_payment_lock(tenant, "2024-01-01", False)
        reopened.mark_bill_unpaid(bills["rent"], "Use later")
        assert reopened.balance(tenant)["credit_paise"] == 101_000
    finally:
        reopened.close()


def test_all_new_payment_paths_reject_partial_bill_payments(account):
    ledger, tenant, bills = account
    ledger.post_charge(bills["rent"])
    ledger.deposit_payment(tenant, 100_000, "2024-01-02", "cash")
    before = ledger.get_revision()
    operations = [
        lambda: ledger.pay_bill(bills["rent"], 30_000, "2024-01-02", "cash"),
        lambda: ledger.record_payment(tenant, 30_000, "2024-01-02", "cash", allocations={bills["rent"]: 30_000}),
        lambda: ledger.apply_credit(tenant, {bills["rent"]: 30_000}, "2024-01-02"),
    ]
    for operation in operations:
        with pytest.raises(ValueError, match="Partial payments"):
            operation()
    assert ledger.get_revision() == before
    assert len(ledger.list_payments(tenant)) == 1
    assert ledger.get_bill(bills["rent"])["paid_paise"] == 0


def test_shared_receipt_correction_cannot_leave_a_partial_bill(account):
    ledger, tenant, bills = account
    ledger.post_charge(bills["rent"])
    first = ledger.deposit_payment(tenant, 30_000, "2024-01-02", "cash")
    ledger.deposit_payment(tenant, 70_000, "2024-01-02", "cash")
    ledger.mark_bill_paid(bills["rent"], "2024-01-02")
    with pytest.raises(ValueError, match="Mark that bill Unpaid"):
        ledger.reverse_payment(first, "Incorrect receipt", "2024-01-03")
    assert ledger.get_bill(bills["rent"])["outstanding_paise"] == 0
    ledger.mark_bill_unpaid(bills["rent"], "Correct receipt", "2024-01-03")
    ledger.reverse_payment(first, "Incorrect receipt", "2024-01-03")
    assert ledger.balance(tenant)["credit_paise"] == 70_000


def test_electricity_zero_requires_review_and_cycles_have_both_bills(account):
    ledger, tenant, bills = account
    with pytest.raises(ValueError, match="amount"):
        ledger.post_charge(bills["electricity"])
    ledger.post_charge(bills["electricity"], 0)
    assert ledger.mark_bill_paid(bills["electricity"]) == 0
    ledger.prepare_monthly_drafts("2024-03-01")
    cycles = ledger.list_bill_cycles(tenant)
    assert len(cycles) == 3
    assert cycles[0]["cycle_end"] == "2024-01-31"
    assert cycles[1]["cycle_end"] == "2024-02-29"
    for cycle in cycles:
        assert {row["kind"] for row in cycle["charges"]} == {"rent", "electricity"}
    assert cycles[0]["review_pending"] == 1


def test_payment_date_uses_only_balance_already_received(account):
    ledger, tenant, bills = account
    ledger.post_charge(bills["rent"])
    ledger.deposit_payment(tenant, 100_000, "2024-01-03", "cash")
    with pytest.raises(ValueError, match="available balance"):
        ledger.mark_bill_paid(bills["rent"], "2024-01-02")
    tomorrow = today_ist() + timedelta(days=1)
    with pytest.raises(ValueError, match="future"):
        ledger.deposit_payment(tenant, 100, tomorrow, "cash")
    with pytest.raises(ValueError, match="future"):
        ledger.mark_bill_paid(bills["rent"], tomorrow)


def test_deposit_transfer_adds_balance_without_auto_payment(account):
    ledger, tenant, bills = account
    ledger.post_charge(bills["rent"])
    ledger.record_deposit(tenant, 100_000, "2024-01-02", "receive")
    ledger.record_deposit(tenant, 100_000, "2024-01-03", "apply", notes="Transfer to balance")
    assert ledger.balance(tenant)["credit_paise"] == 100_000
    assert ledger.get_bill(bills["rent"])["paid_paise"] == 0


def test_legacy_cancelled_required_bills_return_once_for_review(account):
    ledger, tenant, bills = account
    for kind, bill in bills.items():
        ledger.post_charge(bill, 100_000 if kind == "rent" else 20_000)
        cancel_historical_bill(ledger, bill, "Correct the amount", "2024-02-01")
    assert ledger.prepare_monthly_drafts("2024-01-15") == 2
    assert ledger.prepare_monthly_drafts("2024-01-15") == 0
    active = [row for row in ledger.list_charges(tenant) if row["status"] != "void"]
    assert {row["kind"] for row in active} == {"rent", "electricity"}
    assert all(row["status"] == "draft" and row["charge_date"] == "2024-02-01" for row in active)
    assert {row["replaces_charge_id"] for row in active} == set(bills.values())
    assert ledger.balance(tenant, "2024-01-15")["billed_paise"] == 120_000


def test_legacy_cancelled_electricity_replacement_still_follows_occupancy(account):
    ledger, tenant, bills = account
    cancel_historical_bill(ledger, bills["electricity"], "Correction", "2024-01-02")
    ledger.prepare_monthly_drafts("2024-01-15")
    replacement = next(row for row in ledger.list_charges(tenant)
                       if row["kind"] == "electricity" and row["status"] == "draft")
    ledger.move_out(tenant, "2024-01-20", 50_000)
    assert ledger.get_bill(replacement["id"])["period_end"] == "2024-01-20"
    assert ledger.prepare_monthly_drafts("2024-01-31") == 0


def test_cycle_lock_survives_backup_restore(account, tmp_path):
    ledger, tenant, _ = account
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    manager = BackupManager(ledger.data_dir, ledger.db_path)
    backup = manager.create(tmp_path / "locked-account.zip")
    destination = tmp_path / "restored"
    destination.mkdir()
    BackupManager(destination, destination / "grihalekha.sqlite3").restore(backup)
    restored = Ledger(destination)
    try:
        assert restored.list_bill_cycles(tenant)[0]["locked"]
    finally:
        restored.close()


def test_financial_defaults_share_india_current_date(account, monkeypatch):
    ledger, tenant, bills = account
    ledger.post_charge(bills["rent"])
    ledger.deposit_payment(tenant, 100_000, "2024-01-02", "cash")
    ledger.deposit_payment(tenant, 50_000, "2024-01-03", "cash")
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 2, 0, 15, tzinfo=IST))
    ledger.mark_bill_paid(bills["rent"])
    assert ledger.get_bill(bills["rent"])["outstanding_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == 0
    assert ledger.dashboard()["credit_paise"] == 0
    assert len(ledger.list_payments(tenant)) == 1
    assert ledger.list_bill_cycles(tenant)[0]["paid_paise"] == 100_000


def test_amount_correction_creates_required_replacement_in_same_operation(account, monkeypatch):
    ledger, tenant, bills = account
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 15, 12, tzinfo=IST))
    ledger.post_charge(bills["electricity"], 12_000)
    ledger.deposit_payment(tenant, 12_000, "2024-01-10", "cash")
    ledger.mark_bill_paid(bills["electricity"], "2024-01-10")
    ledger.revise_bill_amount(bills["electricity"], 6000, "Correct the amount")
    power = [row for row in ledger.list_charges(tenant) if row["kind"] == "electricity"]
    assert len(power) == 2
    assert power[0]["status"] == "void"
    assert power[1]["status"] == "posted"
    assert power[1]["amount_paise"] == power[1]["outstanding_paise"] == 6000
    assert power[1]["replaces_charge_id"] == bills["electricity"]
    assert power[1]["charge_date"] == "2024-01-15"
    assert ledger.balance(tenant)["credit_paise"] == 12_000
    assert ledger.prepare_monthly_drafts() == 0


def test_amount_correction_rolls_back_every_change_if_replacement_fails(account, monkeypatch):
    ledger, tenant, bills = account
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 15, 12, tzinfo=IST))
    ledger.post_charge(bills["rent"])
    ledger.deposit_payment(tenant, 100_000, "2024-01-10", "cash")
    ledger.mark_bill_paid(bills["rent"], "2024-01-10")
    before = ledger.get_revision()
    def fail(*args, **kwargs):
        raise RuntimeError("simulated replacement failure")
    monkeypatch.setattr(ledger, "_insert_charge", fail)
    with pytest.raises(RuntimeError, match="replacement failure"):
        ledger.revise_bill_amount(bills["rent"], 0, "Correct the amount")
    assert ledger.get_revision() == before
    assert len(ledger.list_charges(tenant)) == 2
    assert ledger.get_bill(bills["rent"])["status"] == "posted"
    assert ledger.get_bill(bills["rent"])["paid_paise"] == 100_000
    assert ledger.balance(tenant)["credit_paise"] == 0
