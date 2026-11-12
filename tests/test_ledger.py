import sqlite3
from datetime import date

import pytest

from grihalekha.ledger import Ledger
from legacy_ledger import cancel_historical_bill


@pytest.fixture
def account(tmp_path):
    ledger = Ledger(tmp_path / "data")
    building = ledger.add_building("कृष्णा निवास", "12 Example Road")
    flat = ledger.add_flat(building, "201", "2", 2)
    tenant = ledger.add_tenancy(
        flat,
        "अमित कुमार",
        "2026-01-05",
        5,
        1_000_000,
        members=[{"name": "सीता", "relationship": "Spouse"}],
    )
    yield ledger, tenant, flat, building
    ledger.close()


def charge(
    ledger,
    tenant,
    amount=1_000_000,
    kind="rent",
    start="2026-01-05",
    end="2026-02-04",
    **kwargs,
):
    return ledger.add_charge(
        tenant,
        kind,
        amount,
        start,
        end,
        end,
        description="Repair" if kind == "misc" else "",
        **kwargs,
    )


def test_flats_expand_and_metadata_can_change(account):
    ledger, tenant, flat, building = account
    for number in range(30):
        ledger.add_flat(building, str(number), "1", 1)
    assert len(ledger.list_flats()) == 31
    ledger.update_building(building, "New name", "New address")
    ledger.update_flat(flat, building, "B201", "Second", 3)
    assert ledger.get_tenancy(tenant)["number"] == "B201"
    with pytest.raises(ValueError):
        ledger.add_flat(building, "b201", "1", 1)


def test_remove_unused_properties_is_atomic_and_audited(account):
    ledger, tenant, protected_flat, protected_building = account
    building = ledger.add_building("Unused", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="flats"):
        ledger.remove_building(building)
    with pytest.raises(ValueError, match="tenant history"):
        ledger.remove_flat(protected_flat)
    assert ledger.get_revision() == revision
    ledger.remove_flat(flat)
    ledger.remove_building(building)
    assert ledger.get_revision() == revision + 2
    assert [b["id"] for b in ledger.list_buildings()] == [protected_building]
    assert [f["id"] for f in ledger.list_flats()] == [protected_flat]
    assert ledger.get_tenancy(tenant)["flat_id"] == protected_flat
    with sqlite3.connect(ledger.db_path) as connection:
        rows = connection.execute("SELECT entity, details FROM audit WHERE action='delete' ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "flat" and '"number": "1"' in rows[0][1]
        assert rows[1][0] == "building" and "Unused" in rows[1][1]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("when", ["former", "upcoming"])
def test_vacant_flats_with_former_or_future_tenants_cannot_be_removed(tmp_path, when):
    ledger = Ledger(tmp_path / when)
    try:
        building = ledger.add_building("Protected", "Address")
        flat = ledger.add_flat(building, "1", "Ground", 1)
        tenant = ledger.add_tenancy(flat, "Tenant", "2020-01-01" if when == "former" else "2190-01-01", 1, 0)
        if when == "former":
            ledger.move_out(tenant, "2020-01-10", 0, "Moved away")
        assert ledger.list_flats()[0]["tenancy_id"] is None
        with pytest.raises(ValueError, match="tenant history"):
            ledger.remove_flat(flat)
        assert ledger.get_tenancy(tenant)
    finally:
        ledger.close()


def test_selective_full_payments_and_remaining_balance(account):
    ledger, tenant, *_ = account
    rent = charge(ledger, tenant)
    electricity = charge(ledger, tenant, 80_000, "electricity")
    fee = charge(ledger, tenant, 20_000, "misc")
    ledger.record_payment(
        tenant, 1_000_000, "2026-02-04", "upi", allocations={rent: 1_000_000}
    )
    assert ledger.balance(tenant, "2026-02-04")["outstanding_paise"] == 100_000
    assert ledger.list_charges(tenant, "2026-02-04")[1]["outstanding_paise"] == 80_000
    ledger.record_payment(tenant, 400_000, "2026-02-06", "cash")
    assert ledger.balance(tenant, "2026-02-06")["outstanding_paise"] == 100_000
    ledger.mark_bill_paid(electricity, "2026-02-06")
    ledger.mark_bill_paid(fee, "2026-02-06")
    result = ledger.balance(tenant, "2026-02-06")
    assert result["outstanding_paise"] == 0
    assert result["credit_paise"] == 300_000
    assert result["net_paise"] == -300_000
    assert ledger.list_charges(tenant, "2026-02-06")[1]["id"] == electricity


def test_one_payment_can_cover_two_months(account):
    ledger, tenant, *_ = account
    first = charge(ledger, tenant)
    second = charge(ledger, tenant, start="2026-02-05", end="2026-03-04")
    receipt = ledger.record_payment(tenant, 2_000_000, "2026-03-04", "bank")
    ledger.mark_bill_paid(first, "2026-03-04")
    ledger.mark_bill_paid(second, "2026-03-04")
    assert ledger.balance(tenant, "2026-03-04")["net_paise"] == 0
    assert len(ledger.list_payments(tenant, "2026-03-04")[0]["allocations"]) == 2
    assert receipt > 0


def test_advance_does_not_pay_drafts(account):
    ledger, tenant, *_ = account
    ledger.record_payment(tenant, 1_200_000, "2026-01-05", "cash")
    assert ledger.prepare_rent_drafts("2026-02-05") == 2
    revision = ledger.get_revision()
    assert ledger.prepare_rent_drafts("2026-02-05") == 0
    assert ledger.get_revision() == revision
    assert ledger.balance(tenant, "2026-02-05")["credit_paise"] == 1_200_000
    ledger.post_charge(ledger.list_charges(tenant)[0]["id"])
    assert ledger.balance(tenant, "2026-02-05")["credit_paise"] == 1_200_000
    ledger.mark_bill_paid(ledger.list_charges(tenant)[0]["id"], "2026-02-05")
    assert ledger.balance(tenant, "2026-02-05")["credit_paise"] == 200_000


def test_partial_draft_requires_explicit_amount(account):
    ledger, _, _, building = account
    flat = ledger.add_flat(building, "202", "2", 2)
    tenant = ledger.add_tenancy(flat, "Partial", "2026-01-12", 5, 1_000_000)
    ledger.prepare_rent_drafts("2026-01-20")
    draft = ledger.list_charges(tenant)[0]
    assert draft["needs_amount"]
    with pytest.raises(ValueError):
        ledger.post_charge(draft["id"])
    ledger.post_charge(draft["id"], 0)
    assert ledger.list_charges(tenant)[0]["status"] == "posted"


def test_overdue_grace_starts_after_last_included_day(account):
    ledger, tenant, *_ = account
    charge(ledger, tenant)
    ledger.update_tenancy(tenant, "अमित कुमार", grace_days=3)
    assert ledger.balance(tenant, "2026-02-04")["overdue_paise"] == 0
    assert ledger.balance(tenant, "2026-02-07")["overdue_paise"] == 0
    assert ledger.balance(tenant, "2026-02-08")["overdue_paise"] == 1_000_000


def test_payment_reversal_preserves_historical_balances(account):
    ledger, tenant, *_ = account
    rent = charge(ledger, tenant)
    payment = ledger.record_payment(tenant, 1_000_000, "2026-02-04", "bank")
    ledger.mark_bill_paid(rent, "2026-02-04")
    ledger.reverse_payment(payment, "Transfer failed", "2026-02-10")
    assert ledger.balance(tenant, "2026-02-09")["outstanding_paise"] == 0
    assert ledger.balance(tenant, "2026-02-10")["outstanding_paise"] == 1_000_000
    snapshot = ledger.statement(tenant, "2026-02-01", "2026-02-28")
    assert snapshot["opening_balance_paise"] == 1_000_000
    assert snapshot["period_payments_paise"] == 1_000_000
    assert snapshot["adjustments_paise"] == 1_000_000
    assert snapshot["closing_balance_paise"] == 1_000_000


def test_charge_cancellation_returns_credit_and_retains_cutoff(account):
    ledger, tenant, *_ = account
    rent = charge(ledger, tenant)
    ledger.record_payment(tenant, 1_000_000, "2026-02-04", "cash")
    ledger.mark_bill_paid(rent, "2026-02-04")
    cancel_historical_bill(ledger, rent, "Wrong period", "2026-02-10")
    assert ledger.balance(tenant, "2026-02-09")["credit_paise"] == 0
    assert ledger.balance(tenant, "2026-02-10")["credit_paise"] == 1_000_000
    assert (
        ledger.statement(tenant, "2026-01-01", "2026-02-28")["closing_balance_paise"]
        == -1_000_000
    )


def test_march_statement_does_not_count_april_payment(account):
    ledger, tenant, *_ = account
    charge(ledger, tenant)
    ledger.record_payment(tenant, 1_000_000, "2026-04-01", "cash")
    snapshot = ledger.statement(tenant, "2026-03-01", "2026-03-31", as_of="2026-03-31")
    assert snapshot["closing_balance_paise"] == 1_000_000
    assert snapshot["payments"] == []
    assert snapshot["outstanding_charges"][0]["paid_paise"] == 0


def test_refund_is_separate_and_cannot_overspend_history(account):
    ledger, tenant, *_ = account
    payment = ledger.record_payment(
        tenant, 100_000, "2026-01-05", "cash", allocations={}
    )
    rent = charge(ledger, tenant, 100_000, charge_date="2026-01-10")
    ledger.mark_bill_paid(rent, "2026-01-10")
    cancel_historical_bill(ledger, rent, "Cancelled", "2026-01-20")
    with pytest.raises(ValueError):
        ledger.refund_credit(tenant, 100_000, "2026-01-06", "cash", "Backdated")
    refund = ledger.refund_credit(
        tenant, 100_000, "2026-01-20", "cash", "Return unused advance"
    )
    assert ledger.balance(tenant, "2026-01-20")["credit_paise"] == 0
    with pytest.raises(ValueError):
        ledger.reverse_payment(payment, "Wrong", "2026-01-21")
    ledger.reverse_refund(refund, "Refund was not sent", "2026-01-21")
    assert ledger.balance(tenant, "2026-01-21")["credit_paise"] == 100_000
    ledger.statement(tenant, "2026-01-05", "2026-02-01")


def test_security_deposits_zero_and_transfers(account):
    ledger, tenant, *_ = account
    revision = ledger.get_revision()
    assert ledger.record_deposit(tenant, 0, "2026-01-05", "receive") == 0
    assert ledger.get_revision() == revision
    ledger.record_deposit(tenant, 500_000, "2026-01-05", "receive")
    charge(ledger, tenant, 300_000)
    ledger.record_deposit(
        tenant, 300_000, "2026-02-04", "apply", notes="Agreed final settlement"
    )
    result = ledger.balance(tenant, "2026-02-04")
    assert result["net_paise"] == 0
    assert result["deposit_paise"] == 200_000
    assert ledger.list_payments(tenant, "2026-02-04")[0]["method"] == "deposit"
    ledger.record_deposit(
        tenant, 200_000, "2026-02-05", "return", notes="Remaining deposit returned"
    )
    assert ledger.balance(tenant, "2026-02-05")["deposit_paise"] == 0
    ledger.statement(tenant, "2026-01-05", "2026-02-28")


def test_opening_balances_atomic_and_not_repeatable(account):
    ledger, tenant, *_ = account
    ledger.prepare_rent_drafts("2026-03-01")
    with pytest.raises(ValueError):
        ledger.set_opening_balances(
            tenant, "2026-03-01", rent_paise=100_000, misc_paise=100
        )
    assert ledger.balance(tenant, "2026-03-01")["net_paise"] == 0
    ledger.set_opening_balances(
        tenant,
        "2026-03-01",
        rent_paise=100_000,
        electricity_paise=20_000,
        advance_paise=10_000,
        deposit_paise=500_000,
    )
    assert ledger.balance(tenant, "2026-03-01")["net_paise"] == 110_000
    assert ledger.balance(tenant, "2026-03-01")["deposit_paise"] == 500_000
    with pytest.raises(ValueError):
        ledger.set_opening_balances(tenant, "2026-03-01", rent_paise=100_000)
    ledger.prepare_rent_drafts("2026-03-05")
    assert all(
        row["period_start"] >= "2026-03-01" for row in ledger.list_charges(tenant)
    )


def test_many_charge_lines_save_atomically(account):
    ledger, tenant, *_ = account
    first = {
        "kind": "misc",
        "amount_paise": 100,
        "period_start": "2026-01-05",
        "period_end": "2026-02-04",
        "due_date": "2026-02-04",
        "description": "Repair",
    }
    with pytest.raises(ValueError):
        ledger.add_charges(tenant, [first, dict(first, description="")])
    assert ledger.list_charges(tenant) == []


def test_payment_allocation_scope_and_limits_are_atomic(account):
    ledger, tenant, _, building = account
    flat2 = ledger.add_flat(building, "203", "2", 3)
    tenant2 = ledger.add_tenancy(flat2, "Other", "2026-01-05", 5, 100)
    other_charge = charge(ledger, tenant2, 100)
    with pytest.raises(ValueError):
        ledger.record_payment(
            tenant, 100, "2026-01-05", "cash", allocations={other_charge: 100}
        )
    assert ledger.list_payments(tenant) == []


def test_moveout_corrects_posted_rent_and_preserves_previous_tenant(account):
    ledger, tenant, flat, *_ = account
    charge(ledger, tenant)
    ledger.record_payment(tenant, 1_000_000, "2026-01-20", "cash")
    ledger.prepare_rent_drafts("2026-03-01")
    ledger.move_out(tenant, "2026-01-25", 600_000)
    current = ledger.balance(tenant)
    assert current["billed_paise"] == 600_000
    assert current["credit_paise"] == 1_000_000
    replacement = next(row for row in ledger.list_charges(tenant) if row["kind"] == "rent" and row["status"] == "posted")
    ledger.mark_bill_paid(replacement["id"])
    assert ledger.balance(tenant)["credit_paise"] == 400_000
    assert all(
        row["status"] == "void"
        for row in ledger.list_charges(tenant)
        if row["period_start"] > "2026-01-25"
    )
    next_tenant = ledger.add_tenancy(
        flat, "Next household", "2026-01-26", 26, 1_100_000
    )
    assert ledger.balance(next_tenant)["net_paise"] == 0
    assert ledger.balance(tenant)["credit_paise"] == 400_000
    ledger.statement(tenant, "2026-01-05", date.today().isoformat())


def test_rate_changes_future_boundary_preserve_posted_history(account):
    ledger, tenant, *_ = account
    old = charge(ledger, tenant)
    future_year = date.today().year + 1
    effective = f"{future_year}-01-05"
    ledger.change_rent(tenant, effective, 1_200_000)
    ledger.prepare_rent_drafts(effective)
    assert (
        next(row for row in ledger.list_charges(tenant) if row["id"] == old)[
            "amount_paise"
        ]
        == 1_000_000
    )
    assert (
        next(
            row
            for row in ledger.list_charges(tenant)
            if row["period_start"] == effective
        )["amount_paise"]
        == 1_200_000
    )
    with pytest.raises(ValueError):
        ledger.change_rent(tenant, f"{future_year}-02-06", 1_300_000)


def test_proof_is_copied_and_survives_original_disappearing(account, tmp_path):
    ledger, tenant, *_ = account
    proof = tmp_path / "proof.pdf"
    proof.write_bytes(b"%PDF-1.7\nTest receipt")
    ledger.record_payment(tenant, 100, "2026-01-05", "bank", proof_path=proof)
    proof.unlink()
    saved = ledger.list_payments(tenant)[0]["proof_path"]
    assert saved.startswith("attachments/")
    assert (ledger.data_dir / saved).read_bytes().startswith(b"%PDF-")
    with pytest.raises(ValueError):
        ledger.record_payment(
            tenant, 100, "2026-01-05", "cash", proof_path=tmp_path / "missing.pdf"
        )
    assert len(ledger.list_payments(tenant)) == 1


def test_newer_or_unrecognized_database_not_silently_recreated(tmp_path):
    data = tmp_path / "bad"
    data.mkdir()
    with sqlite3.connect(data / "grihalekha.sqlite3") as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="newer"):
        Ledger(data)


def test_unrelated_database_refused_and_failed_constructor_releases_file(tmp_path):
    data = tmp_path / "unrelated"
    data.mkdir()
    path = data / "grihalekha.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE precious_data (value TEXT)")
        conn.execute("INSERT INTO precious_data VALUES ('keep me')")
    conn.close()
    with pytest.raises(ValueError, match="no recognized schema"):
        Ledger(data)
    renamed = path.with_name("preserved.sqlite3")
    path.rename(renamed)
    with sqlite3.connect(renamed) as conn:
        assert (
            conn.execute("SELECT value FROM precious_data").fetchone()[0] == "keep me"
        )


def test_released_credit_applies_only_when_it_became_available(account):
    ledger, tenant, *_ = account
    old = charge(ledger, tenant, 100_000)
    ledger.record_payment(tenant, 100_000, "2026-01-05", "cash")
    ledger.mark_bill_paid(old, "2026-01-05")
    cancel_historical_bill(ledger, old, "Wrong charge", "2026-01-20")
    new = charge(ledger, tenant, 100_000, "misc", charge_date="2026-01-10")
    with pytest.raises(ValueError, match="available balance"):
        ledger.mark_bill_paid(new, "2026-01-10")
    ledger.mark_bill_paid(new, "2026-01-20")
    rows = ledger.list_payments(tenant, "2026-01-20")[0]["allocations"]
    assert rows == [
        {"charge_id": new, "amount_paise": 100_000, "effective_on": "2026-01-20"}
    ]
    assert ledger.balance(tenant, "2026-01-15")["net_paise"] == 100_000
    assert ledger.balance(tenant, "2026-01-20")["net_paise"] == 0


def test_backdated_corrections_cannot_invalidate_existing_release_history(account):
    ledger, tenant, *_ = account
    old = charge(ledger, tenant, 100_000)
    payment = ledger.record_payment(tenant, 100_000, "2026-01-05", "cash")
    ledger.mark_bill_paid(old, "2026-01-05")
    ledger.reverse_payment(payment, "Failed transfer", "2026-01-20")
    with pytest.raises(ValueError, match="allocation correction"):
        ledger.revise_bill_amount(old, 0, "Earlier correction", "2026-01-15")
    result = ledger.balance(tenant, "2026-01-16")
    assert (
        result["net_paise"] == result["outstanding_paise"] - result["credit_paise"] == 0
    )


def test_payment_cannot_be_reversed_before_its_refund_correction(account):
    ledger, tenant, *_ = account
    payment = ledger.record_payment(tenant, 100_000, "2026-01-05", "cash")
    returned = ledger.refund_credit(tenant, 100_000, "2026-01-10", "cash", "Return")
    ledger.reverse_refund(returned, "Refund entry mistaken", "2026-01-20")
    with pytest.raises(ValueError, match="refund corrections"):
        ledger.reverse_payment(payment, "Earlier reversal", "2026-01-15")
    ledger.reverse_payment(payment, "Corrected receipt", "2026-01-20")
    result = ledger.balance(tenant, "2026-01-20")
    assert (
        result["net_paise"] == result["outstanding_paise"] - result["credit_paise"] == 0
    )


def test_deposit_reversals_restore_held_funds_and_general_account(account):
    ledger, tenant, *_ = account
    receipt = ledger.record_deposit(tenant, 100_000, "2026-01-05", "receive")
    applied = ledger.record_deposit(
        tenant, 100_000, "2026-01-10", "apply", notes="Agreed transfer"
    )
    with pytest.raises(ValueError, match="used or returned"):
        ledger.reverse_deposit(receipt, "Wrong receipt", "2026-01-15")
    ledger.reverse_deposit(applied, "Transfer was not agreed", "2026-01-20")
    assert ledger.balance(tenant, "2026-01-19")["credit_paise"] == 100_000
    assert ledger.balance(tenant, "2026-01-20")["credit_paise"] == 0
    assert ledger.balance(tenant, "2026-01-20")["deposit_paise"] == 100_000
    ledger.reverse_deposit(receipt, "Wrong receipt", "2026-01-20")
    assert ledger.balance(tenant, "2026-01-20")["deposit_paise"] == 0
    snapshot = ledger.statement(tenant, "2026-01-15", "2026-01-31")
    assert len(snapshot["deposits"]) == 2
    assert {row["kind"] for row in snapshot["deposits"]} == {"reversal"}


def test_cancel_scheduled_rate_recalculates_only_drafts(account):
    ledger, tenant, *_ = account
    year = date.today().year + 1
    effective = f"{year}-01-05"
    ledger.change_rent(tenant, effective, 1_500_000)
    ledger.prepare_rent_drafts(effective)
    rates = ledger.get_tenancy(tenant)["rent_rates"]
    ledger.cancel_rent_change(rates[-1]["id"])
    future = next(
        row for row in ledger.list_charges(tenant) if row["period_start"] == effective
    )
    assert future["amount_paise"] == 1_000_000
    with pytest.raises(ValueError, match="initial and historical"):
        ledger.cancel_rent_change(rates[0]["id"])
    ledger.change_rent(tenant, effective, 1_300_000)
    ledger.post_charge(future["id"])
    with pytest.raises(ValueError, match="already posted"):
        ledger.cancel_rent_change(ledger.get_tenancy(tenant)["rent_rates"][-1]["id"])


def test_backdated_payment_preview_caps_later_settlements(account):
    ledger, tenant, *_ = account
    rent = charge(ledger, tenant, 100_000)
    ledger.record_payment(tenant, 60_000, "2026-01-20", "cash")
    ledger.record_payment(tenant, 40_000, "2026-01-20", "cash")
    ledger.mark_bill_paid(rent, "2026-01-20")
    candidates = ledger.payment_candidates(tenant, "2026-01-10")
    assert candidates == []
    with pytest.raises(ValueError, match="Later payment history"):
        ledger.record_payment(tenant, 100_000, "2026-01-10", "cash", allocations={rent: 100_000})
    assert ledger.balance(tenant, "2026-01-20")["outstanding_paise"] == 0


def test_replacement_charge_cannot_precede_cancellation(account):
    ledger, tenant, *_ = account
    rent = charge(ledger, tenant, 100_000)
    cancel_historical_bill(ledger, rent, "Correction", "2026-01-20")
    with pytest.raises(ValueError, match="cancellation date"):
        charge(ledger, tenant, 120_000, charge_date="2026-01-10")
    replacement = charge(ledger, tenant, 120_000, charge_date="2026-01-20")
    assert (
        next(row for row in ledger.list_charges(tenant) if row["id"] == replacement)[
            "replaces_charge_id"
        ]
        == rent
    )
