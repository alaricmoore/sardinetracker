"""
The clinician portal. Public routes here take a capability token, not a
login, and must never reach a write.
"""

from datetime import date, datetime, timedelta, timezone
from flask import render_template, request, redirect, url_for, Response, send_from_directory
from flask_login import login_required
import db

from appcore import CONFIG, app, uid
from flaremodel import get_current_weights
from routes.clinical import _user_docs_dir
from routes.reports import _burden_series, _serology_tags, generate_findings, select_report_labs, symptom_frequency


# ============================================================
# Clinician portal — capability-link, READ-ONLY, per-specialty
# ============================================================
# Public routes here take a token, not a login. They MUST NOT reach any write,
# admin, or export endpoint — this section only ever reads curated data for the
# link's own user. Management routes below (/portals*) require login as normal.

PORTAL_VIEWS = {"full": "Full record"}   # one read-only record, not per-specialty


def _utcnow_naive() -> datetime:
    """The current UTC time as a naive datetime, the form expires_at is stored
    in. datetime.utcnow() returns the same thing but is deprecated, and would
    break every portal link if a future Python removes it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _valid_portal_link(token: str):
    """Return the link row if the token is real, not revoked, not expired —
    else None. This is the sole gate for portal access."""
    link = db.get_portal_link_by_token(token)
    if not link or link.get("revoked_at"):
        return None
    exp = link.get("expires_at")
    if exp and exp < _utcnow_naive().isoformat():
        return None
    return link


@app.after_request
def _portal_security_headers(resp):
    """Keep portal pages out of indexes and shared caches."""
    if request.path.startswith("/portal/"):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _portal_identity(prefs: dict) -> dict:
    dob = prefs.get("patient_dob")
    age = None
    if dob:
        try:
            b = datetime.strptime(dob, "%Y-%m-%d").date()
            t = date.today()
            age = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
        except ValueError:
            pass
    return {"name": prefs.get("patient_name"), "dob": dob, "age": age}


def _owner_location_key(prefs: dict) -> str:
    lat = prefs.get("location_lat") or CONFIG.get("location_lat")
    lon = prefs.get("location_lon") or CONFIG.get("location_lon")
    if lat and lon:
        return db.make_location_key(float(lat), float(lon))
    return "default"


_IMMUNOSUPPRESSANTS = ("hydroxychloroquine", "plaquenil", "mycophenolate",
                       "cellcept", "methotrexate", "azathioprine", "rituximab",
                       "belimumab", "prednisone", "methylprednisolone")


def _portal_common_ctx(link):
    """Shared template context for every portal page, plus the owner id and
    preferences the per-page data builders need."""
    owner_id = link["user_id"]
    clinician = None
    if link.get("clinician_id"):
        clinician = next((c for c in db.get_all_clinicians(owner_id)
                          if c["id"] == link["clinician_id"]), None)
    prefs = db.get_user_preferences(owner_id) or {}
    ctx = dict(link=link, clinician=clinician, view_label="Clinical record",
               patient=_portal_identity(prefs))
    return ctx, owner_id, prefs


def _portal_meds(owner_id: int):
    """(active, past) medications for an owner — active immuno-flagged and
    sorted immuno first, past sorted by end date, newest first."""
    today = date.today().isoformat()
    meds = db.get_all_medications(owner_id)
    active = [m for m in meds if (m.get("start_date") or "") <= today
              and (not m.get("end_date") or m["end_date"] >= today)]
    for m in active:
        m["_immuno"] = any(k in (m.get("drug_name") or "").lower()
                           for k in _IMMUNOSUPPRESSANTS)
    active.sort(key=lambda m: (not m["_immuno"], m.get("drug_name") or ""))
    inactive = sorted([m for m in meds if m.get("end_date") and m["end_date"] < today],
                      key=lambda m: m.get("end_date") or "", reverse=True)
    return active, inactive


def _portal_intervention_reactions(owner_id: int):
    """Primary interventions (HCQ, MMF) each with their logged side-effect
    events, newest first. 'Reactions' here means symptoms reported *while on*
    the drug — the patient's own logs, not an assertion of drug causation. Only
    side_effect events are surfaced; dose changes and efficacy notes are not
    reactions."""
    meds = db.get_all_medications(owner_id)
    primary = sorted([m for m in meds if m.get("is_primary_intervention")],
                     key=lambda m: m.get("start_date") or "")
    return [{"med": m,
             "reactions": [e for e in db.get_medication_events(owner_id, m["id"])
                           if e.get("event_type") == "side_effect"]}
            for m in primary]


def _portal_medications_ctx(owner_id: int) -> dict:
    active, inactive = _portal_meds(owner_id)
    return {"active_meds": active, "inactive_meds": inactive,
            "interventions": _portal_intervention_reactions(owner_id)}


def _portal_labs(owner_id: int) -> dict:
    all_labs = db.get_lab_results(owner_id)
    return {
        "key_labs": select_report_labs(all_labs),
        "all_labs": sorted(all_labs, key=lambda x: x.get("date") or "", reverse=True),
        "ana_history": sorted(db.get_ana_results(owner_id),
                              key=lambda x: x.get("date") or "", reverse=True),
    }


def _portal_symptom_window(owner_id: int) -> dict:
    """Last-90-day patient-log summary: period stats, symptom frequency, and
    the flares themselves."""
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=90)).isoformat()
    observations = [o for o in db.get_all_daily_observations(owner_id)
                    if start <= o["date"] <= end]
    pain = [o["pain_scale"] for o in observations if o.get("pain_scale") is not None]
    fatigue = [o["fatigue_scale"] for o in observations
               if o.get("fatigue_scale") is not None]
    flares = sorted(
        [{"date": o["date"], "severity": o.get("flare_severity"),
          "pain": o.get("pain_scale"), "fatigue": o.get("fatigue_scale")}
         for o in observations if o.get("flare_occurred")],
        key=lambda f: f["date"], reverse=True)
    return {
        "observations": observations,
        "period": {"start": start, "end": end, "obs_days": len(observations),
                   "flare_days": len(flares),
                   "mean_pain": round(sum(pain) / len(pain), 1) if pain else None,
                   "mean_fatigue": round(sum(fatigue) / len(fatigue), 1) if fatigue else None},
        "symptom_freq": symptom_frequency(observations),
        "flares": flares,
    }


# The record's sections: url slug -> (card title, context builder). The
# overview page links a card to each; /portal/<token>/<section> serves them.
PORTAL_SECTIONS = {
    "documents":   ("Documents",
                    lambda owner_id, prefs: {"documents": db.get_clinical_documents(owner_id)}),
    "medications": ("Medications",
                    lambda owner_id, prefs: _portal_medications_ctx(owner_id)),
    "labs":        ("Lab results",
                    lambda owner_id, prefs: _portal_labs(owner_id)),
    "timeline":    ("Clinical timeline",
                    lambda owner_id, prefs: {"events": sorted(db.get_clinical_events(owner_id),
                                                              key=lambda x: x.get("date") or "",
                                                              reverse=True)}),
    "symptoms":    ("Symptom history",
                    lambda owner_id, prefs: _portal_symptom_window(owner_id)),
}


@app.route("/portal/<token>")
def portal_view(token):
    """The clinician-facing landing page: synopsis, disease-burden chart, and
    auto-findings up top, then a card per record section. No login: the token
    is the key."""
    link = _valid_portal_link(token)
    if not link:
        return Response(render_template("portal_invalid.html"), status=403)
    db.record_portal_access(link["id"], request.path)
    ctx, owner_id, prefs = _portal_common_ctx(link)

    sym = _portal_symptom_window(owner_id)
    period = sym["period"]
    loc_key = _owner_location_key(prefs)
    uv = (db.get_uv_data_range(loc_key, period["start"], period["end"])
          if sym["observations"] else [])
    labs = _portal_labs(owner_id)
    active_meds, inactive_meds = _portal_meds(owner_id)
    events = db.get_clinical_events(owner_id)
    documents = db.get_clinical_documents(owner_id)

    reaction_count = sum(len(iv["reactions"])
                         for iv in _portal_intervention_reactions(owner_id))

    all_obs = sorted(db.get_all_daily_observations(owner_id), key=lambda x: x["date"])
    burden = _burden_series(all_obs, period["start"], period["end"], loc_key, owner_id)

    period_days = max((date.fromisoformat(period["end"])
                       - date.fromisoformat(period["start"])).days, 1)
    ctx.update(
        period=period,
        synopsis={
            "flares_per_month": round(period["flare_days"] / period_days * 30, 1),
            "flare_count": period["flare_days"],
            "period_days": period_days,
            "dmards": [m["drug_name"] for m in active_meds if m["_immuno"]],
            "serology": _serology_tags(labs["key_labs"]),
            "mean_pain": period["mean_pain"],
            "mean_fatigue": period["mean_fatigue"],
        },
        findings=generate_findings(sym["observations"], uv, period["start"],
                                   period["end"], user_id=owner_id),
        burden=burden,
        burden_threshold=get_current_weights(owner_id).get("flare_threshold", 8.0),
        counts={"documents": len(documents), "labs": len(labs["all_labs"]),
                "ana": len(labs["ana_history"]), "meds_active": len(active_meds),
                "meds_past": len(inactive_meds), "events": len(events),
                "reactions": reaction_count, "obs_days": period["obs_days"]},
    )
    return render_template("portal_overview.html", **ctx)


@app.route("/portal/<token>/<section>")
def portal_section(token, section):
    """One section of the read-only record (documents, medications, labs,
    timeline, symptoms) — reached from the overview's cards."""
    link = _valid_portal_link(token)
    if not link:
        return Response(render_template("portal_invalid.html"), status=403)
    if section not in PORTAL_SECTIONS:
        return Response("Not found", status=404)
    db.record_portal_access(link["id"], request.path)
    ctx, owner_id, prefs = _portal_common_ctx(link)
    title, build = PORTAL_SECTIONS[section]
    ctx["section_title"] = title
    ctx.update(build(owner_id, prefs))
    return render_template(f"portal_{section}.html", **ctx)


@app.route("/portal/<token>/document/<int:doc_id>")
def portal_document(token, doc_id):
    """Serve one of the owner's PDFs through a valid portal link — read-only,
    scoped to the link's user, so a token can only reach that patient's docs."""
    link = _valid_portal_link(token)
    if not link:
        return Response("Not found", status=404)
    doc = db.get_clinical_document(link["user_id"], doc_id)
    if not doc or not doc.get("file_name"):
        return Response("Not found", status=404)
    db.record_portal_access(link["id"], request.path)
    return send_from_directory(
        _user_docs_dir(link["user_id"]), doc["file_name"],
        mimetype="application/pdf", as_attachment=False,
        download_name=doc.get("orig_name") or "document.pdf")


@app.route("/portals")
@login_required
def portals_manage():
    links = db.get_portal_links(uid())
    return render_template(
        "portal_manage.html",
        links=links,
        clinicians=db.get_all_clinicians(uid()),
        access_log=db.get_portal_access_log(uid(), limit=50),
        views=PORTAL_VIEWS,
        now=_utcnow_naive().isoformat(),
        base_url=request.host_url.rstrip("/"),
    )


@app.route("/portals/create", methods=["POST"])
@login_required
def portals_create():
    form = request.form
    clinician_id = int(form["clinician_id"]) if form.get("clinician_id") else None
    label = (form.get("label") or "").strip() or None
    try:
        days = max(1, min(365, int(form.get("days") or 30)))
    except ValueError:
        days = 30
    expires_at = (_utcnow_naive() + timedelta(days=days)).isoformat()
    db.create_portal_link(uid(), clinician_id, "full", label, expires_at)
    return redirect(url_for("portals_manage"))


@app.route("/portals/<int:link_id>/revoke", methods=["POST"])
@login_required
def portals_revoke(link_id):
    db.revoke_portal_link(uid(), link_id)
    return redirect(url_for("portals_manage"))
