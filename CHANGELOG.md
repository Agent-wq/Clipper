# Changelog
## 1.1.0
- Added: old cached downloads and transcripts are removed automatically after 14 days unused (setting: "Delete cached videos unused for", 0 turns it off)

## 1.0.1
- Fixed: ffmpeg capability checks no longer time out after 6 s on Linux
- Fixed: the GPU encoder is tested once per job instead of failing once per clip
- Changed: Escape now asks before cancelling a running job
- Removed: duplicate command-line handling and duplicate imports
- Build: pinned dependency versions, GitHub Release created from version tags

## 1.0.0
- Baseline
