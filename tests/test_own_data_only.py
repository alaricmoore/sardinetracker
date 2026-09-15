"""
An account's data is its own.

- Deleting ("delete all data") removes every row that belongs to the signed-in
  user, in every table, and their uploaded document files, and nothing of
  anyone else's. It used to delete the whole database file.
- delete_user used to clear a fixed list of tables that had fallen behind the
  schema, so for anyone with a document, a portal link, a medication event, a
  sync event or UV readings, it failed on a foreign key. It now finds every
  table with a user_id column itself. The schema test below fails if a table
  ever refers to users some other way, which delete_user would not know to clear.
- Downloading ("Download All Data") gives the signed-in user their own records.
  The raw database holds every account's, so it only goes in the zip in
  single_user_mode, where the one account is the server's owner.
"""

import io
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import db


def user_tables(db_path):
    con = sqlite3.connect(db_path)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return [t for t in names
                if "user_id" in [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]]
    finally:
        con.close()


def rows_per_table(db_path, user_id):
    con = sqlite3.connect(db_path)
    try:
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE user_id = ?', (user_id,)).fetchone()[0]
                for t in user_tables(db_path)}
    finally:
        con.close()


def give_everything(user_id, marker):
    """A little of every kind of record, including every table the old list missed.
    `marker` is written into the notes so a test can tell whose rows are whose."""
    day = "2026-01-05"
    db.upsert_daily_observations(user_id, {"date": day, "pain_scale": 3, "notes": marker})
    db.upsert_user_preferences(user_id, {"patient_name": "Test"})
    db.record_health_sync_event(user_id=user_id, posted_at=f"{day}T08:00:00", metric_date=day,
                                fields_updated=["steps"], payload={"steps": 1000})
    db.add_lab_result(user_id, {"date": day, "test_name": "ESR", "numeric_value": 30, "unit": "mm/hr"})
    db.add_clinical_event(user_id, {"date": day, "event_type": "appointment"})
    med = db.add_medication(user_id, {"drug_name": "hydroxychloroquine", "dose": 200, "unit": "mg",
                                      "start_date": "2025-12-09"})
    db.add_medication_event(user_id, med, day, "note", note="fine")
    db.create_taper_schedule(user_id, med, day)
    clinician = db.add_clinician(user_id, {"name": "Dr Test", "specialty": "rheumatology"})
    db.add_clinical_document(user_id, {"title": "letter", "file_name": "letter.pdf"})
    link = db.create_portal_link(user_id, clinician, "full", "link",
                                 (datetime.now(timezone.utc).replace(tzinfo=None)
                                  + timedelta(days=1)).isoformat())
    db.record_portal_access(link["id"], "/portal/x")
    db.insert_uv_sensor_rows([{
        "user_id": user_id, "boot_id": 1, "ms_since_boot": 1, "ts": None, "ts_confidence": None,
        "uva": 1, "uvb": 1, "comp1": 1, "comp2": 1, "uv_index": 0.0, "batt_mv": 3900,
        "event_label": None}])
    return link["id"]


@pytest.fixture
def two_users(fresh_db):
    owner = db.create_user("patient", "Test Patient", "not-a-real-password-hash")
    other = db.create_user("someone", "Someone Else", "not-a-real-password-hash")
    return owner, other


def log_in(app_client, user_id):
    with app_client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


# ------------------------------------------------------------------
# delete_user
# ------------------------------------------------------------------

def test_every_reference_to_users_is_a_user_id_column(fresh_db):
    con = sqlite3.connect(fresh_db)
    try:
        odd = set()
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                "AND name NOT LIKE 'sqlite_%'"):
            for fk in con.execute(f'PRAGMA foreign_key_list("{t}")'):
                if fk[2] == "users" and fk[3] != "user_id":
                    odd.add((t, fk[3]))
    finally:
        con.close()
    assert odd == set()


def test_deleting_a_user_removes_all_of_theirs_and_none_of_anyone_elses(two_users, fresh_db):
    owner, other = two_users
    link_id = give_everything(owner, "owner's note")
    give_everything(other, "other's note")
    before = rows_per_table(fresh_db, owner)
    others_before = rows_per_table(fresh_db, other)
    # The seed really reaches the tables the old list missed.
    for table in ("clinical_documents", "medication_events", "portal_links",
                  "uv_sensor_readings", "health_sync_events"):
        assert before[table] > 0, table

    db.delete_user(owner)

    assert all(n == 0 for n in rows_per_table(fresh_db, owner).values())
    assert rows_per_table(fresh_db, other) == others_before
    assert db.get_user_by_id(owner) is None
    con = sqlite3.connect(fresh_db)
    try:
        assert con.execute("SELECT COUNT(*) FROM portal_access_log WHERE link_id = ?",
                           (link_id,)).fetchone()[0] == 0
    finally:
        con.close()


# ------------------------------------------------------------------
# The delete-all-data route
# ------------------------------------------------------------------

@pytest.fixture
def docs(tmp_path, monkeypatch):
    import routes.clinical
    root = tmp_path / "documents"
    monkeypatch.setattr(routes.clinical, "DOCUMENTS_DIR", str(root))
    return root


def put_file(root, user_id):
    folder = root / f"user_{user_id}"
    folder.mkdir(parents=True)
    (folder / "letter.pdf").write_bytes(b"%PDF-1.4 test")
    return folder


def test_the_route_deletes_only_the_signed_in_account(app_client, two_users, docs, fresh_db):
    owner, other = two_users
    give_everything(owner, "owner's note")
    give_everything(other, "other's note")
    others_before = rows_per_table(fresh_db, other)
    mine, theirs = put_file(docs, owner), put_file(docs, other)
    log_in(app_client, owner)

    resp = app_client.post("/delete/all-data")

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    assert db.get_user_by_id(owner) is None
    assert db.get_user_by_id(other) is not None
    assert rows_per_table(fresh_db, other) == others_before
    assert not mine.exists()
    assert theirs.exists()


def test_the_route_works_for_a_user_with_no_uploaded_files(app_client, two_users, docs):
    owner, _ = two_users
    log_in(app_client, owner)
    assert app_client.post("/delete/all-data").status_code == 200
    assert db.get_user_by_id(owner) is None


def test_if_the_records_cannot_be_deleted_the_files_are_kept(app_client, two_users, docs, monkeypatch):
    owner, _ = two_users
    mine = put_file(docs, owner)
    log_in(app_client, owner)

    def fail(user_id):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
    monkeypatch.setattr(db, "delete_user", fail)

    resp = app_client.post("/delete/all-data")
    assert resp.status_code == 500
    assert resp.get_json()["success"] is False
    assert mine.exists()


# ------------------------------------------------------------------
# Download All Data
# ------------------------------------------------------------------

def download(app_client):
    resp = app_client.get("/export/all-data")
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    return {name: zf.read(name) for name in zf.namelist()}


def test_a_download_on_a_shared_instance_holds_only_your_own_records(app_client, two_users):
    owner, other = two_users
    give_everything(owner, "owner's note")
    give_everything(other, "other's note")
    log_in(app_client, owner)

    files = download(app_client)

    assert "biotracking.db" not in files
    assert "config.json" not in files
    everything = b"".join(files.values())
    assert b"owner's note" in everything
    assert b"other's note" not in everything


def test_a_download_in_single_user_mode_includes_the_raw_database(app_client, fresh_db, monkeypatch):
    import appcore
    owner = db.create_user("owner", "Owner", "not-a-real-password-hash")
    monkeypatch.setitem(appcore.CONFIG, "single_user_mode", True)
    log_in(app_client, owner)

    assert "biotracking.db" in download(app_client)
