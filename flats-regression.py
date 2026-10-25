#!/usr/bin/env python3

from pathlib import Path
import shutil
import subprocess
import sys

from astropy.io import fits

import flat_common as fc


ROOT = Path(__file__).resolve().parent

PLAN = ROOT / "flats-plan.py"
EXEC = ROOT / "flats-execute.py"
NIGHTLY = ROOT / "flats-nightly.py"

PICTURES = fc.PICTURES

REAL_SESSION = "20261003"
ROT_SESSION = "20990101"

TEST_ROOT = (
    PICTURES
    / "ROTSTEPS-Regression"
)


def run(cmd, expect=0):
    print()
    print("$", " ".join(str(x) for x in cmd))

    result = subprocess.run(
        [str(x) for x in cmd],
        text=True,
        capture_output=True,
    )

    if result.stdout:
        print(result.stdout.rstrip())

    if result.stderr:
        print(result.stderr.rstrip())

    if result.returncode != expect:
        raise RuntimeError(
            f"Command returned {result.returncode}, "
            f"expected {expect}"
        )

    return result.stdout + result.stderr


def require(text, needle, label):
    if needle not in text:
        raise RuntimeError(
            f"{label} FAILED\n"
            f"Missing expected text:\n{needle}"
        )

    print(f"PASS: {label}")


def find_source_light():
    candidates = []

    for path in PICTURES.rglob("*.fits"):
        if TEST_ROOT in path.parents:
            continue

        try:
            with fits.open(
                path,
                memmap=False
            ) as hdul:
                hdr = hdul[0].header
        except Exception:
            continue

        imagetype = str(
            hdr.get("IMAGETYP", "")
        ).lower()

        filt = str(
            hdr.get("FILTER", "")
        )

        if (
            "light" in imagetype
            and filt == "L-eNh"
        ):
            candidates.append(path)

    if not candidates:
        raise RuntimeError(
            "Could not find an existing L-eNh "
            "light frame for regression cloning."
        )

    return sorted(candidates)[0]


def create_rotsteps_test():
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)

    TEST_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    source = find_source_light()

    print()
    print(
        f"Using regression source light: {source}"
    )

    cases = [
        (
            "rot_minus4.fits",
            -4.0,
            0.0,
        ),
        (
            "rot_250.fits",
            250.0,
            359.822064,
        ),
    ]

    for (
        name,
        rotsteps,
        rotatang,
    ) in cases:
        dst = TEST_ROOT / name

        shutil.copy2(
            source,
            dst,
        )

        with fits.open(
            dst,
            mode="update",
            memmap=False
        ) as hdul:
            hdr = hdul[0].header

            hdr["SESSION"] = (
                ROT_SESSION,
                "Synthetic regression session",
            )

            hdr["FILTER"] = "L-eNh"

            hdr["ROTSTEPS"] = (
                rotsteps,
                "Synthetic absolute rotator position",
            )

            hdr["ROTATANG"] = (
                rotatang,
                "Synthetic rotator angle",
            )

            hdul.flush()

        print(
            f"Created {dst.name}: "
            f"ROTSTEPS={rotsteps}, "
            f"ROTATANG={rotatang}"
        )


def cleanup_rotsteps_test():
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)

    calibration_root = (
        PICTURES
        / "Calibration"
        / ROT_SESSION
    )

    if calibration_root.exists():
        shutil.rmtree(
            calibration_root
        )


def check(condition, label):
    if not condition:
        raise RuntimeError(f"{label} FAILED")

    print(f"PASS: {label}")


def fixed_exposure_checks():
    """
    Hardware-free checks for fixed-exposure tuning and the
    dark-flat library helpers.
    """
    import tempfile

    import numpy as np

    config = {
        "output": {"root": "/tmp/flatengine-unused"},
        "exposure_policy": {"minimum_seconds": 1.0},
        "flat_target": {},
        "darkflat_library": {},
    }

    ladder = fc.exposure_ladder(config)

    check(
        ladder[0] == 1.0 and 0.5 not in ladder,
        "ladder never goes below 1 s",
    )

    target, tol, cap, floor = fc.flat_targets(config, 14)

    check(
        target == round(16383 * 0.64) and cap == round(16383 * 0.8),
        "14-bit targets",
    )

    # Synthetic GBRG flat: greens 10000, blue 6000, red 4000.
    raw = np.zeros((400, 600), dtype=np.uint16)
    raw[0::2, 0::2] = 10000
    raw[1::2, 1::2] = 10000
    raw[0::2, 1::2] = 6000
    raw[1::2, 0::2] = 4000

    levels = fc.cfa_levels(raw, ceiling=16383)

    check(
        levels["brightest"] == 10000 and levels["weakest"] == 4000,
        "CFA levels independent of Bayer pattern",
    )

    # Simulated panels: linear, and nonlinear with a dim filter.
    def simulate(adu_per_unit_second, power, start_b, start_e):
        b, e = start_b, start_e
        blocked = set()

        for _ in range(12):
            measured = adu_per_unit_second * (b ** power) * e

            done, b, e = fc.tuning_step(
                b, e, {"brightest": measured}, target, tol, cap,
                ladder, 1, 255, 1, blocked=blocked,
            )

            if done:
                return b, e

        raise RuntimeError("did not converge")

    b, e = simulate(80.0, 1.0, 128, 1.0)
    check(e == 1.0, f"broadband converges at 1 s (brightness {b})")

    b, e = simulate(80.0, 1.0, 128, 5.0)
    check(e == 1.0, "steps back down to 1 s when the panel can")

    # Dim filter on a nonlinear panel: full brightness needs about
    # 15 s, so the shortest workable ladder step is 20 s.
    b, e = simulate(1.2, 1.15, 200, 1.0)
    check(e == 20.0, f"narrowband steps up to 20 s (brightness {b})")

    b, e = simulate(1.2, 1.15, 200, 60.0)
    check(e == 20.0, f"narrowband steps down to 20 s (brightness {b})")

    # Library naming is stable for small temperature jitter.
    a = fc.darkflat_library_dir(
        config, "ToupTek SkyEye45AC-plus", 100, 40, 1, 1, 1.0,
        lownoise="ON", ccd_temp=-10.6,
    )
    b_dir = fc.darkflat_library_dir(
        config, "ToupTek SkyEye45AC-plus", 100, 40, 1, 1, 1.0,
        lownoise="ON", ccd_temp=-9.8,
    )

    check(a == b_dir, "library key tolerates CCD-TEMP jitter")
    check(a.name.endswith("_E1s"), "library key carries exposure")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        files = []

        for index in range(3):
            path = tmp / f"df_{index}.fits"
            path.write_bytes(b"x")
            files.append(path)

        check(
            fc.library_files(tmp, 3) is None,
            "library without manifest is not reused",
        )

        fc.write_library_manifest(tmp, files, 1.0, {})

        check(
            fc.library_files(tmp, 3) == files,
            "complete library is reused",
        )

        check(
            fc.library_files(tmp, 4) is None,
            "short library is not reused",
        )

    count_cfg = {
        "frames": {
            "flats_per_group": 30,
            "flats_max_per_group": 100,
            "flat_min_snr": 200,
            "e_per_adu": 0.91,
            "pedestal_adu": 44,
        }
    }

    count, _ = fc.flat_frame_count(
        {"brightest": 10500, "weakest": 4194}, count_cfg,
    )
    check(count == 30, "broadband flat keeps the base count")

    count, details = fc.flat_frame_count(
        {"brightest": 10500, "weakest": 1000}, count_cfg,
    )
    check(
        40 <= count <= 50 and details["master_snr"] >= 200,
        f"weak red channel raises the count ({count} frames)",
    )

    count, details = fc.flat_frame_count(
        {"brightest": 10500, "weakest": 200}, count_cfg,
    )
    check(
        count == 100 and details["capped"],
        "very weak channel is capped at the maximum",
    )

    # Pending dark-flat records and Pushover, isolated in a temp dir.
    import io
    import json as _json
    import os

    with tempfile.TemporaryDirectory() as tmp:
        saved = fc.PENDING_FILE
        fc.PENDING_FILE = Path(tmp) / "pending.json"

        try:
            details = {"exposure": 30.0, "gain": 100, "offset": 40,
                       "xbin": 1, "ybin": 1, "count": 30}

            _, due = fc.record_pending_darkflats(
                Path(tmp) / "lib30", details, "20261025",
            )
            check(due, "first pending request notifies")

            record, due = fc.record_pending_darkflats(
                Path(tmp) / "lib30", details, "20261026",
            )
            check(
                not due and record["sessions"] == ["20261025", "20261026"],
                "repeat request within window does not re-notify",
            )

            check(
                fc.clear_pending(Path(tmp) / "lib30")
                and fc.load_pending() == {},
                "building a library clears its request",
            )
        finally:
            fc.PENDING_FILE = saved

    sent = {}

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(request, timeout=10):
        sent["data"] = request.data.decode()
        return FakeResponse(_json.dumps({"status": 1}).encode())

    push_cfg = {"notify": {"pushover": {"user_key": "u1", "app_token": "t1"}}}

    check(
        fc.notify(push_cfg, "Title", "Body", opener=fake_open)
        and "user=u1" in sent["data"] and "token=t1" in sent["data"],
        "Pushover request carries credentials",
    )

    for name in ("FLATENGINE_PUSHOVER_USER", "FLATENGINE_PUSHOVER_TOKEN"):
        os.environ.pop(name, None)

    check(
        not fc.notify({}, "Title", "Body", opener=fake_open),
        "missing Pushover credentials degrade to printing",
    )

    def failing_open(request, timeout=10):
        raise OSError("network down")

    check(
        not fc.notify(push_cfg, "Title", "Body", opener=failing_open),
        "Pushover failure does not raise",
    )

    rng = np.random.default_rng(1)
    clean = rng.normal(44, 1.5, (600, 900))

    yy, xx = np.mgrid[0:600, 0:900]
    vignette = 1 - 0.3 * (((yy - 300) / 300) ** 2 + ((xx - 450) / 450) ** 2)
    leaked = clean + 20 * vignette
    leaked[1::2, 0::2] += 10

    check(
        not fc.light_leak_report(clean)["suspected"],
        "clean dark passes leak check",
    )

    check(
        fc.light_leak_report(leaked)["suspected"],
        "leaked dark is flagged",
    )


def main():
    print("=" * 72)
    print("FLAT ENGINE REGRESSION")
    print("=" * 72)

    try:
        fixed_exposure_checks()

        # ----------------------------------------------------
        # 1. Syntax checks
        # ----------------------------------------------------
        run([
            sys.executable,
            "-m",
            "py_compile",
            PLAN,
            EXEC,
            NIGHTLY,
            ROOT / "flat_common.py",
            ROOT / "flat-calibrate.py",
            ROOT / "flat-setup.py",
            ROOT / "flat-darkflats.py",
        ])

        print("PASS: Python syntax")

        # ----------------------------------------------------
        # 2. Existing completed session stays idempotent
        # ----------------------------------------------------
        out = run([
            NIGHTLY,
            "--session",
            REAL_SESSION,
            "--dry-run",
        ])

        require(
            out,
            "Groups to run:  0",
            "completed session has no work",
        )

        require(
            out,
            "Groups skipped: 3",
            "completed session manifests recognized",
        )

        # ----------------------------------------------------
        # 3. Planner explicit session
        # ----------------------------------------------------
        out = run([
            PLAN,
            "--session",
            REAL_SESSION,
        ])

        require(
            out,
            f"Session: {REAL_SESSION}",
            "planner honors explicit session",
        )

        # ----------------------------------------------------
        # 4. CLI features
        # ----------------------------------------------------
        out = run([
            EXEC,
            "--help",
        ])

        require(
            out,
            "--rotator-only",
            "executor exposes rotator-only mode",
        )

        require(
            out,
            "--rotsteps",
            "executor exposes rotsteps selector",
        )

        # ----------------------------------------------------
        # 5. Self-contained ROTSTEPS regression session
        # ----------------------------------------------------
        create_rotsteps_test()

        out = run([
            NIGHTLY,
            "--session",
            ROT_SESSION,
            "--dry-run",
        ])

        require(
            out,
            "Light groups:   2",
            "ROTSTEPS session splits into two groups",
        )

        require(
            out,
            "ROTSTEPS=-4.0",
            "negative ROTSTEPS group preserved",
        )

        require(
            out,
            "ROTSTEPS=250.0",
            "positive ROTSTEPS group preserved",
        )

        out = run([
            PLAN,
            "--session",
            ROT_SESSION,
        ])

        require(
            out,
            f"Session: {ROT_SESSION}",
            "planner sees synthetic session",
        )

        require(
            out,
            "Flat groups required: 2",
            "planner preserves two rotation groups",
        )

        require(
            out,
            "ROTSTEPS:     -4.0",
            "planner prints negative rotation",
        )

        require(
            out,
            "ROTSTEPS:     250.0",
            "planner prints positive rotation",
        )

    finally:
        cleanup_rotsteps_test()

    print()
    print("=" * 72)
    print("REGRESSION COMPLETE")
    print("=" * 72)
    print("All tested checks passed.")
    print("Synthetic regression data cleaned up.")


if __name__ == "__main__":
    try:
        main()

    except RuntimeError as e:
        print()
        print("=" * 72)
        print("REGRESSION FAILED")
        print("=" * 72)
        print(str(e))
        print("=" * 72)
        raise SystemExit(2)
