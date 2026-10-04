#!/usr/bin/env python3

from pathlib import Path
from datetime import datetime, timedelta
import argparse
import importlib.util
import json
import subprocess
import sys

from astropy.io import fits

import flat_common as fc


SCRIPT_DIR = Path(__file__).resolve().parent

EXECUTOR = (
    SCRIPT_DIR
    / "flats-execute.py"
)

PICTURES = fc.PICTURES


def load_executor():
    spec = importlib.util.spec_from_file_location(
        "flats_execute",
        EXECUTOR,
    )

    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"Could not load executor: {EXECUTOR}"
        )

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def session_from_dateobs(date_obs):
    dt = datetime.fromisoformat(
        str(date_obs).replace("Z", "+00:00")
    )

    return (
        dt - timedelta(hours=12)
    ).strftime("%Y%m%d")


def latest_light_session():
    sessions = []

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

        session = hdr.get("SESSION")

        if not session:
            date_obs = hdr.get("DATE-OBS")

            if not date_obs:
                continue

            session = session_from_dateobs(
                date_obs
            )

        sessions.append(str(session))

    if not sessions:
        raise RuntimeError(
            "No light-frame sessions found."
        )

    return sorted(set(sessions))[-1]


def manifest_complete(
    path,
    session,
    profile,
    rotsteps,
):
    if not path.exists():
        return False

    try:
        data = json.loads(
            path.read_text()
        )
    except Exception:
        return False

    return (
        data.get("status") == "complete"
        and str(data.get("session")) == session
        and data.get("profile") == profile
        and (
            (
                data.get("rotsteps") is None
                and rotsteps is None
            )
            or (
                data.get("rotsteps") is not None
                and rotsteps is not None
                and abs(
                    float(data.get("rotsteps"))
                    - float(rotsteps)
                ) < 1e-6
            )
        )
    )


def read_manifest_summary(manifest_path):
    try:
        data = json.loads(
            Path(manifest_path).read_text()
        )
    except Exception:
        return None

    return {
        "session": data.get("session"),
        "profile": data.get("profile"),
        "rotsteps": data.get("rotsteps"),
        "exposure": data.get("exposure"),
        "flat_count": len(
            data.get("flats", [])
        ),
        "darkflat_count": len(
            data.get("darkflats", [])
        ),
        "manifest": str(manifest_path),
    }


def print_nightly_summary(
    session,
    completed,
    skipped,
    failures=0,
):
    print()
    print("=" * 72)
    print("NIGHTLY CALIBRATION SUMMARY")
    print("=" * 72)
    print(f"Session: {session}")
    print()

    rows = []

    for item in completed:
        row = dict(item)
        row["result"] = "COMPLETE"
        rows.append(row)

    for item in skipped:
        row = dict(item)
        row["result"] = "SKIPPED"
        rows.append(row)

    rows.sort(
        key=lambda row: (
            str(
                row.get(
                    "profile",
                    ""
                )
            ).lower(),
            float(
                row["rotsteps"]
            )
            if row.get(
                "rotsteps"
            ) is not None
            else float("-inf"),
        )
    )

    for row in rows:
        print("-" * 72)
        print(
            f"Profile:       {row.get('profile')}"
        )

        rotsteps = row.get("rotsteps")

        print(
            "ROTSTEPS:      "
            + (
                str(rotsteps)
                if rotsteps is not None
                else "ignored"
            )
        )

        exposure = row.get("exposure")

        if exposure is not None:
            print(
                f"Exposure:      "
                f"{float(exposure):.7f} s"
            )
        else:
            print(
                "Exposure:      unavailable"
            )

        print(
            f"Flats:         "
            f"{row.get('flat_count', 0)}"
        )

        print(
            f"Dark-flats:    "
            f"{row.get('darkflat_count', 0)}"
        )

        print(
            f"Result:        "
            f"{row.get('result')}"
        )

        print(
            f"Manifest:      "
            f"{row.get('manifest')}"
        )

    print()
    print("-" * 72)
    print(
        f"Groups complete: "
        f"{len(completed)}"
    )
    print(
        f"Groups skipped:  "
        f"{len(skipped)}"
    )
    print(
        f"Failures:        "
        f"{failures}"
    )
    print("=" * 72)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run nightly flat calibration groups "
            "one at a time."
        )
    )

    parser.add_argument(
        "--session",
        help=(
            "Observing-night session ID. "
            "Defaults to latest light session."
        ),
    )

    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the overall confirmation.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the nightly run plan only.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Run groups even if a complete "
            "manifest already exists."
        ),
    )

    args = parser.parse_args()

    ex = load_executor()

    session = (
        args.session
        if args.session
        else latest_light_session()
    )

    config = ex.load_json(ex.CONFIG)

    groups = fc.find_session_groups(
        PICTURES,
        session,
        use_rotsteps=config[
            "capabilities"
        ].get(
            "use_rotsteps_if_present",
            True,
        ),
    )

    if not groups:
        raise RuntimeError(
            f"No light groups found for session {session}."
        )

    group_entries = []

    for key, lights in groups.items():
        profile = key[0]
        rotsteps = key[6]

        group_entries.append(
            (
                profile,
                rotsteps,
                key,
                lights,
            )
        )

    # Prefer the currently installed manual filter, then
    # keep all rotation groups for the same filter together.
    try:
        _, current_filter = (
            ex.get_manual_filter_state()
        )
    except Exception:
        current_filter = None

    group_entries.sort(
        key=lambda entry: (
            0
            if entry[0] == current_filter
            else 1,
            entry[0].lower(),
            float(entry[1])
            if entry[1] is not None
            else float("-inf"),
        )
    )

    run_list = []
    skip_list = []
    skipped_summaries = []

    for (
        profile,
        rotsteps,
        key,
        lights,
    ) in group_entries:
        group_root = ex.build_group_root(
            config,
            session,
            profile,
            rotsteps,
        )

        manifest = (
            group_root
            / "manifest.json"
        )

        if (
            not args.force
            and manifest_complete(
                manifest,
                session,
                profile,
                rotsteps,
            )
        ):
            skip_list.append(
                (
                    profile,
                    len(lights),
                    rotsteps,
                    manifest,
                )
            )

            summary = read_manifest_summary(
                manifest
            )

            if summary is not None:
                skipped_summaries.append(
                    summary
                )
        else:
            run_list.append(
                (
                    profile,
                    len(lights),
                    rotsteps,
                )
            )

    print()
    print("=" * 72)
    print("NIGHTLY FLAT RUN")
    print("=" * 72)
    print(f"Session:        {session}")
    print(f"Light groups:   {len(groups)}")
    print(f"Groups to run:  {len(run_list)}")
    print(f"Groups skipped: {len(skip_list)}")

    if current_filter:
        print(
            f"Current filter: {current_filter}"
        )

    print()

    if run_list:
        print("RUN ORDER")

        for index, (
            profile,
            light_count,
            rotsteps,
        ) in enumerate(
            run_list,
            start=1,
        ):
            rotation = (
                str(rotsteps)
                if rotsteps is not None
                else "ignored"
            )

            print(
                f"  {index}. {profile}"
                f"  lights={light_count}"
                f"  ROTSTEPS={rotation}"
            )

    if skip_list:
        print()
        print("ALREADY COMPLETE")

        for (
            profile,
            light_count,
            rotsteps,
            manifest,
        ) in skip_list:
            rotation = (
                str(rotsteps)
                if rotsteps is not None
                else "ignored"
            )

            print(
                f"  {profile}"
                f"  lights={light_count}"
                f"  ROTSTEPS={rotation}"
                f"  manifest={manifest}"
            )

    print()
    print("=" * 72)

    if args.dry_run:
        print(
            "DRY RUN ONLY. "
            "No hardware was changed."
        )
        return

    if not run_list:
        print(
            "Nothing to do. "
            "All groups are complete."
        )

        print_nightly_summary(
            session=session,
            completed=[],
            skipped=skipped_summaries,
            failures=0,
        )

        return

    if not args.yes:
        answer = input(
            "Run all listed groups? "
            "Type YES to continue: "
        )

        if answer != "YES":
            print("Cancelled.")
            return

    completed = []
    completed_summaries = []

    for index, (
        profile,
        light_count,
        rotsteps,
    ) in enumerate(
        run_list,
        start=1,
    ):
        print()
        print("#" * 72)
        print(
            f"GROUP {index}/{len(run_list)}: "
            f"{profile}"
        )
        print("#" * 72)

        cmd = [
            sys.executable,
            str(EXECUTOR),
            "--session",
            session,
            "--profile",
            profile,
            "--yes",
        ]

        if rotsteps is not None:
            cmd.extend(
                [
                    "--rotsteps",
                    str(rotsteps),
                ]
            )

        result = subprocess.run(
            cmd
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Calibration group {profile!r} "
                f"failed with exit code "
                f"{result.returncode}.\n"
                "Nightly processing stopped. "
                "Remaining groups were NOT run."
            )

        completed.append(profile)

        group_root = ex.build_group_root(
            config,
            session,
            profile,
            rotsteps,
        )

        manifest = (
            group_root
            / "manifest.json"
        )

        summary = read_manifest_summary(
            manifest
        )

        if summary is None:
            raise RuntimeError(
                f"Group {profile!r} completed but "
                f"manifest could not be read: {manifest}"
            )

        completed_summaries.append(
            summary
        )

    print()
    print("=" * 72)
    print("NIGHTLY CALIBRATION COMPLETE")
    print("=" * 72)

    for profile in completed:
        print(f"  PASS: {profile}")

    print("=" * 72)

    print_nightly_summary(
        session=session,
        completed=completed_summaries,
        skipped=skipped_summaries,
        failures=0,
    )


if __name__ == "__main__":
    try:
        main()

    except RuntimeError as e:
        print()
        print("=" * 72)
        print("NIGHTLY CALIBRATION ABORTED")
        print("=" * 72)
        print(str(e).strip())
        print("=" * 72)
        raise SystemExit(2)

    except KeyboardInterrupt:
        print()
        print("Nightly calibration cancelled.")
        raise SystemExit(130)
