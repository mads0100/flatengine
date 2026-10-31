#!/usr/bin/env python3
"""
Calibration catalog: which dark sets you have, wherever the files live.

The capture computer usually hands calibration files to a processing
machine, so FlatEngine tracks dark sets in a catalog instead of
looking for files on disk. Darks for lights and dark-flats share it.

  flatengine catalog                       list cataloged dark sets
  flatengine catalog scan DIR [--dry-run]  register every dark set
                                           found in DIR's FITS headers
                                           (run before moving files)
  flatengine catalog add --like FILE.fits --exposure 120 --count 50
                         [--built YYYY-MM-DD]
                                           register a set shot elsewhere
  flatengine catalog remove KEY            forget a set
  flatengine catalog check [--session ID]  check a session's lights
                                           against the catalog
"""

from collections import defaultdict
from pathlib import Path
import argparse
import json
import sys

from astropy.io import fits

import flat_common as fc


def load_config():
    return json.loads(fc.CONFIG_FILE.read_text())


def is_dark(hdr):
    return "dark" in str(hdr.get("IMAGETYP", "")).lower()


def show(config):
    catalog = fc.load_catalog()["darks"]

    print()
    print("=" * 72)
    print("CALIBRATION CATALOG")
    print("=" * 72)
    print(f"File: {fc.CATALOG_FILE}")

    if not catalog:
        print("Empty. Register sets with "
              "`flatengine catalog scan DIR` or build them with "
              "`flatengine darks --build`.")
        print("=" * 72)
        return

    for key in sorted(
        catalog,
        key=lambda k: (float(catalog[k].get("exposure", 0)), k),
    ):
        record = catalog[key]
        _, problem = fc.catalog_check(
            key,
            record.get("count", 0),
            config,
        )
        status = "OK" if problem is None else f"STALE: {problem}"

        print()
        print(key)
        print(f"  {record.get('count')} x {float(record['exposure']):g} s"
              f"  built {record.get('built')}"
              f"  firmware {record.get('firmware') or 'n/a'}"
              f"  [{status}]")
        print(f"  source {record.get('source', 'n/a')}"
              f"  location {record.get('location', 'n/a')}")

    print("=" * 72)


def scan(config, directory, dry_run):
    directory = Path(directory).expanduser()

    if not directory.is_dir():
        raise RuntimeError(f"Not a directory: {directory}")

    sets = {}
    counts = defaultdict(int)
    dates = defaultdict(str)
    locations = defaultdict(set)
    skipped = 0

    for path in sorted(
        list(directory.rglob("*.fits"))
        + list(directory.rglob("*.fit"))
    ):
        try:
            with fits.open(path, memmap=False) as hdul:
                hdr = hdul[0].header.copy()
        except Exception:
            skipped += 1
            continue

        if not is_dark(hdr) or hdr.get("EXPTIME") is None:
            continue

        if hdr.get("INSTRUME") is None or hdr.get("GAIN") is None:
            skipped += 1
            continue

        key, record = fc.dark_record_from_header(hdr, config)
        sets[key] = record

        # A stacked master counts as the frames it combined.
        counts[key] += int(hdr.get("NCOMBINE", 1) or 1)

        date = str(hdr.get("DATE-OBS", ""))[:10]
        if date > dates[key]:
            dates[key] = date

        locations[key].add(str(path.parent))

    print()
    print(f"Scanned {directory}")

    if not sets:
        print("No dark frames with camera metadata found.")
        return

    for key in sorted(sets, key=lambda k: float(sets[k]["exposure"])):
        record = dict(
            sets[key],
            count=counts[key],
            built=dates[key] or None,
            location=", ".join(sorted(locations[key])),
            source="scan",
        )

        print(f"  {counts[key]:>4} x {float(record['exposure']):g} s  {key}")

        if not dry_run:
            fc.catalog_register(key, record)
            fc.clear_pending(fc.darkflat_library_root(config) / key)
            fc.clear_pending(fc.dark_library_root(config) / key)

    if skipped:
        print(f"Skipped {skipped} unreadable or unlabeled files.")

    if dry_run:
        print("Dry run: nothing registered.")
    else:
        print(f"Registered {len(sets)} dark set(s) in {fc.CATALOG_FILE}")


def add(config, like, exposure, count, built):
    with fits.open(like, memmap=False) as hdul:
        hdr = hdul[0].header.copy()

    key, record = fc.dark_record_from_header(
        hdr, config, exposure=exposure,
    )

    record.update(
        count=int(count),
        source="manual",
        location="registered by hand",
    )

    if built:
        record["built"] = built

    fc.catalog_register(key, record)
    fc.clear_pending(fc.darkflat_library_root(config) / key)
    fc.clear_pending(fc.dark_library_root(config) / key)

    print(f"Registered: {count} x {exposure:g} s  {key}")


def check(config, session):
    session = session or fc.latest_session(fc.PICTURES)

    if session is None:
        print("No light frames found.")
        return

    results = fc.light_dark_requirements(fc.PICTURES, session, config)

    print()
    print(f"DARKS FOR LIGHTS — session {session}")

    for key, record, problem, lights in results:
        status = "OK" if problem is None else f"NEEDED: {problem}"
        print(f"  {lights:>4} lights  {float(record['exposure']):g} s  "
              f"{key}  [{status}]")


def main():
    parser = argparse.ArgumentParser(
        description="Track which dark sets exist, wherever the files live.",
    )
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Register dark sets found in a folder.")
    p_scan.add_argument("directory")
    p_scan.add_argument("--dry-run", action="store_true")

    p_add = sub.add_parser("add", help="Register a set shot elsewhere.")
    p_add.add_argument("--like", type=Path, required=True,
                       help="A frame in the camera state of the set.")
    p_add.add_argument("--exposure", type=float, required=True)
    p_add.add_argument("--count", type=int, required=True)
    p_add.add_argument("--built", help="Date shot, YYYY-MM-DD.")

    p_remove = sub.add_parser("remove", help="Forget a set.")
    p_remove.add_argument("key")

    p_check = sub.add_parser("check", help="Check a session's lights.")
    p_check.add_argument("--session")

    args = parser.parse_args()
    config = load_config()

    if args.command == "scan":
        scan(config, args.directory, args.dry_run)
    elif args.command == "add":
        add(config, args.like, args.exposure, args.count, args.built)
    elif args.command == "remove":
        print("Removed." if fc.catalog_remove(args.key) else "No such key.")
    elif args.command == "check":
        check(config, args.session)
    else:
        show(config)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print()
        print("=" * 72)
        print("CATALOG ERROR")
        print("=" * 72)
        print(str(exc).strip())
        print("=" * 72)
        raise SystemExit(2)
