# ───────────────────── system checks and installers (standard library only) ─────────────────────
# Kept free of tkinter and third-party imports so they also work on a brand-new machine:
#   python clipper_gui.py --version    print the version
#   python clipper_gui.py --check      show what is installed
#   python3 clipper_gui.py --models     list installed Ollama models (and test the connection)
#   python3 clipper_gui.py --install    install everything that is missing
#   python3 clipper_gui.py --script     print a setup script for this kind of system
import ctypes, importlib, importlib.metadata, importlib.util, io, json, os, platform, re, shlex, shutil, subprocess, sys, tempfile, threading, time, zipfile
import urllib.error, urllib.request
from pathlib import Path
from clipper.sysutil import VERSION, WIN, APP_BIN, FROZEN, PYLIBS, proc_kw, py_exe, refresh_paths, which
def sh(cmd, timeout=None):
    # Resolve commands through the refreshed PATH. The first launch of a program on Windows can be slow (virus scan).
    timeout = timeout or 20
    if cmd:
        resolved = which(cmd[0])
        if not resolved: return 1, ""
        cmd = [resolved] + list(cmd[1:])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, **proc_kw()); return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError): return 1, ""

def system_info():
    """(pretty system name, package manager). Linux: pacman, apt or dnf. Windows: winget, or 'direct' (Clipper downloads things itself)."""
    if WIN:
        try: build = sys.getwindowsversion().build
        except AttributeError: build = 0
        return (f"Windows {'11' if build >= 22000 else '10'} (build {build})" if build else platform.platform()), ("winget" if which("winget") else "direct")
    info = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition("=")
                info[key] = value.strip().strip('"')
    except OSError:
        pass
    ids = (info.get("ID", "") + " " + info.get("ID_LIKE", "")).lower()
    groups = (("pacman", ("arch", "cachyos", "manjaro", "endeavouros")),
              ("apt", ("debian", "ubuntu", "linuxmint", "pop")),
              ("dnf", ("fedora", "rhel", "centos")))
    return info.get("PRETTY_NAME", "Linux"), next((manager for manager, keys in groups if any(k in ids for k in keys)), None)

def mgr_label(mgr):
    return {"winget": "winget", "direct": "direct downloads (winget not found)"}.get(mgr) or mgr or "not recognised"

def find_nvidia_smi():
    exe = which("nvidia-smi")
    if exe or not WIN: return exe
    for base in (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32", Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI"):
        if (base / "nvidia-smi.exe").is_file(): return str(base / "nvidia-smi.exe")
    return None

def gpu_info():  # (name, VRAM in GB) of the first NVIDIA GPU, or None
    smi = find_nvidia_smi()
    if not smi: return None
    rc, out = sh([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    if rc or not out: return None
    name, _, mem = out.splitlines()[0].partition(",")
    try: return name.strip(), round(float(mem) / 1024, 1)
    except ValueError: return name.strip(), None

def have_mod(name):
    try: return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError): return False

def pip_ver(dist):
    try: return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError: return None

def pip_name(spec):  # "yt-dlp[default]" -> "yt-dlp"
    return re.split(r"[\[<>=!~; ]", spec, maxsplit=1)[0]

def av_supports_metadata_errors():
    """faster-whisper 1.2.x opens audio with av.open(..., metadata_errors="ignore"). PyAV 19 removed that keyword.
    True = fine, False = this PyAV rejects it, None = PyAV not installed."""
    try: import av
    except Exception: return None
    real = getattr(av.open, "_clipper_real", av.open)  # look through our own workaround below
    try: real(io.BytesIO(b""), metadata_errors="ignore")
    except TypeError as e: return "metadata_errors" not in str(e)  # rejected before it even looks at the data
    except Exception: return True                                   # any other error: the keyword itself was accepted
    return True

def ensure_av_compat():
    """Workaround for PyAV 19 + faster-whisper 1.2.x: drop the one keyword PyAV no longer accepts. Returns True if it was needed."""
    if av_supports_metadata_errors() is not False: return False
    import av
    if getattr(av.open, "_clipper_real", None): return False
    real = av.open
    def open_compat(*a, **kw):
        kw.pop("metadata_errors", None)
        return real(*a, **kw)
    open_compat._clipper_real = real
    av.open = open_compat
    return True

def is_av_mismatch(e):
    return isinstance(e, TypeError) and "metadata_errors" in str(e)

def av_mismatch_help():
    return ("The audio decoder (PyAV) and faster-whisper don't match: PyAV 19 removed an option that faster-whisper still uses. "
            "This is not a GPU or memory problem, so trying other modes would not help.\n"
            "Fix: open the Setup page and click Install on PyAV, then restart Clipper. Or run this and restart:\n  "
            + quote_cmd(pip_argv(["av<19", "faster-whisper"], upgrade=True)))

def ytdlp_cmd():
    """How to start yt-dlp: the program on PATH, or the Python module (works even when its Scripts folder is not on PATH)."""
    exe = which("yt-dlp")
    if exe: return [exe]
    return [py_exe(), "-m", "yt_dlp"] if have_mod("yt_dlp") else None

# winget ids and direct-download names are used on Windows; pkgs/manual are used on Linux.
DEPS = [
    dict(id="tk", name="tkinter", level="required", why="Draws this window.", mods=["tkinter"],
         pkgs={"pacman": ["tk"], "apt": ["python3-tk"], "dnf": ["python3-tkinter"], },
         manual_win="Run the Python installer again, choose Modify, and tick 'tcl/tk and IDLE'.", win_url="https://www.python.org/downloads/windows/"),
    dict(id="ffmpeg", name="ffmpeg", level="required", why="Cuts, crops and encodes the clips. Includes ffprobe and ffplay.", bins=["ffmpeg", "ffprobe"], vflag="-version",
         pkgs={"pacman": ["ffmpeg"], "apt": ["ffmpeg"], "dnf": ["ffmpeg"]}, winget="Gyan.FFmpeg", direct="ffmpeg", win_url="https://www.gyan.dev/ffmpeg/builds/"),
    dict(id="ytdlp", name="yt-dlp", level="required", why="Downloads videos from links. Update it when downloads start failing.", bins=["yt-dlp"], mods=["yt_dlp"], pips=["yt-dlp[default]"], updatable=True),
    dict(id="deno", name="deno", level="recommended", why="A JavaScript runtime that recent YouTube downloads need.", bins=["deno"],
         pkgs={"pacman": ["deno"]}, manual="curl -fsSL https://deno.land/install.sh | sh", url="https://deno.com", winget="DenoLand.Deno", direct="deno", win_url="https://deno.com"),
    dict(id="ollama", name="Ollama", level="optional", why="Runs the local AI that picks the moments. Not needed if the AI picker is off.", bins=["ollama"],
         pkgs={"pacman": ["ollama"]}, manual="curl -fsSL https://ollama.com/install.sh | sh", url="https://ollama.com/download", winget="Ollama.Ollama", direct="ollama", win_url="https://ollama.com/download/windows"),
    dict(id="fw", name="faster-whisper", level="required", why="Turns speech into text.", mods=["faster_whisper"], pips=["faster-whisper", "av<19"]),
    dict(id="av", name="PyAV", level="required", why="Reads the audio for Whisper. faster-whisper 1.2.x does not work with PyAV 19 yet, so this keeps it below 19.", mods=["av"], pips=["av<19"], updatable=True),
    dict(id="cuda", name="CUDA 12 libraries", level="optional", why="Lets Whisper use an NVIDIA GPU. Restart Clipper after installing. Without this, Whisper falls back to the CPU (slower but works).", mods=["nvidia.cublas", "nvidia.cudnn"],
         pips=["nvidia-cublas-cu12", "nvidia-cudnn-cu12"], only_nvidia=True),
    dict(id="yt", name="YouTube upload packages", level="optional", why="Lets Clipper upload finished clips to YouTube. Not needed if you upload by hand.", mods=["googleapiclient", "google_auth_oauthlib"],
         pips=["google-api-python-client", "google-auth-oauthlib"]),
]

def check_dep(d, nvidia):
    if d.get("only_nvidia") and not nvidia: return "skip", "No NVIDIA GPU found, so not needed"
    mods, bins = d.get("mods"), d.get("bins")
    mods_ok = bool(mods) and all(have_mod(m) for m in mods)
    paths = [which(b) for b in bins] if bins else []
    bins_ok = bool(bins) and all(paths)
    if not (mods_ok or bins_ok): return "missing", "not installed" if mods and not bins else "not found"
    v = pip_ver(pip_name(d["pips"][0])) if (mods_ok and d.get("pips")) else None
    if not v and bins_ok:
        # An executable merely existing on PATH is not proof that it can run.
        # Check every declared binary, not just the first one (ffprobe is required too).
        outputs = []
        for binary in bins:
            rc, output = sh([binary, d.get("vflag", "--version")])
            if rc != 0:
                return "missing", f"{binary} was found but failed its version check (exit code {rc})"
            outputs.append(output)
        m = re.search(r"\d+(?:\.\d+)+", outputs[0]) if outputs else None
        v = m.group(0) if m else None
    if d["id"] == "av" and mods_ok and av_supports_metadata_errors() is False:
        return "warn", f"version {v or '?'} is too new for faster-whisper (PyAV 19 removed an option it uses). Click Install to switch to a compatible version"
    if v: return "ok", f"version {v}"
    return "ok", (paths[0] if bins_ok else "installed")

def check_all(nvidia):
    out = []
    for d in DEPS:
        state, detail = check_dep(d, nvidia)
        if state == "missing" and d["level"] != "required": state = "warn"
        out.append(dict(d, state=state, detail=detail))
    return out

def pip_argv(pkgs, upgrade=False, break_sys=True):
    args = [py_exe(), "-m", "pip", "install"] + (["--upgrade"] if upgrade else [])
    if FROZEN:  # no Python on this machine: the exe runs pip itself and installs into its own folder
        return args + ["--target", str(PYLIBS)] + list(pkgs)
    if sys.prefix == sys.base_prefix:
        args += ["--user"]
        if break_sys and not WIN:
            args += ["--break-system-packages"]
    return args + list(pkgs)

def quote_cmd(argv):
    """A command line you can paste into a terminal (PowerShell on Windows)."""
    argv = [str(a) for a in argv]
    if WIN:
        text = " ".join(f'"{a}"' if re.search(r"[<>|&^]", a) else subprocess.list2cmdline([a]) for a in argv)
        return ("& " + text) if argv and " " in argv[0] else text
    return " ".join(shlex.quote(a) for a in argv)

def winget_text(pkg_id):
    return "winget install -e --id %s --source winget --accept-package-agreements --accept-source-agreements" % pkg_id

def winget_argv(pkg_id):
    return [which("winget") or "winget", "install", "-e", "--id", pkg_id, "--source", "winget", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]

def plan_for(d, mgr, nvidia, upgrade=False):
    """Return the install plan for one dependency on this system."""
    upgrade = upgrade or bool(d.get("always_upgrade"))
    if d.get("pips") and not d.get("pkgs") and not d.get("winget") and not d.get("direct"):
        return {"kind": "pip", "pkgs": d["pips"], "upgrade": upgrade, "text": quote_cmd(pip_argv(d["pips"], upgrade))}
    if WIN:
        direct = {"kind": "download", "what": d["direct"], "bins": d.get("bins"), "url": d.get("win_url"),
                  "text": f"Clipper downloads it for you (Install button). By hand: {d.get('win_url')}"} if d.get("direct") else None
        if d.get("winget") and mgr == "winget":
            return {"kind": "winget", "id": d["winget"], "bins": d.get("bins"), "fallback": direct, "url": d.get("win_url"), "text": winget_text(d["winget"])}
        if direct: return direct
        return {"kind": "manual", "text": d.get("manual_win") or "Install it by hand.", "url": d.get("win_url")}
    packages = (d.get("pkgs") or {}).get(mgr)
    if d["id"] == "ollama" and mgr == "pacman":
        packages = ["ollama-cuda" if nvidia else "ollama"]
    if not packages:
        return {"kind": "manual", "text": d.get("manual") or "Install it with your distribution's package manager.", "url": d.get("url")}
    return sys_plan(mgr, packages)

def sys_plan(mgr, pk):
    cmd = {"pacman": "pacman -S --needed --noconfirm", "apt": "apt-get install -y", "dnf": "dnf install -y"}[mgr]
    root = True
    inner = f"apt-get update && {cmd} {' '.join(pk)}" if mgr == "apt" else f"{cmd} {' '.join(pk)}"
    text = ("sudo " + inner.replace(" && ", " && sudo ")) if root else inner
    return {"kind": "sys", "mgr": mgr, "pkgs": list(pk), "root": root, "inner": inner, "text": text}

def merge_plans(plans, mgr):  # one system command (one password prompt) for all system packages, then the rest
    pk = [p for pl in plans if pl["kind"] == "sys" for p in pl["pkgs"]]
    return ([sys_plan(mgr, list(dict.fromkeys(pk)))] if pk else []) + [pl for pl in plans if pl["kind"] != "sys"]

# progress bars and spinners from winget would flood the log with one line per frame
NOISE = re.compile(r"^[\s\-\\|/]*$|[█▒▓░\ufffd]|^\s*[\d.]+\s*[KMG]B\s*/\s*[\d.]+\s*[KMG]B\s*$")

def stream(argv, emit, holder):
    emit("$ " + quote_cmd(argv))
    try: p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, **proc_kw())
    except OSError as e: emit(f"Couldn't start it: {e}"); return 127, []
    holder["p"] = p; out = []
    def pump():
        for line in p.stdout:
            line = line.rstrip(); out.append(line)
            if line and not NOISE.search(line): emit(line)
    t = threading.Thread(target=pump, daemon=True); t.start()
    p.wait(); t.join(timeout=3)  # an installer's background program can keep the pipe open; don't wait for it
    holder["p"] = None
    return (-1 if holder.get("cancel") else p.returncode), out

# ── Windows: download programs directly when winget is missing or fails ──
FFMPEG_ZIPS = ["https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
               "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"]
WINGET_OK = {0, 0x8A150061, 0x8A15002B}  # success, "already installed", "no newer version"

def download_to(url, dest, emit, holder):
    """Save a URL to disk; a short socket timeout keeps cancellation responsive."""
    emit(f"Downloading {url}")
    # urllib applies this timeout to every read, and a timed-out read cannot be resumed. 20 s survives a slow patch
    # on a normal connection; Cancel is still checked between every chunk.
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Clipper"}), timeout=20) as r, open(dest, "wb") as f:
        total, done, last = int(r.headers.get("Content-Length") or 0), 0, time.time()
        while True:
            if holder.get("cancel"): return False
            try:
                chunk = r.read(1 << 16)
            except TimeoutError:
                if holder.get("cancel"): return False
                raise
            if not chunk: break
            f.write(chunk); done += len(chunk)
            if time.time() - last > 2:
                emit(f"  {done / 1e6:.0f} of {total / 1e6:.0f} MB" if total else f"  {done / 1e6:.0f} MB"); last = time.time()
    return True


def extract_exes(zip_path, names, dest_dir):
    """Extract only explicitly named executables; never write archive paths to disk."""
    dest_dir.mkdir(parents=True, exist_ok=True); found = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            base = member.filename.rsplit("/", 1)[-1]
            if member.is_dir() or base.lower() not in names: continue
            # A corrupt or hostile archive should not be able to fill the disk.
            if member.file_size <= 0 or member.file_size > 500 * 1024 * 1024:
                raise zipfile.BadZipFile(f"Unexpected executable size in archive: {base}")
            part = dest_dir / (base + ".part")
            try:
                with zf.open(member) as src, open(part, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if part.stat().st_size != member.file_size:
                    raise zipfile.BadZipFile(f"Incomplete executable in archive: {base}")
                with open(part, "rb") as check:
                    if check.read(2) != b"MZ":
                        raise zipfile.BadZipFile(f"Invalid Windows executable in archive: {base}")
                os.replace(part, dest_dir / base)
                found.append(base.lower())
            finally:
                try: part.unlink()
                except FileNotFoundError: pass
    return found

def install_ffmpeg_direct(emit, holder):
    with tempfile.TemporaryDirectory(prefix="clipper_dl_") as tmp:
        for url in FFMPEG_ZIPS:
            z = Path(tmp) / "ffmpeg.zip"
            try:
                if not download_to(url, z, emit, holder): return False
                stage = Path(tmp) / "bin"
                found = extract_exes(z, {"ffmpeg.exe", "ffprobe.exe", "ffplay.exe"}, stage)
                if {"ffmpeg.exe", "ffprobe.exe"} <= set(found):
                    APP_BIN.mkdir(parents=True, exist_ok=True)
                    for name in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"):
                        candidate = stage / name
                        if candidate.is_file(): os.replace(candidate, APP_BIN / name)
                    refresh_paths()
                    checks = [sh([str(APP_BIN / name), "-version"])[0] for name in ("ffmpeg.exe", "ffprobe.exe")]
                    if all(rc == 0 for rc in checks):
                        emit(f"Installed and verified ffmpeg to {APP_BIN}"); return True
                    emit("The downloaded FFmpeg files did not pass their startup checks.")
            except (OSError, urllib.error.URLError, zipfile.BadZipFile) as e:
                emit(f"That download failed ({e}). Trying the next source..."); continue
    emit("Couldn't download ffmpeg. Check your internet connection, or install it from https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH.")
    return False

def install_deno_direct(emit, holder):
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    with tempfile.TemporaryDirectory(prefix="clipper_dl_") as tmp:
        z = Path(tmp) / "deno.zip"
        if not download_to(f"https://github.com/denoland/deno/releases/latest/download/deno-{arch}-pc-windows-msvc.zip", z, emit, holder): return False
        stage = Path(tmp) / "bin"
        if "deno.exe" not in extract_exes(z, {"deno.exe"}, stage):
            emit("Couldn't find deno.exe in the download."); return False
        APP_BIN.mkdir(parents=True, exist_ok=True)
        os.replace(stage / "deno.exe", APP_BIN / "deno.exe")
        refresh_paths()
        if sh([str(APP_BIN / "deno.exe"), "--version"])[0] != 0:
            emit("The downloaded Deno executable did not pass its startup check."); return False
    emit(f"Installed and verified deno to {APP_BIN}"); return True

def install_ollama_direct(emit, holder):
    with tempfile.TemporaryDirectory(prefix="clipper_dl_") as tmp:
        exe = Path(tmp) / "OllamaSetup.exe"
        if not download_to("https://ollama.com/download/OllamaSetup.exe", exe, emit, holder): return False
        emit("Running the Ollama installer. This can take a minute.")
        rc, _ = stream([str(exe), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], emit, holder)
    return rc == 0

DIRECT_INSTALLERS = {"ffmpeg": install_ffmpeg_direct, "deno": install_deno_direct, "ollama": install_ollama_direct}

def is_admin():
    try: return bool(ctypes.windll.shell32.IsUserAnAdmin()) if WIN else os.geteuid() == 0
    except (AttributeError, OSError): return False

def elevation(gui):
    """Command prefix that gains admin rights on Linux: [] when already root, None when there is no way.
    Windows needs none: winget and the installers ask for rights themselves when they need them."""
    if WIN or is_admin(): return []
    tool = shutil.which("pkexec" if gui else "sudo")
    return [tool] if tool else None

def run_plan(pl, emit, esc, holder):
    """Run one plan. esc is the command prefix for admin rights on Linux, for example ["pkexec"] or ["sudo"]."""
    kind = pl["kind"]
    if kind == "pip":
        for i, brk in enumerate((True, False)):
            rc, out = stream(pip_argv(pl["pkgs"], pl.get("upgrade", False), brk), emit, holder)
            if rc == 0: refresh_paths(); return True
            if holder.get("cancel"): return False
            if any("no module named pip" in l.lower() for l in out):
                emit("pip itself is missing. Installing it first...")
                if stream([py_exe(), "-m", "ensurepip", "--user"], emit, holder)[0] != 0: return False
                rc, out = stream(pip_argv(pl["pkgs"], pl.get("upgrade", False), brk), emit, holder)
                if rc == 0: refresh_paths()
                return rc == 0
            if i == 0 and not any("no such option" in l.lower() for l in out): return False
        return False
    if kind == "sys":
        argv = (esc if pl["root"] else []) + ["sh", "-c", pl["inner"]]
        rc, out = stream(argv, emit, holder)
        if rc != 0 and pl["mgr"] == "pacman" and any("404" in line or "failed retrieving" in line for line in out):
            emit("Your package database looks out of date. Run 'sudo pacman -Syu' once, then try again.")
        return rc == 0
    if kind == "winget":
        rc, _ = stream(winget_argv(pl["id"]), emit, holder)
        refresh_paths()
        if holder.get("cancel"): return False
        if (rc & 0xFFFFFFFF) in WINGET_OK:
            if not pl.get("bins") or all(which(b) for b in pl["bins"]): return True
            emit("winget says it is installed, but the program isn't visible yet.")
        else:
            emit(f"winget couldn't install it (exit code {rc}).")
        if pl.get("fallback"):
            emit("Trying a direct download instead...")
            return run_plan(pl["fallback"], emit, esc, holder)
        return False
    if kind == "download":
        fn = DIRECT_INSTALLERS.get(pl["what"])
        try: ok = bool(fn and fn(emit, holder))
        except (OSError, urllib.error.URLError, zipfile.BadZipFile, ValueError) as e: emit(f"Couldn't download it: {e}"); ok = False
        refresh_paths()
        if ok and pl.get("bins") and not all(which(b) for b in pl["bins"]): emit("Downloaded, but the program still isn't found. Restart Clipper and check again."); return False
        return ok
    emit("This one has to be installed by hand: " + pl["text"]); return False

def setup_script(mgr):
    if WIN: return setup_script_windows(mgr)
    lines = ["#!/usr/bin/env bash", "# Installs required and recommended Clipper dependencies on Linux.", "set -e", ""]
    installable = [d for d in DEPS if d["level"] in ("required", "recommended")]
    system_deps = [d for d in installable if d.get("pkgs")]
    packages = [pkg for dep in system_deps for pkg in (dep.get("pkgs") or {}).get(mgr, [])]
    if mgr and packages:
        lines += ["# System packages", sys_plan(mgr, list(dict.fromkeys(packages)))["text"], ""]
    manual = [d for d in installable if not (d.get("pkgs") or {}).get(mgr) and d.get("manual")]
    if manual:
        lines += ["# Official installers"] + [d["manual"] for d in manual] + [""]
    pips = [pkg for dep in installable if dep.get("pips") and not dep.get("pkgs") and not dep.get("only_nvidia") for pkg in dep["pips"]]
    pips = list(dict.fromkeys(pips))
    if pips:
        pip_flags = "--user --break-system-packages " if sys.prefix == sys.base_prefix else ""
        lines += ["# Python packages", "python3 -m pip install --upgrade " + pip_flags + " ".join(shlex.quote(p) for p in pips), ""]
    lines += ["# Optional: local AI picker (Ollama)", "# curl -fsSL https://ollama.com/install.sh | sh", ""]
    lines += ["# Optional: YouTube upload integration", "# python3 -m pip install --user --break-system-packages google-api-python-client google-auth-oauthlib", ""]
    lines += ["# Optional: NVIDIA GPU acceleration for Whisper", "# python3 -m pip install --user --break-system-packages nvidia-cublas-cu12 nvidia-cudnn-cu12", ""]
    if mgr == "pacman":
        lines += ["# Optional: NVIDIA-accelerated Ollama", "# sudo pacman -S --needed ollama-cuda", ""]
    lines += ["echo 'Done. Start Clipper with: python3 clipper_gui.py'"]
    return "\n".join(lines) + "\n"

def setup_script_windows(mgr):
    py = py_exe()
    pip = lambda pk: f'& "{py}" -m pip install --upgrade --user ' + " ".join(f'"{p}"' for p in pk)
    lines = ["# Installs the Clipper dependencies on Windows.", "# Run it in PowerShell:  powershell -ExecutionPolicy Bypass -File setup_clipper.ps1", ""]
    wg = winget_text
    if mgr == "winget":
        lines += ["# Programs"] + [wg(d["winget"]) for d in DEPS if d.get("winget") and d["level"] in ("required", "recommended")] + [""]
        lines += ["# Optional: local AI picker (Ollama)", "# " + wg("Ollama.Ollama"), ""]
    else:
        lines += ["# winget was not found. Install these by hand (or let the Setup page in Clipper download them):",
                  "#   ffmpeg: https://www.gyan.dev/ffmpeg/builds/   (add its bin folder to PATH)", "#   deno:   https://deno.com",
                  "#   ollama (optional): https://ollama.com/download/windows", ""]
    pips = [pkg for d in DEPS if d["level"] in ("required", "recommended") and d.get("pips") and not d.get("winget") and not d.get("only_nvidia") for pkg in d["pips"]]
    pips = list(dict.fromkeys(pips))
    lines += ["# Python packages", pip(pips), ""]
    lines += ["# Optional: YouTube upload integration", "# " + pip(["google-api-python-client", "google-auth-oauthlib"]), ""]
    lines += ["# Optional: NVIDIA GPU acceleration for Whisper", "# " + pip(["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]), ""]
    lines += ['Write-Host "Done. Start Clipper with: py clipper_gui.py"']
    return "\r\n".join(lines) + "\r\n"

def cli(argv):
    nv, (sysname, mgr) = gpu_info(), system_info()
    print(f"Clipper {VERSION}")
    print(f"System: {sysname} | package manager: {mgr_label(mgr)} | NVIDIA GPU: {nv[0] if nv else 'none'} | Python {sys.version.split()[0]}\n")
    rows = check_all(nv is not None)
    for r in rows: print(f"  [{ {'ok': 'ok', 'missing': 'MISSING', 'warn': 'missing', 'skip': '-'}[r['state']] :^7}] {r['name']:<20} {r['detail']}")
    todo = [r for r in rows if r["state"] in ("missing", "warn") and r["level"] != "optional"]
    if "--script" in argv: print("\n" + setup_script(mgr)); return
    if not todo: print("\nEverything is installed."); return
    plans = [plan_for(r, mgr, nv is not None) for r in todo]
    me = "py clipper_gui.py" if WIN else "python3 clipper_gui.py"
    if "--install" not in argv:
        print(f"\nTo install what is missing, run:  {me} --install\nOr by hand:")
        for r, pl in zip(todo, plans): print(f"  {r['name']}: {pl['text']}")
        return
    esc = elevation(gui=False)
    if esc is None and any(pl["kind"] == "sys" and pl["root"] for pl in plans):
        print("\nNeed root rights but 'sudo' was not found. Run the commands above as root.")
        return
    for pl in merge_plans(plans, mgr):
        print(); ok = run_plan(pl, print, esc or [], {}); print("OK" if ok else "FAILED")

def cli_models(argv):
    """List installed Ollama models and test the connection. Usage: python clipper_gui.py --models"""
    # Allow the user to override the host: python clipper_gui.py --models http://127.0.0.1:11434
    host = "http://127.0.0.1:11434"
    for a in argv[1:]:
        if a.startswith("http"): host = a
    print(f"Testing Ollama at {host} ...")
    if not ollama_up(host):
        print(f"  Ollama is NOT running at {host}.")
        print()
        # Also check via the ollama CLI as a fallback.
        ollama_bin = which("ollama")
        if ollama_bin:
            print("Trying 'ollama list' as a fallback ...")
            rc, out = sh([ollama_bin, "list"], timeout=10)
            if rc == 0 and out.strip():
                print(out)
                return
            print(f"  'ollama list' also failed (exit code {rc}).")
        print()
        print("To start Ollama:")
        print("  - Run: ollama serve   (in a terminal)")
        print("  - Or start it from the Setup page in Clipper's GUI.")
        return
    print(f"  Ollama is running at {host}.")
    models = ollama_models(host)
    if models is None:
        print("  Couldn't list models (unexpected error). Try 'ollama list' in a terminal.")
        return
    if not models:
        print("  No models installed yet.")
        print()
        print("  To download one, run for example:")
        print("    ollama pull llama3.1:8b")
        print("  Or use the 'Download more models' card on the AI picker page in Clipper.")
        return
    print(f"  {len(models)} model(s) installed:")
    for name, size in models:
        gb = size / 1e9 if size else 0
        print(f"    {name:<30}  {gb:.1f} GB")
    print()
    print("To download more: ollama pull <model-name>")

# Whisper model files (downloaded by faster-whisper from Hugging Face on first use)
WHISPER_REPOS = {"tiny": "Systran/faster-whisper-tiny", "base": "Systran/faster-whisper-base", "small": "Systran/faster-whisper-small", "medium": "Systran/faster-whisper-medium",
                 "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "large-v3": "Systran/faster-whisper-large-v3"}
WHISPER_MB = {"tiny": 75, "base": 145, "small": 484, "medium": 1500, "large-v3-turbo": 1600, "large-v3": 3100}

def whisper_cached(name):
    base = Path(os.environ.get("HF_HUB_CACHE") or (Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"))
    return bool(list((base / ("models--" + WHISPER_REPOS[name].replace("/", "--")) / "snapshots").glob("*/model.bin")))

# Ollama over plain HTTP (standard library, so there is no extra package to install)
def _ollama_host_variants(host):
    """Host plus an IPv4 variant, in case 'localhost' resolves to IPv6 but Ollama listens on IPv4."""
    host = host.rstrip("/")
    variants = [host]
    if "localhost" in host: variants.append(host.replace("localhost", "127.0.0.1"))
    return variants

def ollama_up(host):
    for h in _ollama_host_variants(host):
        try:
            urllib.request.urlopen(h, timeout=3).close(); return True
        except urllib.error.HTTPError: return True
        except (urllib.error.URLError, OSError, ValueError): continue
    return False

def ollama_models(host):  # [(name, size_bytes)] or None when Ollama can't be reached
    for h in _ollama_host_variants(host):
        try:
            with urllib.request.urlopen(h + "/api/tags", timeout=4) as r:
                return [(m["name"], m.get("size", 0)) for m in json.loads(r.read())["models"]]
        except (urllib.error.URLError, OSError, ValueError, KeyError): continue
    return None

def ollama_post(host, path, body, timeout):
    last_err = None
    for h in _ollama_host_variants(host):
        try:
            return urllib.request.urlopen(urllib.request.Request(h + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}), timeout=timeout)
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            continue
    raise last_err

SUGGESTED = {
    "llama3.2:3b": (2.0, "Small and fast. A good first try."),
    "gemma3:4b": (3.3, "Small, with good quality for its size."),
    "mistral:7b": (4.4, "A solid all-rounder."),
    "llama3.1:8b": (4.9, "The default. A good balance of speed and quality."),
    "qwen3:8b": (5.2, "Strong, but it thinks before answering, so it can be slower."),
    "gemma3:12b": (8.1, "Higher quality. Needs about 8 GB of VRAM or more."),
}

