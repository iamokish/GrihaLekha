"""Replacement drafts wait for all linked or overlapping legacy corrections."""

from datetime import datetime, timedelta

import pytest

from grihalekha import clock
from grihalekha.dates import as_date
from grihalekha.ledger import Ledger
from legacy_ledger import cancel_historical_bill


@pytest.mark.parametrize("cycle,end", [("2024-01-01", "2024-01-31"), ("2024-02-01", "2024-02-29")])
def test_replacement_uses_latest_manual_cancellation_instead_of_old_anchor(tmp_path, monkeypatch, cycle, end):
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2024, 7, 10, 12, tzinfo=clock.IST))
    ledger = Ledger(tmp_path)
    try:
        building = ledger.add_building("House", "Address")
        flat = ledger.add_flat(building, "1", "Ground", 1)
        tenant = ledger.add_tenancy(flat, "Tenant", "2024-01-01", 1, 100_000)
        ledger.prepare_monthly_drafts(cycle)
        anchored = next(row for row in ledger.list_charges(tenant)
                        if row["kind"] == "electricity" and row["period_start"] == cycle)
        ledger.post_charge(anchored["id"], 2_000)
        cancelled_on = (as_date(cycle) + timedelta(days=4)).isoformat()
        charged_on = (as_date(cycle) + timedelta(days=5)).isoformat()
        cancel_historical_bill(ledger, anchored["id"], "Original correction", cancelled_on)
        # The second case crosses a monthly boundary, so the newer manual
        # cancellation must constrain the February replacement too.
        manual = ledger.add_charge(tenant, "electricity", 3_000, "2024-01-01", end, end,
                                   charge_date=charged_on)
        cancel_historical_bill(ledger, manual, "Scheduled legacy correction", "2024-07-20")
        before = ledger.balance(tenant)
        ledger.prepare_monthly_drafts(cycle)
        replacement = next(row for row in ledger.list_charges(tenant)
                           if row["kind"] == "electricity" and row["period_start"] == cycle
                           and row["status"] == "draft")
        assert replacement["charge_date"] == "2024-07-20"
        assert replacement["replaces_charge_id"] == manual
        assert replacement["electricity_cycle_start"] == cycle
        assert ledger.get_bill(anchored["id"])["electricity_cycle_start"] is None
        ledger.post_charge(replacement["id"], 1_000)
        assert ledger.balance(tenant) == before
        assert ledger.balance(tenant, "2024-07-20")["outstanding_paise"] == 1_000
        assert ledger.prepare_monthly_drafts(cycle) == 0
    finally:
        ledger.close()
