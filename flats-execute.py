#!/usr/bin/env python3

from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from astropy.io import fits

import flat_common as fc


CONFIG = fc.CONFIG_FILE

PROFILES = fc.PROFILE_FILE

PICTURES = fc.PICTURES

TEMPLATE = fc.TEMPLATE_FILE

PROBE_QUEUE = fc.PROBE_QUEUE

EXEC_QUEUE = fc.EXEC_QUEUE

DARKFLAT_QUEUE = fc.DARKFLAT_QUEUE

CAPTURE_IFACE = "org.kde.kstars.Ekos.Capture"
CAPTURE_PATH = "/KStars/Ekos/Capture"
TRAIN = None

FLAT_DEVICE = None

KSTARS_DEST = None


def run(cmd, check=True):
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
    )

    if check and result.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            + " ".join(cmd)
            + "\n\n"
            + result.stderr.strip()
        )

    return result.stdout.strip()


def configure_runtime(config):
    """
    Resolve site-specific Ekos/INDI names from configuration
    and ensure runtime directories exist.
    """
    global TRAIN
    global FLAT_DEVICE

    fc.QUEUE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        TRAIN = str(
            config["ekos"]["optical_train"]
        ).strip()
    except (
        KeyError,
        TypeError,
    ):
        TRAIN = ""

    try:
        FLAT_DEVICE = str(
            config["panel"]["device"]
        ).strip()
    except (
        KeyError,
        TypeError,
    ):
        FLAT_DEVICE = ""

    if not TRAIN:
        raise RuntimeError(
            "CONFIGURATION ERROR\n"
            "Missing ekos.optical_train in flat-config.json."
        )

    if not FLAT_DEVICE:
        raise RuntimeError(
            "CONFIGURATION ERROR\n"
            "Missing panel.device in flat-config.json."
        )


def load_json(path):
    return json.loads(path.read_text())


def observing_session_id(date_obs):
    dt = datetime.fromisoformat(
        str(date_obs).replace("Z", "+00:00")
    )
    return (
        dt - timedelta(hours=12)
    ).strftime("%Y%m%d")


def header_value(hdr, key, default=None):
    value = hdr.get(key, default)

    if isinstance(value, str):
        value = value.strip()

    return value


def profile_from_header(hdr):
    filt = header_value(hdr, "FILTER")

    if filt in (None, "", "--", "None"):
        return "NoFilter"

    return str(filt)


def discover_kstars_capture_dest():
    listing = run([
        "busctl",
        "--user",
        "list",
    ])

    candidates = []

    for line in listing.splitlines():
        parts = line.split()

        if parts and parts[0].startswith(":"):
            candidates.append(parts[0])

    candidates.append("org.kde.kstars")

    for dest in candidates:
        probe = subprocess.run(
            [
                "gdbus",
                "introspect",
                "--session",
                "--dest",
                dest,
                "--object-path",
                CAPTURE_PATH,
            ],
            text=True,
            capture_output=True,
        )

        if (
            probe.returncode == 0
            and CAPTURE_IFACE in probe.stdout
        ):
            return dest

    raise RuntimeError(
        "Could not find an active Ekos Capture instance."
    )


def get_kstars_dest():
    global KSTARS_DEST

    if KSTARS_DEST is None:
        KSTARS_DEST = (
            discover_kstars_capture_dest()
        )

        print(
            "Using KStars D-Bus destination:",
            KSTARS_DEST,
        )

    return KSTARS_DEST


def dbus(method, *args):
    cmd = [
        "gdbus",
        "call",
        "--session",
        "--dest",
        get_kstars_dest(),
        "--object-path",
        CAPTURE_PATH,
        "--method",
        f"{CAPTURE_IFACE}.{method}",
    ]

    cmd.extend(str(x) for x in args)

    return run(cmd)


def indi_get(pattern):
    return run(
        ["indi_getprop", pattern],
        check=False,
    )


def indi_set(expression):
    return run(
        ["indi_setprop", expression]
    )


def get_kstars_rotator_threshold():
    """
    Return the effective KStars rotator tolerance in arcminutes.

    If the user has overridden AstrometryRotatorThreshold in
    ~/.config/kstarsrc, use that value.

    Otherwise use KStars' compiled default of 30 arcminutes.
    """
    kstarsrc = (
        Path.home()
        / ".config"
        / "kstarsrc"
    )

    default_arcmin = 30.0

    if kstarsrc.exists():
        try:
            for raw_line in kstarsrc.read_text().splitlines():
                line = raw_line.strip()

                if not line.startswith(
                    "AstrometryRotatorThreshold="
                ):
                    continue

                _, value = line.split(
                    "=",
                    1,
                )

                arcmin = float(
                    value.strip()
                )

                return (
                    arcmin,
                    "KStars user setting"
                )

        except (
            OSError,
            ValueError,
        ):
            pass

    return (
        default_arcmin,
        "KStars default"
    )


def get_active_rotator(camera):
    text = indi_get(
        f"{camera}.ACTIVE_DEVICES.ACTIVE_ROTATOR"
    )

    prefix = (
        f"{camera}.ACTIVE_DEVICES.ACTIVE_ROTATOR="
    )

    for line in text.splitlines():
        if line.startswith(prefix):
            value = line.split("=", 1)[1].strip()
            return value or None

    return None


def get_rotator_state(device):
    text = indi_get(
        f"{device}.ABS_ROTATOR_POSITION._STATE"
    )

    prefix = (
        f"{device}.ABS_ROTATOR_POSITION._STATE="
    )

    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip()

    return None


def get_rotator_position(device):
    text = indi_get(
        f"{device}.ABS_ROTATOR_POSITION."
        "ROTATOR_ABSOLUTE_POSITION"
    )

    prefix = (
        f"{device}.ABS_ROTATOR_POSITION."
        "ROTATOR_ABSOLUTE_POSITION="
    )

    for line in text.splitlines():
        if line.startswith(prefix):
            return float(
                line.split("=", 1)[1].strip()
            )

    return None


def get_rotator_angle(device):
    text = indi_get(
        f"{device}.ABS_ROTATOR_ANGLE.ANGLE"
    )

    prefix = (
        f"{device}.ABS_ROTATOR_ANGLE.ANGLE="
    )

    for line in text.splitlines():
        if line.startswith(prefix):
            return float(
                line.split("=", 1)[1].strip()
            )

    return None


def circular_angle_difference(a, b):
    delta = abs(float(a) - float(b)) % 360.0
    return min(
        delta,
        360.0 - delta
    )


def move_rotator_to_light_state(
    camera,
    rotsteps,
    rotatang,
    timeout=180,
):
    if rotsteps is None:
        print(
            "ROTSTEPS unavailable; rotator move skipped."
        )
        return

    device = get_active_rotator(camera)

    if not device:
        raise RuntimeError(
            "ROTATOR NOT FOUND\n"
            f"Camera: {camera}"
        )

    current_steps = get_rotator_position(
        device
    )

    current_angle = get_rotator_angle(
        device
    )

    print()
    print("=" * 72)
    print("ROTATOR POSITIONING")
    print("=" * 72)
    print(
        f"Rotator:        {device}"
    )
    print(
        f"Current steps:  {current_steps}"
    )
    print(
        f"Target steps:   {rotsteps}"
    )

    if current_angle is not None:
        print(
            f"Current angle:  "
            f"{current_angle:.6f} deg"
        )

    if rotatang is not None:
        print(
            f"Target angle:   "
            f"{float(rotatang):.6f} deg"
        )

    indi_set(
        f"{device}.ABS_ROTATOR_POSITION."
        f"ROTATOR_ABSOLUTE_POSITION={rotsteps}"
    )

    started = time.time()
    state = None

    while time.time() - started <= timeout:
        state = get_rotator_state(
            device
        )

        current_steps = get_rotator_position(
            device
        )

        if state == "Alert":
            raise RuntimeError(
                "ROTATOR MOVE FAILED\n"
                f"Device: {device}\n"
                f"Target steps: {rotsteps}"
            )

        if state != "Busy":
            break

        time.sleep(0.5)

    else:
        raise RuntimeError(
            "ROTATOR MOVE TIMED OUT\n"
            f"Device: {device}\n"
            f"Target steps: {rotsteps}\n"
            f"Last steps: {current_steps}"
        )

    current_angle = get_rotator_angle(
        device
    )

    print(
        f"Rotator state:  {state}"
    )
    print(
        f"Final steps:    {current_steps}"
    )

    if current_angle is not None:
        print(
            f"Final angle:    "
            f"{current_angle:.6f} deg"
        )

    if rotatang is None:
        print(
            "ROTATANG unavailable; mechanical move "
            "completed, angular verification skipped."
        )
        print("=" * 72)
        return

    if current_angle is None:
        raise RuntimeError(
            "ROTATOR ANGLE UNAVAILABLE\n"
            f"Device: {device}\n"
            "Cannot verify ROTATANG."
        )

    threshold_arcmin, source = (
        get_kstars_rotator_threshold()
    )

    threshold_deg = (
        threshold_arcmin / 60.0
    )

    diff = circular_angle_difference(
        current_angle,
        rotatang,
    )

    print(
        f"Angle error:    "
        f"{diff:.6f} deg"
    )
    print(
        f"Tolerance:      "
        f"{threshold_arcmin:g} arcmin "
        f"({threshold_deg:.6f} deg)"
    )
    print(
        f"Tolerance source: {source}"
    )

    if diff > threshold_deg:
        raise RuntimeError(
            "ROTATOR VERIFICATION FAILED\n"
            f"Target ROTSTEPS: {rotsteps}\n"
            f"Target ROTATANG: {rotatang}\n"
            f"Actual angle:    {current_angle}\n"
            f"Angular error:   {diff:.6f} deg\n"
            f"Allowed error:   {threshold_deg:.6f} deg"
        )

    print(
        "Rotator position verified."
    )
    print("=" * 72)


def ensure_cover_closed(timeout=30):
    indi_set(
        f"{FLAT_DEVICE}.CAP_PARK.PARK=On"
    )

    start = time.time()

    while time.time() - start <= timeout:
        snapshot = indi_get(
            f"{FLAT_DEVICE}.*.*"
        )

        if (
            f"{FLAT_DEVICE}.CAP_PARK.PARK=On"
            in snapshot
            and
            f"{FLAT_DEVICE}.Status.Cover=Closed"
            in snapshot
            and
            f"{FLAT_DEVICE}.Status.Motor=Stopped"
            in snapshot
        ):
            print(
                "Flip-Flat cover: confirmed closed"
            )
            return

        time.sleep(1)

    raise RuntimeError(
        "Timed out waiting for Flip-Flat "
        "cover to close."
    )


def set_brightness(value):
    indi_set(
        f"{FLAT_DEVICE}."
        "FLAT_LIGHT_INTENSITY."
        f"FLAT_LIGHT_INTENSITY_VALUE={value}"
    )


def light_on():
    indi_set(
        f"{FLAT_DEVICE}."
        "FLAT_LIGHT_CONTROL."
        "FLAT_LIGHT_ON=On"
    )


def light_off():
    indi_set(
        f"{FLAT_DEVICE}."
        "FLAT_LIGHT_CONTROL."
        "FLAT_LIGHT_OFF=On"
    )


def find_session_groups(session):
    groups = defaultdict(list)

    for path in PICTURES.rglob("*.fits"):
        try:
            with fits.open(
                path,
                memmap=False
            ) as hdul:
                hdr = hdul[0].header
        except Exception:
            continue

        if (
            "light"
            not in str(
                hdr.get("IMAGETYP", "")
            ).lower()
        ):
            continue

        file_session = hdr.get("SESSION")

        if not file_session:
            date_obs = hdr.get("DATE-OBS")

            if not date_obs:
                continue

            file_session = (
                observing_session_id(
                    date_obs
                )
            )

        if str(file_session) != session:
            continue

        profile = profile_from_header(hdr)

        rotsteps = hdr.get("ROTSTEPS")
        rotatang = hdr.get("ROTATANG")

        key = (
            profile,
            hdr.get("INSTRUME"),
            hdr.get("GAIN"),
            hdr.get("OFFSET"),
            hdr.get("XBINNING", 1),
            hdr.get("YBINNING", 1),
            rotsteps,
            rotatang,
        )

        groups[key].append(path)

    return groups


def fmt_number(value):
    n = float(value)

    if n.is_integer():
        return str(int(n))

    return str(n)


def get_manual_filter_state():
    """
    Return (slot, filter_name) from the INDI Manual Filter driver.

    Returns (None, None) if that driver is not available.
    """
    slot_text = indi_get(
        "Manual Filter.FILTER_SLOT.*"
    )

    slot = None

    for line in slot_text.splitlines():
        prefix = (
            "Manual Filter.FILTER_SLOT."
            "FILTER_SLOT_VALUE="
        )

        if line.startswith(prefix):
            slot = line.split("=", 1)[1].strip()
            break

    if slot is None:
        return None, None

    names_text = indi_get(
        "Manual Filter.FILTER_NAME.*"
    )

    wanted = (
        "Manual Filter.FILTER_NAME."
        f"FILTER_SLOT_NAME_{slot}="
    )

    for line in names_text.splitlines():
        if line.startswith(wanted):
            name = line.split("=", 1)[1].strip()
            return slot, name

    return slot, None


def wait_for_manual_filter(
    profile,
    timeout=600
):
    """
    For the INDI Manual Filter driver, do NOT change the slot
    programmatically.

    Instead, wait for the user to select/confirm the desired
    filter through Ekos/INDI's normal manual-filter workflow.

    If no Manual Filter driver is detected, return immediately.
    The captured FITS FILTER header remains the final safeguard.
    """
    slot, current = get_manual_filter_state()

    if slot is None:
        print(
            "Manual Filter driver not detected; "
            "continuing with FITS verification."
        )
        return

    print(
        f"Current manual filter: {current} "
        f"(slot {slot})"
    )

    if current == profile:
        print(
            f"Required filter already confirmed: "
            f"{profile}"
        )
        return

    print()
    print("=" * 72)
    print("MANUAL FILTER CHANGE REQUIRED")
    print("=" * 72)
    print(f"Required filter: {profile}")
    print(f"Current filter:  {current}")
    print()
    print(
        f"Select {profile} in Ekos and confirm the "
        "physical filter change."
    )
    print(
        "Waiting for the Manual Filter driver "
        "to report the requested state..."
    )
    print("=" * 72)

    started = time.time()
    last_reported = current

    while time.time() - started <= timeout:
        slot, current = get_manual_filter_state()

        if current == profile:
            print()
            print(
                f"Filter confirmed: {profile} "
                f"(slot {slot})"
            )
            return

        if current != last_reported:
            print(
                f"Manual filter now reports: "
                f"{current}"
            )
            last_reported = current

        time.sleep(1)

    raise RuntimeError(
        "MANUAL FILTER CHANGE TIMED OUT\n"
        f"Required filter: {profile}\n"
        f"Last reported:   {current!r}\n\n"
        "No flats were captured."
    )


def set_manual_flat_duration(
    job,
    exposure,
    dark=False,
):
    """
    Configure a Flat/DarkFlat job to use the exact frame
    exposure instead of Ekos ADU auto-adjustment.
    """
    exposure_node = job.find("Exposure")

    if exposure_node is None:
        raise RuntimeError(
            "Queue Job contains no Exposure element."
        )

    exposure_node.text = f"{float(exposure):.7f}"

    calibration = job.find("Calibration")

    if calibration is None:
        raise RuntimeError(
            "Queue Job contains no Calibration element."
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
        "true" if dark else "false"
    )

    # Remove the old ADU-mode children.
    for child in list(flat_duration):
        flat_duration.remove(child)

    type_node = ET.SubElement(
        flat_duration,
        "Type"
    )

    type_node.text = "Manual"


def build_group_root(
    config,
    session,
    profile,
    rotsteps,
):
    group_root = (
        Path(config["output"]["root"])
        / session
        / profile
    )

    if rotsteps is not None:
        group_root = (
            group_root
            / f"ROT_{fmt_number(rotsteps)}"
        )

    return group_root


def apply_common_job_settings(
    job,
    gain,
    offset,
    xbin,
    ybin,
):
    binning = job.find("Binning")
    binning.find("X").text = fmt_number(xbin)
    binning.find("Y").text = fmt_number(ybin)

    props = job.find("Properties")

    for vector in props.findall("PropertyVector"):
        name = vector.get("name")

        if name == "CCD_CONTROLS":
            for element in vector.findall("OneElement"):
                if element.get("name") == "Gain":
                    element.text = fmt_number(gain)

        elif name == "CCD_OFFSET":
            for element in vector.findall("OneElement"):
                if element.get("name") == "OFFSET":
                    element.text = fmt_number(offset)


def build_probe_queue(
    session,
    profile,
    gain,
    offset,
    xbin,
    ybin,
    rotsteps,
    profile_data,
    config,
):
    tree = ET.parse(TEMPLATE)
    root = tree.getroot()
    job = root.find("Job")

    if job is None:
        raise RuntimeError("Template contains no Job element.")

    job.find("Count").text = "1"

    target_name = job.find("TargetName")
    if target_name is not None:
        target_name.text = "FlatCalibration"

    apply_common_job_settings(
        job,
        gain,
        offset,
        xbin,
        ybin,
    )

    group_root = build_group_root(
        config,
        session,
        profile,
        rotsteps,
    )

    probe_dir = group_root / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)

    job.find("FITSDirectory").text = str(probe_dir)

    rotation_text = ""
    if rotsteps is not None:
        rotation_text = f"_R{fmt_number(rotsteps)}"

    prefix = (
        f"{session}_{profile}"
        f"_G{fmt_number(gain)}"
        f"_O{fmt_number(offset)}"
        f"_B{fmt_number(xbin)}x{fmt_number(ybin)}"
        f"{rotation_text}_Probe"
    )

    job.find("PlaceholderFormat").text = prefix

    flat_duration = job.find("./Calibration/FlatDuration")
    flat_duration.set("dark", "false")

    type_node = flat_duration.find("Type")
    type_node.text = "ADU"

    value_node = flat_duration.find("Value")
    value_node.text = str(profile_data["target_adu"])

    tolerance = round(
        profile_data["target_adu"]
        * config["flat_target"]["tolerance_fraction"]
    )

    flat_duration.find("Tolerance").text = str(tolerance)

    tree.write(
        PROBE_QUEUE,
        encoding="utf-8",
        xml_declaration=True,
    )

    return probe_dir


def build_production_flat_queue(
    session,
    profile,
    gain,
    offset,
    xbin,
    ybin,
    rotsteps,
    exposure,
    config,
):
    tree = ET.parse(TEMPLATE)
    root = tree.getroot()
    job = root.find("Job")

    if job is None:
        raise RuntimeError("Template contains no Job element.")

    job.find("Count").text = str(
        config["frames"]["flats_per_group"]
    )

    target_name = job.find("TargetName")
    if target_name is not None:
        target_name.text = "FlatCalibration"

    apply_common_job_settings(
        job,
        gain,
        offset,
        xbin,
        ybin,
    )

    group_root = build_group_root(
        config,
        session,
        profile,
        rotsteps,
    )

    flat_dir = group_root / "flats"
    flat_dir.mkdir(parents=True, exist_ok=True)

    job.find("FITSDirectory").text = str(flat_dir)

    rotation_text = ""
    if rotsteps is not None:
        rotation_text = f"_R{fmt_number(rotsteps)}"

    prefix = (
        f"{session}_{profile}"
        f"_G{fmt_number(gain)}"
        f"_O{fmt_number(offset)}"
        f"_B{fmt_number(xbin)}x{fmt_number(ybin)}"
        f"{rotation_text}_Flat"
    )

    job.find("PlaceholderFormat").text = prefix

    set_manual_flat_duration(
        job,
        exposure,
        dark=False,
    )

    tree.write(
        EXEC_QUEUE,
        encoding="utf-8",
        xml_declaration=True,
    )

    return flat_dir


def build_darkflat_queue(
    session,
    profile,
    gain,
    offset,
    xbin,
    ybin,
    rotsteps,
    exposure,
    config,
):
    tree = ET.parse(TEMPLATE)
    root = tree.getroot()
    job = root.find("Job")

    if job is None:
        raise RuntimeError("Template contains no Job element.")

    type_node = job.find("Type")
    type_node.text = "Dark"

    job.find("Count").text = str(
        config["frames"]["flats_per_group"]
    )

    target_name = job.find("TargetName")
    if target_name is not None:
        target_name.text = "FlatCalibration"

    apply_common_job_settings(
        job,
        gain,
        offset,
        xbin,
        ybin,
    )

    group_root = build_group_root(
        config,
        session,
        profile,
        rotsteps,
    )

    dark_dir = group_root / "darkflats"
    dark_dir.mkdir(parents=True, exist_ok=True)

    job.find("FITSDirectory").text = str(dark_dir)

    rotation_text = ""
    if rotsteps is not None:
        rotation_text = f"_R{fmt_number(rotsteps)}"

    prefix = (
        f"{session}_{profile}"
        f"_G{fmt_number(gain)}"
        f"_O{fmt_number(offset)}"
        f"_B{fmt_number(xbin)}x{fmt_number(ybin)}"
        f"{rotation_text}_DarkFlat"
    )

    job.find("PlaceholderFormat").text = prefix

    set_manual_flat_duration(
        job,
        exposure,
        dark=True,
    )

    tree.write(
        DARKFLAT_QUEUE,
        encoding="utf-8",
        xml_declaration=True,
    )

    return dark_dir


def load_queue(queue_path, queue_name):
    dbus("stop")

    time.sleep(1)

    dbus(
        "clearSequenceQueue"
    )

    result = dbus(
        "loadSequenceQueue",
        str(queue_path.resolve()),
        TRAIN,
        "false",
        queue_name,
    )

    if "true" not in result.lower():
        raise RuntimeError(
            f"Ekos refused queue {queue_path}:\n"
            + result
        )


def wait_for_completion(timeout=1800):
    start = time.time()

    while time.time() - start < timeout:
        result = dbus(
            "getSequenceQueueStatus"
        )

        text = result.lower()

        if "complete" in text:
            return

        if (
            "aborted" in text
            or "error" in text
        ):
            raise RuntimeError(
                "Ekos reported capture failure:\n"
                + result
            )

        time.sleep(2)

    raise RuntimeError(
        "Timed out waiting for flat capture."
    )


def read_probe_result(
    probe_dir,
    started,
    profile,
    session,
):
    """
    Locate the probe FITS created by this run, verify its
    optical state, stamp SESSION, and return (path, exposure).
    """
    candidates = []

    for path in probe_dir.glob("*.fits"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue

        if mtime >= started - 1.0:
            candidates.append(
                (mtime, path)
            )

    if not candidates:
        raise RuntimeError(
            "Ekos reported the ADU probe complete, but "
            "no new probe FITS file was found."
        )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    probe_file = candidates[0][1]

    with fits.open(
        probe_file,
        mode="update",
        memmap=False
    ) as hdul:
        hdr = hdul[0].header

        actual_filter = hdr.get("FILTER")
        exposure = hdr.get("EXPTIME")

        if actual_filter is not None:
            actual_filter = str(
                actual_filter
            ).strip()

        valid_filter = (
            actual_filter == profile
            or (
                profile == "NoFilter"
                and actual_filter in (
                    None,
                    "",
                    "NoFilter",
                )
            )
        )

        if not valid_filter:
            raise RuntimeError(
                "PROBE FILTER VERIFICATION FAILED\n"
                f"Requested profile: {profile}\n"
                f"Captured FILTER:   {actual_filter!r}\n"
                f"Probe file:        {probe_file}"
            )

        if exposure is None:
            raise RuntimeError(
                "Probe FITS contains no EXPTIME value."
            )

        exposure = float(exposure)

        hdr["SESSION"] = (
            session,
            "Observing-night session identifier"
        )

        hdul.flush()

    print(
        f"Probe verified: {probe_file.name}"
    )
    print(
        f"Probe FILTER:   {actual_filter or 'NoFilter'}"
    )
    print(
        f"Probe exposure: {exposure:.7f} s"
    )

    return probe_file, exposure


def write_run_manifest(
    group_root,
    session,
    profile,
    camera,
    gain,
    offset,
    xbin,
    ybin,
    rotsteps,
    brightness,
    exposure,
    flat_files,
    darkflat_files,
):
    manifest = {
        "session": session,
        "profile": profile,
        "camera": camera,
        "gain": gain,
        "offset": offset,
        "binning": {
            "x": xbin,
            "y": ybin,
        },
        "rotsteps": rotsteps,
        "brightness": brightness,
        "exposure": exposure,
        "status": "complete",
        "flats": [
            str(path)
            for path in flat_files
        ],
        "darkflats": [
            str(path)
            for path in darkflat_files
        ],
    }

    manifest_path = (
        group_root
        / "manifest.json"
    )

    manifest_path.write_text(
        json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    print(
        f"Manifest written: {manifest_path}"
    )


def verify_new_darkflats(
    dark_dir,
    count,
    started,
    session,
    expected_exposure,
):
    candidates = []

    for path in dark_dir.glob("*.fits"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue

        if mtime >= started - 1.0:
            candidates.append(path)

    if len(candidates) < count:
        raise RuntimeError(
            f"Expected {count} new dark-flats but found "
            f"{len(candidates)} in {dark_dir}"
        )

    bad = []

    for path in candidates:
        with fits.open(
            path,
            mode="update",
            memmap=False
        ) as hdul:
            hdr = hdul[0].header

            imagetype = str(
                hdr.get("IMAGETYP", "")
            )

            exposure = hdr.get("EXPTIME")

            if exposure is None:
                bad.append(
                    (
                        path.name,
                        "missing EXPTIME"
                    )
                )
                continue

            exposure = float(exposure)

            tolerance = max(
                0.000001,
                expected_exposure * 0.00001
            )

            if "dark" not in imagetype.lower():
                bad.append(
                    (
                        path.name,
                        f"IMAGETYP={imagetype!r}"
                    )
                )
                continue

            if abs(
                exposure
                - expected_exposure
            ) > tolerance:
                bad.append(
                    (
                        path.name,
                        (
                            f"EXPTIME={exposure:.7f}; "
                            f"expected "
                            f"{expected_exposure:.7f}"
                        )
                    )
                )
                continue

            hdr["SESSION"] = (
                session,
                "Observing-night session identifier"
            )

            hdul.flush()

    if bad:
        raise RuntimeError(
            "One or more dark-flats failed verification:\n"
            + "\n".join(
                f"{name}: {reason}"
                for name, reason in bad
            )
        )

    print(
        f"Verified {len(candidates)} "
        "matching dark-flats."
    )

    return sorted(candidates)


def verify_new_flats(
    flat_dir,
    profile,
    count,
    started,
    session,
):
    candidates = []

    for path in flat_dir.glob("*.fits"):
        if path.stat().st_mtime >= started - 1:
            candidates.append(path)

    if len(candidates) < count:
        raise RuntimeError(
            f"Expected {count} new flats but found "
            f"{len(candidates)} in {flat_dir}"
        )

    bad = []

    for path in candidates:
        with fits.open(
            path,
            memmap=False
        ) as hdul:
            actual = hdul[0].header.get(
                "FILTER"
            )

        if actual is not None:
            actual = str(actual).strip()

        valid = (
            actual == profile
            or (
                profile == "NoFilter"
                and actual in (
                    None,
                    "",
                    "NoFilter",
                )
            )
        )

        if not valid:
            bad.append(
                (path.name, actual)
            )
            continue

        # Stamp the observing-night session directly
        # into executor-generated calibration frames.
        with fits.open(
            path,
            mode="update",
            memmap=False
        ) as hdul:
            hdul[0].header["SESSION"] = (
                session,
                "Observing-night session identifier"
            )
            hdul.flush()

    if bad:
        raise RuntimeError(
            "One or more captured flats have "
            "the wrong FILTER value:\n"
            + "\n".join(
                f"{name}: {value!r}"
                for name, value in bad
            )
        )

    print(
        f"Verified {len(candidates)} "
        f"new {profile} flats."
    )

    return sorted(candidates)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--session",
        required=True,
    )

    parser.add_argument(
        "--profile",
        required=True,
    )

    parser.add_argument(
        "--rotsteps",
        type=float,
        default=None,
        help=(
            "Select a specific ROTSTEPS calibration group. "
            "Omit for groups without ROTSTEPS metadata."
        ),
    )

    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation.",
    )

    parser.add_argument(
        "--rotator-only",
        action="store_true",
        help=(
            "Move and verify the rotator for the selected "
            "light group, then exit without capturing."
        ),
    )

    args = parser.parse_args()

    config = load_json(CONFIG)

    configure_runtime(
        config
    )

    profiles = load_json(PROFILES)

    if args.profile not in profiles:
        raise RuntimeError(
            f"No calibrated profile exists for "
            f"{args.profile!r}."
        )

    groups = fc.find_session_groups(
        PICTURES,
        args.session,
        use_rotsteps=config[
            "capabilities"
        ].get(
            "use_rotsteps_if_present",
            True,
        ),
    )

    profile_matches = [
        (key, paths)
        for key, paths in groups.items()
        if key[0] == args.profile
    ]

    if args.rotsteps is None:
        matches = [
            (key, paths)
            for key, paths in profile_matches
            if key[6] is None
        ]
    else:
        matches = [
            (key, paths)
            for key, paths in profile_matches
            if (
                key[6] is not None
                and abs(
                    float(key[6])
                    - float(args.rotsteps)
                ) < 1e-6
            )
        ]

    if not matches:
        rotation_text = (
            str(args.rotsteps)
            if args.rotsteps is not None
            else "none"
        )

        raise RuntimeError(
            f"No lights found for session "
            f"{args.session}, profile "
            f"{args.profile}, ROTSTEPS="
            f"{rotation_text}."
        )

    if len(matches) != 1:
        raise RuntimeError(
            f"Profile {args.profile} resolves "
            f"to {len(matches)} groups. "
            "Single-group executor refuses "
            "to guess."
        )

    key, lights = matches[0]

    (
        profile,
        camera,
        gain,
        offset,
        xbin,
        ybin,
        rotsteps,
    ) = key

    rotatang = fc.group_rotatang(
        lights
    )

    profile_data = profiles[profile]

    print()
    print("=" * 72)
    print("SUPERVISED FLAT EXECUTION")
    print("=" * 72)
    print(f"Session:       {args.session}")
    print(f"Profile:       {profile}")
    print(f"Camera:        {camera}")
    print(f"Gain:          {gain}")
    print(f"Offset:        {offset}")
    print(f"Binning:       {xbin}x{ybin}")
    print(
        "ROTSTEPS:      "
        + (
            str(rotsteps)
            if rotsteps is not None
            else "ignored"
        )
    )
    print(
        "ROTATANG:      "
        + (
            str(rotatang)
            if rotatang is not None
            else "unavailable"
        )
    )
    print(f"Matching lights: {len(lights)}")
    print(
        f"Brightness:    "
        f"{profile_data['brightness']}"
    )
    print(
        f"Target ADU:    "
        f"{profile_data['target_adu']}"
    )
    print(
        f"Flat count:    "
        f"{config['frames']['flats_per_group']}"
    )
    print()

    probe_dir = build_probe_queue(
        session=args.session,
        profile=profile,
        gain=gain,
        offset=offset,
        xbin=xbin,
        ybin=ybin,
        rotsteps=rotsteps,
        profile_data=profile_data,
        config=config,
    )

    print(
        f"Probe queue:     {PROBE_QUEUE}"
    )
    print(
        f"Probe output:    {probe_dir}"
    )
    print()

    if args.rotator_only:
        move_rotator_to_light_state(
            camera,
            rotsteps,
            rotatang,
        )

        print()
        print("=" * 72)
        print("ROTATOR TEST COMPLETE")
        print("=" * 72)
        print("No flats were captured.")
        print("=" * 72)
        return

    if not args.yes:
        answer = input(
            "Run this calibration group? "
            "Type YES to continue: "
        )

        if answer != "YES":
            print("Cancelled.")
            return

    # For a manual-filter system this pauses until the
    # user changes/confirms the requested filter in INDI.
    wait_for_manual_filter(
        profile
    )

    move_rotator_to_light_state(
        camera,
        rotsteps,
        rotatang,
    )

    ensure_cover_closed()

    set_brightness(
        int(profile_data["brightness"])
    )

    # ------------------------------------------------------------
    # Phase 1: one ADU-controlled probe
    # ------------------------------------------------------------

    print()
    print("=" * 72)
    print("PHASE 1: ADU PROBE")
    print("=" * 72)

    light_on()

    try:
        load_queue(
            PROBE_QUEUE,
            "FlatProbe",
        )

        probe_started = time.time()

        result = dbus(
            "start",
            TRAIN
        )

        print(
            f"Ekos start: {result}"
        )

        wait_for_completion()

        probe_file, exposure = (
            read_probe_result(
                probe_dir,
                probe_started,
                profile,
                args.session,
            )
        )

    finally:
        # Leave the panel on only long enough to transition
        # into the production-flat phase below.
        pass

    # ------------------------------------------------------------
    # Phase 2: production flats at FIXED probe exposure
    # ------------------------------------------------------------

    flat_dir = build_production_flat_queue(
        session=args.session,
        profile=profile,
        gain=gain,
        offset=offset,
        xbin=xbin,
        ybin=ybin,
        rotsteps=rotsteps,
        exposure=exposure,
        config=config,
    )

    print()
    print("=" * 72)
    print("PHASE 2: FIXED-EXPOSURE FLATS")
    print("=" * 72)
    print(
        f"Exposure: {exposure:.7f} s"
    )
    print(
        f"Output:   {flat_dir}"
    )

    try:
        load_queue(
            EXEC_QUEUE,
            "FlatProduction",
        )

        flat_started = time.time()

        result = dbus(
            "start",
            TRAIN
        )

        print(
            f"Ekos start: {result}"
        )

        wait_for_completion()

        flat_files = verify_new_flats(
            flat_dir,
            profile,
            config["frames"][
                "flats_per_group"
            ],
            flat_started,
            args.session,
        )

    finally:
        light_off()

    # ------------------------------------------------------------
    # Phase 3: matching dark-flats
    # ------------------------------------------------------------

    if config[
        "frames"
    ].get(
        "darkflats_enabled",
        True
    ):
        dark_dir = build_darkflat_queue(
            session=args.session,
            profile=profile,
            gain=gain,
            offset=offset,
            xbin=xbin,
            ybin=ybin,
            rotsteps=rotsteps,
            exposure=exposure,
            config=config,
        )

        print()
        print("=" * 72)
        print("PHASE 3: MATCHING DARK-FLATS")
        print("=" * 72)
        print(
            f"Exposure: {exposure:.7f} s"
        )
        print(
            f"Output:   {dark_dir}"
        )

        # Panel must remain off for dark-flats.
        light_off()

        load_queue(
            DARKFLAT_QUEUE,
            "DarkFlatProduction",
        )

        dark_started = time.time()

        result = dbus(
            "start",
            TRAIN
        )

        print(
            f"Ekos start: {result}"
        )

        wait_for_completion()

        darkflat_files = verify_new_darkflats(
            dark_dir,
            config["frames"][
                "flats_per_group"
            ],
            dark_started,
            args.session,
            exposure,
        )

    group_root = build_group_root(
        config,
        args.session,
        profile,
        rotsteps,
    )

    write_run_manifest(
        group_root=group_root,
        session=args.session,
        profile=profile,
        camera=camera,
        gain=gain,
        offset=offset,
        xbin=xbin,
        ybin=ybin,
        rotsteps=rotsteps,
        brightness=int(
            profile_data["brightness"]
        ),
        exposure=exposure,
        flat_files=flat_files,
        darkflat_files=darkflat_files,
    )

    print()
    print("=" * 72)
    print("CALIBRATION GROUP COMPLETE")
    print("=" * 72)
    print(
        f"Session:  {args.session}"
    )
    print(
        f"Profile:  {profile}"
    )
    print(
        f"Exposure: {exposure:.7f} s"
    )
    print(
        f"Flats:    {flat_dir}"
    )

    if config[
        "frames"
    ].get(
        "darkflats_enabled",
        True
    ):
        print(
            f"Darkflats: {dark_dir}"
        )

    print("=" * 72)


if __name__ == "__main__":
    try:
        main()

    except RuntimeError as e:
        print()
        print("=" * 72)
        print("FLAT EXECUTION ABORTED")
        print("=" * 72)
        print(str(e).strip())
        print("=" * 72)
        raise SystemExit(2)

    except KeyboardInterrupt:
        print()
        print("Cancelled by user.")
        raise SystemExit(130)
