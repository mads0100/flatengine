#!/usr/bin/env python3

from pathlib import Path
from datetime import datetime
import json
import shutil

import flat_common as fc


CONFIG = fc.CONFIG_FILE


DEFAULTS = {
    "session": {
        "id_mode": "local_date_minus_12h",
        "keyword": "SESSION",
        "include_in_filename": True,
    },

    "flat_target": {
        "mode": "fraction",
        # Ekos ADU target used only by `flatengine calibrate`.
        "fraction": 0.50,
        "tolerance_fraction": 0.05,
        # Nightly tuning targets the brightest Bayer channel.
        "brightest_channel_fraction": 0.64,
        "saturation_cap_fraction": 0.80,
        "weakest_channel_min_adu": 1000,
        "max_tuning_attempts": 8,
    },

    "exposure_policy": {
        "preferred_seconds": 1.0,
        # Rolling-shutter guard: never shoot flats shorter.
        "minimum_seconds": 1.0,
        "maximum_preferred_seconds": 5.0,
        "if_longer_required": "accept_and_warn",
        # Standard flat exposures. The panel is tuned at the
        # shortest step; longer steps are used only when the
        # panel cannot get bright enough (narrowband filters).
        "ladder_seconds": [1, 2, 3, 5, 10, 20, 30, 60],
    },

    "darkflat_library": {
        "enabled": True,
        # "notify": record a missing library, send a Pushover
        # message and continue without dark-flats; build later
        # with `flatengine darkflats --build`.
        # "capture": shoot the missing library immediately.
        "on_missing": "notify",
        # Empty means <output root>/DarkFlatLibrary.
        "root": "",
        "frames": 30,
        "temperature_step_c": 5,
        "leak_threshold_adu": 5.0,
    },

    "frames": {
        # Minimum flats per group.
        "flats_per_group": 30,
        # The count rises until the WEAKEST Bayer channel's master
        # flat reaches flat_min_snr, up to flats_max_per_group.
        "flats_max_per_group": 100,
        "flat_min_snr": 200,
        # Camera conversion gain at the flat gain setting
        # (SkyEye45AC-plus HCG gain 100: 0.91).
        "e_per_adu": 1.0,
        "pedestal_adu": 0,
        "darkflats_enabled": True,
    },

    "panel": {
        "mode": "controllable",
        "device": "Flip Flat",
        "can_power": True,
        "can_set_brightness": True,
        "brightness_min": 1,
        "brightness_max": 255,
        "brightness_step": 1,
    },

    "notify": {
        # Pushover credentials. FLATENGINE_PUSHOVER_USER and
        # FLATENGINE_PUSHOVER_TOKEN environment variables also work.
        "pushover": {
            "enabled": True,
            "user_key": "",
            "app_token": "",
        },
        # Re-send a pending dark-flat reminder at most this often.
        "repeat_hours": 24,
    },

    "capabilities": {
        "use_filter_if_present": True,
        "use_rotsteps_if_present": True,
        "require_rotsteps": False,
    },

    "output": {
        "root": str(
            fc.PICTURES
            / "Calibration"
        ),
        "layout": "session_profile_tree",
        "pixinsight_session_keyword": True,
        "siril_session_directories": True,
    },

    "naming": {
        "flat_pattern": (
            "{session}_{profile}_G{gain}_O{offset}_"
            "B{xbin}x{ybin}{rotation}_Flat_{seq:03d}.fits"
        ),
        "darkflat_pattern": (
            "{session}_{profile}_G{gain}_O{offset}_"
            "B{xbin}x{ybin}_E{exposure}_DarkFlat_{seq:03d}.fits"
        ),
    },
}


def ask_text(prompt, default):
    raw = input(
        f"{prompt} [{default}]: "
    ).strip()

    return raw if raw else default


def ask_int(prompt, default, minimum=None):
    while True:
        raw = input(
            f"{prompt} [{default}]: "
        ).strip()

        if not raw:
            return default

        try:
            value = int(raw)
        except ValueError:
            print("Enter a whole number.")
            continue

        if (
            minimum is not None
            and value < minimum
        ):
            print(
                f"Value must be at least {minimum}."
            )
            continue

        return value


def ask_float(
    prompt,
    default,
    minimum=None,
    maximum=None,
):
    while True:
        raw = input(
            f"{prompt} [{default}]: "
        ).strip()

        if not raw:
            return default

        try:
            value = float(raw)
        except ValueError:
            print("Enter a number.")
            continue

        if (
            minimum is not None
            and value < minimum
        ):
            print(
                f"Value must be >= {minimum}."
            )
            continue

        if (
            maximum is not None
            and value > maximum
        ):
            print(
                f"Value must be <= {maximum}."
            )
            continue

        return value


def ask_bool(prompt, default=True):
    suffix = "Y/n" if default else "y/N"

    while True:
        raw = input(
            f"{prompt} [{suffix}]: "
        ).strip().lower()

        if not raw:
            return default

        if raw in ("y", "yes"):
            return True

        if raw in ("n", "no"):
            return False

        print("Enter y or n.")


def ask_choice(
    prompt,
    choices,
    default,
):
    print()
    print(prompt)

    for index, choice in enumerate(
        choices,
        start=1,
    ):
        marker = (
            " (default)"
            if choice == default
            else ""
        )

        print(
            f"  {index}. {choice}{marker}"
        )

    while True:
        raw = input(
            f"Choose 1-{len(choices)}: "
        ).strip()

        if not raw:
            return default

        try:
            index = int(raw)
        except ValueError:
            print("Enter a number.")
            continue

        if 1 <= index <= len(choices):
            return choices[index - 1]

        print("Invalid choice.")


def load_existing():
    if not CONFIG.exists():
        return None

    try:
        return json.loads(
            CONFIG.read_text()
        )
    except Exception:
        return None


def backup_existing():
    if not CONFIG.exists():
        return None

    stamp = datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )

    backup = CONFIG.with_name(
        f"flat-config.{stamp}.bak.json"
    )

    shutil.copy2(
        CONFIG,
        backup
    )

    return backup


def main():
    print()
    print("=" * 72)
    print("FLAT ENGINE SETUP")
    print("=" * 72)
    print()
    print(
        "Press Enter to accept the value shown in brackets."
    )
    print()

    existing = load_existing()

    if existing:
        print(
            f"Existing config found: {CONFIG}"
        )
        use_existing = ask_bool(
            "Use existing values as defaults?",
            True,
        )
    else:
        use_existing = False

    config = (
        existing
        if use_existing
        else json.loads(
            json.dumps(DEFAULTS)
        )
    )

    print()
    print("GENERAL")
    print("-" * 72)

    config["output"]["root"] = ask_text(
        "Calibration output root",
        config["output"]["root"],
    )

    config["frames"]["flats_per_group"] = ask_int(
        "Flats per group",
        config["frames"]["flats_per_group"],
        minimum=1,
    )

    config["frames"]["darkflats_enabled"] = ask_bool(
        "Capture matching dark-flats?",
        config["frames"]["darkflats_enabled"],
    )

    print()
    print("FLAT TARGET")
    print("-" * 72)

    config["flat_target"]["fraction"] = ask_float(
        "Target fraction of full scale",
        config["flat_target"]["fraction"],
        minimum=0.05,
        maximum=0.95,
    )

    config["flat_target"][
        "tolerance_fraction"
    ] = ask_float(
        "ADU tolerance fraction",
        config["flat_target"][
            "tolerance_fraction"
        ],
        minimum=0.001,
        maximum=0.50,
    )

    print()
    print("EXPOSURE POLICY")
    print("-" * 72)

    config["exposure_policy"][
        "preferred_seconds"
    ] = ask_float(
        "Preferred flat exposure (seconds)",
        config["exposure_policy"][
            "preferred_seconds"
        ],
        minimum=0.001,
    )

    config["exposure_policy"][
        "minimum_seconds"
    ] = ask_float(
        "Minimum flat exposure (rolling-shutter guard; 1.0 recommended)",
        config["exposure_policy"][
            "minimum_seconds"
        ],
        minimum=0.001,
    )

    config["exposure_policy"][
        "maximum_preferred_seconds"
    ] = ask_float(
        "Maximum preferred flat exposure",
        config["exposure_policy"][
            "maximum_preferred_seconds"
        ],
        minimum=0.001,
    )

    panel_mode = ask_choice(
        "Flat panel type",
        [
            "controllable",
            "fixed",
            "manual",
        ],
        config["panel"]["mode"],
    )

    config["panel"]["mode"] = panel_mode

    print()
    print("PANEL")
    print("-" * 72)

    if panel_mode == "controllable":
        config["panel"]["device"] = ask_text(
            "INDI panel device name",
            config["panel"].get(
                "device",
                "Flip Flat",
            ),
        )

        config["panel"]["can_power"] = True
        config["panel"][
            "can_set_brightness"
        ] = True

        config["panel"][
            "brightness_min"
        ] = ask_int(
            "Minimum brightness value",
            config["panel"].get(
                "brightness_min",
                0,
            ),
        )

        config["panel"][
            "brightness_max"
        ] = ask_int(
            "Maximum brightness value",
            config["panel"].get(
                "brightness_max",
                255,
            ),
        )

    elif panel_mode == "fixed":
        config["panel"]["can_power"] = ask_bool(
            "Can software turn the panel on/off?",
            config["panel"].get(
                "can_power",
                True,
            ),
        )

        config["panel"][
            "can_set_brightness"
        ] = False

    else:
        config["panel"]["can_power"] = False
        config["panel"][
            "can_set_brightness"
        ] = False

    print()
    print("OPTIONAL METADATA")
    print("-" * 72)

    config["capabilities"][
        "use_filter_if_present"
    ] = ask_bool(
        "Use FILTER metadata when present?",
        config["capabilities"][
            "use_filter_if_present"
        ],
    )

    config["capabilities"][
        "use_rotsteps_if_present"
    ] = ask_bool(
        "Use ROTSTEPS metadata when present?",
        config["capabilities"][
            "use_rotsteps_if_present"
        ],
    )

    config["capabilities"][
        "require_rotsteps"
    ] = False

    config["session"][
        "id_mode"
    ] = "local_date_minus_12h"

    config["session"][
        "keyword"
    ] = "SESSION"

    config["output"][
        "layout"
    ] = "session_profile_tree"

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(
        json.dumps(
            config,
            indent=2,
        )
    )
    print("=" * 72)
    print()

    if not ask_bool(
        "Save this configuration?",
        True,
    ):
        print("No changes written.")
        return

    CONFIG.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    backup = backup_existing()

    CONFIG.write_text(
        json.dumps(
            config,
            indent=2,
        )
        + "\n"
    )

    print()
    print(
        f"Configuration saved: {CONFIG}"
    )

    if backup:
        print(
            f"Previous config backed up: {backup}"
        )

    print()
    print("Setup complete.")


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print("Setup cancelled.")
        raise SystemExit(130)
