from decimal import Decimal

import pytest
from sqlalchemy import select

from grihalekha import models
from grihalekha.ledger import Ledger
from grihalekha.meter import (
    MAX_METER_READING, format_meter_reading, parse_meter_reading, validate_meter_reading,
)


@pytest.mark.parametrize("text, expected", [
    (None, None), ("", None), ("  ", None), ("0", 0), ("0.00", 0),
    ("123.4", 12340), ("123456.78", 12345678), (Decimal("12.34"), 1234),
    ("999999999999.99", MAX_METER_READING),
])
def test_readings_are_exact_and_blank_is_distinct_from_zero(text, expected):
    assert parse_meter_reading(text) == expected
    formatted = format_meter_reading(expected)
    assert parse_meter_reading(formatted) == expected
    if expected is None:
        assert formatted == ""
    else:
        assert len(formatted.rsplit(".", 1)[1]) == 2


@pytest.mark.parametrize("text", [
    "-1", "1.001", "NaN", "Infinity", "1e2", "12 kWh", "₹12", "1,200.50",
    True, 12.34, "1000000000000", "9" * 101,
])
def test_invalid_meter_inputs_are_rejected_without_rounding(text):
    with pytest.raises(ValueError):
        parse_meter_reading(text)


@pytest.mark.parametrize("value", [True, 1.0, "123", -1, MAX_METER_READING + 1])
def test_service_requires_integer_hundredths(value):
    with pytest.raises(ValueError):
        validate_meter_reading(value)


@pytest.fixture
def account(tmp_path):
    ledger = Ledger(tmp_path / "readings")
    building = ledger.add_building("House", "Address")
    flat = ledger.add_flat(building, "1", "Ground", 1)
    tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000,
                                initial_meter_reading_hundredths=12345)
    ledger.prepare_monthly_drafts("2024-01-01")
    power = next(row["id"] for row in ledger.list_charges(tenant) if row["kind"] == "electricity")
    yield ledger, tenant, power
    ledger.close()


def test_initial_reading_update_omission_preserves_value_and_blank_clears(account):
    ledger, tenant, _ = account
    ledger.update_tenancy(tenant, "Edited tenant")
    assert ledger.get_tenancy(tenant)["initial_meter_reading_hundredths"] == 12345
    ledger.update_tenancy(tenant, "Edited tenant", initial_meter_reading_hundredths=0)
    assert ledger.get_tenancy(tenant)["initial_meter_reading_hundredths"] == 0
    ledger.update_tenancy(tenant, "Edited tenant", initial_meter_reading_hundredths=None)
    assert ledger.get_tenancy(tenant)["initial_meter_reading_hundredths"] is None
    revision = ledger.get_revision()
    with pytest.raises(ValueError):
        ledger.update_tenancy(tenant, "Invalid edit", initial_meter_reading_hundredths=-1)
    assert ledger.get_tenancy(tenant)["tenant_name"] == "Edited tenant"
    assert ledger.get_revision() == revision


def test_record_reading_without_amount_then_post_and_edit_paid_locked_bill(account):
    ledger, tenant, power = account
    ledger.set_meter_reading(power, 15001)
    assert ledger.get_bill(power)["status"] == "draft"
    assert ledger.get_bill(power)["amount_paise"] is None
    assert ledger.prepare_monthly_drafts("2024-01-31") == 0
    assert ledger.get_bill(power)["meter_reading_hundredths"] == 15001
    ledger.post_charge(power, 20000)
    ledger.deposit_payment(tenant, 25000, "2024-01-10", "cash")
    ledger.mark_bill_paid(power, "2024-01-10")
    ledger.set_cycle_payment_lock(tenant, "2024-01-01", True)
    before_bill, before_balance = ledger.get_bill(power), ledger.balance(tenant)
    ledger.set_meter_reading(power, 15002)
    after = ledger.get_bill(power)
    assert after == dict(before_bill, meter_reading_hundredths=15002)
    assert ledger.balance(tenant) == before_balance
    ledger.set_meter_reading(power, None)
    assert ledger.get_bill(power)["meter_reading_hundredths"] is None
    assert ledger.get_bill(power)["payments"] == before_bill["payments"]


def test_post_with_bad_amount_or_reading_rolls_back_both_fields_and_audit(account):
    ledger, tenant, power = account
    before, revision = ledger.get_bill(power), ledger.get_revision()
    for amount, reading in [(-1, 34567), (100, -1), (100, 1.5)]:
        with pytest.raises(ValueError):
            ledger.post_charge(power, amount, meter_reading_hundredths=reading)
        assert ledger.get_bill(power) == before
        assert ledger.get_revision() == revision
    ledger.post_charge(power, 100, meter_reading_hundredths=34567)
    assert ledger.get_bill(power)["meter_reading_hundredths"] == 34567


def test_non_electricity_and_archived_account_reading_edits_are_rejected(account):
    ledger, tenant, power = account
    rent = next(row["id"] for row in ledger.list_charges(tenant) if row["kind"] == "rent")
    with pytest.raises(ValueError, match="only available"):
        ledger.set_meter_reading(rent, 1234)
    with pytest.raises(ValueError, match="only available"):
        ledger.add_charge(tenant, "misc", 100, "2024-01-01", "2024-01-31", "2024-01-31",
                          "Repair", meter_reading_hundredths=1234)
    ledger.move_out(tenant, "2024-01-31", 0)
    for row in ledger.list_charges(tenant):
        if row["status"] == "draft":
            ledger.post_charge(row["id"], 0)
    ledger.archive_tenancy(tenant)
    revision = ledger.get_revision()
    with pytest.raises(ValueError, match="archived"):
        ledger.set_meter_reading(power, 1)
    with pytest.raises(ValueError, match="archived"):
        ledger.update_tenancy(tenant, "Tenant", initial_meter_reading_hundredths=1)
    assert ledger.get_revision() == revision


def test_manual_electricity_reading_and_amount_correction_retain_exact_value(account):
    ledger, tenant, _ = account
    power = ledger.add_charge(tenant, "electricity", 20000, "2024-02-01", "2024-02-29",
                              "2024-02-29", meter_reading_hundredths=23456)
    replacement = ledger.revise_bill_amount(power, 21000, "Correct supplier amount")
    assert ledger.get_bill(replacement)["meter_reading_hundredths"] == 23456
    assert ledger.get_bill(power)["meter_reading_hundredths"] == 23456
    with pytest.raises(ValueError, match="cancelled"):
        ledger.set_meter_reading(power, 1)


def test_noop_meter_edit_does_not_add_audit_or_change_revision(account):
    ledger, _, power = account
    before = ledger.get_revision()
    ledger.set_meter_reading(power, None)
    assert ledger.get_revision() == before
    ledger.set_meter_reading(power, 0)
    with ledger._read() as connection:
        history = connection.execute(select(models.audit).where(
            models.audit.c.action == "update_meter_reading",
            models.audit.c.entity_id == power,
        )).mappings().all()
    assert len(history) == 1
    assert '"meter_reading_hundredths": 0' in history[0]["details"]
