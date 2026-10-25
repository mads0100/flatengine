#!/usr/bin/env python3
"""
Manage pending dark sets: dark-flats and darks for lights.
(`flatengine darks` and `flatengine darkflats` are the same command.)

  flatengine darks                     list pending dark sets
  flatengine darks --add 1 30 --like FILE.fits
                                       request sets for these exposures
                                       in the camera state of FILE.fits
                                       (a light frame requests darks
                                       for lights; anything else
                                       requests dark-flats)
  flatengine darks --build             capture every pending set
  flatengine darks --build --exposure 120
                                       capture only that exposure

Darks are captured with the panel off and the cover closed. Build
after dark with the front end covered: every capture is checked for
light leaks and camera state before it is registered in the
calibration catalog.
"""

from pathlib import Path
import argparse
import importlib.util
import json
import sys

from astropy.io import fits

import flat_common as fc


SCRIPT_DIR = Path(__file__).resolve().parent
EXECUTOR = SCRIPT_DIR / "flats-execute.py"


def load_executor():
    spec = importlib.util.spec_from_file_location(
        "flats_execute",
        EXECUTOR,
    )

    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load executor: {EXECUTOR}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def load_config():
    return json.loads(fc.CONFIG_FILE.read_text())


def list_pending():
    pending = fc.load_pending()

    print()
    print("=" * 72)
    print("PENDING DARK SETS")
    print("=" * 72)

    if not pending:
        print("None. Every requested library has been built.")
        print("=" * 72)
        return pending

    for index, record in enumerate(
        sorted(
            pending.values(),
            key=lambda r: float(r["exposure"]),
        ),
        start=1,
    ):
        print(f"{index}. {fc.describe_pending(record)}")
        print(f"   reason:    {record.get('reason', 'missing')}")
        print(f"   camera:    {record.get('camera')}")
        print(f"   requested: {record.get('first_requested')}"
              f"  sessions: {', '.join(record.get('sessions', [])) or '-'}")
        print(f"   library:   {record['library_dir']}")

    print("=" * 72)
    print("Build after dark, front end covered: "
          "flatengine darks --build")

    return pending


def add_requests(config, exposures, like):
    with fits.open(like, memmap=False) as hdul:
        hdr = hdul[0].header.copy()

    if hdr.get("INSTRUME") is None or hdr.get("GAIN") is None \
            or hdr.get("OFFSET") is None:
        raise RuntimeError(
            f"{like} lacks INSTRUME, GAIN or OFFSET; "
            "use a frame captured by the imaging camera."
        )

    for_lights = fc.is_light_frame(hdr)

    if for_lights:
        kind = "light dark"
        root = fc.dark_library_root(config)
        count = int(config.get("calibration_catalog", {})
                    .get("light_dark_frames", 30))
    else:
        kind = "dark-flat"
        root = fc.darkflat_library_root(config)
        lib_cfg = config.get("darkflat_library", {})
        count = int(lib_cfg.get(
            "frames", config["frames"]["flats_per_group"]))

    for exposure in exposures:
        key, record = fc.dark_record_from_header(
            hdr, config, exposure=exposure,
        )

        _, problem = fc.catalog_check(
            key, count, config, firmware=record.get("firmware"),
        )

        if problem is None:
            print(f"Already cataloged: {exposure:g} s  {key}")
            continue

        pending, _ = fc.record_pending_darkflats(
            root / key,
            dict(record, count=count, kind=kind, reason=problem,
                 catalog_key=key, profile="manual request"),
            session="manual",
            repeat_hours=1e9,
        )

        print(f"Requested: {fc.describe_pending(pending)}")


def verify_camera_state(record, path, step):
    """Refuse to register frames shot in a different camera state."""
    with fits.open(path, memmap=False) as hdul:
        hdr = hdul[0].header

    problems = []

    for key, header_key in (
        ("gain", "GAIN"),
        ("offset", "OFFSET"),
    ):
        if hdr.get(header_key) is None or record.get(key) is None:
            continue  # driver does not record it; nothing to compare

        if float(hdr[header_key]) != float(record[key]):
            problems.append(
                f"{header_key}={hdr.get(header_key)} "
                f"(library needs {record[key]})"
            )

    if record.get("lownoise") not in (None, ""):
        if str(hdr.get("LOWNOISE")) != str(record["lownoise"]):
            problems.append(
                f"LOWNOISE={hdr.get('LOWNOISE')} "
                f"(library needs {record['lownoise']})"
            )

    band = record.get("temperature_band_c")

    if band is not None and hdr.get("CCD-TEMP") is not None:
        actual = round(float(hdr["CCD-TEMP"]) / step) * step

        if actual != float(band):
            problems.append(
                f"CCD-TEMP={hdr['CCD-TEMP']} "
                f"(library needs about {band} C)"
            )

    return problems


def build(config, exposure_filter, assume_yes):
    pending = fc.load_pending()

    records = [
        record
        for record in sorted(
            pending.values(),
            key=lambda r: float(r["exposure"]),
        )
        if exposure_filter is None
        or abs(float(record["exposure"]) - exposure_filter) < 1e-6
    ]

    if not records:
        print("Nothing pending to build.")
        return

    total = sum(
        float(r["exposure"]) * int(r.get("count", 30))
        for r in records
    )

    print()
    print("DARK SETS TO BUILD")

    for record in records:
        print(f"  {fc.describe_pending(record)}")

    print(f"Estimated exposure time: {total / 60:.1f} min")
    print()
    print("Set the camera to the state listed (gain, offset, "
          "low noise, set-point) before continuing.")

    if not assume_yes:
        answer = input(
            "Is it dark and is the front end covered? "
            "Type YES to continue: "
        )

        if answer != "YES":
            print("Cancelled.")
            return

    ex = load_executor()
    ex.configure_runtime(config)

    lib_cfg = config.get("darkflat_library", {})
    step = float(lib_cfg.get("temperature_step_c", 5))
    threshold = float(lib_cfg.get("leak_threshold_adu", 5.0))

    ex.ensure_cover_closed()
    ex.light_off()

    built = []

    for record in records:
        library_dir = Path(record["library_dir"])
        exposure = float(record["exposure"])
        count = int(record.get("count", 30))

        print()
        print("=" * 72)
        print(f"BUILDING {fc.describe_pending(record)}")
        print("=" * 72)

        dark_dir = ex.build_darkflat_queue(
            session="library",
            profile="library",
            gain=record["gain"],
            offset=record["offset"],
            xbin=record["xbin"],
            ybin=record["ybin"],
            rotsteps=None,
            exposure=exposure,
            config=config,
            dark_dir=library_dir,
            count=count,
        )

        started = ex.run_queue(ex.DARKFLAT_QUEUE, "DarkFlatLibrary")

        files = ex.verify_new_darkflats(
            dark_dir, count, started, "library", exposure,
            stamp_session=False,
        )

        problems = verify_camera_state(record, files[0], step)

        if problems:
            raise RuntimeError(
                "CAMERA STATE MISMATCH; library NOT registered:\n"
                + "\n".join(problems)
            )

        with fits.open(files[0], memmap=False) as hdul:
            leak = fc.light_leak_report(hdul[0].data, threshold)

        print(
            f"Leak check: channel spread {leak['channel_spread_adu']} "
            f"ADU, center minus corners "
            f"{leak['center_minus_corners_adu']} ADU"
        )

        if leak["suspected"]:
            fc.notify(
                config,
                "FlatEngine: light leak in dark-flats",
                f"{exposure:g} s library rejected; cover the scope "
                "or wait for dark and rebuild.",
                priority=1,
            )

            raise RuntimeError(
                "LIGHT LEAK SUSPECTED; library NOT registered:\n"
                f"{library_dir}"
            )

        fc.write_library_manifest(library_dir, files, exposure, leak)

        with fits.open(files[0], memmap=False) as hdul:
            key, cat_record = fc.dark_record_from_header(
                hdul[0].header, config, exposure=exposure,
            )

        fc.catalog_register(
            record.get("catalog_key") or key,
            dict(
                cat_record,
                count=len(files),
                location=str(library_dir),
                files=[str(f) for f in files],
                source="flatengine",
            ),
        )

        fc.clear_pending(library_dir)
        built.append(record)

        print(f"Library registered: {library_dir}")

    fc.notify(
        config,
        "FlatEngine: dark sets built",
        "\n".join(fc.describe_pending(r) for r in built),
    )


def main():
    parser = argparse.ArgumentParser(
        description="List, request and build dark sets (darks and dark-flats).",
    )

    parser.add_argument("--build", action="store_true",
                        help="Capture pending libraries.")
    parser.add_argument("--exposure", type=float, default=None,
                        help="With --build, only this exposure.")
    parser.add_argument("--add", type=float, nargs="+", metavar="SECONDS",
                        help="Request libraries for these exposures.")
    parser.add_argument("--like", type=Path,
                        help="With --add: FITS frame whose camera "
                             "state the library must match.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the darkness confirmation.")
    parser.add_argument("--test-notify", action="store_true",
                        help="Send a test Pushover message.")

    args = parser.parse_args()
    config = load_config()

    if args.test_notify:
        ok = fc.notify(config, "FlatEngine test",
                       "Pushover notifications are working.")
        raise SystemExit(0 if ok else 1)

    if args.add:
        if args.like is None:
            raise RuntimeError("--add needs --like FRAME.fits")

        add_requests(config, args.add, args.like)

    if args.build:
        build(config, args.exposure, args.yes)
    else:
        list_pending()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print()
        print("=" * 72)
        print("DARK BUILD ABORTED")
        print("=" * 72)
        print(str(exc).strip())
        print("=" * 72)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print()
        print("Cancelled.")
        raise SystemExit(130)
