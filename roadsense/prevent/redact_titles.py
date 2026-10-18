"""Redact house numbers and personal names from FixMyStreet report titles before the repo goes public.

    python -m roadsense.prevent.redact_titles FILE.csv [FILE.csv ...] [--column title] [--write]

Without --write it only prints every change (and titles worth a human look) so a person can review them.
With --write it rewrites the files in place. Keeps the rest of each title, so triage still reads
"Pothole outside [number] Ashtree Road" as a pothole. Name detection is rule-based and imperfect: check the
"review by eye" list it prints.
"""
from __future__ import annotations

import argparse
import re
import sys

import pandas as pd

STREET = (r"(?:road|rd|street|st|avenue|ave|close|lane|ln|way|drive|dr|crescent|cres|gardens|gdns|grove|place|pl|"
          r"terrace|court|ct|hill|park|row|mews|walk|square|sq|parade|rise|green|view|vale|end|mount|chase)")
# "number 66", "no 66", "no. 66", "#66", "house 12", "flat 3", "unit 4b", "apartment 2"
_HOUSE_WORD = re.compile(r"\b(number|no\.?|nos\.?|house|flat|unit|apartment|apt\.?)\s*#?\s*\d+[a-z]?\b", re.I)
_HASH = re.compile(r"#\s*\d+[a-z]?\b")
# "66 Ashtree Road", "64 peartree avenue", "21-23 Mowbray Road", "12a High Street"
_NUM_STREET = re.compile(r"\b\d+[a-z]?(?:\s*[-/&]\s*\d+[a-z]?)?(?=,?\s+((?:[A-Za-z'’]+\s+){1,3})" + STREET + r"\b)", re.I)
# words that can't be part of a street name, so "the 2 lanes of Bassett Avenue" is not an address
_NOT_NAME = {"of", "on", "in", "at", "the", "a", "an", "near", "by", "to", "and", "lanes", "lane", "potholes", "pothole",
             "holes", "hole", "metres", "meters", "feet", "ft", "m", "yards", "inches", "cm", "mm", "weeks", "days",
             "months", "years", "times", "cars", "buses", "large", "deep", "big", "small", "huge", "more", "separate",
             "inch", "foot", "metre", "meter", "x", "between", "opposite", "outside", "from", "wide"}
# a number joined to an already-redacted one: "between 8 and [number]", "[number]/10", "1 and [number]"
_JOIN_BEFORE = re.compile(r"\b\d+[a-z]?(?=\s*(?:and|to|&|\+|/|-|,)\s*\[number\])", re.I)
_JOIN_AFTER = re.compile(r"(?<=\[number\])(\s*(?:and|to|&|\+|/|-|,)\s*)\d+[a-z]?\b", re.I)
# full UK postcode (identifies a handful of houses): "BN1 6RH", "SO16 3LR"
_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")
# bare house number after a place word at the end of a phrase: "outside 91 [postcode]", "opposite 12."
_BARE = re.compile(r"\b((?:outside|out side|opposite|opp\.?|o/s|adjacent to|next to)\s+)\d+[a-z]?(?:\s*[-/&]\s*\d+[a-z]?)?\b"
                   r"(?!\s*(?:inch|inches|in\b|ft|foot|feet|m\b|cm|mm|metre|meter|yard|mph|mile|x\b|%))", re.I)
# "Mr R C Richards", "Mrs Smith", "Dr. J. Brown" (title word first, then capitalised names or initials)
_NAME = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mx|Dr|Sir|Dame|Rev|Cllr|Councillor)\.?(?:\s+(?:[A-Z][a-z'’-]+|[A-Z]\.?(?![a-z]))){1,4}")
_REVIEW = re.compile(r"\b(neighbour|my (?:house|home|drive|driveway|wife|husband|son|daughter|mum|dad)|"
                     r"[A-Z][a-z]+['’]s (?:house|home|drive))\b", re.I)


def _name_ok(text: str, m: re.Match) -> bool:
    """Skip 'Marine Dr A259' style matches: Dr/St after a capitalised word is a street, not a person."""
    before = text[:m.start()].rstrip().split(" ")
    prev = before[-1] if before and before[-1] else ""
    return not (prev[:1].isupper() and m.group(0).split()[0].rstrip(".") in ("Dr", "St"))


def redact(title) -> str:
    if not isinstance(title, str) or not title:
        return title
    t = _HOUSE_WORD.sub(lambda m: re.sub(r"#?\s*\d+[a-z]?$", " [number]", m.group(0), flags=re.I).replace("  ", " "), title)
    t = _HASH.sub("[number]", t)
    t = _NUM_STREET.sub(lambda m: m.group(0) if set(m.group(1).lower().split()) & _NOT_NAME else "[number]", t)
    for _ in range(4):  # spread across "8 and 10", "14 to 18 and 9 to 11"
        t2 = _JOIN_AFTER.sub(r"\1[number]", _JOIN_BEFORE.sub("[number]", t))
        if t2 == t:
            break
        t = t2
    t = _POSTCODE.sub("[postcode]", t)
    t = _BARE.sub(r"\1[number]", t)
    t = _NAME.sub(lambda m: "[name]" if _name_ok(t, m) else m.group(0), t)
    return t


def needs_review(title) -> bool:
    return isinstance(title, str) and bool(_REVIEW.search(title))


def redact_frame(df: pd.DataFrame, column: str = "title") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (redacted copy, changes table with id/before/after)."""
    out = df.copy()
    before = out[column]
    out[column] = before.map(redact)
    changed = before.fillna("") != out[column].fillna("")
    ids = out[next((c for c in ("id", "fms_id") if c in out), column)]
    changes = pd.DataFrame({"id": ids[changed], "before": before[changed], "after": out.loc[changed, column]})
    return out, changes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--column", default="title")
    ap.add_argument("--write", action="store_true", help="rewrite the files (default: report only)")
    a = ap.parse_args(argv)
    total = 0
    for f in a.files:
        df = pd.read_csv(f, dtype=str, keep_default_na=False)
        if a.column not in df:
            print(f"{f}: no '{a.column}' column, skipped")
            continue
        red, ch = redact_frame(df, a.column)
        review = df[df[a.column].map(needs_review) & ~df.index.isin(ch.index)]
        total += len(ch)
        print(f"\n== {f}: {len(ch)} of {len(df)} titles redacted{' (written)' if a.write else ' (dry run)'}")
        for r in ch.itertuples():
            print(f"  {r.id}: {r.before!r} -> {r.after!r}")
        if len(review):
            print(f"  review by eye ({len(review)}):")
            for r in review.itertuples():
                print(f"    {getattr(r, 'id', getattr(r, 'fms_id', ''))}: {getattr(r, a.column)!r}")
        if a.write:
            red.to_csv(f, index=False, encoding="utf-8", lineterminator="\n")
    print(f"\n{total} titles changed in {len(a.files)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
