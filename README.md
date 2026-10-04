# FlatEngine

FlatEngine automates nightly astrophotography flat-field calibration around KStars/Ekos and INDI.

It discovers the light frames captured during an observing session, groups them by camera state, filter, binning, gain, offset, and rotator position, then creates matching flats and dark-flats using an automated flat panel.

It is designed to be safe, repeatable, and idempotent.

## Features

- Nightly light-frame discovery
- Session-aware grouping
- Filter-aware calibration groups
- Manual filter handling
- Automatic filter compatibility
- ADU-targeted flat probing
- Fixed-exposure production flats
- Matching dark-flats
- Manifest-based completion tracking
- Safe reruns without duplicate work
- ROTSTEPS-aware grouping
- ROTATANG verification
- KStars rotator tolerance integration
- Nightly completion summaries
- Regression test suite
- Installation diagnostics with `flatengine doctor`

## Requirements

FlatEngine expects:

- Python 3
- Astropy
- KStars / Ekos
- INDI
- `indi_getprop`
- `indi_setprop`
- `qdbus`
- `gdbus`

The installer checks these automatically.

## Installation

From the FlatEngine source directory:

    ./install.sh

Default installation layout:

    ~/.local/bin/flatengine

    ~/.local/share/flatengine/
        scripts/
        templates/

    ~/.config/flatengine/
        flat-config.json
        flat-filters.json

    ~/.local/state/flatengine/
        queues/

Existing configuration files are preserved during reinstall.

If `~/.local/bin` is not already on your PATH:

    export PATH="$HOME/.local/bin:$PATH"

Add that line to `~/.bashrc` to make it permanent.

## First-Time Setup

Run:

    flatengine setup

FlatEngine stores its main configuration in:

    ~/.config/flatengine/flat-config.json

Important site-specific settings include the Ekos optical train and flat-panel device.

For example:

    "ekos": {
      "optical_train": "Tak 106"
    }

    "panel": {
      "device": "Flip Flat"
    }

The setup tool also controls:

- calibration output root
- flat target fraction
- ADU tolerance
- preferred exposure
- exposure limits
- flat count
- dark-flat behavior
- panel brightness range
- ROTSTEPS behavior
- filename patterns

## Filter Profiles

Filter-specific panel calibration data is stored in:

    ~/.config/flatengine/flat-filters.json

Profiles may contain values such as:

- target ADU
- panel brightness
- last successful flat exposure
- camera bit depth

## Normal Nightly Workflow

### 1. Inspect the plan

    flatengine plan

Or choose a specific observing session:

    flatengine plan --session 20261003

The planner shows:

- discovered light frames
- required calibration groups
- gain
- offset
- binning
- filter profile
- ROTSTEPS
- target ADU
- output paths

No hardware is moved during planning.

### 2. Dry-run the nightly execution plan

    flatengine nightly --session 20261003 --dry-run

This shows:

- groups requiring calibration
- groups already complete
- filter ordering
- rotation groups
- manifest paths

No hardware is changed.

### 3. Run nightly calibration

    flatengine nightly --session 20261003

For each required group, FlatEngine:

1. Confirms the required filter
2. Moves the rotator if ROTSTEPS metadata exists
3. Verifies rotation using ROTATANG
4. Closes the flat panel
5. Sets panel brightness
6. Runs an ADU probe
7. Captures fixed-exposure flats
8. Captures matching dark-flats
9. Verifies FITS metadata
10. Writes a completion manifest

## Manual Filters

FlatEngine supports the Ekos Manual Filter driver.

When a filter change is required, FlatEngine waits until the Manual Filter driver reports the requested filter state before continuing.

Example:

    Current filter: L-eNh

    MANUAL FILTER CHANGE REQUIRED

    Required filter: L-Xtreme
    Current filter:  L-eNh

    Select L-Xtreme in Ekos and confirm the physical filter change.
    Waiting for the Manual Filter driver to report the requested state...

    Filter confirmed: L-Xtreme

If the required filter is already selected, no manual intervention is requested.

## Rotation-Aware Flats

FlatEngine supports the FITS keywords:

    ROTSTEPS
    ROTATANG

`ROTSTEPS` is treated as the mechanical rotator position.

`ROTATANG` is treated as angular verification metadata.

The calibration group identity uses ROTSTEPS, not ROTATANG. This avoids creating duplicate flat groups when the reported angle differs only by tiny floating-point amounts.

When rotation metadata exists, FlatEngine:

    reads recorded ROTSTEPS
        ->
    commands ABS_ROTATOR_POSITION
        ->
    waits for INDI motion completion
        ->
    reads ABS_ROTATOR_ANGLE
        ->
    compares against recorded ROTATANG
        ->
    accepts the result if it is within the KStars rotator threshold

FlatEngine reads the KStars setting:

    AstrometryRotatorThreshold

from:

    ~/.config/kstarsrc

If no user override exists, FlatEngine uses the KStars default of 30 arcminutes.

Circular angle math is used, so the 0 / 360 degree boundary is handled correctly.

## Test a Rotator Position Only

To test the rotator without taking flats:

    flatengine execute \
      --session 20261003 \
      --profile L-eNh \
      --rotsteps -4 \
      --rotator-only

This will:

- move the rotator
- wait for completion
- verify ROTATANG
- exit without capturing frames

## Completion Manifests

Each completed calibration group receives:

    manifest.json

Example:

    ~/Pictures/Calibration/
        20261003/
            L-eNh/
                manifest.json

Rotation-specific groups are stored under:

    ROT_<steps>

For example:

    ~/Pictures/Calibration/
        20261003/
            L-eNh/
                ROT_-4/
                    flats/
                    darkflats/
                    manifest.json

Manifests allow FlatEngine to safely skip already-completed work.

## Nightly Summary

After execution, FlatEngine prints a compact summary containing:

- profile
- ROTSTEPS
- production exposure
- number of flats
- number of dark-flats
- completion state
- manifest location

Example:

    NIGHTLY CALIBRATION SUMMARY

    Session: 20261003

    Profile:       L-eNh
    ROTSTEPS:      ignored
    Exposure:      0.8772838 s
    Flats:         30
    Dark-flats:    30
    Result:        COMPLETE

## Diagnostics

Run:

    flatengine doctor

This checks:

- config file
- filter-profile file
- capture template
- Pictures root
- queue directory
- required command-line tools
- Astropy
- configured optical train
- configured flat-panel device
- calibration output path
- write permissions
- INDI server visibility
- configured flat-panel visibility

A healthy installation ends with:

    DOCTOR RESULT: ALL CHECKS PASSED

## Regression Testing

Run:

    flatengine regression

The regression suite checks:

- Python syntax
- completed-session idempotence
- manifest recognition
- planner session selection
- ROTSTEPS CLI support
- rotator-only mode
- synthetic multi-rotation grouping
- planner ROTSTEPS handling
- cleanup of synthetic test data

Regression tests do not move hardware.

## Useful Commands

    flatengine help
    flatengine setup
    flatengine plan
    flatengine nightly --dry-run
    flatengine nightly
    flatengine execute --help
    flatengine calibrate --help
    flatengine regression
    flatengine doctor
    flatengine stamp --help

## Ekos Post-Capture Integration

FlatEngine includes:

    session-postcapture.py

This script is intended to be configured as the Ekos Capture
post-capture script for light frames.

Its job is only to stamp the observing-night `SESSION` keyword
into each newly captured FITS file.

It does not run flats or perform calibration during imaging.

The observing-night ID uses the same noon-rollover convention
as FlatEngine, so images captured after midnight remain part of
the previous evening's session.

Typical workflow:

    Ekos captures light
        ->
    session-postcapture.py stamps SESSION
        ->
    imaging continues normally
        ->
    shutdown queue runs flatengine nightly

If `SESSION` is absent, FlatEngine can fall back to `DATE-OBS`,
but using the post-capture script is preferred.

## Session Handling

FlatEngine prefers the FITS keyword:

    SESSION

when it is present.

For older files without SESSION, FlatEngine derives the observing-night session from DATE-OBS using a noon rollover rule so post-midnight frames remain associated with the prior evening.

## Configuration Overrides

The shared path layer supports these environment variables:

    FLATENGINE_CONFIG_DIR
    FLATENGINE_STATE_DIR
    FLATENGINE_DATA_DIR
    FLATENGINE_PICTURES

This allows non-standard installations without modifying source code.

## Safety Philosophy

FlatEngine intentionally refuses to guess when calibration state is ambiguous.

Examples:

- multiple matching rotation groups require explicit ROTSTEPS
- wrong FILTER metadata aborts the group
- missing configured optical train aborts execution
- missing panel device aborts execution
- failed rotator verification aborts capture
- failed manifests stop nightly processing

The goal is to fail loudly rather than quietly produce bad calibration data.

## Development Workflow

Before making changes:

    flatengine regression

After making changes:

    flatengine regression
    flatengine doctor

Only proceed to hardware testing after both pass.

## Current Status

FlatEngine currently supports:

- session-aware light discovery
- manual and automatic filter workflows
- filter-specific panel calibration
- ADU-targeted flat probing
- production flats
- matching dark-flats
- manifest-based reruns
- ROTSTEPS-aware grouping
- ROTATANG verification
- KStars-derived rotator tolerance
- portable configuration paths
- installable command-line tooling
- diagnostics
- regression testing

