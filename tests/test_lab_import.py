"""
Tests for lab CSV import: the web import on the clinical record page (preview,
then commit only the checked rows), the command-line importer in import_labs.py,
and the parsers and reference table they share.

The rule both paths should keep: a bounded result like '<20' is stored as
text, never as the bare number 20, which would read as a real measurement at
the threshold.
"""

import json
from datetime import date

import pytest

import import_labs


# ------------------------------------------------------------------
# Shared parsers and the reference table
# ------------------------------------------------------------------

class TestParsers:
    @pytest.mark.parametrize("text, iso", [
        ("2026-01-06", "2026-01-06"), ("01/06/2026", "2026-01-06"), ("1/6/26", "2026-01-06"),
        ("Jan 06, 2026", "2026-01-06"), ("January 6, 2026", "2026-01-06"),
        ("  2026-01-06  ", "2026-01-06"), ("", None), ("sometime in May", None),
    ])
    def test_parse_date(self, text, iso, capsys):
        assert import_labs.parse_date(text) == iso

    @pytest.mark.parametrize("text, value", [
        ("32", 32.0), ("0.98", 0.98), ("1,234", 1234.0), ("  7 ", 7.0),
        ("", None), ("positive", None),
        ("<20", 20.0),   # strips the bound: callers must check for < and > first
    ])
    def test_parse_float(self, text, value):
        assert import_labs.parse_float(text) == value

    @pytest.mark.parametrize("name, value, expected", [
        ("CRP", 18.4, ("0–10 mg/L", "high")),
        ("crp", 10, ("0–10 mg/L", "normal")),
        (" C3 ", 84, ("90–180 mg/dL", "low")),
        ("TSH", 0.3, ("0.4–4.0 mIU/L", "low")),
        ("Brand New Assay", 5, (None, None)),
    ])
    def test_lookup_reference(self, name, value, expected):
        assert import_labs.lookup_reference(name, value) == expected

    def test_a_value_the_flag_rule_cannot_compare_still_gets_its_range(self):
        assert import_labs.lookup_reference("CRP", "not a number") == ("0–10 mg/L", None)


# ------------------------------------------------------------------
# Web import: parse and tag for review
# ------------------------------------------------------------------

@pytest.fixture
def clinical(app_client):
    import routes.clinical
    return routes.clinical


def normalize(clinical, text, existing=()):
    return clinical._normalize_lab_rows(text, set(existing))


class TestWebCSVParsing:
    def test_a_typical_row(self, clinical):
        rows = normalize(clinical, "Date,Test,Value,Units,Lab,Doctor\n"
                                   "01/06/2026,CRP,18.4,mg/L,Mercy,Dr Test\n")
        assert rows == [{
            "date": "2026-01-06", "test_name": "CRP", "numeric_value": 18.4,
            "qualitative_result": None, "unit": "mg/L", "reference_range": "0–10 mg/L",
            "flag": "high", "provider": "Dr Test", "lab_facility": "Mercy", "status": "new",
        }]

    def test_headers_are_matched_case_insensitively_and_by_alias(self, clinical):
        rows = normalize(clinical, "Date Collected,Analyte,Result,UNIT,Facility,Ordering Provider\n"
                                   "2026-01-06,ESR,30,mm/hr,Quest,Dr Test\n")
        assert (rows[0]["test_name"], rows[0]["numeric_value"], rows[0]["unit"],
                rows[0]["lab_facility"], rows[0]["provider"]) == ("ESR", 30.0, "mm/hr", "Quest", "Dr Test")

    @pytest.mark.parametrize("value", ["<20", ">24.0", "<=0.5"])
    def test_bounded_results_stay_text(self, clinical, value):
        row = normalize(clinical, f"Date,Test,Value\n2026-01-06,anti-dsDNA,{value}\n")[0]
        assert (row["numeric_value"], row["qualitative_result"], row["flag"]) == (None, value, None)

    @pytest.mark.parametrize("value", ["1:40", "Positive", "equivocal"])
    def test_titers_and_words_stay_as_written(self, clinical, value):
        row = normalize(clinical, f"Date,Test,Value\n2026-01-06,ANA,{value}\n")[0]
        assert (row["numeric_value"], row["qualitative_result"]) == (None, value)

    def test_the_labs_own_range_and_flag_win_over_the_built_in_table(self, clinical):
        row = normalize(clinical, "Date,Test,Value,Reference Range,Flag\n"
                                  "2026-01-06,CRP,8,0-5 mg/L,high\n")[0]
        assert (row["reference_range"], row["flag"]) == ("0-5 mg/L", "high")

    def test_rows_missing_a_date_test_or_value_are_skipped(self, clinical):
        rows = normalize(clinical, "Date,Test,Value\n"
                                   ",CRP,5\n2026-01-06,,5\n2026-01-06,CRP,\nnot a date,CRP,5\n"
                                   "2026-01-06,CRP,5\n")
        assert len(rows) == 1

    def test_rows_already_in_the_record_are_tagged_duplicate(self, clinical):
        existing = {clinical._lab_dedup_key("2026-01-06", "CRP", 5.0, None)}
        rows = normalize(clinical, "Date,Test,Value\n2026-01-06,crp,5\n2026-01-06,CRP,6\n", existing)
        assert [r["status"] for r in rows] == ["duplicate", "new"]

    @pytest.mark.parametrize("a, b", [
        (("2026-01-06", "CRP", 3, None), ("2026-01-06", " crp ", "3.0", None)),
        (("2026-01-06", "ANA", None, "Positive"), ("2026-01-06", "ana", None, " positive ")),
    ])
    def test_dedup_key_ignores_formatting(self, clinical, a, b):
        assert clinical._lab_dedup_key(*a) == clinical._lab_dedup_key(*b)


# ------------------------------------------------------------------
# Web import: only checked rows are written
# ------------------------------------------------------------------

def test_commit_writes_only_the_rows_left_checked(app_client, fresh_db):
    import db
    user = db.create_user("patient", "Patient", "not-a-real-password-hash")
    with app_client.session_transaction() as session:
        session["_user_id"] = str(user)
    rows = [
        {"date": "2026-01-06", "test_name": "CRP", "numeric_value": 18.4, "unit": "mg/L"},
        {"date": "2026-01-06", "test_name": "ESR", "numeric_value": 30, "unit": "mm/hr"},
        {"date": "2026-01-06", "test_name": "anti-dsDNA", "numeric_value": None,
         "qualitative_result": "<20", "status": "new"},
    ]
    resp = app_client.post("/clinical/labs/import/commit",
                           data={"rows_json": json.dumps(rows), "include": ["0", "2"]})
    assert resp.status_code == 302
    stored = sorted((l["test_name"], l["numeric_value"], l["qualitative_result"])
                    for l in db.get_lab_results(user))
    assert stored == [("CRP", 18.4, None), ("anti-dsDNA", None, "<20")]


# ------------------------------------------------------------------
# Command-line importer
# ------------------------------------------------------------------

class TestCommandLineImport:
    @pytest.fixture
    def user(self, fresh_db):
        import db
        return db.create_user("patient", "Patient", "not-a-real-password-hash")

    def write_csv(self, tmp_path, body):
        path = tmp_path / "labs.csv"
        path.write_text("Date,Test,Value,Units,Lab,Doctor\n" + body, encoding="utf-8")
        return str(path)

    def stored(self, user):
        import db
        return sorted((l["date"], l["test_name"], l["numeric_value"], l["qualitative_result"], l["flag"])
                      for l in db.get_lab_results(user))

    def test_imports_rows_with_reference_flags(self, user, tmp_path, capsys):
        path = self.write_csv(tmp_path, "01/06/2026,CRP,18.4,mg/L,Mercy,Dr Test\n"
                                        "01/06/2026,ANA Screen,pos,,Mercy,Dr Test\n")
        import_labs.run_import(path, user_id=user)
        assert self.stored(user) == [
            ("2026-01-06", "ANA Screen", None, "positive", None),
            ("2026-01-06", "CRP", 18.4, None, "high"),
        ]

    def test_skips_incomplete_rows(self, user, tmp_path, capsys):
        path = self.write_csv(tmp_path, ",CRP,5,,,\n2026-01-06,,5,,,\n2026-01-06,CRP,,,,\n"
                                        "2026-01-06,CRP,5,mg/L,,\n")
        import_labs.run_import(path, user_id=user)
        assert len(self.stored(user)) == 1
        assert "Rows skipped:    3" in capsys.readouterr().out

    def test_a_dry_run_writes_nothing(self, user, tmp_path, capsys):
        path = self.write_csv(tmp_path, "2026-01-06,CRP,18.4,mg/L,,\n")
        import_labs.run_import(path, user_id=user, dry_run=True)
        assert self.stored(user) == []

    @pytest.mark.parametrize("value", ["<20", ">24.0"])
    def test_a_bounded_result_stays_text(self, user, tmp_path, capsys, value):
        path = self.write_csv(tmp_path, f"2026-01-06,anti-dsDNA,{value},IU/mL,,\n")
        import_labs.run_import(path, user_id=user)
        assert self.stored(user) == [("2026-01-06", "anti-dsDNA", None, value, None)]
