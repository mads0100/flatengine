#!/usr/bin/env python3

import sys
import json
from datetime import datetime, timedelta
from astropy.io import fits


SESSION_KEY = "SESSION"


def session_id_from_dateobs(date_obs):
    dt = datetime.fromisoformat(
        str(date_obs).replace("Z", "+00:00")
    )
    return (dt - timedelta(hours=12)).strftime("%Y%m%d")


def main():
    data = json.loads(sys.stdin.read())

    if data.get("script_type") != "POST_CAPTURE":
        return 0

    fits_info = data.get("fits", {})
    filename = fits_info.get("filename")

    if not filename:
        print("No FITS filename supplied by Ekos.", file=sys.stderr)
        return 1

    with fits.open(
        filename,
        mode="update",
        memmap=False
    ) as hdul:
        hdr = hdul[0].header

        date_obs = hdr.get("DATE-OBS")
        if not date_obs:
            print(
                f"{filename}: DATE-OBS missing",
                file=sys.stderr
            )
            return 1

        session = session_id_from_dateobs(date_obs)

        hdr[SESSION_KEY] = (
            session,
            "Observing-night session identifier"
        )

        hdul.flush()

    print(
        f"SESSION={session} stamped into {filename}"
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"Session stamping failed: {e}", file=sys.stderr)
        sys.exit(1)
