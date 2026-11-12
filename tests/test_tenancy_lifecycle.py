from datetime import date, timedelta

import pytest

from grihalekha.ledger import Ledger
from grihalekha.dates import cycle_bounds


@pytest.fixture
def account(tmp_path):
    ledger = Ledger(tmp_path / "data")
    building = ledger.add_building("First house", "First address")
    other_building = ledger.add_building("Second house", "Second address")
    first = ledger.add_flat(building, "1", "Ground", 1)
    second = ledger.add_flat(other_building, "2", "First", 2)
    tenant = ledger.add_tenancy(first, "Tenant", "2024-01-01", 1, 100_000)
    yield ledger, tenant, first, second
    ledger.close()


def test_transfer_keeps_account_money_and_dated_property_history(account):
    ledger, tenant, first, second = account
    payment = ledger.record_payment(tenant, 12_345, "2024-01-03", "cash", allocations={})
    before = ledger.balance(tenant)
    ledger.transfer_tenancy(tenant, second, "2024-02-15")
    result = ledger.get_tenancy(tenant)
    assert result["id"] == tenant and result["flat_id"] == second
    assert result["building_name"] == "Second house"
    assert [(item["flat_id"], item["starts_on"], item["ends_on"]) for item in result["occupancy_history"]] == [
        (first, "2024-01-01", "2024-02-14"), (second, "2024-02-15", None),
    ]
    assert ledger.balance(tenant) == before
    assert ledger.list_payments(tenant)[0]["id"] == payment
    assert [item["id"] for item in ledger.list_tenancies(first)] == [tenant]
    assert {item["id"]: item["tenancy_id"] for item in ledger.list_flats()} == {first: None, second: tenant}
    with pytest.raises(ValueError, match="history"):
        ledger.remove_flat(first)


def test_future_transfer_keeps_current_location_until_its_date(account):
    ledger, tenant, first, second = account
    future = (date.today() + timedelta(days=30)).isoformat()
    ledger.transfer_tenancy(tenant, second, future)
    assert ledger.get_tenancy(tenant)["flat_id"] == first
    assert {item["id"]: item["tenancy_id"] for item in ledger.list_flats()} == {first: tenant, second: None}
    with pytest.raises(ValueError, match="overlapping"):
        ledger.add_tenancy(second, "Conflicting resident", future, 1, 0)


def test_exchange_moves_both_households_atomically_and_preserves_balances(account):
    ledger, first_tenant, first, second = account
    second_tenant = ledger.add_tenancy(second, "Other tenant", "2024-01-01", 1, 90_000)
    ledger.record_payment(first_tenant, 10_000, "2024-01-02", "cash", allocations={})
    ledger.record_deposit(second_tenant, 5_000, "2024-01-02", "receive")
    before = [ledger.balance(item) for item in (first_tenant, second_tenant)]
    with pytest.raises(ValueError, match="overlapping"):
        ledger.transfer_tenancy(first_tenant, second, "2024-02-01")
    ledger.exchange_tenancies(first_tenant, second_tenant, "2024-02-01")
    assert ledger.get_tenancy(first_tenant)["flat_id"] == second
    assert ledger.get_tenancy(second_tenant)["flat_id"] == first
    assert [ledger.balance(item) for item in (first_tenant, second_tenant)] == before
    assert ledger.dashboard("2024-01-31")["occupied_flats"] == 2
    assert ledger.dashboard("2024-02-01")["occupied_flats"] == 2


def test_exchange_rolls_back_first_move_if_second_rent_is_invalid(account):
    ledger, first_tenant, first, second = account
    second_tenant = ledger.add_tenancy(second, "Other tenant", "2024-01-01", 1, 90_000)
    with pytest.raises(ValueError):
        ledger.exchange_tenancies(first_tenant, second_tenant, "2024-02-01", 110_000, -1)
    for tenant, flat in ((first_tenant, first), (second_tenant, second)):
        result = ledger.get_tenancy(tenant)
        assert result["flat_id"] == flat
        assert len(result["occupancy_history"]) == 1
        assert len(result["rent_rates"]) == 1


def test_transfer_rent_uses_next_full_cycle_without_prorating(account):
    ledger, tenant, _, second = account
    result = ledger.transfer_tenancy(tenant, second, "2024-02-15", rent_paise=120_000)
    assert result["rent_effective_from"] == "2024-03-01"
    ledger.prepare_rent_drafts("2024-03-01")
    bills = {row["period_start"]: row for row in ledger.list_charges(tenant)}
    assert bills["2024-02-01"]["amount_paise"] == 100_000
    assert bills["2024-03-01"]["amount_paise"] == 120_000


def test_moveout_can_extend_then_shorten_with_traceable_rent_corrections(account):
    ledger, tenant, first, _ = account
    ledger.move_out(tenant, "2024-01-15", 50_000)
    original = next(item for item in ledger.list_charges(tenant) if item["kind"] == "rent")
    ledger.record_payment(tenant, 50_000, date.today().isoformat(), "cash")
    ledger.move_out(tenant, "2024-02-20", 70_000, "Extended stay")
    rents = [item for item in ledger.list_charges(tenant) if item["kind"] == "rent"]
    assert next(item for item in rents if item["id"] == original["id"])["status"] == "void"
    january = next(item for item in rents if item["status"] == "draft")
    assert (january["period_start"], january["period_end"], january["amount_paise"]) == ("2024-01-01", "2024-01-31", 100_000)
    assert any(item["status"] == "posted" and item["period_end"] == "2024-02-20" and item["amount_paise"] == 70_000 for item in rents)
    ledger.move_out(tenant, "2024-01-25", 80_000, "Leaving earlier")
    assert ledger.get_tenancy(tenant)["move_out"] == "2024-01-25"
    assert ledger.balance(tenant)["outstanding_paise"] == 80_000
    assert ledger.balance(tenant)["credit_paise"] == 50_000
    next_tenant = ledger.add_tenancy(first, "Next resident", "2024-01-26", 26, 0)
    with pytest.raises(ValueError, match="overlapping"):
        ledger.move_out(tenant, "2024-02-01", 100_000)
    assert ledger.get_tenancy(tenant)["move_out"] == "2024-01-25"
    assert ledger.get_tenancy(next_tenant)["move_in"] == "2024-01-26"


def test_archive_requires_departure_review_and_full_settlement_and_can_reopen(account):
    ledger, tenant, _, _ = account
    with pytest.raises(ValueError, match="last occupied day"):
        ledger.archive_tenancy(tenant)
    ledger.move_out(tenant, "2024-01-20", 0)
    with pytest.raises(ValueError, match="draft"):
        ledger.archive_tenancy(tenant)
    for charge in ledger.list_charges(tenant):
        if charge["status"] == "draft":
            ledger.post_charge(charge["id"], 0)
    ledger.record_deposit(tenant, 100, "2024-01-01", "receive")
    with pytest.raises(ValueError, match="deposits"):
        ledger.archive_tenancy(tenant)
    ledger.record_deposit(tenant, 100, "2024-01-20", "return", notes="Deposit returned at departure")
    ledger.archive_tenancy(tenant)
    assert ledger.list_tenancies() == []
    assert ledger.list_tenancies(include_archived=True)[0]["id"] == tenant
    assert ledger.get_tenancy(tenant)["archived_on"] == date.today().isoformat()
    with pytest.raises(ValueError, match="Reopen"):
        ledger.update_tenancy(tenant, "Changed")
    ledger.unarchive_tenancy(tenant)
    assert ledger.list_tenancies()[0]["id"] == tenant
    assert ledger.get_tenancy(tenant)["move_out"] == "2024-01-20"


def test_planned_transfer_can_be_cancelled_before_shortening_departure(account):
    ledger, tenant, first, second = account
    today = date.today()
    future = cycle_bounds(today, 1)[1] + timedelta(days=10)
    original_payment = ledger.record_payment(tenant, 12_345, "2024-01-02", "cash", allocations={})
    original_balance = ledger.balance(tenant)
    result = ledger.transfer_tenancy(tenant, second, future.isoformat(), rent_paise=120_000)
    ledger.prepare_rent_drafts(result["rent_effective_from"])
    assert ledger.cancel_scheduled_transfer(tenant, "Move no longer needed") == [tenant]
    tenancy = ledger.get_tenancy(tenant)
    assert len(tenancy["occupancy_history"]) == 1
    assert tenancy["flat_id"] == first
    assert len(tenancy["rent_rates"]) == 1
    assert ledger.balance(tenant) == original_balance
    assert ledger.list_payments(tenant)[0]["id"] == original_payment
    restored_draft = next(row for row in ledger.list_charges(tenant) if row["period_start"] == result["rent_effective_from"])
    assert restored_draft["amount_paise"] == 100_000
    ledger.add_tenancy(second, "Destination is free", future.isoformat(), 1, 0)
    ledger.move_out(tenant, (today + timedelta(days=3)).isoformat(), 0, "Leaving before cancelled plan")
    assert ledger.get_tenancy(tenant)["move_out"] < future.isoformat()


def test_cancelling_planned_exchange_restores_both_reservations(account):
    ledger, first_tenant, first, second = account
    second_tenant = ledger.add_tenancy(second, "Other tenant", "2024-01-01", 1, 90_000)
    future = (date.today() + timedelta(days=15)).isoformat()
    ledger.exchange_tenancies(first_tenant, second_tenant, future, 120_000, 110_000)
    assert set(ledger.cancel_scheduled_transfer(second_tenant)) == {first_tenant, second_tenant}
    for tenant, flat in ((first_tenant, first), (second_tenant, second)):
        current = ledger.get_tenancy(tenant)
        assert current["flat_id"] == flat
        assert len(current["occupancy_history"]) == len(current["rent_rates"]) == 1


def test_cannot_cancel_a_transfer_that_has_already_started(account):
    ledger, tenant, _, second = account
    ledger.transfer_tenancy(tenant, second, "2024-02-01")
    with pytest.raises(ValueError, match="not started"):
        ledger.cancel_scheduled_transfer(tenant)


def test_cancelling_exchange_with_posted_rent_requires_unlock_then_rebuilds_bills(account):
    ledger, first_tenant, _, second = account
    second_tenant = ledger.add_tenancy(second, "Other tenant", "2024-01-01", 1, 90_000)
    future = cycle_bounds(date.today(), 1)[1].isoformat()
    ledger.exchange_tenancies(first_tenant, second_tenant, future, 120_000, 110_000)
    ledger.prepare_rent_drafts(future)
    bill = next(row for row in ledger.list_charges(second_tenant) if row["period_start"] == future)
    ledger.post_charge(bill["id"])
    ledger.set_cycle_payment_lock(second_tenant, future, True)
    with pytest.raises(ValueError, match="locked"):
        ledger.cancel_scheduled_transfer(first_tenant)
    for tenant in (first_tenant, second_tenant):
        assert len(ledger.get_tenancy(tenant)["occupancy_history"]) == 2
        assert len(ledger.get_tenancy(tenant)["rent_rates"]) == 2
    ledger.set_cycle_payment_lock(second_tenant, future, False)
    ledger.cancel_scheduled_transfer(first_tenant)
    assert len(ledger.get_tenancy(first_tenant)["occupancy_history"]) == 1
    charges = ledger.list_charges(second_tenant)
    old = next(row for row in charges if row["id"] == bill["id"])
    assert old["amount_paise"] == 110_000 and old["status"] == "void"
    revised = next(row for row in charges if row["replaces_charge_id"] == bill["id"])
    assert revised["status"] == "draft" and revised["amount_paise"] == 90_000


def test_cancellation_cannot_displace_a_new_reservation(account):
    ledger, tenant, first, second = account
    future = (date.today() + timedelta(days=10)).isoformat()
    ledger.transfer_tenancy(tenant, second, future)
    ledger.add_tenancy(first, "Incoming household", future, 1, 0)
    with pytest.raises(ValueError, match="overlapping"):
        ledger.cancel_scheduled_transfer(tenant)
    assert len(ledger.get_tenancy(tenant)["occupancy_history"]) == 2


def test_future_departure_can_be_extended_then_shortened(account):
    ledger, tenant, _, _ = account
    today = date.today()
    ledger.move_out(tenant, (today + timedelta(days=5)).isoformat(), 50_000)
    ledger.move_out(tenant, (today + timedelta(days=9)).isoformat(), 70_000, "Extended")
    ledger.move_out(tenant, (today + timedelta(days=3)).isoformat(), 70_000, "Shortened")
    assert ledger.get_tenancy(tenant)["move_out"] == (today + timedelta(days=3)).isoformat()
    final = [row for row in ledger.list_charges(tenant) if row["kind"] == "rent" and row["status"] == "posted"]
    assert len(final) == 1 and final[0]["amount_paise"] == 70_000
