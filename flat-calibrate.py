#!/usr/bin/env python3

import argparse
import json
import math
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import flat_common as fc


# ---------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------

DEFAULT_TEMPLATE = fc.TEMPLATE_FILE
QUEUE_FILE = fc.QUEUE_DIR / "flat-calibration.esq"
PROFILE_FILE = fc.PROFILE_FILE

CAPTURE_IFACE = "org.kde.kstars.Ekos.Capture"
CAPTURE_PATH = "/KStars/Ekos/Capture"

FLAT_DEVICE = None
TRAIN = None

PANEL_MIN = 0
PANEL_MAX = 255
PANEL_STEP = 10


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def run(cmd, check=True):
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
    )

    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed:\n{' '.join(cmd)}\n"
            f"{result.stdout}\n{result.stderr}"
        )

    return result.stdout.strip()


def indi_get(pattern):
    return run(["indi_getprop", pattern], check=False)


def indi_set(expression):
    return run(["indi_setprop", expression])


KSTARS_DEST = None


def discover_kstars_capture_dest():
    result = run([
        "busctl",
        "--user",
        "list",
    ])

    candidates = []

    for line in result.splitlines():
        parts = line.split()

        if not parts:
            continue

        name = parts[0]

        if name.startswith(":"):
            candidates.append(name)

    candidates.append("org.kde.kstars")

    for dest in candidates:
        try:
            probe = run([
                "gdbus",
                "introspect",
                "--session",
                "--dest", dest,
                "--object-path", CAPTURE_PATH,
            ])

            if CAPTURE_IFACE in probe:
                return dest

        except Exception:
            continue

    raise RuntimeError(
        "Could not find a running KStars instance "
        "with Ekos Capture available."
    )


def get_kstars_dest():
    global KSTARS_DEST

    if KSTARS_DEST is None:
        KSTARS_DEST = discover_kstars_capture_dest()
        print(
            f"Using KStars D-Bus destination: {KSTARS_DEST}"
        )

    return KSTARS_DEST


def dbus(method, *args):
    cmd = [
        "gdbus", "call",
        "--session",
        "--dest", get_kstars_dest(),
        "--object-path", CAPTURE_PATH,
        "--method", f"{CAPTURE_IFACE}.{method}",
    ]

    cmd.extend(str(arg) for arg in args)

    return run(cmd)


def quantize_brightness(value):
    value = max(PANEL_MIN, min(PANEL_MAX, value))

    if value == 0:
        return 0

    q = round(value / PANEL_STEP) * PANEL_STEP

    return int(
        max(PANEL_STEP, min(PANEL_MAX, q))
    )


def current_brightness():
    text = indi_get(
        f"{FLAT_DEVICE}.FLAT_LIGHT_INTENSITY."
        "FLAT_LIGHT_INTENSITY_VALUE"
    )

    if "=" not in text:
        raise RuntimeError(
            "Could not read Flip-Flat brightness."
        )

    return int(round(float(text.split("=", 1)[1])))


def set_brightness(value):
    value = quantize_brightness(value)

    indi_set(
        f"{FLAT_DEVICE}.FLAT_LIGHT_INTENSITY."
        f"FLAT_LIGHT_INTENSITY_VALUE={value}"
    )

    time.sleep(0.5)

    actual = current_brightness()

    print(f"Panel brightness: {actual}")

    return actual


def light_on():
    indi_set(
        f"{FLAT_DEVICE}.FLAT_LIGHT_CONTROL."
        "FLAT_LIGHT_ON=On"
    )


def light_off():
    indi_set(
        f"{FLAT_DEVICE}.FLAT_LIGHT_CONTROL."
        "FLAT_LIGHT_OFF=On"
    )


def ensure_cover_closed(timeout=30):
    indi_set(
        f"{FLAT_DEVICE}.CAP_PARK.PARK=On"
    )

    start = time.time()

    while True:
        snapshot = indi_get(
            f"{FLAT_DEVICE}.*.*"
        )

        park_ok = (
            f"{FLAT_DEVICE}.CAP_PARK.PARK=On"
            in snapshot
        )

        cover_ok = (
            f"{FLAT_DEVICE}.Status.Cover=Closed"
            in snapshot
        )

        motor_ok = (
            f"{FLAT_DEVICE}.Status.Motor=Stopped"
            in snapshot
        )

        if park_ok and cover_ok and motor_ok:
            print("Flip-Flat cover: confirmed closed")
            return

        if time.time() - start > timeout:
            raise RuntimeError(
                "Timed out waiting for Flip-Flat "
                "cover to close."
            )

        time.sleep(1)


# ---------------------------------------------------------------------
# Profile storage
# ---------------------------------------------------------------------

def load_config():
    if not fc.CONFIG_FILE.exists():
        raise RuntimeError(
            "CONFIGURATION ERROR\n"
            f"Config file not found: {fc.CONFIG_FILE}"
        )

    return json.loads(
        fc.CONFIG_FILE.read_text()
    )


def configure_runtime(config):
    global TRAIN
    global FLAT_DEVICE

    TRAIN = str(
        config.get(
            "ekos",
            {}
        ).get(
            "optical_train",
            ""
        )
    ).strip()

    FLAT_DEVICE = str(
        config.get(
            "panel",
            {}
        ).get(
            "device",
            ""
        )
    ).strip()

    if not TRAIN:
        raise RuntimeError(
            "CONFIGURATION ERROR\n"
            "Missing ekos.optical_train."
        )

    if not FLAT_DEVICE:
        raise RuntimeError(
            "CONFIGURATION ERROR\n"
            "Missing panel.device."
        )

    fc.QUEUE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


def load_profiles():
    if not PROFILE_FILE.exists():
        return {}

    return json.loads(
        PROFILE_FILE.read_text()
    )


def save_profile(
    filter_name,
    brightness,
    target_adu,
    target_exposure,
    actual_exposure,
    bits,
):
    profiles = load_profiles()

    profiles[filter_name] = {
        "brightness": brightness,
        "target_adu": target_adu,
        "target_fraction": 0.5,
        "preferred_exposure": target_exposure,
        "last_success_exposure": actual_exposure,
        "camera_bits": bits,
        "updated": time.strftime(
            "%Y-%m-%dT%H:%M:%S"
        ),
    }

    PROFILE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    PROFILE_FILE.write_text(
        json.dumps(
            profiles,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


# ---------------------------------------------------------------------
# Sequence generation
# ---------------------------------------------------------------------

def direct_child(parent, tag):
    child = parent.find(tag)

    if child is None:
        child = ET.SubElement(
            parent,
            tag
        )

    return child


def build_flat_queue(
    template,
    target_adu,
    tolerance,
):
    tree = ET.parse(template)
    root = tree.getroot()

    job = root.find(".//Job")

    if job is None:
        raise RuntimeError(
            "No <Job> found in template ESQ."
        )

    direct_child(
        job,
        "Exposure"
    ).text = "1"

    direct_child(
        job,
        "Type"
    ).text = "Flat"

    direct_child(
        job,
        "Count"
    ).text = "1"

    calibration = job.find(
        "Calibration"
    )

    if calibration is None:
        calibration = ET.SubElement(
            job,
            "Calibration"
        )

    flat_duration = calibration.find(
        "FlatDuration"
    )

    if flat_duration is None:
        flat_duration = ET.SubElement(
            calibration,
            "FlatDuration"
        )

    flat_duration.set(
        "dark",
        "false"
    )

    direct_child(
        flat_duration,
        "Type"
    ).text = "ADU"

    direct_child(
        flat_duration,
        "Value"
    ).text = str(
        int(target_adu)
    )

    direct_child(
        flat_duration,
        "Tolerance"
    ).text = str(
        int(tolerance)
    )

    direct_child(
        flat_duration,
        "SkyFlat"
    ).text = "false"

    QUEUE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    tree.write(
        QUEUE_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    return QUEUE_FILE


# ---------------------------------------------------------------------
# Ekos Capture control
# ---------------------------------------------------------------------

def load_queue(path):
    dbus(
        "stop"
    )

    time.sleep(1)

    dbus(
        "clearSequenceQueue"
    )

    time.sleep(1)

    filename = str(path.resolve())

    result = dbus(
        "loadSequenceQueue",
        filename,
        TRAIN,
        "false",
        "FlatCalibration",
    )

    if "true" not in result.lower():
        raise RuntimeError(
            f"Ekos refused queue:\n{result}"
        )


def wait_for_job(timeout=180):
    start = time.time()

    # One generated job.
    job_id = 0

    while True:
        if time.time() - start > timeout:
            raise RuntimeError(
                "Timed out waiting for flat job."
            )

        state = dbus(
            "getJobState",
            job_id
        )

        pending = dbus(
            "getPendingJobCount"
        )

        print(
            f"\rEkos job state: {state:<25}",
            end="",
            flush=True,
        )

        # After the final accepted flat,
        # pending jobs should become zero.
        if "(0,)" in pending:
            print()
            break

        time.sleep(1)

    exposure_text = dbus(
        "getJobExposureDuration",
        job_id
    )

    # Typical gdbus result:
    # (1.027,)
    inside = (
        exposure_text
        .strip()
        .lstrip("(")
        .rstrip(")")
        .rstrip(",")
    )

    return float(inside)


PROBE_FLAT_DIR = (
    fc.PICTURES
    / "FlatCalibration"
    / "Flat"
)


def find_probe_file_after(start_time):
    """
    Return the newest flat-calibration FITS created after
    this probe attempt began.
    """
    if not PROBE_FLAT_DIR.exists():
        raise RuntimeError(
            f"Flat probe directory does not exist: "
            f"{PROBE_FLAT_DIR}"
        )

    candidates = []

    for path in PROBE_FLAT_DIR.glob("*.fits"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue

        # Small allowance for filesystem timestamp granularity.
        if mtime >= start_time - 1.0:
            candidates.append(
                (mtime, path)
            )

    if not candidates:
        raise RuntimeError(
            "Ekos reported the flat job complete, but no "
            "new flat probe FITS file was found."
        )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    return candidates[0][1]


def verify_probe_filter(
    requested_filter,
    probe_file
):
    from astropy.io import fits

    with fits.open(
        probe_file,
        memmap=False
    ) as hdul:
        actual_filter = hdul[0].header.get(
            "FILTER"
        )

    if actual_filter is not None:
        actual_filter = str(
            actual_filter
        ).strip()

    requested = str(
        requested_filter
    ).strip()

    # Systems with no filter device may legitimately omit FILTER.
    if requested == "NoFilter":
        if actual_filter in (
            None,
            "",
            "NoFilter",
        ):
            print(
                "Verified flat optical state: "
                f"{actual_filter or 'NoFilter'}"
            )
            print(
                f"Probe FITS: {probe_file}"
            )
            return

    elif actual_filter == requested:
        print(
            "Verified flat optical state: "
            f"{actual_filter}"
        )
        print(
            f"Probe FITS: {probe_file}"
        )
        return

    raise RuntimeError(
        "FILTER VERIFICATION FAILED\n"
        f"Requested profile: {requested}\n"
        f"Captured FILTER:   {actual_filter!r}\n"
        f"Captured file:     {probe_file}\n\n"
        "The flat probe does not match the requested "
        "light-state. Calibration profile was NOT saved."
    )


def run_flat_probe(
    queue,
    requested_filter
):
    import time

    load_queue(queue)

    probe_started = time.time()

    result = dbus(
        "start",
        TRAIN
    )

    print(
        f"Ekos start: {result}"
    )

    exposure = wait_for_job()

    probe_file = find_probe_file_after(
        probe_started
    )

    verify_probe_filter(
        requested_filter,
        probe_file
    )

    return exposure


# ---------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------

def calibrate(args):
    ceiling = (2 ** args.bits) - 1

    target_adu = round(
        ceiling
        * args.target_fraction
    )

    tolerance = round(
        target_adu
        * args.adu_tolerance_fraction
    )

    profiles = load_profiles()

    if args.filter in profiles:
        brightness = int(
            profiles[
                args.filter
            ]["brightness"]
        )

        print(
            f"Using learned brightness "
            f"{brightness} for "
            f"{args.filter}"
        )

    elif args.start_brightness is not None:
        brightness = (
            args.start_brightness
        )

    else:
        brightness = (
            current_brightness()
        )

        if brightness <= 0:
            brightness = 120

    brightness = quantize_brightness(
        brightness
    )

    queue = build_flat_queue(
        args.template,
        target_adu,
        tolerance,
    )

    print()
    print("FLAT CALIBRATION")
    print("----------------")
    print(
        f"Filter:              {args.filter}"
    )
    print(
        f"Camera bits:         {args.bits}"
    )
    print(
        f"Effective ceiling:   {ceiling}"
    )
    print(
        f"Target fraction:     "
        f"{args.target_fraction:.3f}"
    )
    print(
        f"Target ADU:          {target_adu}"
    )
    print(
        f"ADU tolerance:       ±{tolerance}"
    )
    print(
        f"Preferred exposure:  "
        f"{args.exposure:.3f} s"
    )
    print(
        f"Acceptable window:   "
        f"{args.min_exposure:.3f}"
        " - "
        f"{args.max_exposure:.3f} s"
    )
    print(
        f"Starting brightness: {brightness}"
    )
    print()

    ensure_cover_closed()

    light_on()

    try:
        for attempt in range(
            1,
            args.max_attempts + 1
        ):
            print(
                f"\n===== ATTEMPT "
                f"{attempt} ====="
            )

            brightness = set_brightness(
                brightness
            )

            exposure = run_flat_probe(
                queue,
                args.filter
            )

            print(
                f"Ekos accepted exposure: "
                f"{exposure:.4f} s"
            )

            if (
                args.min_exposure
                <= exposure
                <= args.max_exposure
            ):
                print()
                print(
                    "CALIBRATION SUCCESS"
                )
                print(
                    f"Brightness: {brightness}"
                )
                print(
                    f"Exposure:   "
                    f"{exposure:.4f} s"
                )
                print(
                    f"Target ADU: {target_adu}"
                )

                save_profile(
                    args.filter,
                    brightness,
                    target_adu,
                    args.exposure,
                    exposure,
                    args.bits,
                )

                print(
                    f"Saved: {PROFILE_FILE}"
                )

                return

            # At a fixed target ADU,
            # required exposure is approximately
            # inversely proportional to panel output.
            ratio = (
                exposure
                / args.exposure
            )

            # Damp the correction slightly so a
            # nonlinear LED response does not cause
            # large oscillations.
            proposed = (
                brightness
                * math.pow(
                    ratio,
                    0.75
                )
            )

            new_brightness = (
                quantize_brightness(
                    proposed
                )
            )

            if new_brightness == brightness:
                if exposure > args.max_exposure:
                    new_brightness = (
                        quantize_brightness(
                            brightness
                            + PANEL_STEP
                        )
                    )
                else:
                    new_brightness = (
                        quantize_brightness(
                            brightness
                            - PANEL_STEP
                        )
                    )

            if new_brightness == brightness:
                raise RuntimeError(
                    "Panel reached its brightness "
                    "limit without reaching the "
                    "desired exposure window."
                )

            print(
                f"Adjusting brightness "
                f"{brightness} -> "
                f"{new_brightness}"
            )

            brightness = new_brightness

        raise RuntimeError(
            "Maximum calibration attempts "
            "reached."
        )

    finally:
        light_off()


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def main():
    config = load_config()
    configure_runtime(config)

    parser = argparse.ArgumentParser(
        description=(
            "Calibrate an INDI flat panel so "
            "Ekos ADU flats converge near a "
            "preferred short exposure."
        )
    )

    parser.add_argument(
        "filter",
        help=(
            "Manual filter/profile name, "
            "for example L or L-Extreme."
        ),
    )

    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE,
    )

    parser.add_argument(
        "--bits",
        type=int,
        default=14,
        help=(
            "Effective camera ADC bit depth. "
            "Default: 14."
        ),
    )

    parser.add_argument(
        "--target-fraction",
        type=float,
        default=0.50,
    )

    parser.add_argument(
        "--adu-tolerance-fraction",
        type=float,
        default=0.05,
    )

    parser.add_argument(
        "--exposure",
        type=float,
        default=1.0,
        help="Preferred flat exposure.",
    )

    parser.add_argument(
        "--min-exposure",
        type=float,
        default=1.0,
        help=(
            "Shortest acceptable exposure. Keep at 1.0 s or "
            "longer to avoid rolling-shutter banding."
        ),
    )

    parser.add_argument(
        "--max-exposure",
        type=float,
        default=1.25,
    )

    parser.add_argument(
        "--start-brightness",
        type=int,
    )

    parser.add_argument(
        "--max-attempts",
        type=int,
        default=6,
    )

    args = parser.parse_args()

    calibrate(args)


if __name__ == "__main__":
    try:
        main()

    except RuntimeError as e:
        print()
        print("=" * 64)
        print("CALIBRATION ABORTED")
        print("=" * 64)
        print(str(e).strip())
        print("=" * 64)
        raise SystemExit(2)

    except KeyboardInterrupt:
        print()
        print("Calibration cancelled by user.")
        raise SystemExit(130)
