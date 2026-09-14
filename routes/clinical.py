"""
The clinical record: labs, ANA, events, medications and the clinician
roster; CSV lab import; and the uploaded clinical document library.
"""

import json
import os
from datetime import date
from flask import render_template, request, redirect, url_for, Response, send_from_directory
from flask_login import login_required
import db

from appcore import CONFIG, DATA_DIR, app, get_user_prefs, uid
from flaremodel import VALID_FLARE_SEVERITIES


# ============================================================
# Clinical record
# ============================================================

@app.route("/clinical")
def clinical_record():
    """Record - labs, ANA, meds, events, clinicians."""
    labs = db.get_lab_results(uid())
    ana = db.get_ana_results(uid())
    meds = db.get_all_medications(uid())
    events = db.get_clinical_events(uid())
    clinicians = db.get_all_clinicians(uid())
    documents = db.get_clinical_documents(uid())
    test_names = db.get_lab_test_names(uid())

    # Split active/inactive meds
    today_str = date.today().isoformat()
    active = [m for m in meds
              if m["start_date"] <= today_str and
                 (m.get("end_date") is None or m["end_date"] >= today_str)]
    inactive = [m for m in meds
                if m.get("end_date") and m["end_date"] < today_str]

    # Build taper schedule lookup keyed by medication_id
    taper_by_med = {}
    for med in active:
        t = db.get_active_taper_for_medication(uid(), med["id"])
        if t:
            taper_by_med[med["id"]] = t

    prefs = get_user_prefs()
    ntfy_configured = bool(prefs.get("ntfy_topic") or CONFIG.get("ntfy_topic"))

    # Flare history for backfill tab
    all_obs = db.get_all_daily_observations(uid())
    flare_history = [
        {"date": o["date"], "flare_severity": o.get("flare_severity"), "notes": o.get("notes")}
        for o in sorted(all_obs, key=lambda x: x["date"], reverse=True)
        if o.get("flare_occurred") == 1
    ]

    return render_template(
        "clinical_record.html",
        labs=labs,
        ana=ana,
        meds=meds,
        active=active,
        inactive=inactive,
        events=events,
        clinicians=clinicians,
        documents=documents,
        test_names=test_names,
        today=date.today().isoformat(),
        taper_by_med=taper_by_med,
        ntfy_configured=ntfy_configured,
        flare_history=flare_history,
    )
    
@app.route("/medication/update/<int:med_id>", methods=["POST"])
def update_medication(med_id):
    """Update an existing medication. Auto-logs a dose_change event when the
    dose value or its unit meaningfully changes; first-time dose entry is
    suppressed so brand-new doses don't show up as a "change"."""
    form = request.form

    current = db.get_medication(uid(), med_id)
    old_dose = current["dose"] if current else None
    old_unit = current["unit"] if current else None

    new_dose = float(form.get("dose")) if form.get("dose") else None
    new_unit = form.get("unit") or None

    db.update_medication(
        user_id=uid(),
        med_id=med_id,
        drug_name=form.get("drug_name"),
        dose=new_dose,
        unit=new_unit,
        frequency=form.get("frequency") or None,
        category=form.get("category") or None,
        indication=form.get("indication") or None,
        start_date=form.get("start_date"),
        end_date=form.get("end_date") or None,
        notes=form.get("notes") or None,
        is_primary_intervention=form.get("is_primary_intervention") == "1",
        is_secondary_intervention=form.get("is_secondary_intervention") == "1",
    )

    should_log = False
    if old_dose is not None and new_dose is not None:
        if old_dose != new_dose or old_unit != new_unit:
            should_log = True
    elif old_dose is not None and new_dose is None:
        should_log = True

    if should_log:
        if old_unit == new_unit and old_unit and new_dose is not None:
            note = f"dose: {old_dose} → {new_dose} {old_unit}"
        else:
            old_str = f"{old_dose} {old_unit}" if old_unit else f"{old_dose}"
            new_str = "(removed)" if new_dose is None else (
                f"{new_dose} {new_unit}" if new_unit else f"{new_dose}"
            )
            note = f"dose: {old_str} → {new_str}"
        db.add_medication_event(
            user_id=uid(),
            medication_id=med_id,
            event_date=date.today().isoformat(),
            event_type="dose_change",
            severity=None,
            note=note,
        )

    return redirect(url_for("clinical_record") + "#medications")


@app.route("/medication/delete/<int:med_id>", methods=["POST"])
def delete_medication(med_id):
    """Delete a medication."""
    db.delete_medication(uid(), med_id)
    return redirect(url_for("clinical_record") + "#medications")


#============================================================
# Clinician management
#============================================================

@app.route("/clinician/add", methods=["POST"])
def add_clinician():
    """Add a new clinician."""
    db.add_clinician(uid(), {
        "name": request.form.get("name"),
        "specialty": request.form.get("specialty"),
        "clinic_name": request.form.get("clinic_name") or None,
        "address": request.form.get("address") or None,
        "phone": request.form.get("phone") or None,
        "email": request.form.get("email") or None,
        "network": request.form.get("network") or None,
        "notes": request.form.get("notes") or None,
    })
    return redirect(url_for("clinical_record") + "#clinicians")


@app.route("/clinician/update/<int:clinician_id>", methods=["POST"])
def update_clinician(clinician_id):
    """Update an existing clinician."""
    form = request.form
    
    db.update_clinician(
        user_id=uid(),
        clinician_id=clinician_id,
        name=form.get("name"),
        specialty=form.get("specialty"),
        clinic_name=form.get("clinic_name") or None,
        address=form.get("address") or None,
        phone=form.get("phone") or None,
        email=form.get("email") or None,
        network=form.get("network") or None,
        notes=form.get("notes") or None,
    )
    
    return redirect(url_for("clinical_record") + "#clinicians")


@app.route("/clinician/delete/<int:clinician_id>", methods=["POST"])
def delete_clinician(clinician_id):
    """Delete a clinician."""
    db.delete_clinician(uid(), clinician_id)
    return redirect(url_for("clinical_record") + "#clinicians")


# ============================================================
# Clinical record - add entries
# ============================================================

@app.route("/clinical/lab/add", methods=["POST"])
def add_lab():
    """Add a lab result."""
    form = request.form
    data = {
        "date": form.get("date"),
        "test_name": form.get("test_name", "").strip(),
        "numeric_value": float(form["numeric_value"])
            if form.get("numeric_value", "").strip() else None,
        "unit": form.get("unit", "").strip() or None,
        "qualitative_result": form.get("qualitative_result", "").strip() or None,
        "reference_range": form.get("reference_range", "").strip() or None,
        "flag": form.get("flag", "").strip() or None,
        "provider": form.get("provider", "").strip() or None,
        "lab_facility": form.get("lab_facility", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
    }
    db.add_lab_result(uid(), data)
    return redirect(url_for("clinical_record") + "#labs")


# ============================================================
# Bulk lab import (CSV upload / paste) with a review step
# ============================================================

def _lab_dedup_key(date_str, test_name, num, qual):
    """Stable key for spotting a lab already in the record.

    Numeric values are formatted with %g so "3" and "3.0" collapse to one key.
    """
    if num is not None:
        try:
            v = "%g" % float(num)
        except (TypeError, ValueError):
            v = str(num).strip().lower()
    else:
        v = (qual or "").strip().lower()
    return (date_str, (test_name or "").strip().lower(), v)


def _normalize_lab_rows(text, existing_keys):
    """Parse a lab CSV (Date, Test, Value, Units[, Lab, Doctor, ...]) into
    lab dicts, auto-filling reference range/flag for known tests and tagging
    each row 'new' or 'duplicate' against what's already stored.

    Header matching is case-insensitive and tolerant of a few aliases, so the
    same parser handles the portal exports and the hand-kept CSVs.
    """
    import csv, io
    from import_labs import lookup_reference, parse_date, parse_float

    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for raw in reader:
        low = {(k or "").strip().lower(): (v or "").strip()
               for k, v in raw.items()}

        def col(*names):
            for n in names:
                if low.get(n):
                    return low[n]
            return ""

        date_str = parse_date(col("date", "collected", "date collected", "result date"))
        test = col("test", "test_name", "test name", "analyte", "name")
        valraw = col("value", "result", "numeric_value", "observation value")
        if not date_str or not test or not valraw:
            continue

        # Keep bounded/inequality results ("<20", ">24.0") and titers as text.
        # parse_float would strip the "<" and store a bare 20, which reads like a
        # real value at the threshold rather than a negative below it.
        if "<" in valraw or ">" in valraw:
            num, qual = None, valraw
        else:
            num = parse_float(valraw)
            qual = None if num is not None else valraw
        unit = col("units", "unit") or None
        provider = col("doctor", "provider", "ordering provider") or None
        facility = col("lab", "facility", "lab_facility", "lab facility") or None
        refrange = col("reference", "reference_range", "reference range", "range") or None
        flag = col("flag", "abnormal") or None

        if num is not None and (not refrange or not flag):
            rr, fl = lookup_reference(test, num)
            refrange = refrange or rr
            flag = flag or fl

        key = _lab_dedup_key(date_str, test, num, qual)
        rows.append({
            "date": date_str, "test_name": test,
            "numeric_value": num, "qualitative_result": qual,
            "unit": unit, "reference_range": refrange, "flag": flag,
            "provider": provider, "lab_facility": facility,
            "status": "duplicate" if key in existing_keys else "new",
        })
    return rows


@app.route("/clinical/labs/import/preview", methods=["POST"])
@login_required
def import_labs_preview():
    """Parse an uploaded or pasted lab CSV and show a review table.

    Nothing is written yet — the parsed rows ride along as hidden JSON and are
    only committed from the preview page, so a bad file never touches the DB.
    """
    text = ""
    f = request.files.get("csv_file")
    if f and f.filename:
        text = f.read().decode("utf-8", errors="replace")
    if not text.strip():
        text = request.form.get("csv_text", "") or ""
    if not text.strip():
        return redirect(url_for("clinical_record") + "#labs")

    existing = db.get_lab_results(uid())
    existing_keys = {
        _lab_dedup_key(l["date"], l["test_name"],
                       l.get("numeric_value"), l.get("qualitative_result"))
        for l in existing
    }
    rows = _normalize_lab_rows(text, existing_keys)
    return render_template(
        "lab_import_preview.html",
        rows=rows,
        rows_json=json.dumps(rows),
        n_new=sum(1 for r in rows if r["status"] == "new"),
        n_dup=sum(1 for r in rows if r["status"] == "duplicate"),
    )


@app.route("/clinical/labs/import/commit", methods=["POST"])
@login_required
def import_labs_commit():
    """Insert only the rows the user kept checked on the preview page."""
    try:
        rows = json.loads(request.form.get("rows_json", "[]"))
    except (ValueError, TypeError):
        rows = []
    include = set(request.form.getlist("include"))
    inserted = 0
    for i, r in enumerate(rows):
        if str(i) not in include:
            continue
        data = {k: r.get(k) for k in (
            "date", "test_name", "numeric_value", "qualitative_result",
            "unit", "reference_range", "flag", "provider", "lab_facility")}
        try:
            db.add_lab_result(uid(), data)
            inserted += 1
        except Exception:
            continue
    return redirect(url_for("clinical_record") + "#labs")


# ============================================================
# Clinical document library (uploaded PDFs, stored on disk)
# ============================================================
DOCUMENTS_DIR = os.path.join(DATA_DIR, "documents")


def _user_docs_dir(user_id: int) -> str:
    d = os.path.join(DOCUMENTS_DIR, f"user_{user_id}")
    os.makedirs(d, exist_ok=True)
    return d


def _extract_pdf_text(blob: bytes) -> str | None:
    """Best-effort text extraction for search. Prefers poppler's pdftotext
    (usually present, much better results here), falls back to pypdf. Returns
    None for scanned PDFs (no text layer) or if neither is available — the
    summary field carries search then, so this never blocks an upload."""
    # 1) pdftotext (poppler)
    try:
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tf:
            tf.write(blob)
            tf.flush()
            out = subprocess.run(["pdftotext", "-q", tf.name, "-"],
                                 capture_output=True, timeout=30)
        text = out.stdout.decode("utf-8", "replace").strip()
        if text:
            return text[:100000]
    except Exception:
        pass
    # 2) pypdf fallback
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(blob))
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        return text[:100000] or None
    except Exception:
        return None


@app.route("/clinical/document/add", methods=["POST"])
@login_required
def add_document():
    """Store an uploaded PDF on disk and record its metadata.

    The file is saved under a random token name (no traversal, no collisions);
    the original filename is kept only for display and download.
    """
    import secrets
    f = request.files.get("pdf_file")
    form = request.form
    if not (f and f.filename) or not f.filename.lower().endswith(".pdf"):
        return redirect(url_for("clinical_record") + "#documents")
    blob = f.read()
    if not blob or len(blob) > 20 * 1024 * 1024:   # 20 MB cap
        return redirect(url_for("clinical_record") + "#documents")

    title = (form.get("title") or "").strip() or \
        os.path.splitext(os.path.basename(f.filename))[0]
    stored = secrets.token_hex(16) + ".pdf"
    with open(os.path.join(_user_docs_dir(uid()), stored), "wb") as out:
        out.write(blob)
    db.add_clinical_document(uid(), {
        "date": form.get("date") or None,
        "title": title,
        "doc_type": form.get("doc_type") or None,
        "specialty": form.get("specialty") or None,
        "provider": form.get("provider") or None,
        "facility": form.get("facility") or None,
        "file_name": stored,
        "orig_name": os.path.basename(f.filename),
        "summary": (form.get("summary") or "").strip() or None,
        "extracted_text": _extract_pdf_text(blob),
    })
    return redirect(url_for("clinical_record") + "#documents")


@app.route("/clinical/document/<int:doc_id>/file")
@login_required
def document_file(doc_id):
    """Serve a stored PDF, scoped to the owning user. Inline by default;
    ?download=1 forces a download."""
    doc = db.get_clinical_document(uid(), doc_id)
    if not doc or not doc.get("file_name"):
        return Response("Not found", status=404)
    return send_from_directory(
        _user_docs_dir(uid()), doc["file_name"],
        mimetype="application/pdf",
        as_attachment=bool(request.args.get("download")),
        download_name=doc.get("orig_name") or "document.pdf",
    )


@app.route("/clinical/document/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    """Remove a document row and its file from disk (scoped to the user)."""
    fname = db.delete_clinical_document(uid(), doc_id)
    if fname:
        try:
            os.remove(os.path.join(_user_docs_dir(uid()), fname))
        except OSError:
            pass
    return redirect(url_for("clinical_record") + "#documents")


@app.route("/clinical/ana/add", methods=["POST"])
def add_ana():
    """Add an ANA result."""
    form = request.form
    patterns_raw = form.get("patterns", "").strip()
    patterns = [p.strip() for p in patterns_raw.split(",") if p.strip()]

    db.add_ana_result(
        user_id=uid(),
        date_str=form.get("date"),
        titer_integer=int(form["titer"]) if form.get("titer", "").strip() else None,
        screen_result=form.get("screen_result", "").strip(),
        patterns=patterns,
        provider=form.get("provider", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
    )
    return redirect(url_for("clinical_record") + "#ana")


@app.route("/clinical/event/add", methods=["POST"])
def add_event():
    """Add a clinical event."""
    form = request.form
    data = {
        "date": form.get("date"),
        "event_type": form.get("event_type", "").strip(),
        "provider": form.get("provider", "").strip() or None,
        "facility": form.get("facility", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        "follow_up_date": form.get("follow_up_date", "").strip() or None,
    }
    db.add_clinical_event(uid(), data)
    return redirect(url_for("clinical_record") + "#events")


@app.route("/backfill/flare", methods=["POST"])
@login_required
def backfill_flare():
    """Record a past flare event (backfill for gaps in tracking)."""
    form = request.form
    flare_date = form.get("date", "").strip()
    severity = form.get("flare_severity", "").strip()
    notes = form.get("notes", "").strip() or None

    if not flare_date or severity not in VALID_FLARE_SEVERITIES:
        return redirect(url_for("clinical_record", msg="Date and severity are required.") + "#backfill")

    try:
        parsed_date = date.fromisoformat(flare_date)
    except ValueError:
        return redirect(url_for("clinical_record", msg="Invalid date format.") + "#backfill")

    if parsed_date > date.today():
        return redirect(url_for("clinical_record", msg="Cannot backfill a future date.") + "#backfill")

    data = {
        "date": flare_date,
        "flare_occurred": 1,
        "flare_severity": severity,
    }
    if notes:
        data["notes"] = notes

    db.upsert_daily_observations(uid(), data)
    severity_label = "ER visit" if severity == "er_visit" else severity
    return redirect(url_for("clinical_record", msg=f"Recorded {severity_label} flare on {flare_date}.") + "#backfill")


@app.route("/backfill/flare/update", methods=["POST"])
@login_required
def backfill_flare_update():
    """Update a backfilled flare entry."""
    form = request.form
    flare_date = form.get("date", "").strip()
    severity = form.get("flare_severity", "").strip()
    notes = form.get("notes", "").strip() or None

    if not flare_date or severity not in VALID_FLARE_SEVERITIES:
        return redirect(url_for("clinical_record", msg="Date and severity are required.") + "#backfill")

    data = {
        "date": flare_date,
        "flare_occurred": 1,
        "flare_severity": severity,
    }
    if notes is not None:
        data["notes"] = notes

    db.upsert_daily_observations(uid(), data)
    return redirect(url_for("clinical_record", msg=f"Updated flare on {flare_date}.") + "#backfill")


@app.route("/backfill/flare/delete", methods=["POST"])
@login_required
def backfill_flare_delete():
    """Remove flare flag from a daily observation (doesn't delete the whole row)."""
    flare_date = request.form.get("date", "").strip()
    if flare_date:
        data = {
            "date": flare_date,
            "flare_occurred": 0,
            "flare_severity": None,
        }
        db.upsert_daily_observations(uid(), data)
    return redirect(url_for("clinical_record", msg=f"Removed flare on {flare_date}.") + "#backfill")


@app.route("/medication/add", methods=["POST"])
def add_medication():
    """Add a new medication."""
    db.add_medication(uid(), {
        "drug_name": request.form.get("drug_name"),
        "dose": request.form.get("dose"),
        "unit": request.form.get("unit"),
        "frequency": request.form.get("frequency"),
        "route": request.form.get("route"),
        "category": request.form.get("category"),
        "indication": request.form.get("indication"),
        "start_date": request.form.get("start_date"),
        "end_date": request.form.get("end_date") or None,
        "notes": request.form.get("notes"),
        "is_primary_intervention": request.form.get("is_primary_intervention") == "1",
        "is_secondary_intervention": request.form.get("is_secondary_intervention") == "1",
    })
    return redirect(url_for("clinical_record") + "#medications")

#=======================================
# Edit/Cancel/Delete
#=======================================

@app.route("/clinical/medication/end/<int:med_id>", methods=["POST"])
def end_medication(med_id):
    """Mark a medication as ended today."""
    end_date = request.form.get("end_date", date.today().isoformat())
    db.end_medication(uid(), med_id, end_date)
    return redirect(url_for("clinical_record") + "#medications")

# lab results update/delete

@app.route("/lab/update/<int:lab_id>", methods=["POST"])
def update_lab(lab_id):
    """Update an existing lab result."""
    form = request.form
    
    def get_float(key):
        val = form.get(key, "").strip()
        try:
            return float(val) if val else None
        except ValueError:
            return None
    
    db.update_lab_result(
        user_id=uid(),
        lab_id=lab_id,
        date=form.get("date"),
        test_name=form.get("test_name"),
        numeric_value=get_float("numeric_value"),
        unit=form.get("unit") or None,
        qualitative_result=form.get("qualitative_result") or None,
        reference_range=form.get("reference_range") or None,
        flag=form.get("flag") or None,
        provider=form.get("provider") or None,
        lab_facility=form.get("lab_facility") or None,
        notes=form.get("notes") or None,
    )
    
    return redirect(url_for("clinical_record") + "#labs")


@app.route("/lab/delete/<int:lab_id>", methods=["POST"])
def delete_lab(lab_id):
    """Delete a lab result."""
    db.delete_lab_result(uid(), lab_id)
    return redirect(url_for("clinical_record") + "#labs")


@app.route("/ana/update/<int:ana_id>", methods=["POST"])
def update_ana(ana_id):
    """Update an existing ANA result."""
    form = request.form
    
    db.update_ana_result(
        user_id=uid(),
        ana_id=ana_id,
        date=form.get("date"),
        titer=form.get("titer") or None,
        patterns=form.get("patterns") or None,
        screen_result=form.get("screen_result") or None,
        provider=form.get("provider") or None,
        notes=form.get("notes") or None,
    )
    
    return redirect(url_for("clinical_record") + "#ana")


@app.route("/ana/delete/<int:ana_id>", methods=["POST"])
def delete_ana(ana_id):
    """Delete an ANA result."""
    db.delete_ana_result(uid(), ana_id)
    return redirect(url_for("clinical_record") + "#ana")


@app.route("/event/update/<int:event_id>", methods=["POST"])
def update_event(event_id):
    """Update an existing clinical event."""
    form = request.form
    
    db.update_clinical_event(
        user_id=uid(),
        event_id=event_id,
        date=form.get("date"),
        event_type=form.get("event_type"),
        provider=form.get("provider") or None,
        facility=form.get("facility") or None,
        notes=form.get("notes") or None,
    )
    
    return redirect(url_for("clinical_record") + "#events")


@app.route("/event/delete/<int:event_id>", methods=["POST"])
def delete_event(event_id):
    """Delete a clinical event."""
    db.delete_clinical_event(uid(), event_id)
    return redirect(url_for("clinical_record") + "#events")
