"""Offline, bilingual account documents and exact, spreadsheet-safe CSV exports.

All accounting is supplied by Ledger.statement(). This module never mutates the
ledger, re-allocates a payment, or recalculates a historical balance from today's
state. Monetary values crossing this boundary must be integer paise.
"""

from __future__ import annotations

import base64
import csv
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from html import escape
from html.parser import HTMLParser
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping
import uuid

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

from .clock import IST, now_ist, today_ist
from .currencies import DEFAULT_CURRENCY, get_currency
from .meter import format_meter_reading
from .money import format_money

_LOGGER = logging.getLogger(__name__)


_LABELS = {
    "statement": ("Account statement", "खाता विवरण"),
    "receipt": ("Payment receipt", "भुगतान रसीद"),
    "property": ("Property", "भवन"),
    "address": ("Address", "पता"),
    "flat": ("Flat", "फ्लैट"),
    "floor": ("Floor", "मंज़िल"),
    "tenant": ("Tenant", "किरायेदार"),
    "phone": ("Phone", "फ़ोन"),
    "move_in": ("Move-in date", "रहने की शुरुआत की तारीख"),
    "initial_meter_reading": ("Initial electricity meter reading", "बिजली मीटर की प्रारंभिक रीडिंग"),
    "meter_reading": ("Meter reading", "मीटर रीडिंग"),
    "electricity_coverage": ("Electricity service period", "बिजली सेवा की अवधि"),
    "deferred": ("Deferred", "बिल बाद में बनेगा"),
    "electricity_deferred": ("Electricity billing deferred", "बिजली का बिल बाद में बनेगा"),
    "included_in_bill": ("Included in bill", "इस बिल में शामिल"),
    "billed_in_cycle": ("Billed in cycle starting", "इस तारीख से शुरू अवधि में बिल बना"),
    "deferred_note": (
        "Deferred electricity is included in a later bill. No electricity amount is charged for the deferred cycle.",
        "स्थगित बिजली का शुल्क बाद के बिल में शामिल होता है। स्थगित अवधि में बिजली की कोई राशि नहीं जोड़ी जाती।",
    ),
    "document": ("Document number", "दस्तावेज़ संख्या"),
    "generated": ("Issued", "जारी करने की तारीख"),
    "period": ("Period", "अवधि"),
    "cutoff": ("Balances as of", "इस तारीख तक की शेष राशि"),
    "rent": ("Rent", "किराया"),
    "electricity": ("Electricity", "बिजली"),
    "misc": ("Other fees", "अन्य शुल्क"),
    "total": ("Total", "कुल"),
    "settled": ("Amount Paid", "भुगतान की गई राशि"),
    "outstanding": ("Bill Pending", "बिल की बकाया राशि"),
    "overdue": ("Past due date", "देय तारीख बीत चुकी है"),
    "advance": ("Available advance", "उपलब्ध अग्रिम"),
    "deposit": ("Security deposit held separately", "अलग रखी गई सुरक्षा जमा राशि"),
    "opening": ("Opening account balance", "प्रारंभिक खाता शेष"),
    "closing": ("Amount left to pay", "भुगतान के लिए शेष राशि"),
    "period_balance": ("Balance after listed transactions", "दिए गए लेनदेन के बाद खाता शेष"),
    "later_changes": ("Net account changes after this period", "इस अवधि के बाद खाते में शुद्ध बदलाव"),
    "payable": ("Amount left to pay", "भुगतान के लिए शेष राशि"),
    "credit": ("Advance remaining after bills", "बिलों के बाद बची अग्रिम राशि"),
    "charges": ("Charges in this statement", "इस विवरण के शुल्क"),
    "payments": ("Payments and account credits", "भुगतान और खाते में जमा राशि"),
    "refunds": ("Advance refunds", "अग्रिम की वापसी"),
    "adjustments": ("Corrections and balance transfers", "सुधार और शेष स्थानांतरण"),
    "deposits": ("Deposit movements — separate account", "जमा राशि का लेनदेन — अलग खाता"),
    "summary": ("Billing periods", "बिल की अवधियाँ"),
    "details": ("Bill details", "बिल का विवरण"),
    "carried": ("Earlier bills still pending", "पहले के बकाया बिल"),
    "other_pending": ("Other bills still pending", "अन्य बकाया बिल"),
    "description": ("Description / reason", "विवरण / कारण"),
    "due": ("Due", "देय तारीख"),
    "date": ("Date", "तारीख"),
    "amount": ("Amount", "राशि"),
    "method": ("Method", "माध्यम"),
    "reference": ("Reference", "संदर्भ"),
    "status": ("Status", "स्थिति"),
    "paid": ("Paid", "भुगतान हो गया"),
    "unpaid": ("Unpaid", "भुगतान नहीं हुआ"),
    "partially_paid": ("Partially paid", "आंशिक भुगतान"),
    "review_pending": ("Review pending", "समीक्षा बाकी है"),
    "cancelled": ("Cancelled", "रद्द किया गया"),
    "not_billed": ("Not billed by cutoff", "अंतिम तारीख तक बिल दर्ज नहीं हुआ"),
    "review_note": (
        "Review pending means the bill has not been confirmed. These amounts are excluded from the amount left to pay.",
        "समीक्षा बाकी होने का अर्थ है कि बिल की पुष्टि नहीं हुई है। ये राशियाँ भुगतान के लिए शेष राशि में शामिल नहीं हैं।",
    ),
    "service_dates": ("Different service dates; see bill details.", "सेवा की तारीखें अलग हैं; बिल का विवरण देखें।"),
    "payable_note": (
        "The amount left to pay includes all confirmed pending bills and deducts available advance. Security deposits are separate.",
        "भुगतान के लिए शेष राशि में सभी पुष्टि किए गए बकाया बिल शामिल हैं और उपलब्ध अग्रिम घटाया गया है। सुरक्षा जमा राशि अलग है।",
    ),
    "current_status_note": (
        "Balances and bill statuses are current as of the time above. Transactions are listed for the selected period; future-dated transactions are excluded.",
        "शेष राशि और बिल की स्थिति ऊपर दिए गए समय तक की हैं। लेनदेन चुनी गई अवधि के हैं; भविष्य की तारीख वाले लेनदेन शामिल नहीं हैं।",
    ),
    "applied": ("Applied to", "समायोजन"),
    "allocations": ("Payment allocation at cutoff", "अंतिम तारीख तक भुगतान का समायोजन"),
    "cash": ("Cash", "नकद"),
    "bank": ("Bank transfer", "बैंक हस्तांतरण"),
    "upi": ("UPI", "यूपीआई"),
    "opening_credit": ("Opening credit", "प्रारंभिक अग्रिम"),
    "deposit_credit": ("Deposit applied to account", "जमा राशि का खाते में समायोजन"),
    "posted": ("Posted", "दर्ज"),
    "void": ("Cancelled; correction listed below", "रद्द; सुधार नीचे दिया गया है"),
    "reversed": ("Reversed", "वापस लिया गया"),
    "receive": ("Received", "प्राप्त"),
    "return": ("Returned", "लौटाई गई"),
    "apply": ("Transferred to balance", "उपलब्ध शेष में स्थानांतरित"),
    "from_balance": ("Transferred from balance", "उपलब्ध शेष से स्थानांतरित"),
    "cancelled_tenancy": ("Cancelled upcoming tenancy", "आगामी किरायेदारी रद्द"),
    "balance_to_deposit": ("Balance transferred to security deposit", "उपलब्ध शेष से सुरक्षा जमा में स्थानांतरण"),
    "balance_to_deposit_reversed": ("Balance-to-deposit transfer reversed", "शेष से सुरक्षा जमा का स्थानांतरण वापस लिया गया"),
    "reverse": ("Deposit entry reversed", "जमा राशि की प्रविष्टि रद्द की गई"),
    "opening_deposit": ("Opening deposit held", "पहले से रखी गई जमा राशि"),
    "charge_cancelled": ("Bill cancelled", "बिल रद्द किया गया"),
    "account_adjustment": ("Account adjustment", "खाते का समायोजन"),
    "payment_reversed": ("Payment reversed", "भुगतान प्रविष्टि वापस ली गई"),
    "refund_reversed": ("Refund reversed", "वापसी की प्रविष्टि रद्द की गई"),
    "void_charge": ("Bill cancelled", "बिल रद्द किया गया"),
    "no_entries": ("No entries in this period.", "इस अवधि में कोई प्रविष्टि नहीं है।"),
    "currency": ("All amounts in Indian rupees (INR).", "सभी राशियाँ भारतीय रुपये (INR) में हैं।"),
    "negative": ("A negative account balance means advance credit.", "ऋणात्मक खाता शेष का अर्थ अग्रिम राशि है।"),
    "originals": (
        "Original charges are shown below; cancellations and corrections appear separately.",
        "मूल शुल्क नीचे दिखाए गए हैं; रद्द प्रविष्टियाँ और सुधार अलग दिए गए हैं।",
    ),
    "deposit_note": (
        "Security deposits are separate from the rent account and available advance.",
        "सुरक्षा जमा राशि किराये के खाते और उपलब्ध अग्रिम से अलग है।",
    ),
    "allocation_note": (
        "Allocations show which bills a payment settled; they are not additional money received.",
        "समायोजन बताता है कि भुगतान किन बिलों में लगा; यह अतिरिक्त प्राप्त राशि नहीं है।",
    ),
    "receipt_note": (
        "This receipt records the payment shown above. Account balances may include other transactions.",
        "यह रसीद ऊपर दिए गए भुगतान का विवरण है। खाते के शेष में अन्य लेनदेन भी शामिल हो सकते हैं।",
    ),
    "bill": ("Bill", "बिल"),
    "proof": ("Payment proof attached to account", "भुगतान का प्रमाण खाते में संलग्न है"),
}

_MONTHS_HI = ("जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर")
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LANGUAGES = {"en", "hi", "bilingual"}


def _paise(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Report amounts must be integer paise.")
    return value


def _decimal(value: int) -> str:
    value = _paise(value)
    sign = "-" if value < 0 else ""
    rupees, remainder = divmod(abs(value), 100)
    return f"{sign}{rupees}.{remainder:02d}"


def _money(value: int, currency_code: str = DEFAULT_CURRENCY) -> str:
    return format_money(_paise(value), currency_code=currency_code).replace("-", "−", 1)


def _text(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _multiline(value: Any) -> str:
    return _text(value).replace("\n", "<br>")


def _label(key: str, language: str) -> str:
    en, hi = _LABELS.get(key, (key.replace("_", " ").capitalize(), key.replace("_", " ")))
    if language == "hi":
        return _text(hi)
    if language == "bilingual":
        return f'{_text(en)}<span class="translation">{_text(hi)}</span>'
    return _text(en)


def _date(value: Any, language: str) -> str:
    if not value:
        return "—"
    raw = str(value)[:10]
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return _text(value)
    month = (_MONTHS_HI if language == "hi" else _MONTHS_EN)[parsed.month - 1]
    return f"{parsed.day:02d} {_text(month)} {parsed.year}"


def _timestamp(value: Any, language: str) -> str:
    """Display recorded timestamps in IST; old date-only snapshots stay dates."""
    if not value:
        return "—"
    raw = str(value)
    if len(raw) == 10:
        return _date(raw, language)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return _text(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(IST)
    return f"{_date(local.date(), language)}, {local:%H:%M:%S} IST"


def _status_label(status: str, language: str) -> str:
    label = _label(status, language)
    if status in {"paid", "unpaid"}:
        return f'<span class="payment-{status}">{label}</span>'
    return label


def _row_date(row: Mapping[str, Any]) -> Any:
    return row.get("paid_on") or row.get("effective_on") or row.get("date") or row.get("charge_date")


def _period(row: Mapping[str, Any], language: str) -> str:
    return f'{_date(row.get("period_start"), language)}<br><span class="quiet">– {_date(row.get("period_end"), language)}</span>'


def _amount(row: Mapping[str, Any], key: str = "amount_paise") -> int:
    value = row.get(key, 0)
    return _paise(value if value is not None else 0)


def _validate_snapshot(snapshot: Mapping[str, Any], language: str) -> None:
    get_currency(snapshot.get("currency_code", DEFAULT_CURRENCY))
    if language not in _LANGUAGES:
        raise ValueError("PDF language must be en, hi or bilingual.")
    if "tenancy" not in snapshot or not isinstance(snapshot["tenancy"], Mapping):
        raise ValueError("A statement must contain tenancy details.")
    for name in ("start", "end"):
        try:
            date.fromisoformat(str(snapshot[name]))
        except (ValueError, KeyError) as exc:
            raise ValueError(f"A statement needs a valid {name} date.") from exc
    if str(snapshot["start"]) > str(snapshot["end"]):
        raise ValueError("The statement start date must not follow its end date.")


@lru_cache(maxsize=1)
def _font_css() -> str:
    directory = Path(__file__).with_name("assets") / "fonts"
    rules = []
    for family, filename, weight in (("GL Devanagari", "NotoSansDevanagari-Regular.ttf", 400), ("GL Devanagari", "NotoSansDevanagari-Bold.ttf", 700), ("GL Noto", "NotoSans-Regular.ttf", 400), ("GL Noto", "NotoSans-Bold.ttf", 700)):
        path = directory / filename
        if not path.is_file():
            raise FileNotFoundError(f"Bundled PDF font is missing: {filename}")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(f"@font-face{{font-family:'{family}';src:url(data:font/ttf;base64,{encoded}) format('truetype');font-weight:{weight};font-style:normal;font-display:block;}}")
    return "".join(rules)


_CSS = """
@page { size: A4; margin: 14mm 12mm 15mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: 'GL Devanagari', 'GL Noto', sans-serif; font-size: 9pt; line-height: 1.48; color: #233b36; margin: 0; }
h1,h2,p { margin: 0; }
h1 { font-size: 22pt; font-weight: 700; letter-spacing: -.5px; }
h2 { font-size: 11pt; margin: 19px 0 8px; font-weight: 700; break-after: avoid; }
.brand { font-family: 'GL Noto', 'GL Devanagari', sans-serif; font-size: 13pt; color: #1f6b59; font-weight: 700; letter-spacing: 1px; margin-bottom: 8px; }
.heading { border-bottom: 2px solid #1f6b59; padding-bottom: 12px; }
.heading .translation { font-size: .7em; color: #48635b; font-weight: 500; }
.translation { display: inline; font-weight: 400; font-size: .89em; }
.translation::before { content: ' / '; }
.heading .translation, .data th .translation, .cards .translation { display: block; }
.heading .translation::before, .data th .translation::before, .cards .translation::before { content: ''; }
.meta { width: 100%; margin: 10px 0 13px; border-collapse: collapse; }
.meta td { width: 50%; vertical-align: top; padding: 5px 8px 5px 0; overflow-wrap: anywhere; }
.meta strong { font-weight: 600; }
.quiet { color: #61756e; }
.note { color: #61756e; margin: 8px 0; font-size: 8pt; }
.cards { width: 100%; border-collapse: separate; border-spacing: 5px 0; margin: 14px -5px; }
.cards td { width: 25%; background: #eef5ef; border: 1px solid #dfe9e0; border-radius: 5px; padding: 9px 7px; vertical-align: top; }
.cards .value { font-size: 12pt; font-weight: 700; margin-top: 3px; white-space: nowrap; }
.cards .caption { font-size: 7.5pt; color: #526d61; }
.data { width: 100%; border-collapse: collapse; table-layout: fixed; }
.data th { background: #edf3ee; color: #214b3b; font-size: 8pt; text-align: left; padding: 7px 5px; vertical-align: top; border-bottom: 1px solid #bed4c4; }
.data td { padding: 7px 5px; border-bottom: 1px solid #e2e9e3; vertical-align: top; overflow-wrap: anywhere; font-size: 8pt; }
.data .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.data thead { display: table-header-group; }
.data tfoot { display: table-row-group; }
.data tr { break-inside: avoid; page-break-inside: avoid; }
.data .total td { border-top: 1px solid #b1c7b9; background: #f4f7f2; font-weight: 700; }
.periods td, .periods th { font-size: 7.5pt; padding: 7px 4px; }
.periods .num { font-size: 7.5pt; }
.reconcile { margin-top: 15px; margin-left: auto; width: 70%; }
.reconcile td { padding: 5px 7px; font-size: 8.5pt; border-bottom: 1px solid #e2e9e3; }
.reconcile td:last-child { text-align: right; white-space: nowrap; }
.reconcile .closing { font-weight: 700; background: #e8f1e9; }
.status { font-size: 7pt; color: #82603d; display: block; margin-top: 3px; }
.payment-paid, .payment-paid .translation { color: #16723c; font-weight: 700; }
.payment-unpaid, .payment-unpaid .translation { color: #ba2525; font-weight: 700; }
.empty { color: #74827c; padding: 10px; background: #f8faf7; }
.receipt-amount { font-size: 27pt; font-weight: 700; background: #e8f1e9; padding: 16px; margin: 16px 0; border-radius: 5px; }
.footer { border-top: 1px solid #ccdace; padding-top: 8px; margin-top: 18px; font-size: 7pt; color: #6c8074; }
"""


def _header(snapshot: Mapping[str, Any], language: str, document_id: str, title: str) -> str:
    tenancy = snapshot["tenancy"]
    generated = str(snapshot.get("generated_at") or now_ist().isoformat())
    cutoff = snapshot.get("balance_calculated_at") or snapshot.get("balance_as_of") or snapshot.get("end")
    property_name = tenancy.get("building_name", tenancy.get("property_name", ""))
    flat = tenancy.get("number", tenancy.get("flat_number", ""))
    subtitle = f'{_label("floor", language)}: {_text(tenancy.get("floor", ""))} · {_text(tenancy.get("bhk", ""))} BHK'
    occupancy = ""
    initial_reading = format_meter_reading(tenancy.get("initial_meter_reading_hundredths"))
    if tenancy.get("move_in") or initial_reading:
        move_in = (f'<strong>{_label("move_in", language)}</strong><br>{_date(tenancy["move_in"], language)}'
                   if tenancy.get("move_in") else "")
        reading = (f'<strong>{_label("initial_meter_reading", language)}</strong><br>{initial_reading} kWh'
                   if initial_reading else "")
        occupancy = f'<tr><td>{move_in}</td><td>{reading}</td></tr>'
    cancellation = ""
    if tenancy.get("cancelled_on"):
        cancellation = (
            f'<p class="note"><strong>{_label("cancelled_tenancy", language)}</strong> · '
            f'{_date(tenancy["cancelled_on"], language)}<br>{_text(tenancy.get("cancellation_reason", ""))}</p>'
        )
    return f"""<div class="heading"><div class="brand">GrihaLekha · गृहलेखा</div><h1>{_label(title, language)}</h1></div>
<table class="meta"><tbody>
<tr><td><strong>{_label('property', language)}</strong><br>{_text(property_name)}</td><td><strong>{_label('tenant', language)}</strong><br>{_text(tenancy.get('tenant_name', ''))}</td></tr>
<tr><td><strong>{_label('address', language)}</strong><br>{_multiline(tenancy.get('address', ''))}</td><td><strong>{_label('flat', language)}</strong>: {_text(flat)}<br><span class="quiet">{subtitle}</span></td></tr>
{occupancy}
<tr><td><strong>{_label('document', language)}</strong><br>{_text(document_id)}</td><td><strong>{_label('generated', language)}</strong>: {_timestamp(generated, language)}<br><strong>{_label('cutoff', language)}</strong>: {_timestamp(cutoff, language)}</td></tr>
</tbody></table>{cancellation}"""


def _currency_label(currency_code: str, language: str) -> str:
    currency = get_currency(currency_code)
    if currency.code == DEFAULT_CURRENCY:
        return _label("currency", language)
    en = f"All amounts in {currency.name} ({currency.code})."
    hi = f"सभी राशियाँ {currency.code} ({currency.symbol}) में हैं।"
    if language == "hi":
        return _text(hi)
    if language == "bilingual":
        return f'{_text(en)}<span class="translation">{_text(hi)}</span>'
    return _text(en)


def _shell(body: str, language: str, title: str, currency_code: str = DEFAULT_CURRENCY) -> str:
    return f"""<!doctype html><html lang="{'hi' if language == 'hi' else 'en'}"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data:; base-uri 'none'; form-action 'none'">
<title>{_text(title)}</title><style>{_font_css()}{_CSS}</style></head><body>{body}
<div class="footer">{_currency_label(currency_code, language)} &nbsp; · &nbsp; GrihaLekha</div></body></html>"""


def _document_id(snapshot: Mapping[str, Any], supplied: str | None, prefix: str) -> str:
    return str(supplied or snapshot.get("document_id") or f"GL-{prefix}-{today_ist():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}")


def _table(headers: Iterable[tuple[str, str]], rows: Iterable[str], css: str = "", widths: tuple[int, ...] = ()) -> str:
    headers = list(headers)
    colgroup = "<colgroup>" + "".join(f'<col style="width:{width}%">' for width in widths) + "</colgroup>" if widths else ""
    return f'<table class="data {css}">{colgroup}<thead><tr>' + "".join(f'<th class="{cls}">{label}</th>' for label, cls in headers) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _electricity_details(item: Mapping[str, Any], language: str) -> str:
    """Optional meter/service information, also used by archival bill history."""
    if item.get("kind") != "electricity":
        return ""
    details = []
    if item.get("electricity_deferred"):
        details.append(_label("electricity_deferred", language))
        if item.get("electricity_covered_by_charge_id") is not None:
            details.append(f'{_label("included_in_bill", language)} #{_text(item["electricity_covered_by_charge_id"])}')
        if item.get("electricity_covered_by_cycle_start"):
            details.append(f'{_label("billed_in_cycle", language)} {_date(item["electricity_covered_by_cycle_start"], language)}')
    else:
        if item.get("electricity_deferred_charge_ids"):
            details.append(f'{_label("bill", language)} #{_text(item.get("id"))}')
        reading = format_meter_reading(item.get("meter_reading_hundredths"))
        if reading:
            details.append(f'{_label("meter_reading", language)}: {reading} kWh')
        start, end = item.get("coverage_period_start"), item.get("coverage_period_end")
        if start and end and (start, end) != (item.get("period_start"), item.get("period_end")):
            details.append(f'{_label("electricity_coverage", language)}: {_date(start, language)} – {_date(end, language)}')
    return '<br><span class="quiet">' + "<br>".join(details) + "</span>" if details else ""


def _charge_details(charges: Iterable[Mapping[str, Any]], language: str, currency_code: str = DEFAULT_CURRENCY) -> str:
    rows = []
    for item in charges:
        status = _bill_status(item)
        kind = _label(str(item.get("kind", "misc")), language)
        period = _period(item, language)
        due = f'{_label("due", language)}: {_date(item.get("due_date"), language)}'
        amount = "—" if item.get("amount_paise") is None or status == "deferred" else _money(_amount(item), currency_code)
        paid = "—" if status in {"review_pending", "not_billed", "deferred"} else _money(_amount(item, "paid_paise"), currency_code)
        pending = "—" if status in {"review_pending", "not_billed", "deferred"} else _money(_amount(item, "outstanding_paise"), currency_code)
        description = item.get("description", "")
        if item.get("status") != "draft":
            # Older posted bills retained instructions meant only for the
            # owner's draft editor. Normalize only these exact built-in texts.
            description = {
                "Monthly electricity — enter the bill amount": "Monthly electricity",
                "Partial period — enter the agreed rent": "Rent for partial period",
            }.get(description, description)
        status_text = _status_label("cancelled" if status == "void" else status, language)
        if item.get("status") == "void":
            if item.get("void_reason"):
                description = f'{description}\n{item["void_reason"]}'
            if item.get("voided_on"):
                status_text += f'<br><span class="quiet">{_date(item["voided_on"], language)}</span>'
        rows.append(f'<tr><td><strong>{kind}</strong><br>{period}<br><span class="quiet">{due}</span></td><td>{_multiline(description)}{_electricity_details(item, language)}</td><td class="num">{amount}</td><td class="num">{paid}</td><td class="num">{pending}</td><td>{status_text}</td></tr>')
    return _table([(_label(k, language), "num" if k in {"amount", "settled", "outstanding"} else "") for k in ("period", "description", "amount", "settled", "outstanding", "status")], rows, widths=(24, 22, 14, 14, 14, 12))


def _bill_status(item: Mapping[str, Any]) -> str:
    if item.get("status") == "void":
        return "cancelled" if item.get("posted_at", "unknown") is None else "void"
    if item.get("kind") == "electricity" and item.get("electricity_deferred"):
        return "deferred"
    if item.get("status") == "draft":
        return "review_pending"
    if item.get("billed_at_cutoff") is False:
        return "not_billed"
    if _amount(item, "outstanding_paise") <= 0:
        return "paid"
    return "partially_paid" if _amount(item, "paid_paise") else "unpaid"


def _legacy_cycles(charges: list[Mapping[str, Any]]) -> list[dict]:
    """Keep older issued snapshots renderable without guessing missing cycles."""
    groups = {}
    for item in charges:
        key = (str(item.get("period_start", "")), str(item.get("period_end", "")))
        row = groups.setdefault(key, {
            "period_start": key[0], "period_end": key[1], "rent_paise": None,
            "electricity_paise": None, "misc_paise": None,
            "billed_paise": 0, "paid_paise": 0, "pending_paise": 0,
        })
        kind = str(item.get("kind", "misc")) + "_paise"
        row[kind] = (row.get(kind) or 0) + _amount(item)
        row["billed_paise"] += _amount(item)
        row["paid_paise"] += _amount(item, "paid_paise")
        row["pending_paise"] += _amount(item, "outstanding_paise")
    for row in groups.values():
        row["payment_status"] = "paid" if row["pending_paise"] <= 0 else "unpaid"
        row["review_pending_kinds"] = [kind for kind in ("rent", "electricity") if row[kind + "_paise"] is None]
        row["review_pending"] = bool(row["review_pending_kinds"])
    return sorted(groups.values(), key=lambda row: (row["period_start"], row["period_end"]))


def _cycle_status(row: Mapping[str, Any], language: str) -> str:
    status = str(row.get("payment_status", "not_billed"))
    deferred = row.get("electricity_deferred") or row.get("electricity_status") == "deferred"
    parts = []
    if row.get("review_pending"):
        kinds = ", ".join(_label(str(kind), language) for kind in row.get("review_pending_kinds", []))
        parts.append(f'{_label("review_pending", language)}<br><span class="quiet">{kinds}</span>')
    if deferred:
        parts.append(_label("deferred", language))
        references = row.get("electricity_covered_by_charge_ids", [])
        if references:
            parts.append(f'<span class="quiet">{_label("included_in_bill", language)} '
                         + ", ".join(f'#{_text(value)}' for value in references) + "</span>")
    if row.get("review_pending") or deferred:
        # Paid describes a completed cycle only. Pending reviews and deferred
        # electricity remain explicit alongside any confirmed unpaid bill.
        if status not in {"paid", "not_billed"}:
            parts.append(_status_label(status, language))
    else:
        parts.append(_status_label(status, language))
    return "<br>".join(parts)


def _cycle_csv_status(row: Mapping[str, Any]) -> str:
    status = str(row.get("payment_status", "not_billed"))
    if row.get("electricity_deferred") or row.get("electricity_status") == "deferred":
        return (_LABELS["deferred"][0] if status in {"paid", "not_billed"}
                else f'{_LABELS[status][0]}; {_LABELS["electricity_deferred"][0]}')
    return _LABELS[status][0]


def statement_html(snapshot: Mapping[str, Any], language: str = "en", document_id: str | None = None) -> str:
    """Render an immutable, as-of snapshot to self-contained printable HTML."""
    _validate_snapshot(snapshot, language)
    currency_code = snapshot.get("currency_code", DEFAULT_CURRENCY)
    charges = list(snapshot.get("charges", []))
    payments = list(snapshot.get("payments", []))
    refunds = list(snapshot.get("refunds", []))
    adjustments = list(snapshot.get("adjustments", []))
    deposits = list(snapshot.get("deposits", []))
    balance = snapshot.get("balance", {})
    identity = _document_id(snapshot, document_id, "S")
    body = [_header(snapshot, language, identity, "statement")]
    body.append(f'<p><strong>{_label("period", language)}</strong>: {_date(snapshot["start"], language)} – {_date(snapshot["end"], language)}</p>')
    if snapshot.get("balance_calculated_at") and "T" in str(snapshot["balance_calculated_at"]):
        body.append(f'<p class="note">{_label("current_status_note", language)}</p>')
    cards = [
        ("payable", max(0, _amount(snapshot, "closing_balance_paise"))),
        ("outstanding", _amount(balance, "outstanding_paise")),
        ("advance", _amount(balance, "credit_paise")),
        ("deposit", _amount(balance, "deposit_paise")),
    ]
    body.append('<table class="cards"><tbody><tr>' + "".join(f'<td><div class="caption">{_label(label, language)}</div><div class="value">{_money(amount, currency_code)}</div></td>' for label, amount in cards) + "</tr></tbody></table>")

    cycles = list(snapshot["billing_cycles"]) if "billing_cycles" in snapshot else _legacy_cycles(charges)
    body.append(f'<h2>{_label("summary", language)}</h2>')
    if cycles:
        show_locations = len({
            (location.get("building_name"), location.get("number"))
            for cycle in cycles for location in cycle.get("locations", [])
        }) > 1
        rows = []
        for cycle in cycles:
            location = ""
            locations = cycle.get("locations", [])
            if locations and show_locations:
                location = '<br><span class="quiet">' + "<br>".join(
                    f'{_text(item.get("building_name", ""))} · {_label("flat", language)} {_text(item.get("number", ""))}'
                    for item in locations
                ) + "</span>"
            if cycle.get("different_service_dates"):
                location += f'<br><span class="quiet">{_label("service_dates", language)}</span>'
            cells = [f'<td>{_period(cycle, language)}{location}</td>']
            for key in ("rent_paise", "electricity_paise", "misc_paise", "billed_paise", "paid_paise", "pending_paise"):
                value = _money(_amount(cycle, key), currency_code) if cycle.get(key) is not None else "—"
                if key in {"rent_paise", "electricity_paise", "misc_paise"} and key.removesuffix("_paise") in cycle.get("cancelled_kinds", []):
                    value += f'<br><span class="status">{_label("cancelled", language)}</span>'
                if key == "electricity_paise" and (cycle.get("electricity_deferred") or cycle.get("electricity_status") == "deferred"):
                    value = f'<span class="status">{_label("deferred", language)}</span>'
                cells.append(f'<td class="num">{value}</td>')
            cells.append(f'<td>{_cycle_status(cycle, language)}</td>')
            rows.append("<tr>" + "".join(cells) + "</tr>")
        body.append(_table([(_label(key, language), "" if key in {"period", "status"} else "num") for key in ("period", "rent", "electricity", "misc", "total", "settled", "outstanding", "status")], rows, "periods", (20, 10, 12, 8, 11, 11, 11, 17)))
        if any(cycle.get("review_pending") for cycle in cycles):
            body.append(f'<p class="note">{_label("review_note", language)}</p>')
        if any(cycle.get("electricity_deferred") or cycle.get("electricity_status") == "deferred" for cycle in cycles):
            body.append(f'<p class="note">{_label("deferred_note", language)}</p>')
    else:
        body.append(f'<p class="empty">{_label("no_entries", language)}</p>')

    details = list(snapshot.get("billing_details", charges))
    if details:
        body.append(f'<h2>{_label("details", language)}</h2>{_charge_details(details, language, currency_code)}')
        if adjustments:
            body.append(f'<p class="note">{_label("originals", language)}</p>')

    detail_ids = {item.get("id") for item in details}
    earlier = [item for item in snapshot.get("outstanding_charges", []) if item.get("id") not in detail_ids and str(item.get("charge_date", "")) < str(snapshot["start"]) and _amount(item, "outstanding_paise") > 0]
    if earlier:
        body.append(f'<h2>{_label("carried", language)}</h2>{_charge_details(earlier, language, currency_code)}')
    other_pending = [item for item in snapshot.get("outstanding_charges", []) if item.get("id") not in detail_ids and str(item.get("charge_date", "")) >= str(snapshot["start"]) and _amount(item, "outstanding_paise") > 0]
    if other_pending:
        body.append(f'<h2>{_label("other_pending", language)}</h2>{_charge_details(other_pending, language, currency_code)}')

    body.append(f'<h2>{_label("payments", language)}</h2>')
    if payments:
        rows = []
        for payment in payments:
            method = str(payment.get("method", "cash"))
            method = {"opening": "opening_credit", "deposit": "deposit_credit"}.get(method, method)
            reversed_note = f'<span class="status">{_label("reversed", language)}: {_date(payment["reversed_on"], language)}</span>' if payment.get("reversed_on") else ""
            notes = _multiline(payment.get("notes", ""))
            rows.append(f'<tr><td>{_date(payment.get("paid_on"), language)}{reversed_note}</td><td>{_label(method, language)}</td><td>{_multiline(payment.get("reference", ""))}<br><span class="quiet">{notes}</span></td><td class="num">{_money(_amount(payment), currency_code)}</td></tr>')
        body.append(_table([(_label(key, language), "num" if key == "amount" else "") for key in ("date", "method", "reference", "amount")], rows, widths=(22, 23, 35, 20)))
    else:
        body.append(f'<p class="empty">{_label("no_entries", language)}</p>')

    for section, movements in (("refunds", refunds), ("adjustments", adjustments), ("deposits", deposits)):
        if not movements:
            continue
        body.append(f'<h2>{_label(section, language)}</h2>')
        rows = []
        for item in movements:
            description = item.get("reason") or item.get("notes") or ""
            action = str(item.get("action") or item.get("kind") or "")
            if section == "deposits" and item.get("opening") and action == "receive":
                action = "opening_deposit"
            action_label = _label(action, language) if action in _LABELS else _text(action.replace("_", " "))
            rows.append(f'<tr><td>{_date(_row_date(item), language)}</td><td>{action_label}<br>{_multiline(description)}</td><td class="num">{_money(_amount(item), currency_code)}</td></tr>')
        body.append(_table([(_label(key, language), "num" if key == "amount" else "") for key in ("date", "description", "amount")], rows, widths=(23, 57, 20)))
        if section == "deposits":
            body.append(f'<p class="note">{_label("deposit_note", language)}</p>')

    opening = _amount(snapshot, "opening_balance_paise")
    closing = _amount(snapshot, "closing_balance_paise")
    charged = _amount(snapshot, "period_charges_paise") if "period_charges_paise" in snapshot else sum(_amount(item) for item in charges)
    received = _amount(snapshot, "period_payments_paise") if "period_payments_paise" in snapshot else sum(_amount(item) for item in payments)
    returned = _amount(snapshot, "period_refunds_paise") if "period_refunds_paise" in snapshot else sum(_amount(item) for item in refunds)
    corrected = _amount(snapshot, "adjustments_paise") if "adjustments_paise" in snapshot else sum(_amount(item) for item in adjustments)
    period_closing = _amount(snapshot, "period_closing_balance_paise") if "period_closing_balance_paise" in snapshot else closing
    if "net_paise" in balance and _amount(balance, "net_paise") != closing:
        raise ValueError("The statement does not reconcile: its current balance differs from the account balance.")
    if opening + charged - received + returned + corrected != period_closing:
        raise ValueError("The statement does not reconcile: opening balance plus transactions differs from closing balance.")
    lines = [("opening", opening), ("charges", charged), ("payments", -received), ("refunds", returned), ("adjustments", corrected)]
    if closing != period_closing:
        lines.extend((("period_balance", period_closing), ("later_changes", closing - period_closing)))
    lines.append(("closing", max(0, closing)))
    if closing < 0:
        lines.append(("credit", -closing))
    body.append('<table class="reconcile"><tbody>' + "".join(f'<tr class="{"closing" if key == "closing" else ""}"><td>{_label(key, language)}</td><td>{_money(amount, currency_code)}</td></tr>' for key, amount in lines) + "</tbody></table>")
    body.append(f'<p class="note">{_label("payable_note", language)}</p>')
    return _shell("".join(body), language, f"GrihaLekha — {identity}", currency_code)


def receipt_html(snapshot: Mapping[str, Any], payment_id: int, language: str = "en", document_id: str | None = None) -> str:
    """Render one actual payment, with allocation details at snapshot cutoff."""
    _validate_snapshot(snapshot, language)
    currency_code = snapshot.get("currency_code", DEFAULT_CURRENCY)
    payment = next((row for row in snapshot.get("payments", []) if row.get("id") == payment_id), None)
    if payment is None:
        raise ValueError("The selected payment is not in this statement period.")
    identity = _document_id(snapshot, document_id, "R")
    method = str(payment.get("method", "cash"))
    method = {"opening": "opening_credit", "deposit": "deposit_credit"}.get(method, method)
    body = [_header(snapshot, language, identity, "receipt")]
    body.append(f'<div class="receipt-amount">{_money(_amount(payment), currency_code)}</div>')
    rows = [("date", _date(payment.get("paid_on"), language)), ("method", _label(method, language)), ("reference", _multiline(payment.get("reference", "")) or "—"), ("description", _multiline(payment.get("notes", "")) or "—")]
    if payment.get("reversed_on"):
        rows.append(("status", f'{_label("reversed", language)}: {_date(payment["reversed_on"], language)}'))
    body.append('<table class="meta"><tbody>' + "".join(f'<tr><td><strong>{_label(key, language)}</strong></td><td>{value}</td></tr>' for key, value in rows) + "</tbody></table>")
    if payment.get("proof_path"):
        body.append(f'<p class="note">{_label("proof", language)}</p>')
    allocations = payment.get("allocations", [])
    if allocations:
        known_charges = {row["id"]: row for row in [*snapshot.get("charges", []), *snapshot.get("outstanding_charges", [])]}
        body.append(f'<h2>{_label("allocations", language)}</h2>')
        items = []
        for allocation in allocations:
            charge_id = allocation.get("charge_id")
            charge = known_charges.get(charge_id)
            detail = f'{_label("bill", language)} #{_text(charge_id)}'
            if charge:
                detail += f'<br>{_label(str(charge.get("kind", "misc")), language)} · {_period(charge, language)}'
            items.append(f'<tr><td>{detail}</td><td>{_date(allocation.get("effective_on"), language)}</td><td class="num">{_money(_amount(allocation), currency_code)}</td></tr>')
        body.append(_table([(_label(key, language), "num" if key == "amount" else "") for key in ("applied", "date", "amount")], items, widths=(53, 27, 20)))
        body.append(f'<p class="note">{_label("allocation_note", language)}</p>')
    body.append(f'<p><strong>{_label("advance", language)}</strong>: {_money(_amount(payment, "available_paise"), currency_code)}</p>')
    body.append(f'<p class="note">{_label("receipt_note", language)}</p>')
    return _shell("".join(body), language, f"GrihaLekha — {identity}", currency_code)


def _safe_csv(value: Any) -> Any:
    """Prevent spreadsheet formulas in text without changing numeric columns."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    # Leading whitespace and C0 controls can hide a formula prefix in Excel.
    stripped = value.lstrip("".join(chr(code) for code in range(33)) + "\ufeff")
    if stripped.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def export_csv(snapshot: Mapping[str, Any], destination: Path) -> list[Path]:
    """Write exact values with currency-labelled columns to UTF-8 BOM CSVs."""
    _validate_snapshot(snapshot, "en")
    currency_code = snapshot.get("currency_code", DEFAULT_CURRENCY)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    tenancy = snapshot["tenancy"]
    tenancy_id = tenancy.get("id", "account")
    prefix = f"grihalekha-{tenancy_id}-{snapshot['start']}-{snapshot['end']}"
    # Prefix fields originate from validated dates and an internal integer ID.
    if not isinstance(tenancy_id, int) and tenancy_id != "account":
        raise ValueError("A CSV account identifier must be an integer.")
    metadata = {"tenancy_id": tenancy_id, "building": tenancy.get("building_name", ""), "flat": tenancy.get("number", ""), "tenant": tenancy.get("tenant_name", ""),
                "move_in": tenancy.get("move_in") or "",
                "initial_meter_reading_kwh": format_meter_reading(tenancy.get("initial_meter_reading_hundredths")),
                "cancelled_on": tenancy.get("cancelled_on") or "", "cancellation_reason": tenancy.get("cancellation_reason") or "",
                "currency_code": currency_code}
    groups: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
    common = list(metadata)
    charge_fields = ["id", "kind", "period_start", "period_end", "charge_date", "due_date", "description", "status", "payment_status", "opening", "meter_reading_kwh", "coverage_period_start", "coverage_period_end", "amount_inr", "amount_paid_inr", "bill_pending_inr", "settled_inr", "outstanding_inr"]
    groups["charges"] = (common + charge_fields, [{**metadata, **{key: item.get(key, "") for key in charge_fields}, "payment_status": _LABELS[_bill_status(item)][0], "meter_reading_kwh": format_meter_reading(item.get("meter_reading_hundredths")) if item.get("kind") == "electricity" and not item.get("electricity_deferred") else "", "amount_inr": _decimal(_amount(item)), "amount_paid_inr": _decimal(_amount(item, "paid_paise")), "bill_pending_inr": _decimal(_amount(item, "outstanding_paise")), "settled_inr": _decimal(_amount(item, "paid_paise")), "outstanding_inr": _decimal(_amount(item, "outstanding_paise"))} for item in snapshot.get("charges", [])])
    payment_fields = ["id", "paid_on", "method", "reference", "notes", "opening", "reversed_on", "amount_inr", "available_inr"]
    groups["payments"] = (common + payment_fields, [{**metadata, **{key: item.get(key, "") for key in payment_fields}, "amount_inr": _decimal(_amount(item)), "available_inr": _decimal(_amount(item, "available_paise"))} for item in snapshot.get("payments", [])])
    allocation_rows = list(snapshot.get("allocations", []))
    if "allocations" not in snapshot:
        allocation_rows = [{**allocation, "payment_id": payment.get("id")} for payment in snapshot.get("payments", []) for allocation in payment.get("allocations", [])]
    allocation_fields = ["payment_id", "charge_id", "effective_on", "amount_inr"]
    groups["allocations"] = (common + allocation_fields, [{**metadata, **{key: item.get(key, "") for key in allocation_fields}, "amount_inr": _decimal(_amount(item))} for item in allocation_rows])
    movement_fields = ["type", "id", "date", "action", "original_kind", "opening", "reversed_on", "reason", "method", "amount_inr"]
    movements = []
    for kind in ("refunds", "adjustments", "deposits"):
        for item in snapshot.get(kind, []):
            movements.append({**metadata, "type": kind, "id": item.get("id", ""), "date": _row_date(item), "action": item.get("action") or item.get("kind", ""), "original_kind": item.get("original_kind", ""), "opening": item.get("opening", False), "reversed_on": item.get("reversed_on", ""), "reason": item.get("reason") or item.get("notes", ""), "method": item.get("method", ""), "amount_inr": _decimal(_amount(item))})
    groups["movements"] = (common + movement_fields, movements)
    summary_fields = ["start", "end", "balance_as_of", "balance_calculated_at", "period_balance_as_of", "period_closing_balance_inr", "opening_balance_inr", "closing_balance_inr", "amount_left_to_pay_inr", "advance_after_bills_inr", "outstanding_inr", "overdue_inr", "advance_inr", "deposit_inr"]
    balance = snapshot.get("balance", {})
    groups["summary"] = (common + summary_fields, [{**metadata, "start": snapshot["start"], "end": snapshot["end"], "balance_as_of": snapshot.get("balance_as_of", snapshot["end"]), "balance_calculated_at": snapshot.get("balance_calculated_at", ""), "period_balance_as_of": snapshot.get("period_balance_as_of", snapshot["end"]), "period_closing_balance_inr": _decimal(_amount(snapshot, "period_closing_balance_paise")) if "period_closing_balance_paise" in snapshot else _decimal(_amount(snapshot, "closing_balance_paise")), "opening_balance_inr": _decimal(_amount(snapshot, "opening_balance_paise")), "closing_balance_inr": _decimal(_amount(snapshot, "closing_balance_paise")), "amount_left_to_pay_inr": _decimal(max(0, _amount(snapshot, "closing_balance_paise"))), "advance_after_bills_inr": _decimal(max(0, -_amount(snapshot, "closing_balance_paise"))), "outstanding_inr": _decimal(_amount(balance, "outstanding_paise")), "overdue_inr": _decimal(_amount(balance, "overdue_paise")), "advance_inr": _decimal(_amount(balance, "credit_paise")), "deposit_inr": _decimal(_amount(balance, "deposit_paise"))}])
    if "billing_cycles" in snapshot:
        cycle_fields = ["cycle_start", "period_start", "period_end", "rent_inr", "electricity_inr", "other_fees_inr", "billed_inr", "amount_paid_inr", "bill_pending_inr", "payment_status", "review_pending", "review_pending_kinds", "electricity_status", "electricity_deferred", "electricity_covered_by_charge_ids"]
        cycle_rows = []
        for cycle in snapshot["billing_cycles"]:
            amounts = {
                target: _decimal(_amount(cycle, source)) if cycle.get(source) is not None else ""
                for target, source in (
                    ("rent_inr", "rent_paise"), ("electricity_inr", "electricity_paise"),
                    ("other_fees_inr", "misc_paise"), ("billed_inr", "billed_paise"),
                    ("amount_paid_inr", "paid_paise"), ("bill_pending_inr", "pending_paise"),
                )
            }
            cycle_rows.append({
                **metadata, **{key: cycle.get(key, "") for key in cycle_fields}, **amounts,
                "payment_status": _cycle_csv_status(cycle),
                "review_pending_kinds": ", ".join(cycle.get("review_pending_kinds", [])),
                "electricity_status": _LABELS.get(str(cycle.get("electricity_status", "")), ("", ""))[0],
                "electricity_covered_by_charge_ids": ", ".join(str(value) for value in cycle.get("electricity_covered_by_charge_ids", [])),
            })
        groups["billing-cycles"] = (common + cycle_fields, cycle_rows)
    electricity = {
        item["id"]: item
        for item in [*snapshot.get("billing_details", snapshot.get("charges", [])), *snapshot.get("outstanding_charges", [])]
        if item.get("kind") == "electricity"
    }
    if any(
        item.get("meter_reading_hundredths") is not None or item.get("electricity_deferred")
        or item.get("electricity_deferred_charge_ids")
        or (item.get("coverage_period_start") and item["coverage_period_start"] != item.get("period_start"))
        for item in electricity.values()
    ):
        # Financial charges.csv retains its transaction-date scope. This
        # supplementary table also preserves readings on unposted drafts and
        # the service periods of deferred/combined electricity bills.
        electricity_fields = ["id", "cycle_start", "period_start", "period_end", "coverage_period_start", "coverage_period_end", "due_date", "description", "status", "payment_status", "electricity_deferred", "electricity_covered_by_charge_id", "electricity_covered_by_cycle_start", "included_deferred_charge_ids", "meter_reading_kwh", "amount_inr", "amount_paid_inr", "bill_pending_inr"]
        electricity_rows = []
        for item in sorted(electricity.values(), key=lambda row: (row.get("period_start", ""), row["id"])):
            status = _bill_status(item)
            pending_review = status in {"deferred", "review_pending", "not_billed"}
            electricity_rows.append({
                **metadata, **{key: item.get(key, "") for key in electricity_fields},
                "cycle_start": item.get("electricity_cycle_start") or item.get("cycle_start") or "",
                "payment_status": _LABELS[status][0],
                "included_deferred_charge_ids": ", ".join(str(value) for value in item.get("electricity_deferred_charge_ids", [])),
                "meter_reading_kwh": "" if item.get("electricity_deferred") else format_meter_reading(item.get("meter_reading_hundredths")),
                "amount_inr": "" if item.get("amount_paise") is None or status == "deferred" else _decimal(_amount(item)),
                "amount_paid_inr": "" if pending_review else _decimal(_amount(item, "paid_paise")),
                "bill_pending_inr": "" if pending_review else _decimal(_amount(item, "outstanding_paise")),
            })
        groups["electricity-details"] = (common + electricity_fields, electricity_rows)
    paths = []
    for name, (fields, rows) in groups.items():
        # Internal field names retain the legacy INR schema. Only the public
        # labels change; generated fixed-point values are never converted.
        money_fields = {key for key in fields if key.endswith("_inr")}
        output_fields = {
            key: key.removesuffix("_inr") + "_" + currency_code.lower() if key in money_fields else key
            for key in fields
        }
        target = destination / f"{prefix}-{name}.csv"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=destination, prefix=".csv-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                writer = csv.DictWriter(handle, fieldnames=list(output_fields.values()), extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    # Money fields are generated fixed-point numeric strings; do
                    # not formula-escape legitimate negative account balances.
                    writer.writerow({output_fields.get(key, key): value if key in money_fields else _safe_csv(value) for key, value in row.items()})
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            paths.append(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if "electricity-details" not in groups:
        # Clearing the last reading must not leave a stale supplement beside
        # freshly exported tables for the same tenant and statement period.
        (destination / f"{prefix}-electricity-details.csv").unlink(missing_ok=True)
    return paths


@dataclass
class _DocumentNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    """Read only document body content, never styles/scripts or external URLs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _DocumentNode("body")
        self.stack = [self.root]
        self.in_body = False

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.in_body = True
            return
        if not self.in_body:
            return
        node = _DocumentNode(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {"br", "img", "hr", "meta", "link", "input", "col"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag == "body":
            self.in_body = False
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.in_body:
            self.stack[-1].children.append(data)


def _native_html(html: str) -> tuple[str, list[tuple[bool, list[int]]]]:
    """Adapt our document to Qt rich text's supported HTML subset.

    Qt rich text cannot use @font-face, CSS grid/modern print styling or colgroup.
    Fonts are registered separately, columns become explicit table constraints,
    and table headers are set through QTextTableFormat after HTML import.
    """
    parser = _DocumentParser()
    parser.feed(html)
    tables: list[tuple[bool, list[int]]] = []

    def render(node: Any, parent: str = "", status_color: str = "") -> str:
        if isinstance(node, str):
            return _text(node)
        tag = node.tag
        cls = node.attrs.get("class", "")
        if tag in {"script", "style", "img", "colgroup", "col"}:
            return ""
        if tag == "br":
            return "<br>"
        if tag == "table":
            columns = []
            for child in node.children:
                if isinstance(child, _DocumentNode) and child.tag == "colgroup":
                    for col in child.children:
                        if isinstance(col, _DocumentNode) and col.tag == "col":
                            width = col.attrs.get("style", "").replace("width:", "").replace("%", "").strip()
                            if width.isdigit():
                                columns.append(int(width))
            data_table = "data" in cls.split()
            tables.append((data_table, columns))
            rows = []

            def table_rows(container):
                for row in container.children:
                    if not isinstance(row, _DocumentNode):
                        continue
                    if row.tag in {"thead", "tbody", "tfoot"}:
                        table_rows(row)
                    elif row.tag == "tr":
                        cells = []
                        header = any(isinstance(cell, _DocumentNode) and cell.tag == "th" for cell in row.children)
                        for index, cell in enumerate(child for child in row.children if isinstance(child, _DocumentNode) and child.tag in {"td", "th"}):
                            cell_cls = cell.attrs.get("class", "")
                            align = ' align="right"' if "num" in cell_cls.split() else ""
                            width = f' width="{columns[index]}%"' if index < len(columns) else ""
                            background = ' bgcolor="#edf3ee"' if header or cls == "cards" or "closing" in row.attrs.get("class", "") else ""
                            content = "".join(render(part, "th" if header else "td") for part in cell.children)
                            if header:
                                content = f"<b>{content}</b>"
                            text_style = ' style="font-size:7.5pt"' if "periods" in cls.split() else ""
                            cells.append(f'<td{align}{width}{background}{text_style}>{content}</td>')
                        rows.append("<tr>" + "".join(cells) + "</tr>")

            table_rows(node)
            return '<table width="100%" cellspacing="0" cellpadding="4">' + "".join(rows) + "</table><p></p>"
        if tag == "span" and cls in {"payment-paid", "payment-unpaid"}:
            status_color = "#16723c" if cls == "payment-paid" else "#ba2525"
        content = "".join(render(child, tag, status_color) for child in node.children)
        if tag == "span":
            if cls == "translation":
                prefix = "<br>" if parent in {"h1", "th"} else " / "
                return f'{prefix}<span style="color:{status_color or "#577066"};font-size:8pt;font-weight:normal">{content}</span>'
            if cls in {"payment-paid", "payment-unpaid"}:
                return f'<span style="color:{status_color};font-weight:bold">{content}</span>'
            return f'<span style="color:#61756e">{content}</span>' if cls in {"quiet", "status"} else content
        if tag == "div":
            if cls == "brand":
                return f'<p style="font-size:12pt;font-weight:bold;color:#1f6b59">{content}</p>'
            if cls == "receipt-amount":
                return f'<p style="font-size:24pt;font-weight:bold;background-color:#edf3ee">{content}</p>'
            if cls == "value":
                return f'<br><b>{content}</b>'
            if cls == "caption":
                return f'<span style="font-size:8pt">{content}</span>'
            if cls == "footer":
                return f'<p style="font-size:7pt;color:#61756e">{content}</p>'
            return content
        if tag == "h1":
            return f'<h1 style="font-size:20pt;color:#214b3b">{content}</h1>'
        if tag == "h2":
            return f'<h2 style="font-size:11pt;color:#214b3b;margin-top:12px;margin-bottom:5px">{content}</h2>'
        if tag in {"strong", "b", "i", "em", "p"}:
            style = ' style="font-size:8pt;color:#61756e"' if tag == "p" and cls == "note" else ""
            return f'<{tag}{style}>{content}</{tag}>'
        return content

    body = render(parser.root)
    return '<html><head><meta charset="utf-8"></head><body>' + body + "</body></html>", tables


@lru_cache(maxsize=1)
def _native_font_family() -> str:
    from PySide6.QtGui import QFontDatabase

    directory = Path(__file__).with_name("assets") / "fonts"
    family = "Noto Sans Devanagari"
    for filename in ("NotoSansDevanagari-Regular.ttf", "NotoSansDevanagari-Bold.ttf", "NotoSans-Regular.ttf", "NotoSans-Bold.ttf"):
        identity = QFontDatabase.addApplicationFont(str(directory / filename))
        if identity < 0:
            raise ValueError(f"Could not load bundled font {filename}.")
        if filename == "NotoSansDevanagari-Regular.ttf":
            names = QFontDatabase.applicationFontFamilies(identity)
            if names:
                family = names[0]
    return family


def _native_pdf_bytes(html: str) -> bytes:
    """Print through Qt's native shaping/font embedding without a subprocess.

    Qt's PDF Unicode maps are incomplete for some shaped Devanagari glyphs.
    Visual output is correct; Chromium remains preferred for Hindi search/copy.
    """
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QColor, QFont, QPageLayout, QPageSize, QPdfWriter, QTextDocument, QTextFrameFormat, QTextLength, QTextTable

    family = _native_font_family()
    native_html, table_layouts = _native_html(html)
    with tempfile.TemporaryDirectory(prefix="grihalekha-native-pdf-") as temporary:
        target = Path(temporary) / "document.pdf"
        writer = QPdfWriter(str(target))
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setPageMargins(QMarginsF(12, 14, 12, 15), QPageLayout.Unit.Millimeter)
        writer.setResolution(96)
        writer.setTitle("GrihaLekha account document")
        writer.setCreator("GrihaLekha")
        document = QTextDocument()
        document.setDefaultFont(QFont(family, 9))
        document.setDefaultStyleSheet("p { margin-top: 3px; margin-bottom: 5px; } td { vertical-align: top; } h1 { margin-top: 4px; margin-bottom: 8px; }")
        # Some Devanagari glyphs extend above Qt's nominal line box. A small
        # inner margin keeps their tips from bleeding across a page boundary.
        document.setDocumentMargin(2)
        document.documentLayout().setPaintDevice(writer)
        document.setHtml(native_html)
        table_index = 0
        for frame in document.rootFrame().childFrames():
            if not isinstance(frame, QTextTable):
                continue
            is_data, widths = table_layouts[table_index] if table_index < len(table_layouts) else (False, [])
            table_index += 1
            table_format = frame.format()
            table_format.setCellSpacing(0)
            table_format.setCellPadding(5)
            table_format.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
            table_format.setHeaderRowCount(1 if is_data else 0)
            table_format.setBorder(0.4 if is_data else 0)
            table_format.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
            table_format.setBorderBrush(QColor("#dce5dd"))
            if widths:
                table_format.setColumnWidthConstraints([QTextLength(QTextLength.Type.PercentageLength, width) for width in widths])
            else:
                table_format.setColumnWidthConstraints([QTextLength(QTextLength.Type.PercentageLength, 100 / frame.columns()) for _ in range(frame.columns())])
            frame.setFormat(table_format)
        document.setPageSize(QSizeF(writer.width(), writer.height()))
        document.print_(writer)
        del document
        del writer
        return target.read_bytes()


class PdfExportJob(QObject):
    """One asynchronous Chromium print job, with isolated offline resources.

    Create/start this object on the GUI thread and keep it alive until either
    signal fires. A temporary local HTML file avoids Qt setHtml's 2 MB URL
    limit when embedding the complete fonts. The destination is replaced only
    after Chromium has returned a complete PDF, so a failure preserves an
    existing document.
    """

    finished = Signal(str)
    failed = Signal(str)

    @property
    def used_renderer(self) -> str:
        """Renderer that handled this job, for diagnostics and smoke checks."""
        return self.renderer_used

    def __init__(self, html: str, destination: Path, parent: QObject | None = None, renderer: str | None = None):
        super().__init__(parent)
        self.renderer = renderer or os.environ.get("GRIHALEKHA_PDF_RENDERER", "auto")
        if self.renderer not in {"auto", "native"}:
            raise ValueError("PDF renderer must be auto or native.")
        self.html = html
        self.destination = Path(destination).resolve()
        self._started = False
        self._done = False
        self.used_native_fallback = False
        self.fallback_reason = ""
        self.renderer_used = ""
        self._native_started = False
        self._page = None
        self._profile = None
        self._interceptor = None
        self._temporary: tempfile.TemporaryDirectory | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._fail("PDF generation timed out. Please try again."))

    def start(self) -> None:
        if self._started:
            raise RuntimeError("A PDF job can only be started once.")
        self._started = True
        if self.renderer == "native":
            self.renderer_used = "native"
            QTimer.singleShot(0, self._native_print)
            return
        self.renderer_used = "webengine"
        try:
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor

            if QGuiApplication.instance() is None:
                raise RuntimeError("PDF export requires a running desktop application.")

            class OfflineRequests(QWebEngineUrlRequestInterceptor):
                def interceptRequest(self, info):
                    if info.requestUrl().scheme().lower() not in {"file", "data", "about", "blob"}:
                        info.block(True)

            self._temporary = tempfile.TemporaryDirectory(prefix="grihalekha-pdf-")
            html_path = Path(self._temporary.name) / "document.html"
            html_path.write_text(self.html, encoding="utf-8")
            self._profile = QWebEngineProfile(self)
            self._profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
            self._profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
            self._interceptor = OfflineRequests(self._profile)
            self._profile.setUrlRequestInterceptor(self._interceptor)
            self._page = QWebEnginePage(self._profile, self)
            settings = self._page.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
            settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
            settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
            settings.setAttribute(QWebEngineSettings.WebAttribute.PrintElementBackgrounds, True)
            self._page.loadFinished.connect(self._loaded)
            self._page.renderProcessTerminated.connect(lambda status, code: self._fail(f"The PDF renderer stopped unexpectedly ({status.name}, exit {code})."))
            self._timer.start(45_000)
            self._page.load(QUrl.fromLocalFile(str(html_path)))
        except Exception as exc:
            self._fail(f"Could not start PDF export: {exc}")

    def _loaded(self, ok: bool) -> None:
        if self._done:
            return
        if not ok:
            self._fail("Could not load the local document for PDF export.")
            return
        # Explicitly request both regular faces. A face used only in bold text
        # otherwise leaves its regular sibling unloaded and font.check false.
        self._page.runJavaScript("document.fonts.load('12px \"GL Devanagari\"', 'किराया क्षत्रिय'); document.fonts.load('12px \"GL Noto\"', 'GrihaLekha');", lambda _: self._check_fonts())

    def _check_fonts(self) -> None:
        if self._done or self._page is None:
            return
        # Query Chromium after layout has started. A loaded font face must also
        # actually resolve the Devanagari sample before printing can proceed.
        script = "document.fonts.status === 'loaded' && document.fonts.check('12px \"GL Devanagari\"', 'किराया क्षत्रिय') && document.fonts.check('12px \"GL Noto\"', 'GrihaLekha')"
        self._page.runJavaScript(script, self._fonts_ready)

    def _fonts_ready(self, ready: Any) -> None:
        if self._done or self._page is None:
            return
        if not ready:
            QTimer.singleShot(100, self._check_fonts)
            return
        from PySide6.QtCore import QMarginsF
        from PySide6.QtGui import QPageLayout, QPageSize

        layout = QPageLayout(QPageSize(QPageSize.PageSizeId.A4), QPageLayout.Orientation.Portrait, QMarginsF(12, 14, 12, 15), QPageLayout.Unit.Millimeter)
        self._page.printToPdf(self._printed, layout)

    def _printed(self, data: Any) -> None:
        if self._done:
            return
        temporary: Path | None = None
        try:
            pdf = bytes(data)
            if len(pdf) < 100 or not pdf.startswith(b"%PDF-"):
                raise ValueError("The renderer returned an empty PDF.")
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.destination.parent, prefix=".pdf-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(pdf)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.destination)
            self._done = True
            self._cleanup()
            self.finished.emit(str(self.destination))
        except Exception as exc:
            self._fail(f"Could not save the PDF: {exc}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _fail(self, message: str) -> None:
        if self._done:
            return
        if self.used_native_fallback and not self._native_started:
            # A terminating Chromium page can emit both load failure and
            # process termination before the queued native fallback runs.
            return
        if not self.used_native_fallback and not self._native_started:
            self.used_native_fallback = True
            self.renderer_used = "native"
            self.fallback_reason = message
            _LOGGER.warning("Chromium PDF unavailable; using native Qt: %s", message)
            self._cleanup()
            QTimer.singleShot(0, self._native_print)
            return
        self._done = True
        self._cleanup()
        self.failed.emit(message)

    def _native_print(self) -> None:
        if self._done or self._native_started:
            return
        self._native_started = True
        try:
            from PySide6.QtGui import QGuiApplication

            if not isinstance(QGuiApplication.instance(), QGuiApplication):
                raise RuntimeError("PDF export requires a running desktop application.")
            self._printed(_native_pdf_bytes(self.html))
        except Exception as exc:
            self._done = True
            self._cleanup()
            self.failed.emit(f"Could not generate the PDF with either renderer: {exc}")

    def _cleanup(self) -> None:
        self._timer.stop()
        if self._page is not None:
            self._page.deleteLater()
            self._page = None
        if self._profile is not None:
            self._profile.deleteLater()
            self._profile = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

