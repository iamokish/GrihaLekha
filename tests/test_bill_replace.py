"""Manual replacement bills stay linked, so undo cannot duplicate their debt."""

from datetime import datetime

import pytest

from grihalekha import clock
from grihalekha.ledger import Ledger
from legacy_ledger import cancel_historical_bill


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 1, 20, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path)
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
    ledger.prepare_monthly_drafts("2024-01-01")
    bill = ledger.add_charge(tenant, "misc", 5000, "2024-01-01", "2024-01-03", "2024-01-03", "Repair")
    yield ledger, tenant, bill
    ledger.close()


def line(amount=3000, **overrides):
    return dict(kind="misc", amount_paise=amount, period_start="2024-01-01",
                period_end="2024-01-03", due_date="2024-01-03", charge_date="2024-01-06",
                description="Correct repair amount", **overrides)


def test_miscellaneous_replacement_links_every_line_and_blocks_undo(account):
    ledger, tenant, bill = account
    ledger.deposit_payment(tenant, 5000, "2024-01-02", "cash")
    ledger.mark_bill_paid(bill, "2024-01-02")
    ledger.void_charge(bill, "Wrong amount", "2024-01-05")
    replacements = ledger.replace_cancelled_bill(bill, [line(2000), line(1000)])
    assert len(replacements) == 2
    assert all(ledger.get_bill(item)["replaces_charge_id"] == bill for item in replacements)
    assert ledger.balance(tenant)["outstanding_paise"] == 3000
    assert ledger.balance(tenant)["credit_paise"] == 5000
    before = ledger.get_revision()
    with pytest.raises(ValueError, match="active replacement"):
        ledger.restore_cancelled_bill(bill, "Undo original cancellation")
    with pytest.raises(ValueError, match="active replacement"):
        ledger.replace_cancelled_bill(bill, [line()])
    assert ledger.get_revision() == before
    assert ledger.balance(tenant)["outstanding_paise"] == 3000


@pytest.mark.parametrize("kind", ["rent", "electricity"])
def test_required_replacement_is_linked_and_must_use_one_line(account, kind):
    ledger, tenant, _ = account
    original = next(bill for bill in ledger.list_charges(tenant) if bill["kind"] == kind)
    ledger.post_charge(original["id"], 20_000)
    cancel_historical_bill(ledger, original["id"], "Wrong amount", "2024-01-05")
    item = line(10_000)
    item.update(kind=kind, period_end="2024-01-31", due_date="2024-01-31")
    with pytest.raises(ValueError, match="one bill"):
        ledger.replace_cancelled_bill(original["id"], [item, item])
    restored = ledger.replace_cancelled_bill(original["id"], [item])[0]
    assert ledger.get_bill(restored)["replaces_charge_id"] == original["id"]
    with pytest.raises(ValueError, match="active replacement"):
        ledger.restore_cancelled_bill(original["id"], "Undo")
    assert ledger.prepare_monthly_drafts("2024-01-20") == 0


@pytest.mark.parametrize("changes,error", [
    ({"kind": "electricity"}, "charge type"),
    ({"opening": True}, "opening-entry"),
    ({"charge_date": "2024-01-04"}, "replacement date"),
    ({"tenancy_id": 999}, "incomplete or invalid"),
])
def test_invalid_replacement_cannot_change_kind_opening_account_or_history(account, changes, error):
    ledger, tenant, bill = account
    ledger.void_charge(bill, "Wrong amount", "2024-01-05")
    before = ledger.list_charges(tenant)
    revision = ledger.get_revision()
    item = line()
    item.update(changes)
    with pytest.raises(ValueError, match=error):
        ledger.replace_cancelled_bill(bill, [item])
    assert ledger.list_charges(tenant) == before
    assert ledger.get_revision() == revision


def test_replacement_batch_rolls_back_after_later_amount_validation_failure(account):
    ledger, tenant, bill = account
    ledger.void_charge(bill, "Wrong amount", "2024-01-05")
    before = ledger.list_charges(tenant)
    revision = ledger.get_revision()
    with pytest.raises(ValueError):
        ledger.replace_cancelled_bill(bill, [line(), line(-1)])
    assert ledger.list_charges(tenant) == before
    assert ledger.get_revision() == revision


def test_replacement_respects_original_and_target_cycle_locks(account):
    ledger, tenant, bill = account
    ledger.void_charge(bill, "Wrong amount", "2024-01-05")
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    with pytest.raises(ValueError, match="locked"):
        ledger.replace_cancelled_bill(bill, [line()])
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", False)
    ledger.add_charge(tenant, "misc", 100, "2024-02-01", "2024-02-02", "2024-02-02", "Other bill")
    ledger.set_cycle_payment_lock(tenant, "2024-02-01", True)
    item = line()
    item.update(period_start="2024-02-01", period_end="2024-02-02", due_date="2024-02-02")
    with pytest.raises(ValueError, match="locked"):
        ledger.replace_cancelled_bill(bill, [item])


def test_replacement_requires_open_account_and_cancelled_bill(account):
    ledger, tenant, bill = account
    with pytest.raises(ValueError, match="cancelled bill"):
        ledger.replace_cancelled_bill(bill, [line()])
    ledger.void_charge(bill, "Wrong amount", "2024-01-05")
    ledger.move_out(tenant, "2024-01-19", 0)
    for item in ledger.list_charges(tenant):
        if item["status"] == "draft":
            ledger.post_charge(item["id"], 0)
    ledger.archive_tenancy(tenant)
    with pytest.raises(ValueError, match="archived"):
        ledger.replace_cancelled_bill(bill, [line()])


def test_replacement_cannot_resurrect_deleted_original_or_child(account):
    ledger, tenant, bill = account
    ledger.void_charge(bill, "Wrong amount", "2024-01-05")
    child = ledger.replace_cancelled_bill(bill, [line()])[0]
    ledger.delete_bill(child, "No bill needed", "2024-01-07")
    with pytest.raises(ValueError, match="deleted"):
        ledger.replace_cancelled_bill(bill, [line()])
    with pytest.raises(ValueError, match="deleted"):
        ledger.replace_cancelled_bill(child, [line()])


def test_replacement_follows_later_linked_scheduled_cancellation(account):
    ledger, tenant, bill = account
    ledger.void_charge(bill, "Wrong amount", "2024-01-05")
    child = ledger.replace_cancelled_bill(bill, [line()])[0]
    ledger.void_charge(child, "Scheduled cancellation", "2024-01-30")
    with pytest.raises(ValueError, match="replacement date"):
        ledger.replace_cancelled_bill(bill, [line()])
    item = line(4000)
    item.pop("charge_date")
    new_bill = ledger.replace_cancelled_bill(bill, [item])[0]
    assert ledger.get_bill(new_bill)["charge_date"] == "2024-01-30"
    assert ledger.balance(tenant, "2024-01-29")["outstanding_paise"] == 3000
    assert ledger.balance(tenant, "2024-01-30")["outstanding_paise"] == 4000


def test_electricity_replacement_rejects_active_overlap_and_scheduled_void_window(account):
    ledger, tenant, _ = account
    original = next(bill for bill in ledger.list_charges(tenant) if bill["kind"] == "electricity")
    cancel_historical_bill(ledger, original["id"], "Wrong amount", "2024-01-05")
    other = ledger.add_charge(tenant, "electricity", 8000, "2024-01-01", "2024-01-31", "2024-01-31")
    item = line(10_000)
    item.update(kind="electricity", period_end="2024-01-31", due_date="2024-01-31")
    with pytest.raises(ValueError, match="active bill"):
        ledger.replace_cancelled_bill(original["id"], [item])
    cancel_historical_bill(ledger, other, "Scheduled cancellation", "2024-01-30")
    with pytest.raises(ValueError, match="replacement date"):
        ledger.replace_cancelled_bill(original["id"], [item])
    item.pop("charge_date")
    replacement = ledger.replace_cancelled_bill(original["id"], [item])[0]
    assert ledger.get_bill(replacement)["charge_date"] == "2024-01-30"
