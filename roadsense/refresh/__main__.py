"""Daily data refresh.

    python -m roadsense.refresh [--city Southampton|Brighton] [--dry-run]

Pulls new FixMyStreet reports and the Open-Meteo forecast, re-scores with the frozen models, rebuilds
the Southampton plan, the Brighton inspection list and triage (new open reports only), validates the
build against the committed snapshot and only then points data/live/current at it. Exits 1 on any
failure, leaving the previous outputs in place. Never touches OSM, flood, wards or DfT data.
"""
import argparse
import sys

from roadsense.refresh.run import CITIES, refresh


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", choices=CITIES, action="append", help="refresh only this city (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="fetch, build and validate in a temp folder; switch nothing")
    a = ap.parse_args(argv)
    return refresh(cities=a.city, dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
