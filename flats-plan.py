#!/usr/bin/env python3

from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import argparse
import json
import sys

from astropy.io import fits

import flat_common as fc


CONFIG = fc.CONFIG_FILE
PROFILES = fc.PROFILE_FILE
PICTURES = fc.PICTURES


def load_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text())


def observing_session_id(date_obs):
    dt = datetime.fromisoformat(
        date_obs.replace("Z", "+00:00")
    )
    return (dt - timedelta(hours=12)).strftime("%Y%m%d")


def header_value(hdr, key, default=None):
    value = hdr.get(key, default)
    if isinstance(value, str):
        value = value.strip()
    return value


def format_number(value, decimals=None):
    if value is None:
        return "unknown"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    if decimals is not None:
        return f"{number:.{decimals}f}"

    if number.is_integer():
        return str(int(number))

    return str(number)


def sanitize_component(value):
    text = str(value).strip()

    safe = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_"):
            safe.append(ch)
        else:
            safe.append("_")

    return "".join(safe)


def render_flat_filename(
    config,
    session,
    profile,
    gain,
    offset,
    xbin,
    ybin,
    rotsteps,
    seq=1,
):
    rotation = ""

    if rotsteps is not None:
        rotation = f"_R{sanitize_component(format_number(rotsteps))}"

    pattern = config["naming"]["flat_pattern"]

    return pattern.format(
        session=session,
        profile=sanitize_component(profile),
        gain=format_number(gain),
        offset=format_number(offset),
        xbin=format_number(xbin),
        ybin=format_number(ybin),
        rotation=rotation,
        seq=seq,
    )


def render_darkflat_filename(
    config,
    session,
    profile,
    gain,
    offset,
    xbin,
    ybin,
    exposure,
    seq=1,
):
    pattern = config["naming"]["darkflat_pattern"]

    return pattern.format(
        session=session,
        profile=sanitize_component(profile),
        gain=format_number(gain),
        offset=format_number(offset),
        xbin=format_number(xbin),
        ybin=format_number(ybin),
        exposure=format_number(exposure, 4),
        seq=seq,
    )


def classify_profile(hdr):
    """
    Filter identity is optional.

    If FITS FILTER exists, use it.
    Otherwise treat the optical path as NoFilter.
    """
    filt = header_value(hdr, "FILTER")

    if filt in (None, "", "--", "None"):
        return "NoFilter"

    return str(filt)


def flat_key(hdr, config):
    camera = header_value(hdr, "INSTRUME", "UnknownCamera")
    gain = header_value(hdr, "GAIN")
    offset = header_value(hdr, "OFFSET")
    xbin = header_value(hdr, "XBINNING", 1)
    ybin = header_value(hdr, "YBINNING", 1)

    profile = classify_profile(hdr)

    rotsteps = None

    if config["capabilities"].get(
        "use_rotsteps_if_present",
        True
    ):
        rotsteps = header_value(
            hdr,
            "ROTSTEPS"
        )

    return (
        camera,
        gain,
        offset,
        xbin,
        ybin,
        profile,
        rotsteps,
    )


def find_lights():
    files = []

    for path in PICTURES.rglob("*.fits"):
        try:
            with fits.open(
                path,
                memmap=False
            ) as hdul:
                hdr = hdul[0].header

            imagetyp = str(
                hdr.get("IMAGETYP", "")
            ).lower()

            if "light" not in imagetyp:
                continue

            file_session = hdr.get("SESSION")

            if not file_session:
                date_obs = hdr.get("DATE-OBS")

                if not date_obs:
                    continue

                file_session = observing_session_id(
                    date_obs
                )

            files.append(
                (
                    path,
                    hdr,
                    str(file_session),
                )
            )

        except Exception:
            continue

    return files


def main():
    parser = argparse.ArgumentParser(
        description="Plan nightly flat calibration groups."
    )

    parser.add_argument(
        "--session",
        help=(
            "Observing-night session ID. "
            "Defaults to latest available session."
        ),
    )

    args = parser.parse_args()

    config = load_json(CONFIG)
    profiles = load_json(PROFILES)

    lights = fc.find_light_frames(
        PICTURES
    )

    if not lights:
        print("No light frames found.")
        sys.exit(1)

    sessions = defaultdict(list)

    for item in lights:
        sessions[item[2]].append(item)

    if args.session:
        selected_session = str(args.session)

        if selected_session not in sessions:
            available = ", ".join(
                sorted(sessions.keys())
            )

            raise RuntimeError(
                f"No lights found for session "
                f"{selected_session}.\n"
                f"Available sessions: {available}"
            )
    else:
        selected_session = sorted(
            sessions.keys()
        )[-1]

    lights = sessions[selected_session]

    groups = defaultdict(list)

    rotsteps_present = 0

    for path, hdr, session in lights:
        key = fc.flat_group_key(
            hdr,
            use_rotsteps=config[
                "capabilities"
            ].get(
                "use_rotsteps_if_present",
                True,
            ),
        )

        if key[6] is not None:
            rotsteps_present += 1

        groups[key].append(path)

    print()
    print("=" * 72)
    print("NIGHTLY FLAT PLAN")
    print("=" * 72)
    print()
    print(
        f"Session: {selected_session}"
    )
    print(
        f"Light frames examined: {len(lights)}"
    )
    print(
        f"Flat groups required: {len(groups)}"
    )

    if rotsteps_present == 0:
        print(
            "Rotation metadata: unavailable "
            "(ROTSTEPS absent)"
        )
        print(
            "Rotator behavior: ignored"
        )

    elif rotsteps_present == len(lights):
        print(
            "Rotation metadata: available"
        )

    else:
        print(
            "WARNING: mixed ROTSTEPS metadata"
        )
        print(
            f"Tagged: {rotsteps_present}"
        )
        print(
            f"Untagged: "
            f"{len(lights) - rotsteps_present}"
        )

    fraction = config[
        "flat_target"
    ]["fraction"]

    tolerance_fraction = config[
        "flat_target"
    ]["tolerance_fraction"]

    preferred = config[
        "exposure_policy"
    ]["preferred_seconds"]

    count = config[
        "frames"
    ]["flats_per_group"]

    print()

    for index, (key, files) in enumerate(
        groups.items(),
        start=1
    ):
        (
            profile,
            camera,
            gain,
            offset,
            xbin,
            ybin,
            rotsteps,
        ) = key

        profile_data = profiles.get(
            profile,
            {}
        )

        bits = profile_data.get(
            "camera_bits",
            14
        )

        target_adu, tol_fraction, _, _ = fc.flat_targets(
            config,
            bits,
        )

        tolerance = round(
            target_adu
            * tol_fraction
        )

        ladder = fc.exposure_ladder(config)

        planned_exposure = fc.snap_to_ladder(
            profile_data.get("flat_exposure"),
            ladder,
        )

        brightness = profile_data.get(
            "brightness",
            "NOT CALIBRATED"
        )

        last_exposure = profile_data.get(
            "last_success_exposure",
            "unknown"
        )

        print("-" * 72)
        print(f"GROUP {index}")
        print("-" * 72)

        print(f"Camera:       {camera}")
        print(f"Gain:         {gain}")
        print(f"Offset:       {offset}")
        print(
            f"Binning:      {xbin}x{ybin}"
        )
        print(f"Profile:      {profile}")

        if rotsteps is None:
            print(
                "ROTSTEPS:     ignored"
            )
        else:
            print(
                f"ROTSTEPS:     {rotsteps}"
            )

        print(
            f"Light frames: {len(files)}"
        )

        print()
        print(
            f"Target ADU:   "
            f"{target_adu} ± {tolerance} "
            "(brightest CFA channel)"
        )
        print(
            f"Exposure:     "
            f"{planned_exposure:g} s (ladder: "
            + ", ".join(f"{step:g}" for step in ladder)
            + " s)"
        )
        print(
            f"Brightness:   {brightness}"
        )
        print(
            f"Last flat:    "
            f"{last_exposure} s"
        )
        print(
            f"Flat count:   {count}"
        )

        output_root = Path(
            config["output"]["root"]
        )

        group_root = (
            output_root
            / selected_session
            / profile
        )

        if rotsteps is not None:
            group_root = (
                group_root
                / f"ROT_{format_number(rotsteps)}"
            )

        flat_dir = (
            group_root
            / "flats"
        )

        if config.get(
            "darkflat_library",
            {},
        ).get("enabled", True):
            darkflat_dir = (
                fc.darkflat_library_root(config)
                / f"<camera state>_E{planned_exposure:g}s"
            )
        else:
            darkflat_dir = (
                group_root
                / "darkflats"
            )

        print()
        print(
            f"Flat output:  {flat_dir}"
        )

        flat_example = render_flat_filename(
            config=config,
            session=selected_session,
            profile=profile,
            gain=gain,
            offset=offset,
            xbin=xbin,
            ybin=ybin,
            rotsteps=rotsteps,
            seq=1,
        )

        print(
            f"Flat example: {flat_example}"
        )

        if config[
            "frames"
        ]["darkflats_enabled"]:
            print(
                f"Dark-flat:    {darkflat_dir}"
            )

            if isinstance(planned_exposure, (int, float)):
                darkflat_example = render_darkflat_filename(
                    config=config,
                    session=selected_session,
                    profile=profile,
                    gain=gain,
                    offset=offset,
                    xbin=xbin,
                    ybin=ybin,
                    exposure=planned_exposure,
                    seq=1,
                )

                print(
                    f"Dark example: {darkflat_example}"
                )
            else:
                print(
                    "Dark example: pending actual "
                    "flat exposure"
                )

        if not profile_data:
            print()
            print(
                "WARNING: profile has not "
                "been calibrated."
            )

        print()

    print("=" * 72)
    print(
        "PLAN ONLY. No devices were moved "
        "and no images were captured."
    )
    print("=" * 72)


if __name__ == "__main__":
    main()
