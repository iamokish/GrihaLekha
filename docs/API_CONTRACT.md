# Internal service contract

Calendar dates are ISO `YYYY-MM-DD` strings; generation timestamps are ISO datetimes. All money values are integers in paise. Public writes raise `ValueError` for invalid input; unexpected database failures remain errors. Return plain dictionaries/lists, not ORM instances. UI catches validation errors and displays them. `clock.now_ist()` and `clock.today_ist()` use fixed UTC+05:30 without requiring a system timezone database.

Optional electricity readings are integer hundredths of kWh, independently of
money. `None` clears a reading; `0` records zero. `meter.parse_meter_reading`
accepts blank or nonnegative text with at most two decimal places, without rounding.
`meter.format_meter_reading` returns a plain two-decimal value or an empty string.

## Ledger

`Ledger(data_dir: Path)` exposes `.data_dir`, `.db_path` and `.close()` (dispose database connections). All mutation methods commit atomically. `get_revision() -> int` changes on financial/profile and display-currency changes for backup scheduling.

- `get_currency_code() -> str`: returns the account folder's display currency,
  defaulting to `INR` for missing or unsupported saved values.
- `set_currency_code(code)`: accepts exactly `INR`, `USD`, `EUR`, `JPY`, `GBP`,
  `CAD`, `AUD`, `SGD`, or `NZD`. Saves the `currency_code` settings row and audit
  entry atomically. Re-selecting the current valid value is a no-op. A valid
  selection repairs an unsupported saved value. No financial rows change.

Money APIs and `*_paise` fields retain integer hundredths for every currency,
including JPY. `money.format_money(paise, symbol=True, currency_code='INR')`
preserves Indian grouping and two decimals. `money.parse_money(value,
currency_code='INR')` accepts plain amounts or that currency's symbol/code prefix,
with the same precision and validation as before. Currency arguments are keyword
only. `decimal_rupees` retains its compatibility name and returns the unchanged
plain two-decimal value. Desktop amount editors continue to require plain
decimals without symbols or commas.

Archived accounts remain readable by ID, including their bills, receipts and
statements. Financial/profile mutations require reopening the account first.
Automatic draft preparation skips archived and cancelled accounts. A cancelled
tenancy remains a settlement account: its marker never implicitly restores
occupancy, and new monthly service charges, tenancy moves, rent changes, and
balance-to-deposit transfers are blocked. Opening dues retain their settlement
and correction paths; they are not charges for the cancelled stay.

- `list_buildings() -> list[dict]`: id, name, address.
- `add_building(name, address) -> int`.
- `remove_building(building_id)` deletes only empty buildings and records their details in the audit history.
- `list_flats() -> list[dict]`: id, building_id, building_name, address, number, floor, bhk, tenant_name (or empty), tenancy_id (or None). Occupancy is resolved for today from dated flat history; a future transfer does not move the current occupant early.
- `add_flat(building_id, number, floor, bhk) -> int`.
- `remove_flat(flat_id)` deletes only flats without any tenancy history (including former/upcoming tenants); the deletion is audited atomically.
- `list_tenancies(flat_id=None, include_archived=False) -> list[dict]`: id, flat_id, tenant_name, phone, move_in, move_out, opening_date, archived_on, cancelled_on, cancellation_reason, billing_day, grace_days, rent_paise, members, rent_rates, occupancy_history; plus building_id, building_name, address, number, floor, bhk. Archived accounts are excluded unless requested. Cancelled accounts remain visible for settlement; their preserved occupancy rows do not reserve a flat. A flat filter matches any recorded occupancy in that flat, including former occupants.
- `get_tenancy(tenancy_id) -> dict` returns the same fields, including an archived account. The displayed flat is the latest occupancy that has begun, or the first planned occupancy for an upcoming tenant. `occupancy_history` is ordered by `starts_on`; each row includes id, tenancy_id, flat_id, starts_on, ends_on (nullable, inclusive), rent_rate_id and paired_tenancy_id (nullable), plus building_id, building_name, address, number, floor, bhk. `archived_on` is null for open accounts.
- `add_tenancy(flat_id, tenant_name, move_in, billing_day, rent_paise, grace_days=0, phone='', members=None, opening_date=None, initial_meter_reading_hundredths=None) -> int`. Members are dictionaries with name, relationship. Opening date is account start; do not draft periods before it. The optional initial meter reading describes move-in and is returned by tenancy reads.
- `update_tenancy(tenancy_id, tenant_name, phone='', members=None, grace_days=0, *, initial_meter_reading_hundredths=UNSET)` updates contact/family and optional initial reading. Omitting the reading preserves it; explicitly passing `None` clears it.
- `change_rent(tenancy_id, effective_from, rent_paise)` validates the chosen future cycle boundary, preserves old charges/rates.
- `prepare_rent_drafts(as_of=None) -> int`: legacy rent-only operation; idempotent, returns number created. Uses today in IST when omitted.
- `prepare_monthly_drafts(as_of=None) -> int`: prepares required rent and electricity through the requested date, defaulting to today in IST, and returns the number of drafts created or adjusted. Rent uses saved rates; electricity starts with an unknown amount requiring review, while an explicitly posted zero amount is valid. Existing manual electricity bills prevent duplicate automatic drafts. Historical cancellations within occupancy are repaired with linked replacement drafts; required bills cannot be manually cancelled. Changed occupancy can trim, stop or restore automatic electricity drafts, resetting changed amounts for review. Move-out operations explicitly correct affected posted automatic electricity bills before preparation. Repeating without changes returns zero.
- `list_charges(tenancy_id, as_of=None, *, include_deleted=False) -> list[dict]`: id, tenancy_id, kind (`rent`, `electricity`, `misc`), amount_paise (nullable for electricity or manual partial drafts), period_start, period_end (inclusive), charge_date, due_date, description, status (`draft`, `posted`, `void`), paid_paise, outstanding_paise, needs_amount (bool), opening (bool), cycle_start and electricity_cycle_start (nullable automatic-cycle keys). Draft amounts do not count as debt. `deleted_on` is nullable; deleted bills are omitted by default even for an explicit historical cutoff. Internal callers can request `include_deleted=True`.
- `get_bill(charge_id, as_of=None) -> dict`: charge fields above, plus `billing_cycle_start`, `payment_locked` and `payments`, an ordered list of allocations effective by the cutoff with payment date, method, reference, cancellation and reversal details. Defaults to today in IST. Posted and cancelled bills can be reopened for inspection; cancellation/reversal dates remain available for interpreting history. Lock state is the current saved state. Deleted bills remain readable by ID for internal history, with `deleted_on` retained.
- `list_bill_cycles(tenancy_id, as_of=None, *, include_deleted=False, include_stopped=False) -> list[dict]`: groups existing bills in cycle order. Each group contains `cycle_start`, `cycle_end`, `period_start`, `period_end`, `locked`, `charges`, `total_paise`, `paid_paise`, `pending_paise`, a `review_pending` count, `moved_out_cycle` and `last_occupied_day`. Child charges include `billing_cycle_start` and `payment_locked`. The cutoff defaults to today in IST; passing `as_of` excludes bills dated later than that date. Deleted bills are hidden unless `include_deleted=True`; removed post-departure bills are hidden unless `include_stopped=True` or they were active at an explicit historical cutoff.
- `set_cycle_payment_lock(tenancy_id, cycle_start, locked)`: saves an audited boolean lock for an existing cycle boundary; setting the same state twice is a no-op. A locked cycle rejects bill payment changes, explicit bill additions/posting/cancellation and reversals affecting its paid bills until unlocked. Receiving money into balance is allowed. Locks do not change bill amounts or payment status.
- `add_charge(tenancy_id, kind, amount_paise, period_start, period_end, due_date, description='', charge_date=None, draft=False, opening=False, apply_credit=False, meter_reading_hundredths=None) -> int`. A nonempty reading is accepted only for electricity; batch charge items accept the same field.
- `post_charge(charge_id, amount_paise=None, apply_credit=False, *, meter_reading_hundredths=UNSET)`: confirms a reviewed amount, including zero; by default posting never consumes balance. The UI always leaves `apply_credit` false. A supplied reading is saved atomically with posting; omission retains the saved reading. Posting a normal automatic electricity draft includes consecutive preceding uncovered deferred cycles, stopping at any missing, unreviewed, or separately billed cycle. All affected cycles must be unlocked. A deferred source itself must first have its deferral undone.
- `set_meter_reading(charge_id, meter_reading_hundredths)`: updates only the reading on an electricity draft or posted bill, including a paid or cycle-locked bill. Blank clears it; setting the same value is a no-op. Rejects archived accounts, cancelled/deleted bills, non-electricity charges, and nonempty readings on deferred cycles. Records the previous and new reading in audit history; does not change financial dates, amounts, or allocations.
- `set_electricity_deferred(charge_id, deferred)`: requires a boolean, an open noncancelled account, and an unlocked unposted automatic monthly electricity draft. Deferring clears amount and reading and leaves a nonfinancial draft marker. Undoing allows separate review unless an active later bill already covers it. Existing overlapping electricity bills prevent deferral. Repeated identical changes are no-ops. Uncovered deferrals prevent archival; covered deferred records do not. Corrections move coverage links to the replacement, while voided covering bills leave earlier occupied deferrals unresolved.

Electricity charge dictionaries include `meter_reading_hundredths`,
`electricity_deferred`, `electricity_covered_by_charge_id`, and derived
`electricity_covered_by_cycle_start`, `coverage_period_start`, `coverage_period_end`,
`electricity_deferred_charge_ids`, and `electricity_deferred_cycle_starts`.
`affected_locked_cycle_starts` identifies locked cycles included in an electricity
bill, and its `payment_locked` also reflects locks on those earlier cycles.
`electricity_scheduled_covering_charge_id` identifies a recorded future covering
bill for UI navigation and edit guards, without treating it as current debt or
changing the cutoff-aware covering reference.
Coverage dates include earlier service without changing the combined bill's own
monthly anchor or duplicating its debt in earlier cycles. A target reference is
shown only when the covering bill is active at the requested cutoff. Physical
links to voided bills are retained internally until a later bill replaces them.
Audited coverage associations preserve earlier bill versions and cancellation
date boundaries even after corrections or undoing and reapplying deferral.
Reading edits are reference corrections; historical exports use the latest saved
reading, while monetary calculations retain their effective-date behavior.
- `void_charge(charge_id, reason, effective_on=None, *, prepare_replacement=False)` cancels miscellaneous charges with dated history and releases allocations to balance; requires an unlocked cycle. Rent/electricity are rejected regardless of `prepare_replacement`, which remains for caller compatibility. Internal occupancy corrections retain their own dated reversal path.
- `revise_bill_amount(charge_id, amount_paise, reason, effective_on=None) -> int`: atomically corrects a posted bill, including zero, and returns the linked replacement ID. Miscellaneous bills must have no applied money at the correction date; mark paid/partially paid fees Unpaid first. Existing rent/electricity corrections release applied money without consuming balance. Keeps the original bill, description, service/due dates, future scheduling, and linked history. Defaults to the later of today in IST and the bill's charge date; an explicit effective date must respect the charge and allocation chronology. Saving the existing amount is a no-op. Requires a reason, open account, and unlocked cycle. Drafts use `post_charge` instead.
- `restore_cancelled_bill(charge_id, reason, effective_on=None) -> int`: atomically returns the ID of a linked restored bill, keeping the original cancelled row and released allocations intact. A posted original returns with its original amount as Unpaid (zero remains fully paid); an unposted original returns as a draft. Reuses a single untouched automatic replacement when safe, without consuming balance. Defaults to the latest of today in IST, the original bill/cancellation dates and relevant linked corrections. Earlier effective dates, posted/edited replacements, repeated restoration, deleted bills/descendants and required bills outside the current recorded stay are rejected. The account and all affected cycles must be editable/unlocked.
- `replace_cancelled_bill(charge_id, items) -> list[int]`: atomically posts replacement lines and links every line to the cancelled original. Preserves charge type, account and opening-entry status; validates all affected cycle locks, linked history and cancellation dates. Rent/electricity require one replacement line; miscellaneous fees may use multiple same-kind lines. Active or deleted descendants block another replacement. No balance is consumed. The UI uses this operation for manual replacements; generic additions are not inferred to be replacements.
- `delete_bill(charge_id, reason, effective_on=None)`: removes only a `misc` bill from normal lists and new tenant documents. Requires a reason, an open account and an unlocked cycle. Cancels the financial bill, releases allocations to balance, marks `deleted_on`, and records the original bill in the audit log in one transaction; receipt and bill rows remain stored. Rent/electricity and already deleted bills are rejected. The deletion date defaults to today in IST and cannot be future-dated. A future bill may be hidden now with its financial cancellation effective no earlier than its bill date; a later scheduled cancellation must take effect before deletion.

- `deposit_payment(tenancy_id, amount_paise, paid_on, method, reference='', notes='', proof_path=None) -> int`: records a positive receipt into available account balance without paying any bills. This is the UI's Add payment operation, distinct from the security-deposit register. Validates the actual receipt date against today in IST and copies an optional proof atomically.
- `mark_bill_paid(charge_id, effective_on=None) -> int`: pays a posted bill's full remaining amount from existing available balance, possibly across multiple receipts, and returns the amount applied. Insufficient balance rolls back the entire operation. Defaults to today in IST; rejects future dates or dates before the charge, uses only receipts available by the effective date, and protects later allocation/refund history from overspending. Already paid and zero-value bills return zero. Requires an unlocked cycle.
- `mark_bill_unpaid(charge_id, reason, effective_on=None, *, keep_credit=True)`: cancels all active allocations to the posted bill and returns their money to balance. Receipts, other bills and historical cutoffs are preserved; no refund or new receipt is created. A nonempty reason is required; the correction cannot precede affected payments or lie after today in IST. Requires an unlocked cycle. `keep_credit` remains accepted for caller compatibility; receipt retention is always enabled.
- `pay_bill(charge_id, amount_paise, paid_on, method, reference='', notes='', proof_path=None) -> int`: compatibility API for atomically receiving and applying a payment to one bill. The positive amount must equal its entire remaining amount; partial payments and future receipt dates are rejected. Returns the receipt ID and copies an optional proof. The current UI uses `deposit_payment` followed by `mark_bill_paid` instead.
- `list_payments(tenancy_id, as_of=None) -> list[dict]`: id, tenancy_id, amount_paise, paid_on, method (`cash`, `bank`, `upi`, `opening`, `deposit`), reference, notes, proof_path (relative or empty), available_paise, reversed_on, opening, allocations (list of charge_id, amount_paise, effective_on).
- `record_payment(tenancy_id, amount_paise, paid_on, method, reference='', notes='', allocations=None, proof_path=None, opening=False) -> int`: `None` or `{}` retains the receipt as available balance. Explicit `{charge_id: paise}` mappings remain supported for compatibility, but each positive amount must pay that bill's full remaining amount. Validate scope, limits, dates and cycle locks. Copy supplied external proof into managed storage within this operation; nonopening receipts cannot have a future date.
- `apply_credit(tenancy_id, allocations=None, effective_on=None)`: compatibility operation for explicitly applying balance. A mapping must pay each selected bill in full; `None` considers oldest posted dues and leaves a bill unchanged if funds cannot cover its entire remaining amount. Neither receipt entry nor posting calls this automatically by default; the current UI has no allocation editor.
- `reverse_payment(payment_id, reason, effective_on=None)` corrects a mistaken receipt and releases its allocations without losing cutoff history. Locked affected cycles must be unlocked. If a bill also uses another receipt, mark that bill Unpaid first so reversing one receipt cannot leave a new partial payment. Active transfers from that receipt into deposits must be reversed first; the receipt correction cannot precede those transfer reversals.
- `refund_credit(tenancy_id, amount_paise, paid_on, method, reason) -> int`: cannot exceed unallocated credit; no fake reversal.
- `record_deposit(tenancy_id, amount_paise, paid_on, action, method='cash', notes='', opening=False) -> int`: action `receive`, `return`, `apply`; apply makes an explicit transfer to account balance without paying bills or recording new cash. Zero input is accepted as a no-op where appropriate.
- `transfer_balance_to_deposit(tenancy_id, amount_paise, paid_on, notes) -> int`: atomically moves a positive amount of existing receipt credit into the deposit held and returns the deposit movement ID. Requires a reason, an open noncancelled account, and a date from account opening through today. Uses only receipt funds available at the chosen date and every later allocation/refund/transfer boundary. Stores an internal deposit receipt with `deposit_sources` links; creates no cash receipt or refund. Insufficient funds roll back the entire operation.
- `reverse_deposit(deposit_id, reason, effective_on=None)`: reverses a deposit movement with dated history. Reversing an internal balance-to-deposit receipt releases its original receipt sources back to available balance. Deposit receipts can be reversed only when sufficient deposit remains at the correction date and all later boundaries. Reversing a deposit-to-balance movement also reverses its linked internal payment, respecting its downstream uses.
- `preview_cancel_upcoming_tenancy(tenancy_id) -> dict`: read-only preview for an open, never-started tenancy with `move_in` after today. Rejects already-cancelled accounts and linked planned exchanges. Returns `revision`, `tenancy_id`, `tenant_name`, `planned_move_in`, `cancelled_on`, `released_flat_ids`, `cancelled_bill_ids`, `draft_bill_ids`, `posted_bill_ids`, `remaining_bill_ids`, `returned_paise`, `locked_cycle_starts`, current/future credit and deposit amounts, and `advanced_cancellations` for safe legacy scheduled corrections.
- `cancel_upcoming_tenancy(tenancy_id, reason, *, expected_revision=None) -> dict`: recomputes that plan in one write transaction, checks locks/history, releases all flat reservations, and stores `cancelled_on` plus `cancellation_reason`. Passing the reviewed `revision` rejects a stale approval before writes; the UI supplies it. Cancels nonopening rent/electricity for the unstarted stay without creating replacement drafts; cancellation dates never precede charge/allocation history. Retains miscellaneous fees, opening dues, receipt/deposit records, occupancy history, issued documents, and the account for settlement. No cash refund occurs. Returns the same effect summary. Rebooking requires a new tenancy.
- `preview_move_out(tenancy_id, last_day, final_rent_paise) -> dict`: reads the final cycle, stopped/corrected bill IDs, stopped cycle starts, returned balance and affected locks without changing data. Also identifies automatic electricity bills requiring review.
- `move_out(tenancy_id, last_day, final_rent_paise, reason='Final rent agreed at move-out') -> dict`: records or revises the inclusive last occupied day and returns the same effects summary. Removes nonopening bills starting after departure with dated reversals, returning applied money while preserving receipts and earlier balances. Corrects final rent; changed automatic electricity periods require review. Extensions restore monthly drafts. A departure before account opening requires zero final rent and preserves opening dues. Checks affected cycle locks, flat overlaps and latest transfer date atomically.
- `preview_cancel_move_out(tenancy_id) -> dict` and `cancel_scheduled_move_out(tenancy_id, reason='Cancelled planned move-out') -> dict`: preview or cancel a future departure. Validate indefinite flat availability and locks; restore monthly drafts through previously prepared periods. Changed posted final bills are reversed, returning applied money without repaying replacements. Reject completed departures. Summary includes `previous_last_day`, `corrected_bill_ids`, `electricity_review_bill_ids`, `returned_paise`, `locked_cycle_starts`, and `resumed_through`.
- `transfer_tenancy(tenancy_id, flat_id, effective_on, rent_paise=None, reason='Flat transfer') -> dict`: appends dated occupancy in a different flat, retaining the same tenant account, payments, debt, credit and deposits. The date must follow the latest occupancy start and fall within the tenancy. An optional new monthly rent starts at the transfer date if it is a billing-cycle boundary, otherwise at the next full cycle; there is no automatic mid-cycle proration. Conflicting occupancy, scheduled rates or posted affected rent are rejected. Returns `{'rent_effective_from': date_or_none}`.
- `exchange_tenancies(first_id, second_id, effective_on, first_rent_paise=None, second_rent_paise=None, reason='Flat exchange') -> dict`: atomically swaps both households' future occupancy using the same transfer/rate rules, with no money moved between their accounts. Returns `{tenancy_id: rent_effective_date_or_none}` for both tenants.
- `cancel_scheduled_transfer(tenancy_id, reason='Cancelled planned flat transfer') -> list[int]`: cancels the latest never-started move and restores the prior reservation. Linked exchanges cancel both plans atomically. Linked future rent rates are removed and unposted rent suggestions recalculated; posted affected bills receive dated corrections and replacement drafts, respecting locks and allocation dates. A new reservation in the original flat can prevent cancellation. Historical moves are retained. Returns affected tenancy IDs.
- `archive_tenancy(tenancy_id)`: closes a departed or cancelled account only after all retained drafts are reviewed and unpaid bills, available balance, and deposits are zero both today and across future dated entries. For an occupied stay, prepares missing/replacement required drafts before checking; cancelling a bill cannot bypass required review. A cancelled never-started stay does not acquire new required drafts. Explicit zero-value electricity is valid. History is retained and mutations are blocked until reopening.
- `unarchive_tenancy(tenancy_id)`: reopens an archived account without changing departure, cancellation, or financial history. Reopening a cancelled account allows settlement; it does not restore a reservation or resume monthly billing.
- `prepare_tenancy_deletion(tenancy_id) -> dict`: validates an archived, departed-or-cancelled, settled account and captures its revision, full history tables including `deposit_sources`, statement and row counts for review. Rejects cross-account financial references.
- `export_tenancy_deletion_csv(preview, destination) -> dict`: atomically exports a ZIP of full CSV history, readable statement tables and payment proofs. `tenancy_deletion_html(preview, language)` generates PDF source using the existing renderer. `tenancy_deletion_backup_file(preview, path, kind='pdf')` verifies completed output and registers a revision-bound checksum artifact.
- `delete_archived_tenancy(tenancy_id, *, expected_revision, backups=()) -> dict`: deletes only the reviewed archived account in one transaction, then removes unshared owned proofs. A nonempty backup list requires both verified CSV and PDF artifacts; an empty list is reserved for an explicit skip-export choice. Stale revisions or modified exports abort deletion. Other tenants, buildings/flats, shared proofs, issued PDFs and existing backups remain. Returns `deleted_tenancy_id` and any `cleanup_warnings`.
- `balance(tenancy_id, as_of=None) -> dict`: billed_paise, paid_paise, outstanding_paise, overdue_paise, credit_paise, net_paise (positive payable, negative credit), deposit_paise.
- `dashboard(as_of=None) -> dict`: outstanding_paise, overdue_paise, credit_paise, deposit_paise, occupied_flats, total_flats, draft_count.
- `statement(tenancy_id, start, end, *, as_of=None) -> dict`: see below. UI exports omit `as_of` to show current IST balances; explicit cutoffs remain available for accounting/history callers.

## Statement snapshot

Dictionary: `tenancy` (above, with property details resolved at the balance cutoff), `start`, `end`, `generated_at`, `balance_as_of`, `balance_calculated_at`, `period_balance_as_of`, `opening_balance_paise`, `period_closing_balance_paise`, `closing_balance_paise`, `balance`, `charges`, `outstanding_charges`, `payments`, `refunds`, `adjustments`, `deposits`, `allocations`, `billing_cycles` and `billing_details`.

By default, `generated_at` and `balance_calculated_at` contain the current IST
datetime; `balance_as_of` is its calendar date. Current `balance`,
`closing_balance_paise`, bill amounts paid/pending and payment status all use
that cutoff, independently of the selected period. Selecting a past or future
end date does not move the current balance cutoff. An explicit `as_of` overrides
the accounting cutoff and sets `balance_calculated_at` to that date, while
`generated_at` still records the actual generation timestamp.

`period_balance_as_of` is the earlier of `end` and the balance cutoff.
`charges` contains posted charges in the transaction interval through that
date, with their payment/correction status evaluated at the balance cutoff.
`outstanding_charges` includes all currently unpaid posted bills, including
those outside the selected period. `payments`, `refunds`, `adjustments` and
`deposits` contain interval movements; `allocations` contains allocations active
at the balance cutoff. Charge dictionaries carry period/due dates and
billed/paid/outstanding amounts. Payment dictionaries carry actual dates,
methods and allocation data. Refund/deposit/adjustment rows have date or
paid_on/effective_on, amount_paise, reason/notes, action/kind as needed.

Internal balance-to-deposit receipts appear in `deposits` with method `internal`
and action `from_balance`. `balance.transferred_to_deposit_paise` reports the
active receipt credit reserved into deposits at the requested cutoff.
Their account-side movements use `balance_to_deposit` adjustments; reversals use
`balance_to_deposit_reversed`. These adjustments reconcile available credit
without increasing actual payment or cash-refund totals. Receipt
`available_paise` excludes active funds linked through `deposit_sources`.
Cancelled never-started tenancies do not gain expected monthly review cycles;
retained miscellaneous/opening settlement items remain reportable.
PDF headers identify cancelled upcoming tenancies and their cancellation reason;
CSV metadata includes `cancelled_on` and `cancellation_reason`.

Deleted bills are excluded from bill rows, cycle totals and allocation details
in all newly generated snapshots, including explicit historical cutoffs. Their
net selected-period charge/cancellation effect is consolidated into an
`account_adjustment` dated at the period end, with no bill ID, description or
deletion reason; a zero net effect adds no row. This retains exact dated opening,
period and current balances without exposing deleted bill details in PDFs/CSVs.
The raw ledger and previously issued documents remain unchanged.

`period_charges_paise`, `period_payments_paise`, `period_refunds_paise` and
`adjustments_paise` preserve transaction-date reconciliation:
`opening_balance_paise + period_charges_paise - period_payments_paise + period_refunds_paise + adjustments_paise == period_closing_balance_paise`.
Opening balance is measured on the day before `start`, capped at the balance
cutoff for a wholly future period. The difference between
`closing_balance_paise` and `period_closing_balance_paise` explains later
movements when a past reporting period is selected.
Deposits remain separate. A tenant-facing amount left to pay is
`max(0, closing_balance_paise)`; a negative net balance is shown separately as
advance after bills. Unreviewed amounts never inflate either total.

`billing_cycles` covers every occupied monthly cycle intersecting the reporting
period, including missing/unreviewed rent or electricity. Additional nonrent
service periods after departure can appear with a null `cycle_start`. A row has:

- `cycle_start`, `period_start`, `period_end`, `partial`, `moved_out_cycle`, `last_occupied_day`.
- `rent_paise`, `electricity_paise`, `misc_paise`, `billed_paise`, `paid_paise`, `pending_paise`, calculated only from bills effective at cutoff. A null rent/electricity amount means no posted amount, not a confirmed zero bill.
- `payment_status`: `paid`, `unpaid`, `not_billed` or `cancelled`. A cycle with any pending posted amount is `unpaid`, even when other bills in it are fully paid.
- `review_pending`, `review_pending_kinds`, `cancelled_kinds`, `rent_status`, `electricity_status`; `status` is `review_pending` when any expected bill needs review, otherwise the payment status. A period can have paid posted bills while another component still needs review.
- `charge_ids`, `different_service_dates`, and `locations` (occupancy-history rows intersecting that cycle, including both locations for a transfer within the period).

Existing partial allocations remain readable; individual bill/component status
can retain `partially_paid` to explain historical data. New writes must pay the
full remaining amount and never silently rewrite that history.

`billing_details` contains the deduplicated bills needed to explain those
cycles as well as the statement's transaction charges, including drafts and
cancelled entries. Its optional `billed_at_cutoff` flag distinguishes a bill
with an amount effective at cutoff. Each bill belongs to one cycle even when
its service dates span multiple cycles; service dates remain visible in the
detail rows. These summaries are read-only and do not create or post bills.

## Reports

Snapshots include `currency_code`, captured by `Ledger.statement()` in the same
read transaction as the account data. Reports validate that code and default to
INR for older snapshots with no currency field. All renderers use the snapshot
value, independent of later changes to the saved preference.

`statement_html(snapshot, language='en', document_id=None) -> str`; languages `en`, `hi`, `bilingual`.

`receipt_html(snapshot, payment_id, language='en', document_id=None) -> str`.

PDF headers render Issued and Balances as of with date, time and IST. Paid
status text is green and Unpaid is red, including translated labels. Period
reconciliation stays separate from the current Amount left to pay; a cycle
awaiting required review is not presented as fully Paid.

`export_csv(snapshot, destination: Path) -> list[Path]` writes clearly named UTF-8 BOM CSV tables to a destination directory. Snapshots with `billing_cycles` add a billing-cycles CSV with amounts paid/pending and review status. Existing financial CSV columns remain available alongside tenant-friendly amount/status columns.

CSV summary metadata identifies `currency_code`. Money-column suffixes use the
selected lowercase code (`amount_usd`, `rent_jpy`, etc.); INR keeps the existing
`*_inr` names. All numeric strings remain exact two-decimal values, and negative
generated amounts remain numeric while user text retains formula protection.
Archived-account CSV history retains its integer `*_paise` fields; its manifest
identifies the display currency used for the accompanying statement exports.

Statement headers and CSV metadata include move-in and the optional initial
meter reading. Electricity bill details print readings and combined coverage.
When readings or deferrals exist, an additional electricity-details CSV includes
unposted drafts and deferred references, without changing the transaction-date
scope of the financial charges CSV.

`PdfExportJob(html, destination: Path, parent=None)` QObject, signals `finished(str)` absolute saved path and `failed(str)`, method `.start()`. Caller retains reference until finished. Must emit failure and have timeout rather than hang. Application owns QApplication. PDF renderer can run in event loop without freezing UI; assets loaded locally. Root/UI will copy/record issued PDFs under data_dir/documents as needed.

## Schema and backup

The current SQLite schema is version 6. Opening a valid version-1, version-2,
version-3, version-4, or version-5 database creates a verified online SQLite snapshot under
`data_dir/migration-backups/` before an atomic upgrade. The snapshot includes
committed WAL data. Version 1 also gains archived-account state, electricity
cycle keys and dated occupancy history; versions below 3 gain cycle payment
locks; versions below 4 gain nullable `charges.deleted_on`. Version 5 adds
nullable `tenancies.cancelled_on` and `cancellation_reason`, plus
`deposit_sources(id, deposit_id, payment_id, amount_paise)` with positive amounts,
foreign keys, and a unique deposit/payment pair. Existing deposits are unchanged;
only new balance-to-deposit transfers receive source links. Version 6 adds
nullable integer `tenancies.initial_meter_reading_hundredths` and
`charges.meter_reading_hundredths`, a nonnullable `charges.electricity_deferred`
boolean defaulting to false, and indexed nullable
`charges.electricity_covered_by_charge_id` referencing charges with RESTRICT.
Readings are bounded to 0–99,999,999,999,999 hundredths. Existing financial
rows, historical partial payments and cycle locks are retained. A failed upgrade
rolls back schema changes and leaves the safety copy available. Unknown,
incomplete, corrupt or newer schemas are rejected.

`BackupManager(data_dir: Path, db_path: Path)`; `.create(destination=None) -> Path` creates complete verified ZIP, retaining latest two only for automatic managed backups. `.list_backups() -> list[Path]`. `.validate(path) -> dict` verifies checksums, required database tables/columns, references and version. `.restore(path) -> Path` is called only after Ledger.close(), stages validated source and preserves live data; it returns the safety backup (or the recovery folder when prior data cannot be verified). Caller recreates Ledger after restore. `.latest_status() -> str`. Files include DB, attachments/ and documents/ directories. No folders outside data_dir or archive traversal permitted. The UI runs validation/restoration in a worker, prevents concurrent backups and shows an import review before replacing data.

Version-1 through version-6 backup databases are validated against their
respective required tables/columns. Restoring an older ZIP preserves its schema
until the next `Ledger` open performs the upgrade. Current accounts create
version-6 backup manifests including readings, coverage, transfer sources, and cancellation history. Migration snapshots are
separate from the two rotating managed ZIP restore points.

## UI

`grihalekha.ui.MainWindow(ledger)` exposes the desktop workflows. `__main__.py`
owns CLI startup, application-data paths, the single-instance lock, fonts and
logging. The UI owns automatic/close backups, issued PDF copies, and recovery
feedback through `BackupManager`.

The UI uses Add payment → available balance → Mark Paid from Balance, with
whole-bill payments and Mark Unpaid returning applied money to balance. Bills
are grouped by cycle with Open/review and Lock/Unlock controls above the table;
cancellation, replacement and draft preparation are under Bill actions. The
same menu offers **Undo bill cancellation…** and **Delete miscellaneous bill…**;
bill details expose **Undo cancellation…** and **Delete bill…** where eligible.
**Correct bill amount…** opens draft review or posted-bill correction. Unpaid
miscellaneous bills are supported; paid miscellaneous bills must be marked
Unpaid first. **Account actions → Transfer balance to security deposit** collects
a date, positive amount, and reason without a cash-method field. **Cancel
upcoming tenant…** previews affected reservations, bills, and money, then requires
a reason and confirmation. Cancelled accounts are visibly labelled and keep
settlement actions while reservation changes remain disabled.
Cancellation undo and fee deletion collect a reason, respect cycle locks and archived accounts,
and preserve received money and dated history. Scheduled cancellations can also
be undone; the restored bill retains its future effective date. Calendar hover
uses native viewport cell geometry, preserving adjacent-month dates,
selection, date limits, keyboard behavior and high-DPI rendering while repainting
only the affected cells. Neutral charcoal styling, corrected dropdown borders,
persistent column resizing and building-filtered flats remain in place.
