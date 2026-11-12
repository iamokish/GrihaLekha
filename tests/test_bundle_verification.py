"""Checks for the verifier's fixture and failure detection, without a compiler."""

from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest


SPEC = importlib.util.spec_from_file_location("grihalekha_bundle_verifier", Path(__file__).resolve().parents[1] / "packaging" / "verify_build.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def test_fixture_leaves_real_work_for_the_compiled_app(tmp_path):
    directory = tmp_path / "account"
    fixture = verifier._prepare_account(directory)
    assert not (tmp_path / "original-payment-proof.png").exists()
    Ledger, _ = verifier._source_services()
    ledger = Ledger(directory)
    try:
        assert ledger.list_tenancies()[0]["id"] == fixture["tenant_id"]
        assert ledger.list_charges(fixture["draft_tenant_id"]) == []
        balance = ledger.balance(fixture["tenant_id"], fixture["as_of"])
        assert balance["outstanding_paise"] == 100_075
        assert balance["credit_paise"] == 0
        rent = ledger.get_bill(fixture["rent_charge_id"], fixture["as_of"])
        electricity = ledger.get_bill(fixture["electricity_charge_id"], fixture["as_of"])
        assert rent["paid_paise"] == rent["amount_paise"] == 1_000_050
        assert rent["outstanding_paise"] == 0
        assert electricity["electricity_cycle_start"] == rent["cycle_start"]
        assert electricity["paid_paise"] == 0
        assert electricity["outstanding_paise"] == 80_025
        with pytest.raises(ValueError, match="did not generate monthly rent and electricity drafts"):
            verifier._check_populated_account(directory, fixture)
        ledger.prepare_monthly_drafts(fixture["as_of"])
    finally:
        ledger.close()
    result = verifier._check_populated_account(directory, fixture)
    assert result["generated_drafts"] >= 2
    assert result["generated_drafts_by_kind"]["rent"] >= 1
    assert result["generated_drafts_by_kind"]["electricity"] >= 1
    assert result["outstanding_paise"] == 100_075


@pytest.mark.parametrize("change", ["amount_paise=amount_paise-1", "effective_on='2000-01-01'"])
def test_account_verifier_detects_a_changed_allocation(tmp_path, change):
    directory = tmp_path / "account"
    fixture = verifier._prepare_account(directory)
    Ledger, _ = verifier._source_services()
    ledger = Ledger(directory)
    try:
        ledger.prepare_monthly_drafts(fixture["as_of"])
    finally:
        ledger.close()
    with closing(sqlite3.connect(directory / "grihalekha.sqlite3")) as connection, connection:
        connection.execute(f"UPDATE allocations SET {change} WHERE payment_id=?", (fixture["payment_id"],))
    with pytest.raises(ValueError, match="selective rent payment"):
        verifier._check_populated_account(directory, fixture)


def test_account_verifier_requires_electricity_drafts_too(tmp_path):
    directory = tmp_path / "account"
    fixture = verifier._prepare_account(directory)
    Ledger, _ = verifier._source_services()
    ledger = Ledger(directory)
    try:
        ledger.prepare_rent_drafts(fixture["as_of"])
    finally:
        ledger.close()
    with pytest.raises(ValueError, match="did not generate monthly rent and electricity drafts"):
        verifier._check_populated_account(directory, fixture)


def test_native_pdf_check_uses_exact_money_and_embedded_fonts(qapp, tmp_path):
    from grihalekha.reports import _native_pdf_bytes, statement_html

    directory = tmp_path / "account"
    fixture = verifier._prepare_account(directory)
    Ledger, _ = verifier._source_services()
    ledger = Ledger(directory)
    try:
        tenant = ledger.get_tenancy(fixture["tenant_id"])
        snapshot = ledger.statement(fixture["tenant_id"], tenant["opening_date"], fixture["as_of"])
    finally:
        ledger.close()
    destination = tmp_path / "statement.pdf"
    destination.write_bytes(_native_pdf_bytes(statement_html(snapshot, "bilingual")))
    result = verifier._inspect_pdf(destination, tmp_path, "native-check")
    assert result["pages"] >= 1
    assert all(font["embedded_bytes"] > 0 for font in result["fonts"])
    assert result["hindi_extraction_required"] is False


def _fake_resources(tmp_path):
    bundle = tmp_path / "bundle"
    fonts = tmp_path / "source-fonts"
    qt = tmp_path / "source-qt"
    fonts.mkdir()
    (qt / "resources").mkdir(parents=True)
    (qt / "translations" / "qtwebengine_locales").mkdir(parents=True)
    (bundle / "assets" / "fonts").mkdir(parents=True)
    (bundle / "grihalekha" / "assets" / "icons").mkdir(parents=True)
    (bundle / "grihalekha" / "assets" / "ui").mkdir(parents=True)
    (bundle / "PySide6" / "resources").mkdir(parents=True)
    (bundle / "PySide6" / "translations" / "qtwebengine_locales").mkdir(parents=True)
    for name in verifier.FONT_FILES:
        (fonts / name).write_bytes(f"Fixture source {name}".encode())
        shutil.copy2(fonts / name, bundle / "assets" / "fonts" / name)
    for name in verifier.ICON_FILES:
        shutil.copy2(verifier.ROOT / "icons" / name, bundle / "grihalekha" / "assets" / "icons" / name)
    for name in verifier.UI_ICON_FILES:
        shutil.copy2(verifier.ROOT / "src" / "grihalekha" / "assets" / "ui" / name, bundle / "grihalekha" / "assets" / "ui" / name)
    for name in verifier.WEBENGINE_FILES:
        (qt / "resources" / name).write_bytes(f"Fixture source {name}".encode())
        shutil.copy2(qt / "resources" / name, bundle / "PySide6" / "resources" / name)
    source_locale = qt / "translations" / "qtwebengine_locales" / "en-US.pak"
    source_locale.write_bytes(b"Fixture locale")
    shutil.copy2(source_locale, bundle / "PySide6" / "translations" / "qtwebengine_locales" / source_locale.name)
    helper = bundle / ("QtWebEngineProcess.exe" if os.name == "nt" else "QtWebEngineProcess")
    helper.write_bytes(b"Fixture helper")
    if os.name != "nt":
        helper.chmod(0o755)
    (bundle / "qt6.conf").write_text("[Paths]\nPrefix=.\n")
    return bundle, fonts, qt


@pytest.mark.parametrize("relative", ["assets/fonts/NotoSans-Regular.ttf", "PySide6/resources/icudtl.dat", "PySide6/translations/qtwebengine_locales/en-US.pak"])
def test_resource_verifier_detects_changed_fonts_and_engine_data(tmp_path, relative):
    bundle, fonts, qt = _fake_resources(tmp_path)
    assert verifier._check_resources(bundle, fonts, qt)["webengine_helpers"]
    (bundle / relative).write_bytes(b"Different data")
    with pytest.raises(ValueError, match="differs"):
        verifier._check_resources(bundle, fonts, qt)


def test_verification_preserves_failure_report(tmp_path):
    output = tmp_path / "reports"
    output.mkdir()
    previous = output / "previous-check.txt"
    previous.write_text("Preserve me")
    with pytest.raises(verifier.BundleVerificationError) as caught:
        verifier.verify_bundle(tmp_path / "missing-bundle", output)
    report = json.loads(caught.value.report_path.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert "executable is missing" in report["error"]
    assert previous.read_text() == "Preserve me"


def test_child_environment_cannot_borrow_source_paths_or_disable_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "source-package")
    monkeypatch.setenv("QT_PLUGIN_PATH", "source-qt")
    monkeypatch.setenv("QTWEBENGINE_DISABLE_SANDBOX", "1")
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    env = verifier._process_environment(tmp_path, tmp_path / "bundle", "auto")
    assert env["GRIHALEKHA_PDF_RENDERER"] == "auto"
    assert env["QT_QPA_PLATFORM"] == "offscreen"
    assert "PYTHONPATH" not in env
    assert "QT_PLUGIN_PATH" not in env
    assert "QTWEBENGINE_DISABLE_SANDBOX" not in env
    assert "QTWEBENGINE_CHROMIUM_FLAGS" not in env
