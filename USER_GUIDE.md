# Using GrihaLekha

## First setup

1. Open **Buildings & flats**, choose **+ Building**, and enter its name and full
   address.
2. Choose **+ Flat**. Enter the flat number, floor and BHK type. Repeat for each
   flat. Add more buildings or flats later using the same controls.
3. Choose **Add tenant**. Select the flat, enter the primary tenant's name,
   optional phone, household members, move-in date, monthly rent and billing day.
   Optionally enter the electric meter's initial reading on the move-in day.
4. If you already have records for this tenant, set the account opening date and
   use **Account actions → Set opening balances**. Enter unpaid amounts and any
   advance/deposit held on that date. Do not also re-enter the same historical
   bills and payments.

Enter amounts in the displayed currency: `8500` or `8500.50`, without commas
or currency symbols. Displays use two decimal places. More than two decimal
places are rejected rather than silently rounded.

Meter readings are cumulative kWh, with up to two decimal places (for example,
`1234.56`). Blank means not recorded; `0` is a recorded zero. Change or clear the
initial reading through **Edit household details**. Readings are reference information;
you enter electricity bill amounts yourself.

## Display currency

Choose **Display currency** in the sidebar. INR is the default for new accounts
and existing accounts without a saved preference.

| Currency | Code | Symbol |
| --- | --- | --- |
| Indian Rupee | INR | ₹ |
| US Dollar | USD | $ |
| Euro | EUR | € |
| Japanese Yen | JPY | ¥ |
| British Pound | GBP | £ |
| Canadian Dollar | CAD | C$ |
| Australian Dollar | AUD | A$ |
| Singapore Dollar | SGD | S$ |
| New Zealand Dollar | NZD | NZ$ |

This choice applies to every tenant in the current data folder, including new
statements, receipts, and CSV exports. It changes only symbols and currency
labels: `₹1,234.50` becomes `$1,234.50` when choosing USD. No exchange rate or
conversion is applied. Amounts, digit grouping, and two decimal places stay the
same, including for JPY. Names, notes, meter readings, dates, and report language
are unchanged. Examples elsewhere in this guide use the default INR.

The choice is saved when selected and included in complete backups. Restoring
a backup restores its currency preference; a backup without one uses INR.
Already issued PDF documents keep their original currency labels. CSV money
column names identify the selected code, such as `amount_usd`; numeric values
are unchanged. INR exports retain their existing `amount_inr` columns.

## Tables and dates

Drag the divider between column headings to adjust a table column. Your chosen
widths remain in place when the table refreshes or you switch tabs. Double-click
a divider to fit that column to its contents. Wide tables scroll horizontally.
In **Buildings & flats**, selecting a building in the top table filters the flats
below. Choose **All buildings** to see every flat again.

Dates can be typed directly or selected from the calendar dropdown. Use the
arrows to browse months, click the month or year to jump further, or choose
**Today**. The outlined date marks today; the filled date is your selection.
Moving over an available date highlights its number and background without
changing your selection. Unavailable dates stay disabled.
Press **Escape** to close the calendar.

## Monthly bills

Select the household under **Tenant accounts**. **Bills and monthly drafts**
groups bills under monthly cycle headings and automatically includes rent and
electricity for each occupied cycle. Double-click a draft or choose
**Open / review bill**. Rent uses the saved rate; enter that
month's electricity amount. Review and save to post the bill. A missing amount
stays **Review pending** and does not count as debt; enter zero explicitly when
nothing is owed. **Bill actions → Prepare missing drafts** catches up any missed
cycles. **Open / review bill** and **Lock cycle / Unlock cycle** stay above the
table; cancellation, undo, deletion, replacement and draft preparation are in
**Bill actions**.

Electricity draft review also accepts an optional meter reading. To save a
reading while the amount is still unknown, or correct a reading later, use
**Bill actions → Edit meter reading…**. Posted electricity bill details also
offer this action. Reading edits preserve the bill amount, payments, and
available balance, even when the cycle is locked. Reopen an archived account
before editing its readings.

To bill electricity less often, select its monthly draft and choose
**Skip electricity billing this cycle** in draft review, or **Bill actions →
Skip electricity this cycle…**. It becomes **Deferred**, with no amount or meter reading; skipping clears
any values previously entered on that draft. Rent continues as usual.

For example, defer January and February, then review March electricity. The
review shows January–March as the electricity service period. Enter the combined
amount and an optional latest meter reading. January and February link to that
one bill, so the amount is charged only once. Consecutive deferred cycles directly
before the bill are included automatically; a normal billed or unreviewed cycle
ends that group. Review months in order for a combined bill.

Use **Undo electricity deferral** to review a deferred month separately before
it is included in a posted bill. Once included, correct the combined bill's amount
if needed. Deferral and billing require unlocked affected cycles. Unbilled
deferred electricity must be resolved before archiving the account. If a move-out
change removes the combined bill, earlier occupied deferred months require
billing again.

Posted bills can be opened again to see their details and payment history.
Use **Add charges** for additional charges or miscellaneous fees; every
miscellaneous fee needs a reason. Existing manual electricity bills are retained.
Rent and electricity remain recorded for each occupied cycle and cannot be
cancelled or deleted. Electricity may be deferred into a later bill. Enter ₹0
explicitly when nothing is owed. To fix an amount, choose
**Correct amount…** in the bill details or **Bill actions → Correct bill amount…**.
This also opens draft review when the selected bill is still a draft. Posted
bill corrections keep the original amount and dated history; see **Corrections
and refunds** below. **Prepare missing drafts** also repairs required bills
missing from older records.

For example, a billing day of 5 gives 5 February–4 March. Rent is due on 4 March;
with zero grace days it passes its due date on 5 March. A billing day of 31 clamps
to the last available day in shorter months, then returns to 31 when possible.

## Payments, available balance and proofs

Choose **+ Add payment** when you receive money. Enter the actual receipt date,
amount and cash, bank transfer or UPI method; optionally add a reference and
proof image/PDF. Saving increases **Available balance**. It does not change any
bill's Paid/Unpaid status, so the same action also handles money received ahead
of billing. Future receipt dates and dates before the account opening date are
not accepted.

Open a posted bill and choose **Mark Paid from Balance**. Confirm the full
remaining amount to deduct from the available balance. If the balance is too
small, add the missing money first. New partial bill payments are not allowed.
Several receipts can fund one bill, and one receipt can fund several bills, but
each bill is paid in full when you mark it Paid. Posting a bill does not spend
the balance automatically.

For example, add ₹10,000, then mark a ₹10,000 rent bill Paid. The balance becomes
₹0 and an ₹800 electricity bill stays Unpaid. Add another ₹1,000 and mark the
electricity bill Paid; ₹200 remains available for another bill.

Select a cycle heading or any bill in that cycle and choose **Lock cycle** to
protect its payment status. Choose **Unlock cycle** before marking bills Paid
or Unpaid, posting/cancelling affected bills, or reversing a receipt that paid
them. You can still add payments to the tenant's balance while cycles are
locked. A lock does not mark any bill Paid.

Security deposits remain separate. **Account actions → Transfer deposit to
balance** explicitly moves deposit money into the available balance; open bills
and mark them Paid afterward. This transfer does not record new cash received.

To set aside money already in the balance, choose **Account actions → Transfer
balance to security deposit**. Enter the amount, transfer date, and reason.
For example, transferring ₹3,000 from a ₹10,000 available balance leaves ₹7,000
available and adds ₹3,000 to the deposit held. The original receipts remain;
there is no new receipt or cash refund. The amount must be available on the
chosen date without taking money already used by later bills, refunds, or
transfers. Future transfer dates are not accepted.

If that transfer was entered by mistake, select its deposit movement under
**Deposits and adjustments** and use
**Correct selected refund / deposit**. The correction returns the amount to its
original receipt balance and retains the transfer history. Money already used
or returned from the deposit must be resolved first. A receipt that funded an
active deposit transfer cannot be reversed until that transfer is corrected.

## Corrections and refunds

For an unpaid rent, electricity, or miscellaneous bill with the wrong amount,
select **Bill actions → Correct bill amount…**, enter the correct amount and a
reason, then save. The revised bill keeps its description, service period, and
due date; the original bill and correction remain in history. The revised bill
is Unpaid unless its amount is ₹0. Correcting a future bill retains its future
schedule. Unlock the cycle first; archived accounts must be reopened.

For a miscellaneous bill with applied money, choose **Mark Unpaid…** before
correcting its amount. Existing paid rent/electricity corrections return their
applied money to balance automatically; mark the revised bill Paid separately.
Saving the same amount leaves the bill unchanged.

Cancel an incorrect miscellaneous charge or reverse an incorrect payment with a date and
reason. The original entry remains in history. Reversing a payment reopens its
bills; cancelling a paid charge releases its money as credit.

To undo a cancellation, select the cancelled bill and choose **Bill actions →
Undo bill cancellation…**, or open it and choose **Undo cancellation…**. Enter
a reason. A previously posted bill returns at its original amount as Unpaid;
an unreviewed bill returns as a draft. A zero-value bill needs no payment. The
original cancelled record remains in history, and no money is deducted from
balance. An unused automatic replacement draft is reused. If a replacement
was already posted, correct that replacement first. The restored bill keeps
its service period and cannot take effect before the original bill or its
cancellation, including future-dated bills.

Use **Bill actions → Replace cancelled bill** when correcting its amount. Every
replacement is linked to the cancelled bill, so undo cannot duplicate an active
replacement. For older miscellaneous bills without saved links, the app asks
you to review later fees before undoing. Continue only when those are separate
fees and the original should also be restored.

To remove a miscellaneous fee, select it and choose **Bill actions → Delete
miscellaneous bill…**, or open it and choose **Delete bill…**. Enter a reason.
The bill disappears from the bills table and newly generated statements; any
applied money returns to the available balance. Receipts and internal history
are retained. Rent and electricity cannot be deleted, and **Undo cancellation**
does not restore a deleted fee. Unlock the cycle and reopen an archived account
before either undoing a cancellation or deleting a bill.

Inside a paid bill, **Mark Unpaid…** returns its applied money to the available
balance while keeping the original receipts. Enter a reason; the correction uses
today's date in IST. Other paid bills are unaffected. Older partial-payment records remain
visible; you can pay their full remaining amount or return their applied money
to balance with the same actions.

For a receipt entered by mistake, use **Reverse payment** in the Payments tab.
If a bill used multiple receipts, mark that bill Unpaid before reversing one
receipt, so the correction cannot leave a new partial payment. Unlock affected
cycles first. Dated history and already issued statements remain intact.

To return real money to a tenant, choose **Account actions → Refund available
balance**. A payment reversal corrects a record; a refund records money actually
returned.

## Cancelling an upcoming tenant

Select the planned household under **Tenant accounts**, then choose **Account
actions → Cancel upcoming tenant…**. This is available only when the move-in
date is after today and the stay has never started. For a tenant who has already
started their stay, record a move-out instead.

Review the released flat reservations, affected rent/electricity bills, other
bills, and retained balances. Enter a reason and confirm. Unlock affected cycles
first. A linked planned flat exchange must be cancelled before cancelling the
tenant. If account data changes during the review, open a fresh preview before
confirming again.

Cancellation releases the flat for another tenant and stops future monthly
drafts. Nonopening rent and electricity bills for the never-started stay are
cancelled; miscellaneous fees and opening dues remain for review and settlement.
Any recorded receipts, available balance, and deposits remain in the account.
Cancellation does not issue a cash refund. Use **Refund available balance** or
**Return security deposit** to record money actually returned.

The account is labelled **Cancelled**, with the date and reason retained. Once
all remaining bills and money are settled, it can be closed and archived.
Reopening an archived cancelled account allows settlement changes; it does not
restore its reservation or restart billing. Create a new tenancy if the household
books a stay again.

## Rent changes and tenant departure

Use **Change monthly rent** with the effective cycle date. Previous posted bills
retain their original amounts.

Choose **Record / change move-out**, select the **Moved out cycle**, then enter
the exact last occupied day and agreed final rent, including ₹0. You can select
a cycle months in the past. The date cannot precede move-in or a later recorded
flat transfer. If departure predates account opening, enter older dues through
opening balances; no additional final rent is added.

Review the affected bills and returned balance before saving. Bills whose service
period starts after departure are removed, including paid bills: their applied
money returns to available balance and the original receipts remain. Earlier dues
remain payable, and **Add payment** still accepts money received months later.
Enter the actual receipt date and then mark the old bills Paid. Monthly drafts
stop after the final occupied cycle, which is labelled **Moved out cycle**.

Use the same action to extend or shorten a stay. Changed posted rent uses dated
corrections; changed automatic electricity periods return as drafts for review.
Manually entered electricity service periods retain their dates. No amount is
automatically prorated. Unlock any affected cycles before changing departure.
Create a separate tenancy for the next household.

Use **Cancel planned move-out** if a future departure is no longer happening.
Occupancy continues and monthly drafts resume. Review the restored bills; any
released money stays in balance. A conflicting new reservation prevents cancellation.

**Move to another flat** and **Exchange flats with another tenant** keep each
tenant's existing bills, payments, advances and deposits. Choose the first day
in the new flat. Optional new rent starts on that day if it is a cycle boundary,
otherwise on the next full cycle; the dialog shows the date. **Flat history**
records every location. **Cancel planned flat move** cancels a future move and
its linked rent change; an exchange cancels both plans together. Cancel an
incompatible planned move before shortening the stay past its date.

After departure, use **Close and archive account** once all bills have been reviewed
and dues, available balance and deposit have been settled. Required rent and
electricity bills must be reviewed or included in a later posted bill; post an explicit ₹0 electricity bill when
appropriate. **Show archived** reveals closed
accounts for viewing and statements. **Reopen archived account** enables changes.

To remove a settled archived account permanently, select it under **Show archived**
and choose **Account actions → Delete archived tenant data…**. Saving both CSV and
PDF records is selected by default. Choose a folder; the app creates a ZIP of
complete CSV history and payment proofs, plus a readable PDF. It asks for final
confirmation only after both exports finish. Cancelling either step preserves
the account. You can explicitly skip exports, then confirm deletion without them.
Existing application backups and issued PDFs remain. CSV/PDF records are for
reference; a full application backup is needed to restore a workspace.

## Statements and receipts

Use **Export statement** to select dates and English, Hindi or bilingual output.
Choose a PDF statement or CSV tables. Select a payment and choose **Payment
receipt** to produce a receipt.
Statements include the move-in date, any initial meter reading, and recorded
electricity bill readings. Deferred months show their status and linked combined
bill. CSV exports include an electricity details table when readings or deferrals
are present, including readings saved on drafts.
Every billing cycle intersecting the selected period is shown, including missing
or unreviewed bills. **Amount Paid**, **Bill Pending** and the status column show
what has been paid and what still needs attention. Paid appears in green and
Unpaid in red; a cycle remains Unpaid while any posted bill has money pending.
Review pending remains visible until required amounts have been confirmed.

**Issued** and **Balances as of** include the current date and time in India
Standard Time (IST). The selected dates choose which billing cycles and period
transactions to show; balances and bill payment status always reflect the time
the statement is generated. Selecting a future end date does not treat future
payments or bills as current. If a March bill is marked Paid in April, a March
statement generated afterward shows that bill as Paid.

**Amount left to pay** includes all currently pending bills and deducts the
available balance; deposits stay separate. Bills remain Unpaid until you mark
them Paid, even if the balance already covers them. Review-pending amounts are
excluded until confirmed. The selected period's opening balance and financial
movements reconcile separately, with later changes shown when needed to explain
the current amount. CSV export includes these separate totals and a billing-cycle
summary.

Balance-to-deposit transfers and their reversals appear as internal movements
with their dates and reasons. They change available balance and deposit held,
but do not increase actual receipt or cash-refund totals.

Deleted miscellaneous bills and their details are omitted from new statements
and CSV exports. A past-period statement may include a generic **Account
adjustment** so its opening balance and period transactions still reconcile;
that adjustment contains no deleted bill details. Previously issued PDFs stay
unchanged.

Hindi names and reasons must be entered in Hindi if that is how you want them
printed. Changing the template language translates labels, not personal data.
PDFs include bundled fonts and can be read without installing fonts separately.
Issued PDFs are kept with the application data so an already issued statement
is preserved after later corrections.

## Backups and moving to another computer

Use **Backups & data → Back up now** to create a verified snapshot. GrihaLekha
retains the latest two managed snapshots. It also backs up changed data every
30 minutes and before normal application exit.

Choose **Export to drive** for a complete ZIP you keep on an external drive.
That copy is independent of the two managed backups. To move to another
computer, install GrihaLekha there and choose **Import from backup…** on the
welcome screen or in **Backups & data**. Select the ZIP and review its creation
date, contents and size, then choose **Import and replace**. Proofs and saved
statements travel with the database. Import replaces the current workspace;
it does not merge two sets of accounts.

To return to a managed snapshot, select it under **Managed restore points** and
choose **Restore selected…** (or double-click the row). The same review appears.
Validation and import show progress while the app protects the current files.
A safety backup of healthy current data becomes a managed restore point; the
completion message shows the location of the saved copy.

Restoration replaces the active account data, so the application first verifies
the selected backup and protects current files. If the old database is already
damaged, an explicitly unverified `recovery-*` folder may remain for diagnosis.
It is not one of the two verified backups. A restore interrupted before
completion is rolled back on the next start.

The current database schema is version 6. It adds optional meter readings and
links from deferred electricity months to their combined bill, while retaining
existing financial history, deposits, and cycle payment locks. Opening a
version-1, version-2, version-3, version-4, or version-5 account first creates a verified
database snapshot in `migration-backups`, then upgrades it. These upgrade
snapshots are separate from the latest two managed ZIP
backups. Older backups remain supported and upgrade when opened after restoration.

## Tables and removing unused properties

Hovering or selecting a table entry highlights its entire row, including rows
with input fields. Columns keep their widths after changes, fill the available
space, and cannot be narrowed past their header titles. On smaller windows,
scroll horizontally to reach the remaining columns.

In **Buildings & flats**, select an unused flat and choose **Remove flat**.
Confirm its name before removal. Flats with any tenant history, including
former or upcoming tenants, cannot be removed. Their accounts stay available.
Remove or move all flats out of a building before choosing **Remove building**.
Both removals require confirmation and are recorded in the audit history.
