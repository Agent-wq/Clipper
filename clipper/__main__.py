import sys
from clipper.sysutil import WIN, VERSION, FROZEN  # importing this also runs refresh_paths() and preload_cuda()

if FROZEN and len(sys.argv) >= 3 and sys.argv[1] == "-m":
    # Inside the exe there is no Python to run "python -m yt_dlp" or "python -m pip".
    # The exe runs the module itself, so these child processes never open another Clipper window.
    import os, runpy
    for _n in ("stdout", "stderr"):
        if getattr(sys, _n) is None: setattr(sys, _n, open(os.devnull, "w"))
    _mod = sys.argv[2]
    sys.argv = [_mod] + sys.argv[3:]
    runpy.run_module(_mod, run_name="__main__", alter_sys=True)
    sys.exit(0)

if not (WIN or sys.platform.startswith("linux")):
    raise SystemExit("Clipper supports Windows and Linux.")

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(errors="replace")
    except Exception: pass

if any(a in sys.argv for a in ("--version", "-V")):
    print(f"Clipper {VERSION}")
    sys.exit(0)

if any(a in sys.argv for a in ("--check", "--install", "--script", "--models")):
    from clipper.setup import cli, cli_models
    if "--models" in sys.argv: cli_models(sys.argv)
    else: cli(sys.argv)
    sys.exit(0)

def main():
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        from clipper.qt.app import run
    except ImportError as e:
        if "PySide6" not in str(e) and "shiboken" not in str(e): raise
        raise SystemExit(f"The window needs PySide6 ({e}).\nInstall it with:  python3 -m pip install --user PySide6-Essentials\n"
                         "Or install everything at once:  python3 run.py --install")
    sys.exit(run())

if __name__ == "__main__":
    main()
