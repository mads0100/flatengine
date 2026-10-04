#!/usr/bin/env python3

from pathlib import Path
import importlib.util
import shutil
import sys

import flat_common as fc


def check(label, ok, detail=""):
    status = "OK" if ok else "FAIL"

    print(
        f"{label:<28} {status}"
        + (
            f"  {detail}"
            if detail
            else ""
        )
    )

    return ok


def main():
    print("=" * 72)
    print("FLATENGINE DOCTOR")
    print("=" * 72)

    failures = 0

    # --------------------------------------------------------
    # Core paths
    # --------------------------------------------------------

    print()
    print("PATHS")

    checks = [
        (
            "Config file",
            fc.CONFIG_FILE.exists(),
            str(fc.CONFIG_FILE),
        ),
        (
            "Profiles file",
            fc.PROFILE_FILE.exists(),
            str(fc.PROFILE_FILE),
        ),
        (
            "Template",
            fc.TEMPLATE_FILE.exists(),
            str(fc.TEMPLATE_FILE),
        ),
        (
            "Pictures root",
            fc.PICTURES.exists(),
            str(fc.PICTURES),
        ),
        (
            "Queue directory",
            fc.QUEUE_DIR.exists(),
            str(fc.QUEUE_DIR),
        ),
    ]

    for label, ok, detail in checks:
        if not check(label, ok, detail):
            failures += 1

    # --------------------------------------------------------
    # Required commands
    # --------------------------------------------------------

    print()
    print("COMMANDS")

    for cmd in (
        "python3",
        "indi_getprop",
        "indi_setprop",
        "qdbus",
        "gdbus",
    ):
        path = shutil.which(cmd)

        if not check(
            cmd,
            path is not None,
            path or "not found",
        ):
            failures += 1

    # --------------------------------------------------------
    # Python dependencies
    # --------------------------------------------------------

    print()
    print("PYTHON")

    try:
        import astropy

        check(
            "astropy",
            True,
            getattr(
                astropy,
                "__version__",
                "installed",
            ),
        )

    except Exception as e:
        check(
            "astropy",
            False,
            str(e),
        )
        failures += 1

    # --------------------------------------------------------
    # Configuration content
    # --------------------------------------------------------

    print()
    print("CONFIGURATION")

    try:
        import json

        config = json.loads(
            fc.CONFIG_FILE.read_text()
        )

        train = (
            config.get("ekos", {})
            .get("optical_train")
        )

        panel = (
            config.get("panel", {})
            .get("device")
        )

        check(
            "Ekos optical train",
            bool(train),
            str(train),
        )

        check(
            "Flat panel device",
            bool(panel),
            str(panel),
        )

        output_root = (
            config.get("output", {})
            .get("root")
        )

        check(
            "Calibration output",
            bool(output_root),
            str(output_root),
        )

    except Exception as e:
        check(
            "Config parse",
            False,
            str(e),
        )

        failures += 1
        config = None

    # --------------------------------------------------------
    # Writeability
    # --------------------------------------------------------

    print()
    print("WRITE ACCESS")

    for label, path in (
        ("Config directory", fc.CONFIG_DIR),
        ("Queue directory", fc.QUEUE_DIR),
        ("State directory", fc.STATE_DIR),
    ):
        try:
            path.mkdir(
                parents=True,
                exist_ok=True,
            )

            probe = path / ".flatengine-write-test"

            probe.write_text(
                "ok\n"
            )

            probe.unlink()

            ok = True

        except Exception as e:
            ok = False
            detail = str(e)

        else:
            detail = str(path)

        if not check(
            label,
            ok,
            detail,
        ):
            failures += 1

    # --------------------------------------------------------
    # INDI visibility
    # --------------------------------------------------------

    print()
    print("INDI")

    getprop = shutil.which(
        "indi_getprop"
    )

    if getprop:
        import subprocess

        result = subprocess.run(
            [
                getprop,
                "*.*.*",
            ],
            text=True,
            capture_output=True,
        )

        indi_ok = (
            result.returncode == 0
            and bool(
                result.stdout.strip()
            )
        )

        check(
            "INDI server",
            indi_ok,
            (
                "properties visible"
                if indi_ok
                else "no properties returned"
            ),
        )

        if not indi_ok:
            failures += 1

        if config:
            panel = (
                config.get("panel", {})
                .get("device")
            )

            if panel:
                panel_seen = (
                    f"{panel}."
                    in result.stdout
                )

                check(
                    "Configured panel",
                    panel_seen,
                    panel,
                )

            train = (
                config.get("ekos", {})
                .get("optical_train")
            )

            if train:
                check(
                    "Configured train",
                    True,
                    train,
                )

    print()
    print("=" * 72)

    if failures:
        print(
            f"DOCTOR RESULT: {failures} problem(s) found"
        )
        print("=" * 72)
        raise SystemExit(2)

    print("DOCTOR RESULT: ALL CHECKS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    main()
