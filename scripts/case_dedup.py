#!/usr/bin/env python3
"""
Case Deduplication Analysis Tool — GitHub Issue #7
Reads a CSV export of ER simulator cases (from Google Sheets),
groups them by similarity, and outputs a JSON dedup report.

Usage:
    python scripts/case_dedup.py <cases.csv> [--output report.json] [--threshold 0.85]
    python scripts/case_dedup.py --help

ZSF: exits 0 with empty report when CSV missing or no cases found.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

# ── Column names (match import_cases_from_csv.py exactly) ────────────────────
CASE_ID_COL       = "Case_Organization_Case_ID"
SPARK_TITLE_COL   = "Case_Organization_Spark_Title"
REVEAL_TITLE_COL  = "Case_Organization_Reveal_Title"
COMPLAINT_COL     = "Patient_Demographics_and_Clinical_Data_Presenting_Complaint"
VITALS_COL        = "Monitor_Vital_Signs_Initial_Vitals"
AGE_COL           = "Patient_Demographics_and_Clinical_Data_Age"
GENDER_COL        = "Patient_Demographics_and_Clinical_Data_Gender"
DIFFICULTY_COL    = "Difficulty_Level"
CATEGORY_COL      = "Case_Organization_Medical_Category"


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = str(text or "").lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fingerprint(row: dict[str, Any]) -> str:
    """
    Build a dedup fingerprint from the fields most likely to uniquely identify
    a case.  Weighted: spark_title > complaint > vitals > age/gender.
    """
    parts = [
        _normalize(row.get(SPARK_TITLE_COL, "")),
        _normalize(row.get(COMPLAINT_COL, "")),
        _normalize(row.get(AGE_COL, "")),
        _normalize(row.get(GENDER_COL, "")),
    ]
    return " || ".join(p for p in parts if p)


def _jaccard(a: str, b: str) -> float:
    """Token-level Jaccard similarity between two normalised strings."""
    sa, sb = set(a.split()), set(b.split())
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _cluster(cases: list[dict], threshold: float) -> list[list[dict]]:
    """
    Greedy single-pass clustering.  O(n²) — fine for ≤1000 cases.
    Each case is assigned to the first existing cluster whose centroid
    (first member) has Jaccard similarity ≥ threshold to the candidate.
    """
    clusters: list[list[dict]] = []
    fps: list[str] = []

    for case in cases:
        fp = case["_fp"]
        placed = False
        for i, centroid_fp in enumerate(fps):
            if _jaccard(fp, centroid_fp) >= threshold:
                clusters[i].append(case)
                placed = True
                break
        if not placed:
            clusters.append([case])
            fps.append(fp)

    return clusters


def _load_cases(csv_path: Path) -> list[dict]:
    """Read CSV rows that have a case_id."""
    cases: list[dict] = []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                case_id = str(row.get(CASE_ID_COL) or "").strip()
                if not case_id:
                    continue
                row["_fp"] = _fingerprint(row)
                cases.append(dict(row))
    except FileNotFoundError:
        pass  # ZSF: return empty list
    return cases


def _build_report(clusters: list[list[dict]], threshold: float) -> dict:
    """Assemble the JSON report."""
    total = sum(len(c) for c in clusters)
    unique_clusters = len(clusters)

    duplicate_groups = []
    recommended_keep: list[dict] = []
    recommended_remove: list[dict] = []

    for cluster in clusters:
        # Sort by case_id to pick a stable "keep" candidate (lowest ID)
        sorted_c = sorted(cluster, key=lambda r: r.get(CASE_ID_COL, ""))
        keep = sorted_c[0]
        dups = sorted_c[1:]

        recommended_keep.append({
            "case_id":     keep.get(CASE_ID_COL, ""),
            "spark_title": keep.get(SPARK_TITLE_COL, ""),
            "complaint":   keep.get(COMPLAINT_COL, ""),
            "age":         keep.get(AGE_COL, ""),
            "gender":      keep.get(GENDER_COL, ""),
            "duplicate_count": len(dups),
        })

        if dups:
            duplicate_groups.append({
                "keep": {
                    "case_id":     keep.get(CASE_ID_COL, ""),
                    "spark_title": keep.get(SPARK_TITLE_COL, ""),
                },
                "duplicates": [
                    {
                        "case_id":     d.get(CASE_ID_COL, ""),
                        "spark_title": d.get(SPARK_TITLE_COL, ""),
                        "complaint":   d.get(COMPLAINT_COL, ""),
                    }
                    for d in dups
                ],
            })
            recommended_remove.extend(
                {"case_id": d.get(CASE_ID_COL, ""), "spark_title": d.get(SPARK_TITLE_COL, "")}
                for d in dups
            )

    return {
        "meta": {
            "tool":      "case_dedup.py",
            "issue":     "GitHub #7",
            "threshold": threshold,
        },
        "summary": {
            "total":           total,
            "unique_clusters": unique_clusters,
            "duplicate_count": total - unique_clusters,
            "reduction_pct":   round((total - unique_clusters) / total * 100, 1) if total else 0,
        },
        "duplicate_groups":  duplicate_groups,
        "recommended_keep":  recommended_keep,
        "recommended_remove": recommended_remove,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deduplicate ER simulator cases from a CSV export."
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        default="",
        help="Path to the CSV file exported from the case spreadsheet.",
    )
    parser.add_argument(
        "--output", "-o",
        default="",
        help="Write JSON report to this file (default: stdout).",
    )
    parser.add_argument(
        "--threshold", "-t",
        type=float,
        default=0.75,
        help="Jaccard similarity threshold for grouping duplicates (default: 0.75).",
    )

    args = parser.parse_args(argv)

    # ── ZSF: graceful empty report when no CSV given / file missing ───────────
    empty_report = {
        "meta": {"tool": "case_dedup.py", "issue": "GitHub #7", "threshold": args.threshold},
        "summary": {"total": 0, "unique_clusters": 0, "duplicate_count": 0, "reduction_pct": 0},
        "duplicate_groups": [],
        "recommended_keep": [],
        "recommended_remove": [],
    }

    csv_path = Path(args.csv_path) if args.csv_path else None
    if not csv_path or not csv_path.exists():
        if csv_path and not csv_path.exists():
            print(f"[case_dedup] WARNING: CSV not found: {csv_path}", file=sys.stderr)
        report_json = json.dumps(empty_report, indent=2)
        if args.output:
            Path(args.output).write_text(report_json)
        else:
            print(report_json)
        return 0

    cases = _load_cases(csv_path)
    if not cases:
        print("[case_dedup] WARNING: no cases with case_id found in CSV.", file=sys.stderr)
        report_json = json.dumps(empty_report, indent=2)
        if args.output:
            Path(args.output).write_text(report_json)
        else:
            print(report_json)
        return 0

    clusters = _cluster(cases, threshold=args.threshold)
    report = _build_report(clusters, threshold=args.threshold)

    # ── Summary to stderr so stdout stays pure JSON ───────────────────────────
    s = report["summary"]
    print(
        f"[case_dedup] {s['total']} cases → {s['unique_clusters']} unique clusters "
        f"({s['duplicate_count']} duplicates, {s['reduction_pct']}% reduction)",
        file=sys.stderr,
    )

    report_json = json.dumps(report, indent=2)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report_json)
        print(f"[case_dedup] Report written to {out_path}", file=sys.stderr)
    else:
        print(report_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
