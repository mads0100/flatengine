#!/usr/bin/env python3

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import math

from astropy.io import fits

import json
import os
import re
import time

import numpy as np


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


# ---------------------------------------------------------------------
# Fixed-exposure flat tuning
#
# Flats are shot at a fixed exposure chosen from a short ladder of
# standard steps (1 s first). The panel brightness is adjusted to hit
# the target level. Only when the panel cannot get bright enough does
# the exposure step up the ladder. Standard steps keep dark-flat
# libraries reusable across sessions.
# ---------------------------------------------------------------------

DEFAULT_EXPOSURE_LADDER = [
    1.0,
    2.0,
    3.0,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
]


def _fmt(value):
    value = float(value)

    if value.is_integer():
        return str(int(value))

    return f"{value:g}"


def exposure_ladder(config):
    """
    Standard flat exposures, shortest first, never below
    exposure_policy.minimum_seconds (rolling-shutter guard).
    """
    policy = config.get(
        "exposure_policy",
        {},
    )

    ladder = (
        policy.get("ladder_seconds")
        or DEFAULT_EXPOSURE_LADDER
    )

    minimum = float(
        policy.get(
            "minimum_seconds",
            1.0,
        )
    )

    steps = sorted(
        {
            float(value)
            for value in ladder
            if float(value) >= minimum
        }
    )

    if not steps:
        raise RuntimeError(
            "CONFIGURATION ERROR\n"
            "exposure_policy.ladder_seconds has no step at or "
            f"above minimum_seconds={minimum}."
        )

    return steps


def snap_to_ladder(exposure, ladder):
    """
    Return the ladder step nearest to a stored exposure,
    or the first step when nothing is stored.
    """
    if exposure is None:
        return ladder[0]

    return min(
        ladder,
        key=lambda step: abs(step - float(exposure)),
    )


def flat_targets(config, bits=14):
    """
    Return the brightest-CFA-channel target, tolerance fraction,
    saturation cap and weakest-channel floor, all in ADU.
    """
    ceiling = (2 ** int(bits)) - 1

    target_cfg = config.get(
        "flat_target",
        {},
    )

    target = round(
        ceiling
        * float(
            target_cfg.get(
                "brightest_channel_fraction",
                0.64,
            )
        )
    )

    tolerance = float(
        target_cfg.get(
            "tolerance_fraction",
            0.05,
        )
    )

    cap = round(
        ceiling
        * float(
            target_cfg.get(
                "saturation_cap_fraction",
                0.80,
            )
        )
    )

    floor = int(
        target_cfg.get(
            "weakest_channel_min_adu",
            1000,
        )
    )

    return target, tolerance, cap, floor


def cfa_levels(data, ceiling=None):
    """
    Median of each 2x2 Bayer position over the central 80% of the
    frame. Works without knowing the Bayer pattern, which matters
    because some drivers write the wrong BAYERPAT.

    Returns brightest/weakest position medians, the per-position
    medians and the saturated-pixel fraction.
    """
    data = np.asarray(data)

    if data.ndim != 2:
        raise RuntimeError(
            f"Expected a 2-D raw frame, got shape {data.shape}."
        )

    height, width = data.shape

    # Keep crop origins even so Bayer phase is preserved.
    y0 = (height // 10) // 2 * 2
    x0 = (width // 10) // 2 * 2
    y1 = height - y0
    x1 = width - x0

    crop = data[y0:y1, x0:x1]

    positions = {}

    for dy in (0, 1):
        for dx in (0, 1):
            channel = crop[dy::2, dx::2]

            # Every other pixel of the channel is plenty for a median.
            positions[f"{dy}{dx}"] = float(
                np.median(channel[::2, ::2])
            )

    saturated = None

    if ceiling is not None:
        saturated = float(
            np.mean(crop >= ceiling)
        )

    return {
        "brightest": max(positions.values()),
        "weakest": min(positions.values()),
        "positions": positions,
        "saturated_fraction": saturated,
    }


def read_cfa_levels(path, bits=14):
    with fits.open(
        path,
        memmap=False,
    ) as hdul:
        data = hdul[0].data

    if data is None:
        raise RuntimeError(
            f"FITS file has no image data: {path}"
        )

    return cfa_levels(
        data,
        ceiling=(2 ** int(bits)) - 1,
    )


def quantize_brightness(
    value,
    b_min,
    b_max,
    b_step=1,
):
    value = max(
        float(b_min),
        min(float(b_max), float(value)),
    )

    step = max(1, int(b_step))

    quantized = int(round(value / step) * step)

    return int(
        max(int(b_min), min(int(b_max), quantized))
    )


def in_flat_tolerance(levels, target, tolerance, cap):
    brightest = levels["brightest"]

    return (
        abs(brightest - target) <= target * tolerance
        and brightest <= cap
    )


def plan_next_probe(
    brightness,
    exposure,
    measured,
    target,
    ladder,
    b_min,
    b_max,
    b_step=1,
    gamma=0.85,
    blocked=(),
):
    """
    Choose the next (brightness, exposure) after a probe measured
    `measured` ADU in the brightest CFA channel.

    Panel output is treated as roughly proportional to brightness,
    damped by `gamma` for nonlinear LED panels. The shortest ladder
    exposure that the panel can serve is always preferred.

    `blocked` holds exposures already proven too dim at maximum
    brightness; they are never retried, which prevents oscillation
    when the panel's response is nonlinear near its top end.

    Raises RuntimeError when no setting can reach the target.
    """
    measured = max(float(measured), 1.0)

    index = ladder.index(exposure)

    needed = brightness * (target / measured) ** gamma

    def q(value):
        return quantize_brightness(
            value,
            b_min,
            b_max,
            b_step,
        )

    # Prefer a shorter exposure when the panel can carry it.
    if index > 0 and ladder[index - 1] not in blocked:
        shorter = ladder[index - 1]
        needed_shorter = needed * exposure / shorter

        if needed_shorter <= b_max:
            return q(needed_shorter), shorter

    if needed > b_max:
        # Signal is exactly linear in exposure, so predict the
        # exposure needed at full brightness and jump straight to
        # the shortest ladder step that covers it.
        at_max = measured * (b_max / brightness) ** (1.0 / gamma)
        required = exposure * target / at_max

        candidates = [
            step
            for step in ladder
            if step >= required
            and step > exposure
            and step not in blocked
        ]

        if brightness < b_max and required <= exposure:
            return int(b_max), exposure

        if candidates:
            step = candidates[0]

            return (
                q(b_max * (required / step) ** gamma),
                step,
            )

        if brightness < b_max:
            return int(b_max), exposure

        raise RuntimeError(
            "FLAT TOO DIM\n"
            f"Panel is at maximum brightness ({b_max}) and the "
            f"longest ladder exposure ({ladder[-1]} s) still "
            f"reads {measured:.0f} ADU against a target of "
            f"{target} ADU.\n"
            "Add a longer step to exposure_policy.ladder_seconds."
        )

    if needed < b_min:
        if brightness > b_min:
            return int(b_min), exposure

        raise RuntimeError(
            "FLAT TOO BRIGHT\n"
            f"Panel is at minimum brightness ({b_min}) at the "
            f"shortest allowed exposure ({ladder[0]} s) and still "
            f"reads {measured:.0f} ADU against a target of "
            f"{target} ADU.\n"
            "Shorter exposures risk rolling-shutter banding; add "
            "a diffuser or neutral-density sheet to the panel."
        )

    new_brightness = q(needed)

    if new_brightness == brightness:
        # Integer rounding stalled the update: nudge one step.
        step = max(1, int(b_step))

        if measured < target:
            new_brightness = q(brightness + step)
        else:
            new_brightness = q(brightness - step)

    return new_brightness, exposure



def tuning_step(
    brightness,
    exposure,
    levels,
    target,
    tolerance,
    cap,
    ladder,
    b_min,
    b_max,
    b_step=1,
    blocked=None,
):
    """
    One decision of the panel-tuning loop.

    Returns (done, brightness, exposure). `done` is True only when
    the probe is in tolerance AND no shorter ladder step could be
    served by the panel, so flats always use the shortest workable
    exposure. `blocked` (a set) accumulates exposures proven too dim
    at maximum brightness.
    """
    if blocked is None:
        blocked = set()

    measured = levels["brightest"]

    if in_flat_tolerance(levels, target, tolerance, cap):
        index = ladder.index(exposure)

        if index > 0 and ladder[index - 1] not in blocked:
            shorter = ladder[index - 1]
            needed = brightness * exposure / shorter

            if needed <= b_max:
                return (
                    False,
                    quantize_brightness(
                        needed,
                        b_min,
                        b_max,
                        b_step,
                    ),
                    shorter,
                )

        return True, brightness, exposure

    if (
        brightness >= b_max
        and measured < target * (1 - tolerance)
    ):
        blocked.add(exposure)

    new_brightness, new_exposure = plan_next_probe(
        brightness=brightness,
        exposure=exposure,
        measured=measured,
        target=target,
        ladder=ladder,
        b_min=b_min,
        b_max=b_max,
        b_step=b_step,
        blocked=blocked,
    )

    return False, new_brightness, new_exposure

# ---------------------------------------------------------------------
# Dark-flat library
#
# Dark-flats depend only on exposure and camera state, not on the
# optics, so one set is reused across sessions and filters.
# ---------------------------------------------------------------------

LIBRARY_MANIFEST = "library.json"


def _safe_name(text):
    return re.sub(
        r"[^A-Za-z0-9.+-]+",
        "-",
        str(text),
    ).strip("-") or "camera"


def darkflat_library_root(config):
    lib = config.get(
        "darkflat_library",
        {},
    )

    root = lib.get("root")

    if root:
        return Path(root).expanduser()

    return (
        Path(config["output"]["root"]).expanduser()
        / "DarkFlatLibrary"
    )


def darkflat_library_dir(
    config,
    camera,
    gain,
    offset,
    xbin,
    ybin,
    exposure,
    lownoise=None,
    ccd_temp=None,
):
    lib = config.get(
        "darkflat_library",
        {},
    )

    step = float(
        lib.get(
            "temperature_step_c",
            5,
        )
    )

    if ccd_temp is None:
        temp_text = "NA"
    else:
        temp_text = _fmt(
            round(float(ccd_temp) / step) * step
        )

    if lownoise in (None, ""):
        lownoise_text = "NA"
    else:
        lownoise_text = _safe_name(lownoise)

    name = (
        f"{_safe_name(camera)}"
        f"_G{_fmt(gain)}"
        f"_O{_fmt(offset)}"
        f"_B{_fmt(xbin)}x{_fmt(ybin)}"
        f"_LN{lownoise_text}"
        f"_T{temp_text}C"
        f"_E{_fmt(exposure)}s"
    )

    return darkflat_library_root(config) / name


def library_files(library_dir, count):
    """
    Return the library's dark-flat paths when the library is
    complete (manifest present, enough files, all on disk).
    Otherwise return None.
    """
    manifest_path = Path(library_dir) / LIBRARY_MANIFEST

    if not manifest_path.exists():
        return None

    try:
        manifest = json.loads(
            manifest_path.read_text()
        )
    except (OSError, ValueError):
        return None

    files = [
        Path(path)
        for path in manifest.get("files", [])
    ]

    if len(files) < int(count):
        return None

    if not all(path.exists() for path in files):
        return None

    return files


def write_library_manifest(
    library_dir,
    files,
    exposure,
    leak_report,
):
    manifest = {
        "exposure": exposure,
        "count": len(files),
        "files": [str(path) for path in files],
        "leak_check": leak_report,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    path = Path(library_dir) / LIBRARY_MANIFEST

    path.write_text(
        json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    return path


def light_leak_report(data, threshold_adu=5.0):
    """
    Detect light reaching a dark frame.

    Light shows up as Bayer channels that differ from each other
    and as a center that is brighter than the corners (the
    optics' vignetting pattern). A clean dark has equal channels
    and a flat field. A 2% border is ignored so edge readout rows
    and corner glow at the extreme edge do not trip the check.
    """
    data = np.asarray(data, dtype=np.float64)

    height, width = data.shape

    border_y = max(2, (height // 50) // 2 * 2)
    border_x = max(2, (width // 50) // 2 * 2)

    inner = data[
        border_y:height - border_y,
        border_x:width - border_x,
    ]

    levels = cfa_levels(inner)

    channel_spread = (
        levels["brightest"]
        - levels["weakest"]
    )

    h, w = inner.shape
    ph = max(2, h // 10)
    pw = max(2, w // 10)

    center = float(
        np.median(
            inner[
                h // 2 - ph // 2:h // 2 + ph // 2,
                w // 2 - pw // 2:w // 2 + pw // 2,
            ]
        )
    )

    corners = [
        float(np.median(inner[:ph, :pw])),
        float(np.median(inner[:ph, -pw:])),
        float(np.median(inner[-ph:, :pw])),
        float(np.median(inner[-ph:, -pw:])),
    ]

    center_excess = center - float(np.median(corners))

    suspected = (
        channel_spread > threshold_adu
        or center_excess > threshold_adu
    )

    return {
        "suspected": bool(suspected),
        "channel_spread_adu": round(channel_spread, 2),
        "center_minus_corners_adu": round(center_excess, 2),
        "center_adu": round(center, 2),
        "corner_adu": [round(value, 2) for value in corners],
        "threshold_adu": float(threshold_adu),
    }
