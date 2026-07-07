"""
biotracking import_labs.py
--------------------------
Imports lab results from the flare_lab_results CSV export
into the lab_results table.

Expected columns:
    Date, Test, Value, Units, Lab, Doctor

Usage:
    python import_labs.py path/to/flare_lab_results.csv
    python import_labs.py path/to/flare_lab_results.csv --dry-run
    python import_labs.py path/to/flare_lab_results.csv --preview 5
    python import_labs.py path/to/flare_lab_results.csv --user-id 2
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from typing import Optional


# ============================================================
# Known reference ranges and flags
# Add to this dict as needed - keyed by test name (case-insensitive)
# ============================================================
REFERENCE_RANGES = {
    "mcv":          ("80–100 fL",       lambda v: "high" if v > 100 else "low" if v < 80 else "normal"),
    "rbc":          ("4.2–5.4 M/uL",    lambda v: "low" if v < 4.2 else "normal"),
    "wbc":          ("4.5–11.0 K/uL",   lambda v: "high" if v > 11.0 else "low" if v < 4.5 else "normal"),
    "hemoglobin":   ("12.0–16.0 g/dL",  lambda v: "low" if v < 12.0 else "normal"),
    "hematocrit":   ("36–46 %",         lambda v: "low" if v < 36 else "normal"),
    "platelets":    ("150–400 K/uL",    lambda v: "high" if v > 400 else "low" if v < 150 else "normal"),
    "crp":          ("0–10 mg/L",       lambda v: "high" if v > 10 else "normal"),
    "esr":          ("0–20 mm/hr",      lambda v: "high" if v > 20 else "normal"),
    "c3":           ("90–180 mg/dL",    lambda v: "low" if v < 90 else "normal"),
    "c4":           ("16–47 mg/dL",     lambda v: "low" if v < 16 else "normal"),
    "tsh":          ("0.4–4.0 mIU/L",   lambda v: "high" if v > 4.0 else "low" if v < 0.4 else "normal"),
    "ferritin":     ("12–150 ng/mL",    lambda v: "high" if v > 150 else "low" if v < 12 else "normal"),
    "d-dimer":      ("0–0.5 mg/L",      lambda v: "high" if v > 0.5 else "normal"),
    "rf":           ("0–14 IU/mL",      lambda v: "high" if v > 14 else "normal"),
    "anti-ccp":     ("0–20 U/mL",       lambda v: "high" if v > 20 else "normal"),
    "vitamin d":    ("30–100 ng/mL",    lambda v: "low" if v < 30 else "normal"),
    "anti-dsdna":   ("0–200 IU/mL",     lambda v: "high" if v > 200 else "normal"),
}


def lookup_reference(test_name: str, value: float):
    """Return (reference_range, flag) for a known test, or (None, None)."""
    key = test_name.lower().strip()
    if key in REFERENCE_RANGES:
        ref_range, flag_fn = REFERENCE_RANGES[key]
        try:
            return ref_range, flag_fn(value)
        except Exception:
            return ref_range, None
    return None, None


def parse_date(value: str) -> Optional[str]:
    """Parse date to YYYY-MM-DD."""
    if not value or not value.strip():
        return None
    value = value.strip()
    formats = [
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%b %d, %Y",
        "%B %d, %Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    print(f"  WARNING: could not parse date '{value}'")
    return None


def parse_float(value: str) -> Optional[float]:
    if not value or not value.strip():
        return None
    # Strip common non-numeric characters
    cleaned = value.strip().replace("<", "").replace(">", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def run_import(csv_path: str, user_id: int = 1, dry_run: bool = False,
               preview: Optional[int] = None) -> None:

    if not os.path.exists(csv_path):
        print(f"ERROR: file not found: {csv_path}")
        sys.exit(1)

    print(f"sardinetracker lab import")
    print(f"=======================")
    print(f"File:     {csv_path}")
    print(f"User ID:  {user_id}")
    print(f"Dry run:  {dry_run}")
    if preview:
        print(f"Preview:  first {preview} rows")
    print()

    if not dry_run:
        import db

    processed = 0
    imported  = 0
    skipped   = 0
    errors    = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row in reader:
            processed += 1

            if preview and processed > preview:
                break

            # Parse fields
            date_str = parse_date(row.get("Date", ""))
            test_name = row.get("Test", "").strip()
            value_str = row.get("Value", "").strip()
            unit = row.get("Units", "").strip() or None
            lab_facility = row.get("Lab", "").strip() or None
            provider = row.get("Doctor", "").strip() or None

            # Validate required fields
            if not date_str:
                skipped += 1
                continue

            if not test_name:
                skipped += 1
                continue

            if not value_str:
                skipped += 1
                continue

            # Parse value - could be numeric or qualitative
            numeric_value = parse_float(value_str)
            qualitative_result = None

            # If not numeric, treat as qualitative
            if numeric_value is None:
                qual_lower = value_str.lower()
                if qual_lower in ("positive", "pos", "+"):
                    qualitative_result = "positive"
                elif qual_lower in ("negative", "neg", "-"):
                    qualitative_result = "negative"
                elif qual_lower in ("equivocal", "borderline"):
                    qualitative_result = "equivocal"
                else:
                    qualitative_result = value_str  # preserve as-is

            # Look up reference range and flag for known tests
            reference_range = None
            flag = None
            if numeric_value is not None:
                reference_range, flag = lookup_reference(test_name, numeric_value)

            if dry_run or preview:
                flag_str = f" [{flag}]" if flag else ""
                print(f"  {date_str}  {test_name:<20}  "
                      f"{value_str} {unit or ''}{flag_str}  "
                      f"({provider or '—'})")
                imported += 1
                continue

            try:
                data = {
                    "date":               date_str,
                    "test_name":          test_name,
                    "numeric_value":      numeric_value,
                    "unit":               unit,
                    "qualitative_result": qualitative_result,
                    "reference_range":    reference_range,
                    "flag":               flag,
                    "provider":           provider,
                    "lab_facility":       lab_facility,
                }
                db.add_lab_result(user_id, data)
                imported += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR on row {processed} ({date_str} {test_name}): {e}")

    print()
    print(f"Results")
    print(f"-------")
    print(f"Rows processed:  {processed}")
    print(f"Rows imported:   {imported}")
    print(f"Rows skipped:    {skipped}  (missing date, test, or value)")
    print(f"Errors:          {errors}")

    if dry_run:
        print()
        print("Dry run — nothing written. Remove --dry-run to import.")
    elif imported > 0:
        print()
        print("Import complete.")
        print("Open the clinical record → labs tab to verify.")


def _resolve_user_id(args) -> int:
    """Resolve user_id from --user (username) or --user-id (int)."""
    if args.user:
        import db
        user = db.get_user_by_username(args.user)
        if not user:
            print(f"ERROR: no user with username '{args.user}'")
            sys.exit(1)
        return user["id"]
    return args.user_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import lab results from flare_lab_results CSV"
    )
    parser.add_argument("csv_file", help="Path to lab results CSV file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing to database")
    parser.add_argument("--preview", type=int, metavar="N",
                        help="Preview first N rows only")
    user_group = parser.add_mutually_exclusive_group()
    user_group.add_argument("--user", type=str,
                            help="Username to import data for")
    user_group.add_argument("--user-id", type=int, default=1,
                            help="User ID to import data for (default: 1)")

    args = parser.parse_args()
    user_id = _resolve_user_id(args)
    run_import(args.csv_file, user_id=user_id,
              dry_run=args.dry_run, preview=args.preview)