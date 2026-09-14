"""
Search, CSV exports, the clinical report, and the UV correlated report.
"""

import json
import os
from datetime import date, datetime
from flask import render_template, request, redirect, url_for, Response, send_file
import db
import zipfile
import csv
from io import StringIO

from appcore import CONFIG, DATA_DIR, app, get_location_key, get_user_prefs, uid
from flaremodel import CUSTOM_WEIGHTS_PATH, _inject_cycle_phase, _inject_scoring_context, _score_components
from routes.clinical import DOCUMENTS_DIR


#======================================
# Export Lab/Meds/Clinicians/Events
#======================================



def _write_patient_header(writer):
    """Write patient name/DOB metadata rows at the top of a CSV export."""
    prefs = get_user_prefs()
    name = prefs.get("patient_name") or CONFIG.get("patient_name", "")
    dob = prefs.get("patient_dob") or CONFIG.get("patient_dob", "")
    writer.writerow(["Patient:", name, "DOB:", dob])
    writer.writerow(["Export date:", date.today().isoformat()])
    writer.writerow([])

@app.route("/export/labs")
def export_labs():
    """Export lab results as CSV within date range."""
    start_date = request.args.get("start")
    end_date = request.args.get("end")
    
    if not start_date or not end_date:
        return "Missing date range parameters", 400
    
    # Get labs in date range
    all_labs = db.get_lab_results(uid())
    filtered_labs = [
        lab for lab in all_labs
        if start_date <= lab["date"] <= end_date
    ]
    
    # Sort by date (most recent first)
    filtered_labs.sort(key=lambda x: x["date"], reverse=True)
    
    # Create CSV
    output = StringIO()
    writer = csv.writer(output)
    _write_patient_header(writer)

    # Write header
    writer.writerow([
        'Date',
        'Test Name',
        'Numeric Value',
        'Unit',
        'Qualitative Result',
        'Reference Range',
        'Flag',
        'Provider',
        'Lab Facility',
        'Notes'
    ])
    
    # Write data rows
    for lab in filtered_labs:
        writer.writerow([
            lab.get('date', ''),
            lab.get('test_name', ''),
            lab.get('numeric_value', '') if lab.get('numeric_value') is not None else '',
            lab.get('unit', '') or '',
            lab.get('qualitative_result', '') or '',
            lab.get('reference_range', '') or '',
            lab.get('flag', '') or '',
            lab.get('provider', '') or '',
            lab.get('lab_facility', '') or '',
            lab.get('notes', '') or ''
        ])
    
    # Prepare response
    csv_data = output.getvalue()
    output.close()
    
    # Generate filename with date range
    filename = f"lab_results_{start_date}_to_{end_date}.csv"
    
    # Return as downloadable CSV
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route("/export/clinicians")
def export_clinicians():
    """Export all clinicians as CSV."""
    
    # Get all clinicians
    clinicians = db.get_all_clinicians(uid())
    
    # Sort by name
    clinicians.sort(key=lambda x: x.get('name', '').lower())
    
    # Create CSV
    output = StringIO()
    writer = csv.writer(output)
    _write_patient_header(writer)

    # Write header
    writer.writerow([
        'Name',
        'Specialty',
        'Clinic Name',
        'Phone',
        'Email/Portal',
        'Network',
        'Address',
        'Notes'
    ])
    
    # Write data rows
    for c in clinicians:
        writer.writerow([
            c.get('name', ''),
            c.get('specialty', ''),
            c.get('clinic_name', '') or '',
            c.get('phone', '') or '',
            c.get('email', '') or '',
            c.get('network', '') or '',
            c.get('address', '') or '',
            c.get('notes', '') or ''
        ])
    
    # Prepare response
    csv_data = output.getvalue()
    output.close()
    
    # Generate filename with today's date
    today = date.today().isoformat()
    filename = f"clinicians_{today}.csv"
    
    # Return as downloadable CSV
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )
    
@app.route("/export/medications")
def export_medications():
    """Export medications as CSV with filter (active/all/inactive)."""
    
    filter_type = request.args.get("filter", "active")
    
    # Get all medications
    all_meds = db.get_all_medications(uid())
    
    # Filter based on selection
    today_str = date.today().isoformat()
    
    if filter_type == "active":
        filtered_meds = [
            m for m in all_meds 
            if m["start_date"] <= today_str and
               (m.get("end_date") is None or m["end_date"] >= today_str)
        ]
        filename_suffix = "active"
    elif filter_type == "inactive":
        filtered_meds = [
            m for m in all_meds 
            if m.get("end_date") and m["end_date"] < today_str
        ]
        filename_suffix = "inactive"
    else:  # all
        filtered_meds = all_meds
        filename_suffix = "all"
    
    # Sort by start date (most recent first)
    filtered_meds.sort(key=lambda x: x.get("start_date", ""), reverse=True)
    
    # Create CSV
    output = StringIO()
    writer = csv.writer(output)
    _write_patient_header(writer)

    # Write header
    writer.writerow([
        'Drug Name',
        'Dose',
        'Unit',
        'Frequency',
        'Route',
        'Category',
        'Indication',
        'Start Date',
        'End Date',
        'Primary Intervention',
        'Secondary Intervention',
        'Notes'
    ])
    
    # Write data rows
    for med in filtered_meds:
        writer.writerow([
            med.get('drug_name', ''),
            med.get('dose', '') if med.get('dose') is not None else '',
            med.get('unit', '') or '',
            med.get('frequency', '') or '',
            med.get('route', '') or '',
            med.get('category', '') or '',
            med.get('indication', '') or '',
            med.get('start_date', ''),
            med.get('end_date', '') or '',
            'Yes' if med.get('is_primary_intervention') == 1 else 'No',
            'Yes' if med.get('is_secondary_intervention') == 1 else 'No',
            med.get('notes', '') or ''
        ])
    
    # Prepare response
    csv_data = output.getvalue()
    output.close()
    
    # Generate filename
    today = date.today().isoformat()
    filename = f"medications_{filename_suffix}_{today}.csv"
    
    # Return as downloadable CSV
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )
    
@app.route("/export/events")
def export_events():
    """Export clinical events as CSV within date range and optional event type filter."""
    
    start_date = request.args.get("start")
    end_date = request.args.get("end")
    event_type = request.args.get("type", "all")
    
    if not start_date or not end_date:
        return "Missing date range parameters", 400
    
    # Get all events
    all_events = db.get_clinical_events(uid())
    
    # Filter by date range
    filtered_events = [
        event for event in all_events
        if start_date <= event["date"] <= end_date
    ]
    
    # Filter by event type if not "all"
    if event_type != "all":
        filtered_events = [
            event for event in filtered_events
            if event.get("event_type") == event_type
        ]
    
    # Sort by date (most recent first)
    filtered_events.sort(key=lambda x: x["date"], reverse=True)
    
    # Create CSV
    output = StringIO()
    writer = csv.writer(output)
    _write_patient_header(writer)

    # Write header
    writer.writerow([
        'Date',
        'Event Type',
        'Provider',
        'Facility',
        'Follow-up Date',
        'Notes'
    ])
    
    # Write data rows
    for event in filtered_events:
        writer.writerow([
            event.get('date', ''),
            event.get('event_type', ''),
            event.get('provider', '') or '',
            event.get('facility', '') or '',
            event.get('follow_up_date', '') or '',
            event.get('notes', '') or ''
        ])
    
    # Prepare response
    csv_data = output.getvalue()
    output.close()
    
    # Generate filename
    type_suffix = event_type if event_type != "all" else "all"
    filename = f"events_{type_suffix}_{start_date}_to_{end_date}.csv"
    
    # Return as downloadable CSV
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


# ============================================================
# Search
# ============================================================

@app.route("/search", methods=["GET", "POST"])
def search():
    """Search through observations and clinical notes."""
    
    # Get query from either GET or POST
    query = request.args.get("q", "").strip() or request.form.get("query", "").strip()
    
    # Easter egg: redirect to lab for help queries
    if query.lower() in ['help', 'user manual', 'cli', 'lab', 'code', 'weights', 'tune', 'manual']:
        return redirect(url_for('forecast_lab'))

    grouped = {
        "daily":       [],
        "labs":        [],
        "events":      [],
        "medications": [],
    }
    total = 0

    # Always fetch full dataset for report summary and chart
    all_observations = db.get_all_daily_observations(uid())
    all_meds         = db.get_all_medications(uid())

    tracking_start = all_observations[0]["date"] if all_observations else None
    tracking_end   = all_observations[-1]["date"] if all_observations else None

    today_str = date.today().isoformat()
    active_meds = [
        m for m in all_meds
        if m["start_date"] <= today_str and
           (m.get("end_date") is None or m["end_date"] >= today_str)
    ]

    uv_all = []
    if tracking_start and tracking_end:
        uv_all = db.get_uv_data_range(get_location_key(), tracking_start, tracking_end)

    chart_dataset = {
        "dates": [o["date"] for o in all_observations],
        "sleep": [o.get("hours_slept") for o in all_observations],
        "bbt":   [o.get("basal_temp_delta") for o in all_observations],
        "uv":    {u["date"]: u.get("uv_noon") for u in uv_all},
    }

    if query:
        q = query.lower()

        # Daily entries
        for o in all_observations:
            fields = [
                o.get("notes") or "",
                o.get("neuro_notes") or "",
                o.get("cognitive_notes") or "",
                o.get("musculature_notes") or "",
                o.get("migraine_notes") or "",
                o.get("air_hunger_notes") or "",
                o.get("derm_notes") or "",
                o.get("emotional_notes") or "",
            ]
            combined = " ".join(fields).lower()
            if q in combined:
                snippet = next(
                    (f.strip() for f in fields if q in f.lower() and f.strip()),
                    ""
                )
                grouped["daily"].append({
                    "id":      f"daily_{o['date']}",
                    "date":    o["date"],
                    "type":    "daily",
                    "title":   "daily entry",
                    "snippet": snippet[:200] if snippet else combined[:200],
                    "pain":    o.get("pain_scale"),
                    "fatigue": o.get("fatigue_scale"),
                })
                total += 1

        # Lab results
        labs = db.get_lab_results(uid())
        for lab in labs:
            fields = [
                lab.get("test_name") or "",
                lab.get("notes") or "",
                lab.get("provider") or "",
                lab.get("lab_facility") or "",
            ]
            combined = " ".join(fields).lower()
            if q in combined:
                val = (f"{lab['numeric_value']} {lab['unit'] or ''}".strip()
                       if lab.get("numeric_value") is not None
                       else lab.get("qualitative_result") or "")
                grouped["labs"].append({
                    "id":      f"lab_{lab['id']}",
                    "date":    lab["date"],
                    "type":    "lab",
                    "title":   lab["test_name"],
                    "snippet": f"{val} — {lab.get('notes') or lab.get('provider') or ''}".strip(" —"),
                })
                total += 1

        # Clinical events
        events = db.get_clinical_events(uid())
        for e in events:
            fields = [
                e.get("notes") or "",
                e.get("provider") or "",
                e.get("facility") or "",
                e.get("event_type") or "",
            ]
            combined = " ".join(fields).lower()
            if q in combined:
                snippet = next(
                    (f.strip() for f in fields if q in f.lower() and f.strip()),
                    ""
                )
                grouped["events"].append({
                    "id":      f"event_{e['id']}",
                    "date":    e["date"],
                    "type":    "event",
                    "title":   f"{e['event_type']} — {e.get('provider') or e.get('facility') or ''}".strip(" —"),
                    "snippet": snippet[:200],
                })
                total += 1

        # Medications
        for med in all_meds:
            fields = [
                med.get("drug_name") or "",
                med.get("indication") or "",
                med.get("notes") or "",
            ]
            combined = " ".join(fields).lower()
            if q in combined:
                dose_str = f"{med.get('dose') or ''} {med.get('unit') or ''} {med.get('frequency') or ''}".strip()
                grouped["medications"].append({
                    "id":      f"med_{med['id']}",
                    "date":    med["start_date"],
                    "type":    "medication",
                    "title":   med["drug_name"],
                    "snippet": f"{dose_str} — {med.get('indication') or ''}".strip(" —"),
                })
                total += 1

        for key in grouped:
            grouped[key].sort(key=lambda x: x["date"], reverse=True)

    return render_template(
        "search.html",
        query=query,
        grouped=grouped,
        total=total,
        tracking_start=tracking_start,
        tracking_end=tracking_end,
        active_meds=active_meds,
        chart_dataset_json=json.dumps(chart_dataset),
        patient_name=get_user_prefs().get("patient_name") or CONFIG.get("patient_name", ""),
    )

# ============================================================
# Data Management & Export
# ============================================================

def _build_backup_zip(user_id: int):
    """Assemble the full-backup zip: raw database, CSVs of every user-scoped
    table, and — in single_user_mode only, where user == server owner —
    config.json and custom weights. Uploaded documents come along either way
    (scoped to the requesting user)."""
    from io import BytesIO

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Checkpoint the WAL first or the copied .db file silently misses
        # everything written since the last checkpoint.
        with db.get_db() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        if os.path.exists(db.DB_FILE):
            zipf.write(db.DB_FILE, "biotracking.db")

        _export_csvs_to_zip(zipf, user_id)

        # Server-owner files: only when this server belongs to exactly the
        # requesting user (the phone-local app). On a multi-user server,
        # config.json holds secrets that aren't any one user's to export.
        if CONFIG.get("single_user_mode"):
            config_path = os.path.join(DATA_DIR, "config.json")
            if os.path.exists(config_path):
                zipf.write(config_path, "config.json")
            if os.path.exists(CUSTOM_WEIGHTS_PATH):
                zipf.write(CUSTOM_WEIGHTS_PATH, "config/custom_weights.json")

        docs_dir = os.path.join(DOCUMENTS_DIR, f"user_{user_id}")
        if os.path.isdir(docs_dir):
            for root, _dirs, files in os.walk(docs_dir):
                for fname in files:
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, DOCUMENTS_DIR)
                    zipf.write(full, os.path.join("documents", rel))

    zip_buffer.seek(0)
    return zip_buffer


@app.route("/export/all-data")
def export_all_data():
    """Export complete database and all data as ZIP file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        _build_backup_zip(uid()),
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'biotracking_backup_{timestamp}.zip'
    )


def _export_csvs_to_zip(zipf, user_id):
    """Export all database tables as CSV strings into a ZipFile."""

    def make_csv(data: list, columns: list) -> str:
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in data:
            writer.writerow([row.get(col, '') for col in columns])
        return output.getvalue()

    # Daily observations — all columns
    daily_obs = db.get_all_observations(user_id)
    zipf.writestr("daily_observations.csv", make_csv(daily_obs, [
        'date', 'steps', 'hours_slept', 'hrv', 'hrv_rmssd',
        'resting_heart_rate', 'spo2', 'respiratory_rate',
        'basal_temp_delta', 'sun_exposure_min', 'uv_protection_level', 'stayed_indoors',
        'pain_scale', 'fatigue_scale', 'emotional_state', 'emotional_notes',
        'neurological', 'neuro_notes', 'cognitive', 'cognitive_notes',
        'musculature', 'musculature_notes', 'migraine', 'migraine_notes',
        'pulmonary', 'pulmonary_notes', 'dermatological', 'derm_notes',
        'rheumatic', 'rheumatic_notes', 'mucosal', 'mucosal_notes',
        'gastro', 'gastro_notes',
        'air_hunger', 'air_hunger_notes', 'word_loss', 'word_loss_notes',
        'period_flow', 'cramping', 'cycle_notes',
        'flare_occurred', 'flare_severity',
        'strike_physical', 'strike_environmental',
        'notes',
    ]))

    # Labs
    zipf.writestr("labs.csv", make_csv(db.get_lab_results(user_id), [
        'date', 'test_name', 'numeric_value', 'unit', 'qualitative_result',
        'reference_range', 'flag', 'provider', 'lab_facility', 'notes'
    ]))

    # Medications
    zipf.writestr("medications.csv", make_csv(db.get_all_medications(user_id), [
        'drug_name', 'dose', 'unit', 'frequency', 'route', 'category',
        'indication', 'start_date', 'end_date', 'is_primary_intervention',
        'is_secondary_intervention', 'notes'
    ]))

    # Events
    zipf.writestr("events.csv", make_csv(db.get_clinical_events(user_id), [
        'date', 'event_type', 'provider', 'facility', 'follow_up_date', 'notes'
    ]))

    # Clinicians
    zipf.writestr("clinicians.csv", make_csv(db.get_all_clinicians(user_id), [
        'name', 'specialty', 'clinic_name', 'phone', 'email', 'network',
        'address', 'notes'
    ]))

    # ANA results
    zipf.writestr("ana_results.csv", make_csv(db.get_ana_results(user_id), [
        'date', 'titer', 'screen_result', 'patterns', 'provider', 'notes'
    ]))

    # Remaining user-scoped tables — complete (all-column) dumps so the export
    # is truly "everything", not just the human-friendly curated views above.
    # The raw .db is already in the zip; these make the data portable as CSV
    # too. Generic dumper keeps this list the single place to extend.
    for table, filename in [
        ("medication_events",  "medication_events.csv"),
        ("bc_history",         "bc_history.csv"),
        ("taper_schedules",    "taper_schedules.csv"),
        ("scheduled_doses",    "scheduled_doses.csv"),
        ("uv_sensor_readings", "uv_sensor_readings.csv"),
        ("health_sync_events", "health_sync_events.csv"),
        ("user_preferences",   "user_preferences.csv"),
    ]:
        dump = db.export_table_for_user(table, user_id)
        zipf.writestr(filename, make_csv(dump["rows"], dump["columns"]))

# ============================================================
# Clinical Report
# ============================================================

def generate_findings(observations, uv_data, start_date, end_date, n_obs=None, user_id=None):
    """Auto-generate clinical findings from data.

    user_id defaults to the logged-in user; pass it explicitly (e.g. from the
    read-only portal, which has no session) to scope the medication lookup.
    """
    import numpy as np
    from scipy import stats

    findings = []
    if n_obs is None:
        n_obs = len(observations)

    # UV lag correlation for period
    if len(observations) >= 10 and len(uv_data) >= 10:
        obs_by_date = {o["date"]: o for o in observations}
        uv_by_date  = {u["date"]: u for u in uv_data}

        dates_with_both = [d for d in obs_by_date
                           if d in uv_by_date and uv_by_date[d].get("uv_noon")]

        if len(dates_with_both) >= 10:
            uv_vals = []
            muscle_vals = []
            for d in dates_with_both:
                uv = uv_by_date[d].get("uv_noon")
                muscle = obs_by_date[d].get("musculature")
                if uv is not None and muscle is not None:
                    uv_vals.append(float(uv))
                    muscle_vals.append(float(muscle))

            if len(uv_vals) >= 8:
                r, p = stats.pearsonr(np.array(uv_vals), np.array(muscle_vals))
                if p < 0.01 and abs(r) >= 0.15:
                    findings.append({
                        "type": "uv_correlation",
                        "text": f"UV exposure shows significant same-day correlation with musculature symptoms (r={r:.3f}, p={p:.4f}, n={len(uv_vals)})."
                    })

    # Flare frequency
    if observations:
        flare_n = sum(1 for o in observations if o.get('flare_occurred') == 1)
        period_days = max(
            (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days, 1
        )
        per_month = round(flare_n / period_days * 30, 1)
        if flare_n > 0:
            findings.append({
                'type': 'flare_frequency',
                'text': f'{flare_n} high-burden day{"s" if flare_n != 1 else ""} recorded in this period '
                        f'({per_month}/month over {period_days} days).'
            })
        else:
            findings.append({'type': 'flare_frequency', 'text': 'No high-burden days recorded in this period.'})

    # Highest-burden symptom category
    if observations and n_obs:
        sym_counts = {
            key: sum(1 for o in observations if o.get(key))
            for key in ['neurological', 'cognitive', 'musculature', 'migraine',
                        'pulmonary', 'dermatological', 'rheumatic', 'gastro', 'mucosal']
        }
        labels = {
            'neurological': 'Neurological', 'cognitive': 'Cognitive',
            'musculature': 'Musculature', 'migraine': 'Migraine',
            'pulmonary': 'Pulmonary', 'dermatological': 'Dermatological',
            'rheumatic': 'Rheumatic', 'gastro': 'Gastrointestinal', 'mucosal': 'Mucosal'
        }
        top = max(sym_counts, key=sym_counts.get)
        top_pct = round(sym_counts[top] / n_obs * 100)
        if sym_counts[top] > 0:
            findings.append({
                'type': 'symptom_burden',
                'text': f'{labels[top]} symptoms were the most frequently reported category, '
                        f'present on {sym_counts[top]} of {n_obs} days ({top_pct}%).'
            })

        # Neurological involvement — flag for rheumatology
        neuro_n = sym_counts.get('neurological', 0)
        neuro_pct = round(neuro_n / n_obs * 100)
        if neuro_pct >= 10:
            findings.append({
                'type': 'neurological',
                'text': f'Neurological symptoms present on {neuro_n} of {n_obs} days ({neuro_pct}%).'
            })

    # Medications started during this period
    all_meds = db.get_all_medications(user_id if user_id is not None else uid())
    meds_started = [m for m in all_meds if start_date <= m['start_date'] <= end_date]
    for med in meds_started:
        dose_str = f"{med.get('dose', '') or ''} {med.get('unit', '') or ''}".strip()
        indication = f" — {med['indication']}" if med.get('indication') else ''
        findings.append({
            'type': 'medication_change',
            'text': f"{med['drug_name']}{(' ' + dose_str) if dose_str else ''} "
                    f"started {med['start_date']}{indication}."
        })

    return findings

# ============================================================
# UV correlated report generation
# ============================================================

# --- Curated lab selection for the clinical report -------------------------
# "Clinically meaningful marker," not "any abnormal." Core markers are always
# surfaced (most recent of each, ALL-TIME — never clipped to the report window,
# since the lupus band / complement / D-dimer that prove a case are usually
# older than 90 days). A second "watch" set surfaces ONLY when out of range
# (anomalous lipids and WBC-differential shifts like lymphopenia/eosinopenia
# are SARD-relevant). Everything else is excluded.
# Spelling variants of the same analyte -> one canonical marker.
_LAB_CANON = {
    "d dimer": "d-dimer",
    "rheumatoid factor": "rf",
}
_LAB_CORE = {
    "ana screen", "ana titer", "ana pattern", "anti-dsdna", "ena panel",
    "rf", "lupus band test",
    "c3", "c4", "igg", "iga", "igm",
    "crp", "esr", "d-dimer", "creatine kinase",
}
_LAB_ABNORMAL_ONLY = {
    "hdl", "ldl", "total cholesterol", "non-hdl cholesterol", "triglycerides",
    "lymphocytes", "leukocytes", "wbc", "neutrophils", "absolute eosinophils",
}
_LAB_ABNORMAL_FLAGS = {"high", "low", "critical", "abnormal"}
_LAB_ABNORMAL_QUAL = ("positive", "reactive", "detected", "abnormal")


def _lab_marker(lab):
    raw = (lab.get("test_name") or "").strip().lower()
    return _LAB_CANON.get(raw, raw)


def _lab_is_abnormal(lab):
    if (lab.get("flag") or "").strip().lower() in _LAB_ABNORMAL_FLAGS:
        return True
    q = (lab.get("qualitative_result") or "").strip().lower()
    return any(t in q for t in _LAB_ABNORMAL_QUAL)


def select_report_labs(all_labs):
    """Curated markers for the report — only what argues the case.

    A marker is surfaced only if it has been abnormal/positive at least once
    (a marker that's always been normal — IgG, CK, a normal CRP — is noise on a
    clinical handout). Exceptions kept for context: the ANA panel travels
    together (titer + pattern shown whenever the screen is positive), and
    complement is shown as a C3/C4 pair. Per surfaced marker, show the most
    recent value AND the most abnormal on record (if a different draw), so
    seroconversion history (RF/ANA once positive, now negative) is preserved.
    Spelling variants are collapsed. Returns one list, most recent first.
    """
    by_marker = {}
    for lab in all_labs:
        name = _lab_marker(lab)
        if name in _LAB_CORE or name in _LAB_ABNORMAL_ONLY:
            by_marker.setdefault(name, []).append(lab)

    # Markers worth surfacing: anything ever abnormal/positive...
    keep = {m for m, labs in by_marker.items()
            if any(_lab_is_abnormal(l) for l in labs)}
    # ...plus ANA-panel cohesion and complement pairing for context.
    if "ana screen" in keep:
        keep |= {"ana titer", "ana pattern"}
    if keep & {"c3", "c4"}:
        keep |= {"c3", "c4"}

    picked = []
    for name in keep:
        labs = by_marker.get(name) or []
        if name in _LAB_ABNORMAL_ONLY:          # lipids/WBC: only abnormal draws
            labs = [l for l in labs if _lab_is_abnormal(l)]
        if not labs:
            continue
        labs.sort(key=lambda l: l.get("date") or "")
        most_recent = labs[-1]
        chosen = [most_recent]
        abnormals = [l for l in labs if _lab_is_abnormal(l)]
        if abnormals and abnormals[-1] is not most_recent:
            chosen.append(abnormals[-1])        # most recent abnormal draw
        picked.extend(chosen)

    picked.sort(key=lambda l: l.get("date") or "", reverse=True)
    return picked


# Symptom categories for the report's frequency table (label order = display order)
_SYMPTOM_FREQ_KEYS = [
    ('neurological',   'Neurological'),
    ('cognitive',      'Cognitive'),
    ('musculature',    'Musculature'),
    ('migraine',       'Migraine'),
    ('pulmonary',      'Pulmonary'),
    ('dermatological', 'Dermatological'),
    ('rheumatic',      'Rheumatic'),
    ('gastro',         'Gastrointestinal'),
    ('mucosal',        'Mucosal'),
]


def symptom_frequency(observations):
    """Days each symptom category was flagged (categories present at least
    once), sorted by count descending. Shared by /report and the portal."""
    n_obs = len(observations)
    return sorted(
        [
            {'name': label, 'count': count,
             'percent': round(count / n_obs * 100) if n_obs else 0}
            for key, label in _SYMPTOM_FREQ_KEYS
            if (count := sum(1 for o in observations if o.get(key)))
        ],
        key=lambda x: x['count'], reverse=True
    )


def _serology_tags(key_labs):
    """Headline serology strings for the synopsis strip — the markers that
    argue the case (lupus band, ANA, dsDNA, complements)."""
    serology, seen = [], set()
    for lab in key_labs:
        nm = (lab.get("test_name") or "").lower()
        flg = (lab.get("flag") or "").lower()
        ql = (lab.get("qualitative_result") or "").lower()
        tag = None
        if "lupus band" in nm and "positive" in ql:
            tag = "lupus band +"
        elif nm.startswith("ana screen") and "positive" in ql:
            tag = "ANA +"
        elif "dsdna" in nm and flg in ("high", "abnormal", "critical"):
            tag = "anti-dsDNA ↑"
        elif nm == "c4" and flg == "low":
            tag = "C4 low"
        elif nm == "c3" and flg == "low":
            tag = "C3 low"
        if tag and tag not in seen:
            seen.add(tag)
            serology.append(tag)
    return serology


def _burden_series(all_obs_sorted, start_date, end_date, loc_key, user_id):
    """Per-day disease-burden score attribution for the window (same model as
    /model). Full history feeds the multi-day lookback; only the window gets
    scored. Shared by /report and the portal overview."""
    _inject_cycle_phase(all_obs_sorted)
    by_date = {o["date"]: o for o in all_obs_sorted}
    window = [o for o in all_obs_sorted if start_date <= o["date"] <= end_date]
    _inject_scoring_context(window, by_date, loc_key)
    series = []
    for o in window:
        comp = _score_components(o, user_id=user_id)
        series.append({
            "date": o["date"], "total": comp["total"],
            "uv": comp["uv"], "exertion": comp["exertion"],
            "temperature": comp["temperature"], "symptoms": comp["symptoms"],
            "pain_fatigue": comp["pain_fatigue"], "cycle": comp["cycle"],
            "burden_delta": comp["burden_delta"],
            "rmssd": comp["rmssd"], "rmssd_instability": comp["rmssd_instability"],
            "resp_rate": comp["resp_rate"],
            "flare": o.get("flare_occurred") == 1, "severity": o.get("flare_severity"),
        })
    return series
