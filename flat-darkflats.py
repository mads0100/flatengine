#!/usr/bin/env python3
"""
Manage the dark-flat library.

  flatengine darkflats                 list pending libraries
  flatengine darkflats --add 1 30 --like FILE.fits
                                       request libraries for these
                                       exposures, using the camera
                                       state recorded in FILE.fits
  flatengine darkflats --build         capture every pending library
  flatengine darkflats --build --exposure 30
                                       capture only that exposure

Dark-flats are captured with the panel off and the cover closed.
Build libraries after dark with the front end covered: every
capture is checked for light leaks and is not registered if one
is found.
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
    print("PENDING DARK-FLAT LIBRARIES")
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
        print(f"   camera:    {record.get('camera')}")
        print(f"   requested: {record.get('first_requested')}"
              f"  sessions: {', '.join(record.get('sessions', [])) or '-'}")
        print(f"   library:   {record['library_dir']}")

    print("=" * 72)
    print("Build after dark, front end covered: "
          "flatengine darkflats --build")

    return pending


def add_requests(config, exposures, like):
    with fits.open(like, memmap=False) as hdul:
        hdr = hdul[0].header

    lib_cfg = config.get("darkflat_library", {})
    step = float(lib_cfg.get("temperature_step_c", 5))
    count = int(lib_cfg.get("frames", config["frames"]["flats_per_group"]))

    camera = hdr.get("INSTRUME")
    gain = hdr.get("GAIN")
    offset = hdr.get("OFFSET")
    xbin = hdr.get("XBINNING", 1)
    ybin = hdr.get("YBINNING", 1)
    lownoise = hdr.get("LOWNOISE")
    ccd_temp = hdr.get("CCD-TEMP")

    if camera is None or gain is None or offset is None:
        raise RuntimeError(
            f"{like} lacks INSTRUME, GAIN or OFFSET; "
            "use a frame captured by the imaging camera."
        )

    for exposure in exposures:
        library_dir = fc.darkflat_library_dir(
            config, camera, gain, offset, xbin, ybin, exposure,
            lownoise=lownoise, ccd_temp=ccd_temp,
        )

        if fc.library_files(library_dir, count):
            print(f"Already built: {exposure:g} s  {library_dir}")
            continue

        record, _ = fc.record_pending_darkflats(
            library_dir,
            {
                "camera": camera,
                "gain": gain,
                "offset": offset,
                "xbin": xbin,
                "ybin": ybin,
                "exposure": float(exposure),
                "lownoise": lownoise,
                "temperature_band_c": (
                    None if ccd_temp is None
                    else round(float(ccd_temp) / step) * step
                ),
                "count": count,
                "profile": "manual request",
            },
            session="manual",
            repeat_hours=1e9,
        )

        print(f"Requested: {fc.describe_pending(record)}")


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
    print("DARK-FLAT LIBRARIES TO BUILD")

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
        fc.clear_pending(library_dir)
        built.append(record)

        print(f"Library registered: {library_dir}")

    fc.notify(
        config,
        "FlatEngine: dark-flat libraries built",
        "\n".join(fc.describe_pending(r) for r in built),
    )


def main():
    parser = argparse.ArgumentParser(
        description="List, request and build dark-flat libraries.",
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
        print("DARK-FLAT LIBRARY ABORTED")
        print("=" * 72)
        print(str(exc).strip())
        print("=" * 72)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print()
        print("Cancelled.")
        raise SystemExit(130)
