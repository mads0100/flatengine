#!/usr/bin/env python3

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import math

from astropy.io import fits

import os


HOME = Path.home()

CONFIG_DIR = Path(
    os.environ.get(
        "FLATENGINE_CONFIG_DIR",
        HOME / ".config" / "flatengine",
    )
)

STATE_DIR = Path(
    os.environ.get(
        "FLATENGINE_STATE_DIR",
        HOME / ".local" / "state" / "flatengine",
    )
)

DATA_DIR = Path(
    os.environ.get(
        "FLATENGINE_DATA_DIR",
        HOME / ".local" / "share" / "flatengine",
    )
)

PICTURES = Path(
    os.environ.get(
        "FLATENGINE_PICTURES",
        HOME / "Pictures",
    )
)

CONFIG_FILE = CONFIG_DIR / "flat-config.json"
PROFILE_FILE = CONFIG_DIR / "flat-filters.json"

QUEUE_DIR = STATE_DIR / "queues"

TEMPLATE_FILE = (
    DATA_DIR
    / "templates"
    / "flat-calibration.esq"
)

PROBE_QUEUE = QUEUE_DIR / "flat-probe.esq"
EXEC_QUEUE = QUEUE_DIR / "flat-execute.esq"
DARKFLAT_QUEUE = QUEUE_DIR / "darkflat-execute.esq"



def observing_session_id(date_obs):
    """
    Convert DATE-OBS into the observing-night ID.

    The observing day rolls over at local noon so frames
    captured after midnight remain associated with the
    previous evening's observing session.
    """
    dt = datetime.fromisoformat(
        str(date_obs).replace("Z", "+00:00")
    )

    return (
        dt - timedelta(hours=12)
    ).strftime("%Y%m%d")


def session_from_header(hdr):
    """
    SESSION is authoritative when present.
    DATE-OBS is the fallback for older frames.
    """
    session = hdr.get("SESSION")

    if session not in (
        None,
        "",
        "None",
    ):
        return str(session)

    date_obs = hdr.get("DATE-OBS")

    if not date_obs:
        return None

    return observing_session_id(date_obs)


def profile_from_header(hdr):
    """
    FILTER is optional.

    Missing/blank filter state becomes NoFilter.
    """
    filt = hdr.get("FILTER")

    if filt in (
        None,
        "",
        "--",
        "None",
    ):
        return "NoFilter"

    return str(filt)


def is_light_frame(hdr):
    return (
        "light"
        in str(
            hdr.get("IMAGETYP", "")
        ).lower()
    )


def flat_group_key(
    hdr,
    use_rotsteps=True,
):
    """
    Canonical calibration-group identity.

    ROTATANG deliberately does NOT participate in grouping.
    Small floating-point differences in reported angle must
    not split lights taken at the same mechanical position.
    """
    rotsteps = (
        hdr.get("ROTSTEPS")
        if use_rotsteps
        else None
    )

    return (
        profile_from_header(hdr),
        hdr.get("INSTRUME"),
        hdr.get("GAIN"),
        hdr.get("OFFSET"),
        hdr.get("XBINNING", 1),
        hdr.get("YBINNING", 1),
        rotsteps,
    )


def find_light_frames(
    pictures,
    session=None,
):
    pictures = Path(pictures)
    results = []

    for path in pictures.rglob("*.fits"):
        try:
            with fits.open(
                path,
                memmap=False,
            ) as hdul:
                hdr = hdul[0].header.copy()
        except Exception:
            continue

        if not is_light_frame(hdr):
            continue

        file_session = session_from_header(
            hdr
        )

        if file_session is None:
            continue

        if (
            session is not None
            and str(file_session)
            != str(session)
        ):
            continue

        results.append(
            (
                path,
                hdr,
                str(file_session),
            )
        )

    return results


def find_session_groups(
    pictures,
    session,
    use_rotsteps=True,
):
    groups = defaultdict(list)

    for path, hdr, _ in find_light_frames(
        pictures,
        session=session,
    ):
        key = flat_group_key(
            hdr,
            use_rotsteps=use_rotsteps,
        )

        groups[key].append(path)

    return groups


def _circular_mean_degrees(values):
    if not values:
        return None

    radians = [
        math.radians(
            float(value)
        )
        for value in values
    ]

    x = sum(
        math.cos(value)
        for value in radians
    )

    y = sum(
        math.sin(value)
        for value in radians
    )

    if (
        abs(x) < 1e-12
        and abs(y) < 1e-12
    ):
        return float(values[0]) % 360.0

    angle = math.degrees(
        math.atan2(y, x)
    )

    return angle % 360.0


def group_rotatang(paths):
    """
    Return one representative ROTATANG for a mechanical
    ROTSTEPS group.

    Circular mean handles the 359° / 0° boundary correctly.
    """
    values = []

    for path in paths:
        try:
            with fits.open(
                path,
                memmap=False,
            ) as hdul:
                angle = hdul[0].header.get(
                    "ROTATANG"
                )
        except Exception:
            continue

        if angle is not None:
            values.append(
                float(angle)
            )

    return _circular_mean_degrees(
        values
    )


def latest_session(pictures):
    sessions = {
        session
        for _, _, session
        in find_light_frames(pictures)
    }

    if not sessions:
        return None

    return sorted(sessions)[-1]
