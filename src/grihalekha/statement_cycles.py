"""Read-only monthly billing summaries for tenant statements.

Unreviewed or missing bills are shown explicitly and never contribute to money
owed. Each bill belongs to one cycle, even when its service dates span cycles.
"""

from .dates import cycle_bounds, iter_account_periods


def payment_status(paid: int, pending: int) -> str:
    if pending <= 0:
        return "paid"
    return "partially_paid" if paid > 0 else "unpaid"


def billing_cycles(tenancy: dict, start: str, end: str, charges: list[dict], *, require_monthly: bool = False) -> list[dict]:
    moved_out_cycle = (cycle_bounds(tenancy["move_out"], tenancy["billing_day"])[0].isoformat()
                       if tenancy.get("move_out") else None)
    periods = {
        period["cycle_start"]: period
        for period in iter_account_periods(tenancy, end)
        if period["period_end"] >= start
    }
    rows = {key: [] for key in periods}
    for charge in charges:
        if charge.get("deleted_on"):
            continue
        if (tenancy.get("cancelled_on") and not charge.get("opening")
                and charge["kind"] in {"rent", "electricity"}):
            continue
        anchor = charge.get("cycle_start") or charge.get("electricity_cycle_start")
        if not anchor:
            anchor = cycle_bounds(charge["period_start"], tenancy["billing_day"])[0].isoformat()
        if anchor not in rows:
            if charge.get("status") == "void":
                # Automatically removed drafts outside recorded occupancy do
                # not create new monthly rows after the tenant has left.
                continue
            # Late fees or electricity after departure still need an explicit
            # service-period row, without inventing a new monthly rent bill.
            key = (charge["period_start"], charge["period_end"])
            if key not in rows:
                periods[key] = {
                    "cycle_start": None,
                    "period_start": key[0],
                    "period_end": key[1],
                    "partial": True,
                }
                rows[key] = []
            rows[key].append(charge)
        else:
            rows[anchor].append(charge)

    result = []
    for key, period in periods.items():
        items = rows[key]
        expected = ("rent", "electricity") if period["cycle_start"] else ()
        posted = [item for item in items if item.get("status") != "void" and item.get("billed_at_cutoff", item.get("status") == "posted")]
        deferred = [item for item in items if item.get("status") == "draft"
                    and item.get("electricity_deferred")]
        draft = [item for item in items if item.get("status") == "draft"
                 and not item.get("electricity_deferred")]
        cancelled = [
            kind for kind in ("rent", "electricity", "misc")
            if any(item["kind"] == kind and item.get("status") == "void" for item in items)
            and not any(item["kind"] == kind and item.get("status") != "void" for item in items)
        ]
        review = [kind for kind in expected if (require_monthly or kind not in cancelled)
                  and not (kind == "electricity" and deferred)
                  and not any(item["kind"] == kind and item.get("status") == "posted" for item in items)]
        review.extend(item["kind"] for item in draft if item["kind"] not in review)
        amounts = {
            kind: sum(item["amount_paise"] for item in posted if item["kind"] == kind)
            if any(item["kind"] == kind for item in posted) else (0 if kind == "misc" else None)
            for kind in ("rent", "electricity", "misc")
        }
        paid = sum(item["paid_paise"] for item in posted)
        pending = sum(item["outstanding_paise"] for item in posted)
        # A cycle can contain paid rent and unpaid electricity. The cycle is
        # unpaid until every posted bill is paid; historical partial amounts
        # remain visible on their individual bill rows.
        status = ("paid" if pending <= 0 else "unpaid") if posted else "cancelled" if cancelled and not draft else "not_billed"
        row = {
            **period,
            "moved_out_cycle": bool(moved_out_cycle and period["cycle_start"] == moved_out_cycle),
            "last_occupied_day": tenancy["move_out"] if moved_out_cycle and period["cycle_start"] == moved_out_cycle else None,
            **{kind + "_paise": amount for kind, amount in amounts.items()},
            "billed_paise": sum(item["amount_paise"] for item in posted),
            "paid_paise": paid,
            "pending_paise": pending,
            "payment_status": status,
            "review_pending": bool(review),
            "review_pending_kinds": review,
            "cancelled_kinds": cancelled,
            "status": "review_pending" if review else "deferred" if deferred and pending <= 0 else status,
            "electricity_deferred": bool(deferred),
            "electricity_covered_by_charge_ids": sorted({
                item["electricity_covered_by_charge_id"] for item in deferred
                if item.get("electricity_covered_by_charge_id")}),
            "charge_ids": [item["id"] for item in items],
            "different_service_dates": any(
                (item["period_start"], item["period_end"]) != (period["period_start"], period["period_end"])
                for item in items
            ),
        }
        for kind in ("rent", "electricity"):
            component = [item for item in posted if item["kind"] == kind]
            row[kind + "_status"] = (
                "deferred" if kind == "electricity" and deferred else
                "review_pending" if kind in review else "cancelled" if kind in cancelled else
                payment_status(sum(item["paid_paise"] for item in component), sum(item["outstanding_paise"] for item in component))
                if component else "not_billed"
            )
        row["locations"] = [
            dict(location)
            for location in tenancy.get("occupancy_history", [])
            if not tenancy.get("cancelled_on") and location["starts_on"] <= period["period_end"]
            and (location.get("ends_on") is None or location["ends_on"] >= period["period_start"])
        ]
        result.append(row)
    return sorted(result, key=lambda row: (row["period_start"], row["period_end"]))
