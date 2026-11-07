# Provenance

XpengUSB is an independent adaptation of [marcone/teslausb](https://github.com/marcone/teslausb), imported on 7 November 2026 at commit `fb4ed15b46dc41a2ec7c8a7a2fe0783ce1d2ffd9` (branch `main-dev`). Full ancestor history and the original MIT license are retained.

The original project credits cimryan and the community behind its initial Reddit implementation. Copyright (c) 2018 cimryan remains in `LICENSE`. Contributions by marcone and other upstream contributors are preserved in git history. New XpengUSB adaptations are distributed under the same MIT license.

This revision replaces the Tesla-specific installer, filename index, archive loop, cleanup policies, web viewer and vehicle controls with a smaller G6-focused implementation. USB mass-storage configfs setup and independent copy-on-write image snapshots are derived from the upstream design. Historical files can be inspected with `git show fb4ed15b46dc41a2ec7c8a7a2fe0783ce1d2ffd9:PATH`.

XPENG, Tesla and Raspberry Pi names belong to their respective owners. This project is not affiliated with or endorsed by them. Historical attribution is intentional and must not be renamed.
