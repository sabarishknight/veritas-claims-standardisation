#!/usr/bin/env python3
"""Reproduce every claim in docs/data-profile.md from the sample files.

No pipeline code is used: this reads the raw JSON and prints the evidence, so
the findings can be checked independently of the implementation that acts on
them.

    python3 scripts/profile_samples.py
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCOMING = ROOT / "sample-data" / "incoming"


def rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def load_documents():
    out = []
    for path in sorted(INCOMING.glob("*.json")):
        payload = json.loads(path.read_text())
        meta = {m["key"]: m["value"] for m in payload["data"]["metaDetails"]}
        for index, record in enumerate(payload["data"]["responseDetails"]):
            out.append({
                "file": path.name, "index": index,
                "classifier": record["classifier"], "data": record["data"],
                "meta": meta, "envelope": payload["data"],
                "trace": payload["traceId"],
            })
    return out


def number(text):
    match = re.match(r"^\s*([-+]?\d*\.?\d+)", str(text or ""))
    return float(match.group(1)) if match else None


def interval(text):
    s = str(text or "").strip()
    m = re.match(r"^([-+]?\d*\.?\d+)\s*[-–]\s*([-+]?\d*\.?\d+)$", s)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.match(r"^<\s*=?\s*([-+]?\d*\.?\d+)$", s)
    if m:
        return None, float(m.group(1))
    m = re.match(r"^>\s*=?\s*([-+]?\d*\.?\d+)$", s)
    if m:
        return float(m.group(1)), None
    return None


def main() -> None:
    docs = load_documents()

    rule("F-1  Lab rows are misaligned: names sit one row away from their values")
    lab = next(d for d in docs if d["file"] == "Sample_JSON_file2.json"
               and d["classifier"] == "lab_report")
    print(f"  {'row':>3}  {'test_name':26} {'range':16} {'unit':14} result")
    for i, row in enumerate(lab["data"]["report_details"][:6]):
        print(f"  {i:>3}  {row['test_name'][:26]:26} {row['range'][:16]:16} "
              f"{row['unit'][:14]:14} {row['result']}")
    print("\n  Row 0 is named haemoglobin but carries 4000-10000 cell/cu.mm, which is")
    print("  a total white cell count. Row 1 names the WBC count but carries")
    print("  4.50-5.50 mil/cu.mm, which is a red cell count. The names are the CBC")
    print("  panel in printed order; the values are the same panel one step later.")

    rule("F-2  The source's own interpretation disagrees with the arithmetic")
    wrong = total = 0
    examples = []
    for doc in docs:
        if doc["classifier"] != "lab_report":
            continue
        for row in doc["data"]["report_details"]:
            value = number(row["result"])
            bounds = interval(row["range"])
            claimed = (row.get("test_analytics") or "").strip().lower()
            if value is None or not bounds or claimed not in ("normal", "low", "high"):
                continue
            low, high = bounds
            total += 1
            truth = ("normal" if (low is None or value >= low)
                     and (high is None or value <= high)
                     else "low" if low is not None and value < low else "high")
            if truth != claimed:
                wrong += 1
                if len(examples) < 6:
                    examples.append(
                        (doc["file"], row["test_name"][:30], row["result"],
                         row["range"], claimed, truth))
    for file, name, result, rng, claimed, truth in examples:
        print(f"  {file:24} {name:32} {result:>8} in {rng:>14}  "
              f"source={claimed:7} arithmetic={truth}")
    print(f"\n  {wrong} of {total} checkable rows disagree "
          f"({wrong / max(total, 1):.0%}).")

    rule("F-3  The same field means different things in different files")
    for doc in docs:
        if doc["classifier"] != "lab_report":
            continue
        values = collections.Counter(
            r.get("test_analytics") for r in doc["data"]["report_details"])
        top = ", ".join(f"{k!r}x{v}" for k, v in values.most_common(5))
        print(f"  {doc['file']:26} source={doc['meta'].get('source_system'):10} {top}")

    rule("F-4  Duplicates do not share identifiers")
    transport = {"traceid", "correlationid", "documentid", "claim_no", "nt_code"}

    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k.casefold() not in transport}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    groups = collections.defaultdict(list)
    for doc in docs:
        digest = hashlib.sha256(
            json.dumps(strip(doc["data"]), sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()[:16]
        groups[digest].append(f"{doc['file']}#{doc['index']}")
    for digest, members in groups.items():
        marker = "DUPLICATE" if len(members) > 1 else "unique   "
        print(f"  {marker}  {digest}  {members}")
    print()
    for doc in docs:
        if doc["file"] in ("Sample_JSON_file1.json", "Sample_JSON_file3.json"):
            print(f"  {doc['file']:26} documentId={doc['envelope']['documentId']}  "
                  f"claim={doc['meta'].get('claim_no')}")

    rule("F-5  Panels are repeated across pages")
    for doc in docs:
        if doc["classifier"] != "lab_report":
            continue
        rows = doc["data"]["report_details"]
        seen, repeats = set(), 0
        for row in rows:
            key = (row["test_name"], row["result"], row["range"], row["unit"])
            if key in seen:
                repeats += 1
            seen.add(key)
        print(f"  {doc['file']:26} {len(rows):4} rows, {repeats:3} verbatim repeats")

    rule("F-7  Values hidden inside test names")
    pattern = re.compile(r"[-(]\s*\d")
    for doc in docs:
        if doc["classifier"] != "lab_report":
            continue
        for row in doc["data"]["report_details"]:
            if pattern.search(row["test_name"]) and not row["result"]:
                print(f"  {doc['file']:26} {row['test_name']!r}")

    rule("F-8  Placeholders and leaked structure")
    for doc in docs:
        if doc["classifier"] != "lab_report":
            continue
        info = doc["data"]["basic_info"]
        print(f"  {doc['file']:26} bill_date={info.get('bill_date')!r:16} "
              f"reports_date={info.get('reports_date')!r}")
        for row in doc["data"]["report_details"]:
            why = []
            if row["result"] and row["result"] == row["range"]:
                why.append("result==range")
            if row["unit"] and row["unit"] == row["test_name"]:
                why.append("unit==test_name")
            if row["test_name"] in ("test_name", "result", "range", "unit"):
                why.append("header-row-leaked")
            if why:
                print(f"      {','.join(why):28} {json.dumps(row)[:110]}")

    rule("F-9  Unit spellings across the corpus")
    units = collections.Counter(
        r["unit"] for d in docs if d["classifier"] == "lab_report"
        for r in d["data"]["report_details"])
    print(f"  {len(units)} distinct spellings")
    print("  " + " | ".join(f"{k!r}:{v}" for k, v in units.most_common(22)))

    rule("F-10  Ambiguity only the unit can settle")
    pairs = collections.defaultdict(set)
    for doc in docs:
        if doc["classifier"] != "lab_report":
            continue
        for row in doc["data"]["report_details"]:
            pairs[row["test_name"].strip().upper()].add(row["unit"])
    for name in ("NEUTROPHILS", "LYMPHOCYTES", "EOSINOPHILS", "MONOCYTES", "BASOPHILS"):
        if name in pairs and len(pairs[name]) > 1:
            print(f"  {name:16} reported in {sorted(pairs[name])}")

    print("\nDone. Each finding above maps to a mechanism in docs/data-profile.md.")


if __name__ == "__main__":
    main()
