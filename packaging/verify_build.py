"""Verify a relocated standalone bundle using isolated account data.

Run from a development environment with the project's optional dev dependencies:
    python packaging/verify_build.py dist/GrihaLekha --output-dir build/verification

No real account folder is read or modified. Each invocation preserves its own
fixtures, screenshots, PDFs, subprocess logs and JSON report for inspection.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import zlib


ROOT = Path(__file__).resolve().parents[1]
FONT_FILES = (
    "NotoSans-Regular.ttf",
    "NotoSans-Bold.ttf",
    "NotoSansDevanagari-Regular.ttf",
    "NotoSansDevanagari-Bold.ttf",
    "OFL-NotoSans.txt",
    "OFL-Devanagari.txt",
)
ICON_FILES = ("grihalekha.ico", "grihalekha-icon-512.png")
UI_ICON_FILES = ("chevron-down.svg", "chevron-up.svg", "chevron-left.svg", "chevron-right.svg", "check.svg")
WEBENGINE_FILES = (
    "icudtl.dat",
    "v8_context_snapshot.bin",
    "qtwebengine_resources.pak",
    "qtwebengine_resources_100p.pak",
    "qtwebengine_resources_200p.pak",
)
EXPECTED_AMOUNTS = ("10,000.50", "800.25", "200.50", "1,000.75")
PROCESS_TIMEOUT = 120


class BundleVerificationError(RuntimeError):
    """A failed verification whose report and diagnostic files were preserved."""

    def __init__(self, message: str, report_path: Path):
        super().__init__(f"{message} See {report_path}")
        self.report_path = report_path


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _matching_files(directory: Path, name: str) -> list[Path]:
    matches = sorted(path for path in directory.rglob(name) if path.is_file())
    for path in matches:
        _require(path.resolve().is_relative_to(directory.resolve()), f"Bundled file points outside the bundle: {path}")
    return matches


def _matching_hashes(bundle: Path, name: str, source: Path) -> list[dict]:
    _require(source.is_file(), f"The build environment is missing source resource {source}.")
    matches = _matching_files(bundle, name)
    _require(bool(matches), f"The compiled bundle is missing {name}.")
    expected = _hash(source)
    result = []
    for path in matches:
        _require(path.stat().st_size > 0, f"Bundled resource is empty: {path}")
        actual = _hash(path)
        _require(actual == expected, f"Bundled resource differs from the installed source: {path}")
        result.append({"path": path.relative_to(bundle).as_posix(), "sha256": actual, "bytes": path.stat().st_size})
    return result


def _check_resources(bundle: Path, font_dir: Path | None = None, qt_dir: Path | None = None) -> dict:
    if font_dir is None:
        font_dir = ROOT / "src" / "grihalekha" / "assets" / "fonts"
    if qt_dir is None:
        import PySide6

        qt_dir = Path(PySide6.__file__).resolve().parent
    _require(not (bundle / "_internal").exists(), "The bundle contains an old _internal layout; build Nuitka into a fresh staging directory.")
    result: dict = {"fonts_and_licenses": [], "icons": [], "ui_icons": [], "webengine_resources": [], "locales": []}
    for name in FONT_FILES:
        result["fonts_and_licenses"].extend(_matching_hashes(bundle, name, font_dir / name))
    for name in ICON_FILES:
        result["icons"].extend(_matching_hashes(bundle, name, ROOT / "icons" / name))
    for name in UI_ICON_FILES:
        result["ui_icons"].extend(_matching_hashes(bundle, name, ROOT / "src" / "grihalekha" / "assets" / "ui" / name))
    for name in WEBENGINE_FILES:
        sources = _matching_files(qt_dir, name)
        _require(bool(sources), f"The installed PySide6 is missing WebEngine resource {name}.")
        result["webengine_resources"].extend(_matching_hashes(bundle, name, sources[0]))

    helper_name = "QtWebEngineProcess.exe" if os.name == "nt" else "QtWebEngineProcess"
    helpers = _matching_files(bundle, helper_name)
    _require(bool(helpers), f"The compiled bundle is missing {helper_name}.")
    for helper in helpers:
        _require(helper.stat().st_size > 0, f"The WebEngine helper is empty: {helper}")
        if os.name != "nt":
            _require(os.access(helper, os.X_OK), f"The WebEngine helper is not executable: {helper}")
    result["webengine_helpers"] = [path.relative_to(bundle).as_posix() for path in helpers]
    configs = _matching_files(bundle, "qt6.conf")
    _require(bool(configs), "The compiled bundle is missing Nuitka's qt6.conf.")
    for config in configs:
        _require("[Paths]" in config.read_text(encoding="utf-8-sig"), f"The bundled Qt configuration has no [Paths] section: {config}")
    result["qt_configs"] = [path.relative_to(bundle).as_posix() for path in configs]

    locales = sorted(path for path in bundle.rglob("*.pak") if path.parent.name == "qtwebengine_locales" and path.is_file())
    _require(any(path.name == "en-US.pak" for path in locales), "The compiled bundle is missing the en-US WebEngine locale.")
    source_locales = {path.name: path for path in qt_dir.rglob("*.pak") if path.parent.name == "qtwebengine_locales" and path.is_file()}
    for path in locales:
        _require(path.resolve().is_relative_to(bundle), f"Bundled locale points outside the bundle: {path}")
        source = source_locales.get(path.name)
        _require(source is not None, f"No installed source exists for bundled locale {path.name}.")
        result["locales"].append({"path": path.relative_to(bundle).as_posix(), "sha256": _hash(path)})
        _require(_hash(path) == _hash(source), f"Bundled WebEngine locale differs from its source: {path}")
    return result


def _source_services():
    source = str(ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from grihalekha.backups import BackupManager
    from grihalekha.ledger import Ledger

    return Ledger, BackupManager


def _png() -> bytes:
    def chunk(kind: bytes, content: bytes) -> bytes:
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\x20\x80\x50\xff")) + chunk(b"IEND", b"")


def _prepare_account(directory: Path) -> dict:
    Ledger, _ = _source_services()
    from grihalekha.clock import today_ist

    today = today_ist()
    start = today.replace(day=1)
    previous = date(start.year - 1, 12, 1) if start.month == 1 else date(start.year, start.month - 1, 1)
    proof = directory.parent / "original-payment-proof.png"
    proof.write_bytes(_png())
    proof_hash = _hash(proof)
    ledger = Ledger(directory)
    try:
        building = ledger.add_building("कृष्णा निवास", "12 उदाहरण मार्ग, नई दिल्ली")
        flat = ledger.add_flat(building, "201", "2", 2)
        tenant = ledger.add_tenancy(flat, "अमित कुमार", start.isoformat(), 1, 1_000_050, members=[{"name": "दृष्टि त्रिवेदी", "relationship": "Spouse"}])
        ledger.prepare_monthly_drafts(today.isoformat())
        monthly_bills = {bill["kind"]: bill for bill in ledger.list_charges(tenant)}
        rent = monthly_bills["rent"]
        ledger.post_charge(rent["id"])
        electricity = monthly_bills["electricity"]["id"]
        ledger.post_charge(electricity, amount_paise=80_025)
        fee = ledger.add_charge(tenant, "misc", 20_050, start.isoformat(), today.isoformat(), today.isoformat(), "दरवाज़े के ताले की मरम्मत")
        payment = ledger.deposit_payment(tenant, 1_000_050, today.isoformat(), "upi", reference="BUNDLE-VERIFY-ONLY", proof_path=proof)
        ledger.mark_bill_paid(rent["id"], effective_on=today.isoformat())
        managed_proof = ledger.list_payments(tenant)[0]["proof_path"]
        # Added after draft preparation, with an earlier move-in so the populated
        # account remains first in the app's newest-tenancy statement selection.
        second_flat = ledger.add_flat(building, "202", "2", 1)
        second = ledger.add_tenancy(second_flat, "नया परिवार", previous.isoformat(), 1, 900_000)
        _require(not ledger.list_charges(second), "The second audit tenancy must start with no monthly drafts.")
        _require(ledger.list_tenancies()[0]["id"] == tenant, "The PDF smoke check would select the wrong audit tenancy.")
        fixture = {"as_of": today.isoformat(), "tenant_id": tenant, "draft_tenant_id": second, "rent_charge_id": rent["id"], "electricity_charge_id": electricity, "fee_charge_id": fee, "payment_id": payment, "proof_path": managed_proof, "proof_sha256": proof_hash, "expected_outstanding_paise": 100_075, "expected_paid_paise": 1_000_050}
    finally:
        ledger.close()
    proof.unlink()
    return fixture


def _process_environment(run_dir: Path, bundle: Path, renderer: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR", "QTWEBENGINEPROCESS_PATH", "QTWEBENGINE_RESOURCES_PATH", "QTWEBENGINE_LOCALES_PATH", "QTWEBENGINE_DISABLE_SANDBOX", "QTWEBENGINE_CHROMIUM_FLAGS", "QT_LOGGING_RULES", "LD_LIBRARY_PATH"):
        env.pop(key, None)
    temporary = run_dir / "temporary"
    temporary.mkdir(exist_ok=True)
    env.update(QT_QPA_PLATFORM="offscreen", GRIHALEKHA_PDF_RENDERER=renderer, TMP=str(temporary), TEMP=str(temporary), TMPDIR=str(temporary))
    if os.name == "nt":
        system = Path(env.get("SystemRoot", r"C:\Windows"))
        env["PATH"] = os.pathsep.join((str(bundle), str(system / "System32"), str(system)))
    else:
        env["PATH"] = os.pathsep.join((str(bundle), "/usr/bin", "/bin"))
    return env


def _stop_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        try:
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def _run_application(executable: Path, run_dir: Path, data_dir: Path, label: str, renderer: str = "auto", pdf: Path | None = None) -> dict:
    screenshot = run_dir / f"{label}.png"
    command = [str(executable), "--smoke-test", "--data-dir", str(data_dir), "--screenshot", str(screenshot)]
    if pdf is not None:
        command.extend(("--smoke-pdf", str(pdf)))
    options: dict = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options.update(startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        options["start_new_session"] = True
    log_path = data_dir / "logs" / "grihalekha.log"
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=run_dir, env=_process_environment(run_dir, executable.parent, renderer), stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=PROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b"Process output did not close after timeout."
    stdout_path, stderr_path = run_dir / f"{label}.stdout.log", run_dir / f"{label}.stderr.log"
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    app_log = ""
    if log_path.exists():
        with log_path.open("rb") as stream:
            stream.seek(log_offset)
            app_log = stream.read().decode("utf-8", errors="replace")
    (run_dir / f"{label}.application.log").write_text(app_log, encoding="utf-8")
    details = {"command": command, "exit_code": process.returncode, "seconds": round(time.monotonic() - started, 3), "stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "application_log": str(run_dir / f"{label}.application.log"), "screenshot": str(screenshot), "renderer_requested": renderer}
    (run_dir / f"{label}.process.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
    _require(not timed_out, f"Compiled {label} check timed out after {PROCESS_TIMEOUT} seconds; see {stderr_path}.")
    _require(process.returncode == 0, f"Compiled {label} check exited with code {process.returncode}; see {stderr_path} and its application log.")
    output = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace") + app_log
    missing = re.search(r"ModuleNotFoundError|ImportError:|No module named|Could not find QtWebEngineProcess|Could not find.*(?:WebEngine|resource|\.pak|icudtl)|(?:resource|\.pak|icudtl).*not found|Could not load.*Qt platform plugin", output, re.IGNORECASE)
    _require(missing is None, f"Compiled {label} check reported a missing module, helper or resource: {missing.group(0) if missing else ''}; see its logs.")
    _check_screenshot(screenshot)
    _require(not (data_dir / "application.lock").exists(), f"Compiled {label} check left the account lock behind.")
    if pdf is not None:
        renderer_match = re.findall(r"Packaged PDF check completed using (\w+)", app_log)
        _require(bool(renderer_match), f"Compiled {label} check did not log its PDF renderer.")
        details["renderer_used"] = renderer_match[-1]
        if renderer == "native":
            _require(renderer_match[-1] == "native", "The explicit native PDF check did not use native Qt.")
    return details


def _check_screenshot(path: Path) -> None:
    _require(path.is_file(), f"The compiled application did not save a screenshot: {path}")
    with path.open("rb") as stream:
        header = stream.read(24)
    _require(header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) == 24, f"Invalid application screenshot: {path}")
    width, height = struct.unpack(">II", header[16:24])
    _require(width >= 320 and height >= 200, f"The application screenshot has unexpected dimensions {width}×{height}.")


def _database_connection(path: Path):
    _require(path.is_file(), f"The compiled app did not create its database: {path}")
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))


def _check_database(connection: sqlite3.Connection) -> None:
    _require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "The compiled application's database failed its integrity check.")
    _require(connection.execute("PRAGMA foreign_key_check").fetchone() is None, "The compiled application's database has broken record references.")


def _check_empty_account(directory: Path) -> dict:
    with _database_connection(directory / "grihalekha.sqlite3") as connection:
        _check_database(connection)
        counts = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("buildings", "flats", "tenancies", "charges", "payments")}
    _require(all(value == 0 for value in counts.values()), "The fresh compiled app unexpectedly inserted tenant or financial records.")
    return counts


def _check_populated_account(directory: Path, fixture: dict) -> dict:
    with _database_connection(directory / "grihalekha.sqlite3") as connection:
        _check_database(connection)
        _require(connection.execute("SELECT COUNT(*) FROM tenancies").fetchone()[0] == 2, "The compiled app changed the audit tenancy count.")
        draft_counts = dict(connection.execute("SELECT kind, COUNT(*) FROM charges WHERE tenancy_id=? AND status='draft' GROUP BY kind", (fixture["draft_tenant_id"],)))
        draft_count = sum(draft_counts.values())
        _require(draft_counts.get("rent", 0) > 0 and draft_counts.get("electricity", 0) > 0, "The compiled app did not generate monthly rent and electricity drafts for the newly added tenancy.")
        paid = connection.execute("SELECT amount_paise, paid_on, proof_path FROM payments WHERE id=?", (fixture["payment_id"],)).fetchone()
        _require(paid == (fixture["expected_paid_paise"], fixture["as_of"], fixture["proof_path"]), "The payment amount, receipt date or managed proof reference changed during compiled startup.")
        allocations = connection.execute("SELECT charge_id, amount_paise, effective_on FROM allocations WHERE payment_id=? AND cancelled_on IS NULL", (fixture["payment_id"],)).fetchall()
        _require(allocations == [(fixture["rent_charge_id"], fixture["expected_paid_paise"], fixture["as_of"])], "The selective rent payment or its effective date changed during compiled startup.")
        amounts = dict(connection.execute("SELECT id, amount_paise FROM charges WHERE id IN (?, ?, ?)", (fixture["rent_charge_id"], fixture["electricity_charge_id"], fixture["fee_charge_id"])))
        _require(amounts == {fixture["rent_charge_id"]: 1_000_050, fixture["electricity_charge_id"]: 80_025, fixture["fee_charge_id"]: 20_050}, "The compiled app changed the expected charge amounts.")
    proof = directory / fixture["proof_path"]
    _require(proof.resolve().is_relative_to(directory.resolve()) and proof.is_file(), "The payment proof is missing from managed storage.")
    _require(_hash(proof) == fixture["proof_sha256"], "The managed payment proof changed during compiled startup.")
    Ledger, _ = _source_services()
    ledger = Ledger(directory)
    try:
        balance = ledger.balance(fixture["tenant_id"], fixture["as_of"])
        _require(balance["outstanding_paise"] == fixture["expected_outstanding_paise"] and balance["net_paise"] == fixture["expected_outstanding_paise"] and balance["credit_paise"] == 0, "The compiled application's account does not preserve the exact expected balance.")
    finally:
        ledger.close()
    return {"tenancy_count": 2, "generated_drafts": draft_count, "generated_drafts_by_kind": draft_counts, "outstanding_paise": balance["outstanding_paise"], "credit_paise": balance["credit_paise"], "proof_sha256": fixture["proof_sha256"]}


def _inspect_pdf(path: Path, run_dir: Path, label: str) -> dict:
    try:
        import pymupdf
    except ImportError as error:
        raise ValueError("Compiled PDF verification needs the project's dev dependencies, including PyMuPDF. Run uv sync --extra dev.") from error
    _require(path.is_file() and path.stat().st_size > 1000, f"The compiled app did not produce a complete PDF: {path}")
    with pymupdf.open(path) as document:
        _require(len(document) > 0, "The compiled PDF has no pages.")
        extracted = "\n".join(page.get_text() for page in document)
        (run_dir / f"{label}.extracted.txt").write_text(extracted, encoding="utf-8")
        normalized = re.sub(r"\s+", "", extracted)
        _require("₹" in extracted, "The compiled PDF has no extractable rupee symbol.")
        for amount in EXPECTED_AMOUNTS:
            _require(amount in normalized, f"The compiled PDF is missing exact INR amount {amount}.")
        fonts = []
        for xref in sorted({font[0] for page in document for font in page.get_fonts()}):
            name, extension, kind, data = document.extract_font(xref)
            fonts.append({"name": name, "extension": extension, "type": kind, "embedded_bytes": len(data)})
        _require(bool(fonts) and all(font["embedded_bytes"] > 0 for font in fonts), "The compiled PDF contains fonts that are not embedded.")
        _require(any("Devanagari" in font["name"] for font in fonts), "The compiled PDF does not embed a Devanagari font.")
        preview = run_dir / f"{label}.page1.png"
        document[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(preview)
        return {"path": str(path), "pages": len(document), "bytes": path.stat().st_size, "fonts": fonts, "exact_inr_values": list(EXPECTED_AMOUNTS), "preview": str(preview), "hindi_extraction_required": False}


def _check_backup(directory: Path, fixture: dict, pdf: Path) -> dict:
    _, BackupManager = _source_services()
    manager = BackupManager(directory, directory / "grihalekha.sqlite3")
    backups = manager.list_backups()
    _require(1 <= len(backups) <= 2, "The compiled app did not create a managed backup on close, or exceeded retention.")
    manifest = manager.validate(backups[0])
    _require(fixture["proof_path"] in manifest["files"], "The compiled backup is missing the managed payment proof.")
    _require(pdf.relative_to(directory).as_posix() in manifest["files"], "The compiled backup is missing the generated PDF.")
    _require(manifest["files"][fixture["proof_path"]]["sha256"] == fixture["proof_sha256"], "The backed-up proof does not match the recorded payment proof.")
    return {"path": str(backups[0]), "managed_count": len(backups), "schema": manifest["schema"], "included_files": sorted(manifest["files"])}


def verify_bundle(bundle: Path, output_dir: Path, *, require_chromium: bool = False) -> dict:
    """Return a verification report; raise BundleVerificationError on failure."""
    bundle = Path(bundle).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=output_dir))
    report_path = run_dir / "report.json"
    report: dict = {"ok": False, "bundle": str(bundle), "run_dir": str(run_dir), "report_path": str(report_path), "started_at": datetime.now(timezone.utc).isoformat(), "checks": {}}
    try:
        executable = bundle / ("GrihaLekha.exe" if os.name == "nt" else "GrihaLekha")
        _require(executable.is_file(), f"Compiled application executable is missing: {executable}")
        _require(executable.resolve().is_relative_to(bundle), "The application executable points outside the bundle.")
        report["checks"]["resources"] = _check_resources(bundle)
        empty_dir = run_dir / "empty-account"
        report["checks"]["empty_startup"] = _run_application(executable, run_dir, empty_dir, "empty-startup")
        report["checks"]["empty_database"] = _check_empty_account(empty_dir)
        populated_dir = run_dir / "populated-account"
        fixture = _prepare_account(populated_dir)
        report["fixture"] = fixture
        auto_pdf = populated_dir / "documents" / "GL-VERIFY-AUTO.pdf"
        report["checks"]["auto_startup"] = _run_application(executable, run_dir, populated_dir, "auto-pdf", pdf=auto_pdf)
        report["checks"]["auto_pdf"] = _inspect_pdf(auto_pdf, run_dir, "auto-pdf")
        report["checks"]["populated_database"] = _check_populated_account(populated_dir, fixture)
        report["checks"]["compiled_backup"] = _check_backup(populated_dir, fixture, auto_pdf)
        native_pdf = populated_dir / "documents" / "GL-VERIFY-NATIVE.pdf"
        report["checks"]["native_startup"] = _run_application(executable, run_dir, populated_dir, "native-pdf", renderer="native", pdf=native_pdf)
        report["checks"]["native_pdf"] = _inspect_pdf(native_pdf, run_dir, "native-pdf")
        report["checks"]["populated_database_after_native"] = _check_populated_account(populated_dir, fixture)
        report["chromium_runtime_verified"] = report["checks"]["auto_startup"]["renderer_used"] == "webengine"
        report["limitations"] = ["Native Qt prints Hindi correctly but copying/searching some shaped Hindi characters can be inaccurate."]
        if not report["chromium_runtime_verified"]:
            report["limitations"].append("The default PDF check used native fallback. Bundled Chromium resources were verified, but Chromium execution was not established in this environment.")
        if require_chromium:
            _require(report["chromium_runtime_verified"], "This release check requires the Chromium PDF renderer; native fallback alone is insufficient.")
        report["ok"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise BundleVerificationError(str(error), report_path) from error
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated startup, PDF and backup checks against a compiled GrihaLekha bundle.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--require-chromium", action="store_true", help="Fail if the default renderer falls back to native Qt")
    args = parser.parse_args(argv)
    try:
        report = verify_bundle(args.bundle, args.output_dir, require_chromium=args.require_chromium)
    except BundleVerificationError as error:
        report = json.loads(error.report_path.read_text(encoding="utf-8"))
        print(json.dumps(report, ensure_ascii=True, indent=2), file=sys.stderr)
        return 1
    # ASCII JSON also works in legacy Windows consoles when paths contain Hindi.
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
