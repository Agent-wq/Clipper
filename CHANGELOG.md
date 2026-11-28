# Changelog

## 2.2.0
- Changed: new Qt interface (black theme, orange accent) replaces the Tkinter one
- Removed: Tkinter dependency
- Changed: Linux needs the xcb-cursor library (Arch: `xcb-util-cursor`)

## 2.1.0
- Added: Drop down menu to select fonts which are installed on the system
- Changed: Instead of black bars in face cam it is now blur
- Misc: Bunch of other changes and fixes
## 2.0.1
- Added: Working face tracking is added split needs to be turned off for it to work and face tracking needs to be turned on

## 2.0.0
- Added: Face tracking is added split needs to be turned off for it to work and face tracking needs to be turned on(Doesn't work yet)
  
## 1.2.0
- Changed: Now user is able to upload clips anytime and seperately by selecting which clips to upload

## 1.1.1
- Internal: removed unused imports, added unit tests and lint checks to the build
- Changed: program lookup no longer re-reads the PATH before every command

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
