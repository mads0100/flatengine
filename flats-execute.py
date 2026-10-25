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
    exposure,
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

    # Fixed exposure: FlatEngine tunes the panel brightness itself
    # instead of letting Ekos float the exposure to hit an ADU.
    set_manual_flat_duration(
        job,
        exposure,
        dark=False,
    )

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
    count=None,
):
    tree = ET.parse(TEMPLATE)
    root = tree.getroot()
    job = root.find("Job")

    if job is None:
        raise RuntimeError("Template contains no Job element.")

    if count is None:
        count = config["frames"]["flats_per_group"]

    job.find("Count").text = str(int(count))

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
    dark_dir=None,
    count=None,
):
    tree = ET.parse(TEMPLATE)
    root = tree.getroot()
    job = root.find("Job")

    if job is None:
        raise RuntimeError("Template contains no Job element.")

    type_node = job.find("Type")
    type_node.text = "Dark"

    if count is None:
        count = config["frames"]["flats_per_group"]

    job.find("Count").text = str(int(count))

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

    library = dark_dir is not None

    if dark_dir is None:
        dark_dir = group_root / "darkflats"

    dark_dir = Path(dark_dir)
    dark_dir.mkdir(parents=True, exist_ok=True)

    job.find("FITSDirectory").text = str(dark_dir)

    if library:
        # Library dark-flats are shared across sessions and
        # filters, so the name carries only the camera state.
        prefix = (
            f"G{fmt_number(gain)}"
            f"_O{fmt_number(offset)}"
            f"_B{fmt_number(xbin)}x{fmt_number(ybin)}"
            f"_E{fmt_number(exposure)}_DarkFlat"
        )
    else:
        rotation_text = ""
        if rotsteps is not None:
            rotation_text = f"_R{fmt_number(rotsteps)}"

        prefix = (
            f"{session}_{profile}"
            f"_G{fmt_number(gain)}"
            f"_O{fmt_number(offset)}"
            f"_B{fmt_number(xbin)}x{fmt_number(ybin)}"
            f"{rotation_text}_E{fmt_number(exposure)}_DarkFlat"
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
    flat_levels=None,
    target_adu=None,
    darkflat_library=None,
    darkflats_pending=False,
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
        "target_adu": target_adu,
        "flat_levels": flat_levels,
        "darkflat_library": (
            str(darkflat_library)
            if darkflat_library is not None
            else None
        ),
        "darkflats_pending": bool(darkflats_pending),
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
    stamp_session=True,
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

            if stamp_session:
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


# ---------------------------------------------------------------------
# Fixed-exposure tuning and dark-flat library
# ---------------------------------------------------------------------

def run_queue(queue_path, queue_name):
    load_queue(
        queue_path,
        queue_name,
    )

    started = time.time()

    result = dbus(
        "start",
        TRAIN
    )

    print(
        f"Ekos start: {result}"
    )

    wait_for_completion()

    return started


def panel_limits(config):
    panel = config.get(
        "panel",
        {},
    )

    # 0 means "off" on most panels, so never tune down to it.
    b_min = max(
        1,
        int(panel.get("brightness_min", 1)),
    )

    b_max = int(
        panel.get("brightness_max", 255)
    )

    b_step = int(
        panel.get("brightness_step", 1)
    )

    return b_min, b_max, b_step


def tune_flat_settings(
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
    """
    Probe at a fixed ladder exposure and adjust panel brightness
    until the brightest CFA channel lands on target. Step the
    exposure up the ladder only when the panel runs out.

    Returns (brightness, exposure, levels, probe_file).
    """
    ladder = fc.exposure_ladder(config)

    bits = int(
        profile_data.get("camera_bits", 14)
    )

    target, tolerance, cap, floor = fc.flat_targets(
        config,
        bits,
    )

    accept_floor = fc.flat_floor(
        config,
        bits,
    )

    b_min, b_max, b_step = panel_limits(config)

    max_attempts = int(
        config.get(
            "flat_target",
            {},
        ).get(
            "max_tuning_attempts",
            8,
        )
    )

    exposure = fc.snap_to_ladder(
        profile_data.get("flat_exposure"),
        ladder,
    )

    brightness = fc.quantize_brightness(
        profile_data.get(
            "brightness",
            (b_min + b_max) // 2,
        ),
        b_min,
        b_max,
        b_step,
    )

    print()
    print("=" * 72)
    print("PHASE 1: FIXED-EXPOSURE PANEL TUNING")
    print("=" * 72)
    print(
        f"Target:     brightest CFA channel {target} ADU "
        f"± {tolerance * 100:.0f}%"
    )
    print(
        f"Cap:        {cap} ADU   "
        f"Weak-channel floor: {floor} ADU"
    )
    print(
        "Accept:     "
        + (
            f"down to {accept_floor} ADU at full brightness "
            "before lengthening the exposure"
            if accept_floor is not None
            else "target only (min_brightest_fraction = 0)"
        )
    )
    print(
        "Ladder:     "
        + ", ".join(f"{step:g} s" for step in ladder)
    )
    print(
        f"Panel:      {b_min}-{b_max} (step {b_step})"
    )

    blocked = set()

    for attempt in range(1, max_attempts + 1):
        set_brightness(brightness)

        probe_dir = build_probe_queue(
            session=session,
            profile=profile,
            gain=gain,
            offset=offset,
            xbin=xbin,
            ybin=ybin,
            rotsteps=rotsteps,
            exposure=exposure,
            config=config,
        )

        light_on()

        started = run_queue(
            PROBE_QUEUE,
            "FlatProbe",
        )

        probe_file, _ = read_probe_result(
            probe_dir,
            started,
            profile,
            session,
        )

        levels = fc.read_cfa_levels(
            probe_file,
            bits,
        )

        print(
            f"Probe {attempt}: brightness {brightness:>3}  "
            f"exposure {exposure:g} s  "
            f"brightest {levels['brightest']:.0f}  "
            f"weakest {levels['weakest']:.0f} ADU"
        )

        done, next_brightness, next_exposure = fc.tuning_step(
            brightness=brightness,
            exposure=exposure,
            levels=levels,
            target=target,
            tolerance=tolerance,
            cap=cap,
            ladder=ladder,
            b_min=b_min,
            b_max=b_max,
            b_step=b_step,
            blocked=blocked,
            floor=accept_floor,
            weak_floor=floor + float(
                config.get("frames", {}).get("pedestal_adu", 0)
            ),
        )

        if done:
            pedestal = float(
                config.get("frames", {}).get("pedestal_adu", 0)
            )

            error_pct = fc.additive_error_percent(
                levels,
                error_adu=2.0,
                pedestal=pedestal,
            )

            levels = dict(levels)
            levels["accepted_below_target"] = (
                levels["brightest"] < target * (1 - tolerance)
            )
            levels["additive_error_2adu_percent"] = round(
                error_pct,
                3,
            )

            print()
            print("FLAT LEVEL ANALYSIS")
            print(
                f"  Brightest channel: {levels['brightest']:.0f} ADU "
                f"({100 * levels['brightest'] / ((2 ** bits) - 1):.0f}% "
                f"of full scale; target {target})"
            )
            print(
                f"  Weakest channel:   {levels['weakest']:.0f} ADU"
            )
            print(
                f"  A 2 ADU calibration error = {error_pct:.3f}% "
                "of the weakest channel"
            )

            if levels["accepted_below_target"]:
                print(
                    f"  Accepted below target at full brightness to "
                    f"hold {exposure:g} s; the frame count compensates."
                )

            if levels["weakest"] < floor:
                print(
                    f"  NOTE: weakest channel under {floor} ADU; "
                    "the flat count is raised to compensate."
                )

            if error_pct > 0.2:
                print(
                    "  WARNING: this flat is sensitive to small "
                    "calibration errors (over 0.2% per 2 ADU). Match "
                    "dark-flats exactly and avoid light leaks."
                )

            return brightness, exposure, levels, probe_file

        brightness = next_brightness
        exposure = next_exposure

    raise RuntimeError(
        "PANEL TUNING DID NOT CONVERGE\n"
        f"No setting reached {target} ADU ± "
        f"{tolerance * 100:.0f}% in {max_attempts} probes.\n"
        "Raise flat_target.max_tuning_attempts or check the "
        "panel's brightness response."
    )


def save_learned_profile(
    profile,
    brightness,
    exposure,
    target,
):
    """
    Remember the tuned brightness and ladder exposure so the next
    session's first probe usually lands in tolerance.
    """
    profiles = load_json(PROFILES)

    entry = profiles.get(profile, {})

    entry.update(
        {
            "brightness": int(brightness),
            "flat_exposure": float(exposure),
            "last_success_exposure": float(exposure),
            "target_adu": int(target),
            "target_metric": "brightest_cfa_channel",
            "updated": time.strftime(
                "%Y-%m-%dT%H:%M:%S"
            ),
        }
    )

    profiles[profile] = entry

    PROFILES.write_text(
        json.dumps(
            profiles,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    print(
        f"Profile updated: {profile} -> brightness "
        f"{brightness}, exposure {exposure:g} s"
    )


def acquire_darkflats(
    session,
    profile,
    camera,
    gain,
    offset,
    xbin,
    ybin,
    rotsteps,
    exposure,
    probe_file,
    config,
):
    """
    Return (darkflat_files, library_dir_or_None, pending).

    With the library enabled (default), dark-flats are reused
    from a library keyed on camera state and exposure. When no
    complete library exists, darkflat_library.on_missing decides:

      "notify"  (default) record the request, send a Pushover
                message and continue without dark-flats; build
                it later with `flatengine darkflats --build`
      "capture" capture the library now, unattended

    A light-leak check guards every new capture.
    """
    lib_cfg = config.get(
        "darkflat_library",
        {},
    )

    use_library = lib_cfg.get(
        "enabled",
        True,
    )

    count = int(
        lib_cfg.get(
            "frames",
            config["frames"]["flats_per_group"],
        )
    )

    leak_threshold = float(
        lib_cfg.get(
            "leak_threshold_adu",
            5.0,
        )
    )

    with fits.open(
        probe_file,
        memmap=False,
    ) as hdul:
        hdr = hdul[0].header
        ccd_temp = hdr.get("CCD-TEMP")
        lownoise = hdr.get("LOWNOISE")

    print()
    print("=" * 72)
    print("PHASE 3: DARK-FLATS")
    print("=" * 72)
    print(
        f"Exposure: {exposure:g} s"
    )

    library_dir = None

    if use_library:
        library_dir = fc.darkflat_library_dir(
            config,
            camera,
            gain,
            offset,
            xbin,
            ybin,
            exposure,
            lownoise=lownoise,
            ccd_temp=ccd_temp,
        )

        existing = fc.library_files(
            library_dir,
            count,
        )

        if existing:
            print(
                f"Reusing dark-flat library: {library_dir}"
            )
            print(
                f"Library frames: {len(existing)}"
            )

            return existing, library_dir, False

        on_missing = str(
            lib_cfg.get(
                "on_missing",
                "notify",
            )
        ).lower()

        if on_missing == "notify":
            step = float(
                lib_cfg.get(
                    "temperature_step_c",
                    5,
                )
            )

            details = {
                "camera": camera,
                "gain": gain,
                "offset": offset,
                "xbin": xbin,
                "ybin": ybin,
                "exposure": float(exposure),
                "lownoise": lownoise,
                "temperature_band_c": (
                    None
                    if ccd_temp is None
                    else round(float(ccd_temp) / step) * step
                ),
                "count": count,
                "profile": profile,
            }

            record, due = fc.record_pending_darkflats(
                library_dir,
                details,
                session,
                repeat_hours=config.get(
                    "notify",
                    {},
                ).get(
                    "repeat_hours",
                    24,
                ),
            )

            print(
                "No dark-flat library for this exposure. "
                "Recorded as pending (not captured):"
            )
            print(
                f"  {fc.describe_pending(record)}"
            )
            print(
                f"  {library_dir}"
            )
            print(
                "Build it after dark with: "
                "flatengine darkflats --build"
            )

            if due:
                fc.notify(
                    config,
                    "FlatEngine: dark-flats needed",
                    (
                        f"No library for {profile} flats "
                        f"(session {session}).\n"
                        f"Shoot {fc.describe_pending(record)}.\n"
                        "After dark, front end covered: "
                        "flatengine darkflats --build"
                    ),
                )

            return [], library_dir, True

        print(
            f"No complete library; building: {library_dir}"
        )

    # Panel must be off for dark-flats.
    light_off()

    dark_dir = build_darkflat_queue(
        session=session,
        profile=profile,
        gain=gain,
        offset=offset,
        xbin=xbin,
        ybin=ybin,
        rotsteps=rotsteps,
        exposure=exposure,
        config=config,
        dark_dir=library_dir,
        count=count,
    )

    print(
        f"Output:   {dark_dir}"
    )

    started = run_queue(
        DARKFLAT_QUEUE,
        "DarkFlatProduction",
    )

    darkflat_files = verify_new_darkflats(
        dark_dir,
        count,
        started,
        session,
        exposure,
        stamp_session=not use_library,
    )

    with fits.open(
        darkflat_files[0],
        memmap=False,
    ) as hdul:
        leak = fc.light_leak_report(
            hdul[0].data,
            leak_threshold,
        )

    print(
        f"Leak check: channel spread "
        f"{leak['channel_spread_adu']} ADU, "
        f"center minus corners "
        f"{leak['center_minus_corners_adu']} ADU "
        f"(limit {leak_threshold:g})"
    )

    if leak["suspected"]:
        fc.notify(
            config,
            "FlatEngine: light leak in dark-flats",
            (
                f"{exposure:g} s dark-flats rejected: channel "
                f"spread {leak['channel_spread_adu']} ADU, center "
                f"minus corners {leak['center_minus_corners_adu']} "
                "ADU. Not added to the library."
            ),
            priority=1,
        )

        raise RuntimeError(
            "LIGHT LEAK SUSPECTED IN DARK-FLATS\n"
            f"Channel spread {leak['channel_spread_adu']} ADU, "
            f"center minus corners "
            f"{leak['center_minus_corners_adu']} ADU.\n"
            "Cover the front end and camera, or shoot after "
            "dark. The frames were kept but NOT registered as a "
            f"library:\n{dark_dir}"
        )

    if use_library:
        manifest = fc.write_library_manifest(
            library_dir,
            darkflat_files,
            exposure,
            leak,
        )

        print(
            f"Library registered: {manifest}"
        )

        fc.clear_pending(library_dir)

    return darkflat_files, library_dir, False



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
        print(
            f"No learned profile for {args.profile!r}; "
            "tuning starts from mid-range brightness at the "
            "shortest ladder exposure."
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

    profile_data = profiles.get(profile, {})

    target_adu, _, _, _ = fc.flat_targets(
        config,
        profile_data.get("camera_bits", 14),
    )

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
        f"{profile_data.get('brightness', 'not yet learned')}"
    )
    print(
        f"Exposure:      "
        f"{profile_data.get('flat_exposure', 'not yet learned')}"
    )
    print(
        f"Target ADU:    "
        f"{target_adu} (brightest CFA channel)"
    )
    print(
        f"Flat count:    "
        f"{config['frames']['flats_per_group']}"
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

    # ------------------------------------------------------------
    # Phase 1: tune panel brightness at a fixed ladder exposure
    # ------------------------------------------------------------

    try:
        (
            brightness,
            exposure,
            flat_levels,
            probe_file,
        ) = tune_flat_settings(
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

        save_learned_profile(
            profile,
            brightness,
            exposure,
            target_adu,
        )

        # --------------------------------------------------------
        # Phase 2: production flats at the tuned settings
        # --------------------------------------------------------

        flat_count, count_details = fc.flat_frame_count(
            flat_levels,
            config,
        )

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
            count=flat_count,
        )

        print()
        print("=" * 72)
        print("PHASE 2: FIXED-EXPOSURE FLATS")
        print("=" * 72)
        print(
            f"Exposure:   {exposure:g} s"
        )
        print(
            f"Brightness: {brightness}"
        )
        print(
            f"Frames:     {flat_count} "
            f"(weakest channel {count_details['weakest_adu']:.0f} ADU, "
            f"master SNR {count_details['master_snr']:.0f} "
            f"vs target {count_details['target_snr']:.0f})"
        )

        if count_details["capped"]:
            print(
                f"WARNING: {count_details['frames_needed']} frames "
                "needed for the target SNR; capped at "
                f"{flat_count} by frames.flats_max_per_group."
            )

        print(
            f"Output:     {flat_dir}"
        )

        set_brightness(brightness)

        light_on()

        flat_started = run_queue(
            EXEC_QUEUE,
            "FlatProduction",
        )

        flat_files = verify_new_flats(
            flat_dir,
            profile,
            flat_count,
            flat_started,
            args.session,
        )

    finally:
        light_off()

    # ------------------------------------------------------------
    # Phase 3: dark-flats (library reuse or capture)
    # ------------------------------------------------------------

    darkflat_files = []
    library_dir = None
    darkflats_pending = False

    if config[
        "frames"
    ].get(
        "darkflats_enabled",
        True
    ):
        darkflat_files, library_dir, darkflats_pending = acquire_darkflats(
            session=args.session,
            profile=profile,
            camera=camera,
            gain=gain,
            offset=offset,
            xbin=xbin,
            ybin=ybin,
            rotsteps=rotsteps,
            exposure=exposure,
            probe_file=probe_file,
            config=config,
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
        brightness=brightness,
        exposure=exposure,
        flat_files=flat_files,
        darkflat_files=darkflat_files,
        flat_levels=dict(
            flat_levels,
            frame_count=count_details,
        ),
        target_adu=target_adu,
        darkflat_library=library_dir,
        darkflats_pending=darkflats_pending,
    )

    print()
    print("=" * 72)
    print("CALIBRATION GROUP COMPLETE")
    print("=" * 72)
    print(
        f"Session:    {args.session}"
    )
    print(
        f"Profile:    {profile}"
    )
    print(
        f"Exposure:   {exposure:g} s"
    )
    print(
        f"Brightness: {brightness}"
    )
    print(
        f"Flats:      {flat_dir}"
    )

    if darkflats_pending:
        print(
            f"Dark-flats: PENDING (library not built): {library_dir}"
        )
    elif darkflat_files:
        print(
            "Dark-flats: "
            + str(
                library_dir
                if library_dir is not None
                else Path(darkflat_files[0]).parent
            )
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
