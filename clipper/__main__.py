import sys
from clipper.sysutil import WIN, VERSION  # importing this also runs refresh_paths() and preload_cuda()

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
    from clipper.ui.app import App
    App().mainloop()

if __name__ == "__main__":
    main()
