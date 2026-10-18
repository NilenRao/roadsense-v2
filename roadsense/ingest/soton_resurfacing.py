"""
Southampton highway maintenance schemes (resurfacing, surface treatment, patching) from Southampton City Council
pages, matched to our road segments.

    python -m roadsense.ingest.soton_resurfacing

Inputs in data/raw/soton_resurfacing/ (KEEP LOCAL, git-ignored):
  roads-closures-improvements_<date>.json  every table on the live "Roadworks, closures and improvements" page
        (https://www.southampton.gov.uk/travel-transport/roadworks/roads-closures-improvements/) as read in a
        browser on <date> (the site sits behind an Imperva check, so it is not fetched here). 2026 schemes.
  <page>_wayback_<timestamp>.html  Wayback Machine copies (https://web.archive.org/web/<timestamp>id_/<url>) of
        three council pages that now return "Page Not Found" (checked 17 Oct 2026):
        dft-quarterly   DfT Highway Maintenance Funding Quarterly Report, July 2024 (2023/24 completed, with cost/area)
        network-north   Network North highway maintenance (2023/24 and 2024/25, indicative cost/area)
        news-45-roads   news article "45 additional roads ... resurfacing" (2023/24 programme, roads + ward only;
                        the article says the list is not definitive)
Output: data/processed/soton_resurfacing.csv, one row per (scheme row, matched segment); a scheme row with no
        match is kept once with match_status set and no segment_id. Rows repeated unchanged across Wayback copies
        of the same page are kept once (earliest copy).

Matching rule: road name AND ward, both normalised, must be equal. Normalisation is case, punctuation, "&"/"and"
and standard street-type abbreviations (Ave, Rd, St, ...) only; spelling variants are NOT corrected and nothing
is matched on name alone or by distance. A segment named "A / B" matches road A or road B. A scheme covers part
of a road ("junction X to junction Y"); the match is every segment of that road in that ward, so it can include
segments outside the scheme's extent.

Manual links: data/resurfacing_manual_matches.csv (council_name, segment_id, reason, how_checked), written by hand
after a map check. A council row named there is linked only to the listed segments, with match_status "manual" and
the reason and check carried into the output; this replaces (does not add to) its automatic matches, so a false
name match such as "A33 Millbrook Road West (Flyover)" -> every Millbrook Road West segment is removed.

A leading road number ("A33 Millbrook Road West") is dropped before matching; a bracketed part ("(Flyover)") is kept
as the extent, not used for matching. Cost and area are filled only where a page gives them. Data only: nothing
here feeds scoring or ranking. Source: Southampton City Council (via the Wayback Machine for the removed pages).
"""
import glob, json, os, re
from html.parser import HTMLParser

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW = os.path.join(ROOT, "data", "raw", "soton_resurfacing")
OUT = os.path.join(ROOT, "data", "processed", "soton_resurfacing.csv")
SOURCE = "Southampton City Council, Roadworks, closures and improvements"

# tables with scheme rows: (caption prefix, kind). The "Roadworks as of ..." table (utility works, no ward) is skipped.
TABLES = {"Table 1": "planned", "Table 2": "completed", "Changes to programme": "changed"}
ABBREV = {"ave": "avenue", "rd": "road", "st": "street", "cres": "crescent", "cl": "close", "dr": "drive",
          "ln": "lane", "gdns": "gardens", "gr": "grove", "pl": "place", "sq": "square", "tce": "terrace"}
COLS = ["source", "source_url", "retrieved_at", "archived_at", "table", "scheme", "road", "extent", "ward_text",
        "work_type", "date_text", "year", "status", "cost_gbp", "area_m2", "match_status", "segment_id",
        "segment_name", "segment_ward", "match_reason", "match_checked"]
MANUAL = os.path.join(ROOT, "data", "resurfacing_manual_matches.csv")  # hand-checked links (council_name, segment_id, ...)
WAYBACK = {"dft-quarterly": ("Southampton City Council, DfT Highway Maintenance Funding Quarterly Report (July 2024), "
                             "via Wayback Machine", "https://www.southampton.gov.uk/travel-transport/roadworks/"
                             "roads-closures-improvements/dft-highway-maintenance-funding-quarterly-report/"),
           "network-north": ("Southampton City Council, Network North highway maintenance, via Wayback Machine",
                             "https://www.southampton.gov.uk/travel-transport/roadworks/roads-closures-improvements/"
                             "network-north-highway-maintenance/"),
           "news-45-roads": ("Southampton City Council news, 45 additional roads for resurfacing (2023/24 programme), "
                             "via Wayback Machine", "https://www.southampton.gov.uk/news/article/southampton-city-"
                             "council-announces-45-additional-roads-that-will-benefit-from-resurfacing-and-improvements/")}


def norm_road(s):
    words = re.sub(r"[^a-z0-9 ]", " ", str(s).lower().replace("&", " and ")).split()
    if len(words) > 1 and re.fullmatch(r"[abm]\d+", words[0]):  # road number prefix, e.g. "A33 Millbrook Road West"
        words = words[1:]
    return " ".join(ABBREV.get(w, w) for w in words)


def norm_ward(s):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(s).lower().replace("&", " and ")).split())


def split_scheme(scheme):
    """'Carlton Place(junction A to junction B)' -> ('Carlton Place', 'junction A to junction B')."""
    m = re.match(r"\s*([^(]+?)\s*(\((.*)\))?\s*$", scheme)
    return (m.group(1).strip(), (m.group(3) or "").strip()) if m else (scheme.strip(), "")


def year_of(text):
    """Year from the page's date text. The page has typos like 'May 202026': the last four digits are taken."""
    m = re.search(r"(\d{4,6})", str(text))
    return int(m.group(1)[-4:]) if m else None


def load_rows(path):
    page = json.load(open(path, encoding="utf-8"))
    rows = []
    for t in page["tables"]:
        kind = next((k for cap, k in TABLES.items() if t["caption"].startswith(cap)), None)
        if kind is None:
            continue
        for cells in t["rows"][1:]:  # by position: Table 2's header row is copied from the roadworks table
            scheme, ward, work, date = cells[0], cells[1], cells[2], cells[3]
            road, extent = split_scheme(scheme)
            rows.append({"source": SOURCE, "source_url": page["url"], "retrieved_at": page["retrieved_at"],
                         "archived_at": None, "table": kind, "scheme": scheme, "road": road, "extent": extent,
                         "ward_text": ward, "work_type": work, "date_text": date, "year": year_of(date),
                         "status": cells[4] if len(cells) > 4 else "", "cost_gbp": None, "area_m2": None})
    return rows


class _Page(HTMLParser):
    """Tables (rows of cell text) with the last text line before each table, plus all text lines."""
    def __init__(self):
        super().__init__()
        self.lines, self.tables, self._buf, self._cell, self._skip = [], [], [], None, 0

    def _flush(self):
        t = " ".join("".join(self._buf).split())
        if t:
            self.lines.append(t)
        self._buf = []

    def handle_starttag(self, tag, a):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self._flush()
            self.tables.append({"caption": self.lines[-1] if self.lines else "", "rows": []})
        elif tag == "tr" and self.tables:
            self.tables[-1]["rows"].append([])
        elif tag in ("td", "th"):
            self._cell = []
        elif tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4"):
            (self._cell.append(" / ") if self._cell is not None else self._flush())

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1
        elif tag in ("td", "th") and self._cell is not None and self.tables and self.tables[-1]["rows"]:
            self.tables[-1]["rows"][-1].append(" ".join("".join(self._cell).split()).strip(" /"))
            self._cell = None

    def handle_data(self, d):
        if not self._skip:
            (self._cell if self._cell is not None else self._buf).append(d)


def _num(s):
    m = re.search(r"[\d,]+(\.\d+)?", str(s))
    return float(m.group(0).replace(",", "")) if m else None


def load_wayback(path):
    page, ts = re.match(r"(.+)_wayback_(\d{14})\.html$", os.path.basename(path)).groups()
    source, url = WAYBACK[page]
    base = {"source": source, "source_url": f"https://web.archive.org/web/{ts}/{url}", "retrieved_at": "2026-10-17",
            "archived_at": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"}
    p = _Page()
    p.feed(open(path, encoding="utf-8", errors="replace").read())
    p._flush()
    rows = []
    if page == "news-45-roads":  # "Road, Ward" lines between the intro and the Pothole Patrol paragraph
        start = next(i for i, l in enumerate(p.lines) if "list of identified locations" in l)
        for l in p.lines[start + 1:]:
            if l.startswith("As part of a separate initiative"):
                break
            road, _, ward = l.rpartition(",")
            road, extent = split_scheme(road)
            rows.append({**base, "table": "identified", "scheme": l, "road": road, "extent": extent,
                         "ward_text": ward.strip(), "work_type": "resurfacing and improvements (identified)",
                         "date_text": "2023/24 programme", "year": "2023/24",
                         "status": "identified, not definitive", "cost_gbp": None, "area_m2": None})
        return rows
    for t in p.tables:
        head = t["rows"][0] if t["rows"] else []
        if len(head) < 2 or head[1] != "Ward":
            continue
        kind = "completed" if "completed" in head[0] else "planned"
        fy = re.search(r"\d{4}/\d{2}", t["caption"])
        for cells in t["rows"][1:]:
            if len(cells) < 5:
                continue
            road, extent = split_scheme(cells[0])
            rows.append({**base, "table": kind, "scheme": cells[0], "road": road, "extent": extent,
                         "ward_text": cells[1], "work_type": cells[2], "date_text": t["caption"],
                         "year": fy.group(0) if fy else None, "status": kind, "cost_gbp": _num(cells[3]),
                         "area_m2": _num(cells[4])})
    return rows


def load_manual(path=MANUAL):
    """{normalised council name: [(segment_id, reason, how_checked), ...]} from the hand-checked file."""
    if not os.path.exists(path):
        return {}
    out = {}
    for r in pd.read_csv(path).itertuples():
        out.setdefault(" ".join(r.council_name.lower().split()), []).append((r.segment_id, r.reason, r.how_checked))
    return out


def match(rows, seg, manual=None):
    """Exact road name + ward match. A council row listed in the manual file is linked ONLY to the segments listed
    there (match_status "manual"): its automatic matches, if any, are dropped."""
    manual = manual or {}
    seg = seg[seg["city"] == "Southampton"]
    by_id = seg.set_index("segment_id")
    idx = {}
    for r in seg.itertuples():
        for part in str(r.name or "").split(" / "):
            idx.setdefault((norm_road(part), norm_ward(r.ward)), []).append(r)
    known_roads = {k[0] for k in idx}
    out = []
    for row in rows:
        man = manual.get(" ".join(row["scheme"].lower().split()))
        if man:
            for sid, reason, how in man:
                s = by_id.loc[sid]
                out.append({**row, "match_status": "manual", "segment_id": sid, "segment_name": s["name"],
                            "segment_ward": s["ward"], "match_reason": reason, "match_checked": how})
            continue
        wards = [w for w in re.split(r"/", row["ward_text"]) if w.strip()]
        hits = [s for w in wards for s in idx.get((norm_road(row["road"]), norm_ward(w)), [])]
        if hits:
            for s in hits:
                out.append({**row, "match_status": "matched", "segment_id": s.segment_id, "segment_name": s.name,
                            "segment_ward": s.ward, "match_reason": "road name and ward equal after normalisation"})
        else:
            why = "road in another ward" if norm_road(row["road"]) in known_roads else "road name not found"
            out.append({**row, "match_status": f"unmatched: {why}", "segment_id": None, "segment_name": None,
                        "segment_ward": None})
    return pd.DataFrame(out, columns=COLS)


def main():
    paths = sorted(glob.glob(os.path.join(RAW, "roads-closures-improvements_*.json")))
    if not paths:
        raise SystemExit(f"no saved page in {RAW}")
    rows = load_rows(paths[-1])
    seen = set()
    for p in sorted(glob.glob(os.path.join(RAW, "*_wayback_*.html"))):  # earliest copy first
        for r in load_wayback(p):
            key = (r["source"], r["scheme"], r["ward_text"], r["work_type"], r["cost_gbp"], r["area_m2"], r["year"])
            if key not in seen:
                seen.add(key)
                rows.append(r)
    seg = pd.read_parquet(os.path.join(ROOT, "data", "processed", "segments.parquet"),
                          columns=["segment_id", "city", "name", "ward"])
    df = match(rows, seg, load_manual())
    df.to_csv(OUT, index=False)
    schemes = df.drop_duplicates(["source", "table", "scheme", "ward_text", "work_type", "date_text", "cost_gbp"])
    for src, g in schemes.groupby("source", sort=False):
        ok = g["match_status"].isin(["matched", "manual"])
        print(f"{src}: {len(g)} scheme rows, {(g['match_status'] == 'matched').sum()} matched, "
              f"{(g['match_status'] == 'manual').sum()} manual, {(~ok).sum()} unmatched")
        for r in g[~ok].itertuples():
            print(f"  unmatched: {r.road} [{r.ward_text}] ({r.match_status})")
    print(f"{df['segment_id'].nunique()} segments matched in all; wrote {OUT}")


if __name__ == "__main__":
    main()
