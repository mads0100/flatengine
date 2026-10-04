#!/usr/bin/env python3

from pathlib import Path
from datetime import datetime, timedelta
import argparse

from astropy.io import fits

import flat_common as fc


DEFAULT_ROOT = fc.PICTURES
SESSION_KEY = "SESSION"


def session_id_from_dateobs(date_obs):
    dt = datetime.fromisoformat(
        str(date_obs).replace("Z", "+00:00")
    )

    return (dt - timedelta(hours=12)).strftime("%Y%m%d")


def inspect_file(path):
    with fits.open(path, memmap=False) as hdul:
        hdr = hdul[0].header

        date_obs = hdr.get("DATE-OBS")
        existing = hdr.get(SESSION_KEY)
        imagetyp = hdr.get("IMAGETYP", "")

    if not date_obs:
        return {
            "path": path,
            "status": "skip",
            "reason": "missing DATE-OBS",
            "imagetype": imagetyp,
        }

    session = session_id_from_dateobs(date_obs)

    return {
        "path": path,
        "status": "ok",
        "session": session,
        "existing": existing,
        "imagetype": imagetyp,
        "date_obs": date_obs,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Preview SESSION FITS-header stamping "
            "without modifying files."
        )
    )

    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="Directory tree containing FITS files."
    )

    parser.add_argument(
        "--session",
        help=(
            "Only show files belonging to this session, "
            "for example 20261001."
        )
    )

    parser.add_argument(
        "--types",
        nargs="*",
        default=["Light", "Flat", "Dark"],
        help=(
            "Image-type substrings to include. "
            "Default: Light Flat Dark"
        )
    )

    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "Actually write SESSION into matching FITS headers. "
            "Without this flag the script is dry-run only."
        )
    )

    args = parser.parse_args()

    files = sorted(args.root.rglob("*.fits"))

    matched = 0
    skipped = 0
    already_correct = 0
    would_change = 0

    print()
    print("=" * 78)
    print("SESSION STAMP PREVIEW")
    print("=" * 78)
    print(f"Root: {args.root}")
    print(
        "Mode: WRITE"
        if args.write
        else "Mode: DRY RUN"
    )
    print()

    for path in files:
        try:
            info = inspect_file(path)
        except Exception as e:
            skipped += 1
            print(f"SKIP  {path}")
            print(f"      ERROR: {e}")
            continue

        if info["status"] != "ok":
            skipped += 1
            continue

        imagetype = str(info["imagetype"])

        if args.types:
            if not any(
                t.lower() in imagetype.lower()
                for t in args.types
            ):
                continue

        if args.session and info["session"] != args.session:
            continue

        matched += 1

        existing = info["existing"]
        desired = info["session"]

        if existing == desired:
            already_correct += 1
            state = "OK"

        else:
            would_change += 1

            if args.write:
                with fits.open(
                    path,
                    mode="update",
                    memmap=False
                ) as hdul:
                    hdul[0].header[
                        SESSION_KEY
                    ] = (
                        desired,
                        "Observing-night session identifier"
                    )
                    hdul.flush()

                state = "WROTE"

            else:
                state = "WRITE"

        print(f"{state:5} {path}")
        print(f"      IMAGETYP: {imagetype}")
        print(f"      DATE-OBS: {info['date_obs']}")
        print(f"      SESSION : {existing!r} -> {desired!r}")
        print()

    print("=" * 78)
    print(f"Matched files:        {matched}")
    print(f"Already correct:      {already_correct}")
    print(f"Would write SESSION:  {would_change}")
    print(f"Skipped/unreadable:   {skipped}")
    print()
    if args.write:
        print("Matching FITS headers were updated.")
    else:
        print("NO FILES WERE MODIFIED.")
    print("=" * 78)


if __name__ == "__main__":
    main()
