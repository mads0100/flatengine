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


def main():
    print("=" * 72)
    print("FLAT ENGINE REGRESSION")
    print("=" * 72)

    try:
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
