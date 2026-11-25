#!/usr/bin/env python3
"""Clipper 1.00: turn a long video into short vertical clips with captions and Instagram descriptions.
Everything runs locally: yt-dlp + faster-whisper + Ollama + ffmpeg. Works on Windows 10/11 and Linux.
Run: python clipper_gui.py  (Windows: py clipper_gui.py, or double-click "Run Clipper.bat")."""
import ctypes, glob, hashlib, io, itertools, json, math, os, platform, queue, random, re, shlex, shutil, subprocess, sys, sysconfig, tempfile, threading, time, traceback, unicodedata, zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
import importlib.metadata, importlib.util, urllib.error, urllib.request

VERSION = "1.00"
WIN = os.name == "nt"
if not (WIN or sys.platform.startswith("linux")):
    raise SystemExit("Clipper supports Windows and Linux.")

for _stream in (sys.stdout, sys.stderr):  # a console with a legacy code page must not crash on a stray character
    try: _stream.reconfigure(errors="replace")
    except Exception: pass

# ───────────────────── platform helpers ─────────────────────
NOWIN = subprocess.CREATE_NO_WINDOW if WIN else 0  # keeps black console windows from flashing up on Windows

def local_appdata():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")

APP_BIN = local_appdata() / "Clipper" / "bin"  # Windows: programs the Setup page downloads directly (ffmpeg, deno) live here
DEFAULT_FONT = "Arial" if WIN else "Sans"      # caption font that every system has
MONO = "Consolas" if WIN else "Monospace"
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

def proc_kw():
    """Settings for every child process: no console window, no stdin, UTF-8 text, and a UTF-8 environment for Python tools."""
    kw = dict(stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
              env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1"))
    if WIN: kw["creationflags"] = NOWIN
    return kw

def quiet_kw():
    """For programs that open their own window (ffplay) or run on their own (ollama serve): no console, no pipes."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if WIN: kw["creationflags"] = NOWIN
    return kw

def py_exe():
    """The console Python for child processes (pythonw.exe has no console streams to pipe)."""
    exe = Path(sys.executable)
    if WIN and exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file(): return str(exe.parent / "python.exe")
    return sys.executable

def kill_tree(p):
    """Stop a process and everything it started (yt-dlp starts ffmpeg, winget starts installers)."""
    if p is None or p.poll() is not None: return
    if WIN:
        try: subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=10, **quiet_kw_run())
        except (OSError, subprocess.SubprocessError): pass
    try: p.kill()
    except OSError: pass

def quiet_kw_run():  # subprocess.run with capture_output must not also get stdout/stderr
    return dict(stdin=subprocess.DEVNULL, creationflags=NOWIN) if WIN else dict(stdin=subprocess.DEVNULL)

def split_args(s):
    """Split a user-typed argument string like a shell would. Windows rules on Windows, so C:\\paths survive."""
    s = (s or "").strip()
    if not s: return []
    if WIN:
        try:
            n, fn = ctypes.c_int(0), ctypes.windll.shell32.CommandLineToArgvW
            fn.restype, fn.argtypes = ctypes.POINTER(ctypes.c_wchar_p), [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
            argv = fn("clipper " + s, ctypes.byref(n))
            if argv:
                try: return [argv[i] for i in range(1, n.value)]
                finally: ctypes.windll.kernel32.LocalFree(argv)
        except (AttributeError, OSError): pass
        return [t.strip('"') for t in s.split()]
    return shlex.split(s)

def safe_dirname(title):
    """A folder name that is valid on Windows too: no trailing dot or space, no reserved device names."""
    keep = lambda ch: ch in "-_ " or unicodedata.category(ch)[0] in "LNM"  # letters, digits and combining marks (Devanagari, Arabic vowel signs)
    name = "".join(ch for ch in str(title) if keep(ch)).strip()[:60].rstrip(" .")
    return name + "_" if name.upper() in RESERVED else name

# Keep BLAS/OpenMP thread use bounded on high-core-count systems.
if "MKL_NUM_THREADS" not in os.environ:
    os.environ["MKL_NUM_THREADS"] = "4"
if "OMP_NUM_THREADS" not in os.environ:
    os.environ["OMP_NUM_THREADS"] = "4"

def windows_registry_path():
    """PATH as stored in the registry, so programs installed after Clipper started are found without a restart."""
    import winreg
    out = []
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"), (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(hive, sub) as key:
                out += [os.path.expandvars(p) for p in str(winreg.QueryValueEx(key, "Path")[0]).split(";") if p.strip()]
        except OSError: pass
    return out

def windows_extra_dirs():
    """Places where the programs Clipper installs end up on Windows, even before PATH knows about them."""
    la, pf = local_appdata(), Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    dirs = [APP_BIN, la / "Microsoft" / "WinGet" / "Links", la / "Programs" / "Ollama", Path.home() / ".deno" / "bin",
            pf / "NVIDIA Corporation" / "NVSMI", Path(sys.prefix) / "Scripts"]
    try: dirs += sorted((la / "Microsoft" / "WinGet" / "Packages").glob("Gyan.FFmpeg*/ffmpeg-*/bin"))
    except OSError: pass
    return dirs

def refresh_paths():  # expose freshly installed user-level programs and Python packages
    import site
    try:
        us = site.getusersitepackages()
        if os.path.isdir(us) and us not in sys.path:
            site.addsitedir(us)
    except Exception:
        pass
    current = os.environ.get("PATH", "").split(os.pathsep)
    if WIN:
        try: scripts = Path(sysconfig.get_path("scripts", "nt_user"))
        except (KeyError, OSError): scripts = None
        extra = windows_extra_dirs() + ([scripts] if scripts else []) + [Path(p) for p in windows_registry_path()]
        norm = lambda p: os.path.normcase(os.path.normpath(str(p)))
        have, add = {norm(p) for p in current if p}, []
        for p in extra:
            if norm(p) not in have and Path(p).is_dir(): have.add(norm(p)); add.append(str(p))
        if add: os.environ["PATH"] = os.pathsep.join([p for p in current if p] + add)
    else:
        for path in [Path.home() / ".local" / "bin", Path.home() / ".deno" / "bin"]:
            if path.is_dir() and str(path) not in current:
                os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")
    importlib.invalidate_caches()

refresh_paths()

_dll_dirs = []  # os.add_dll_directory handles; they must stay referenced
def preload_cuda():
    """Let CTranslate2 find the CUDA 12 libraries installed from pip (nvidia-cublas-cu12, nvidia-cudnn-cu12)."""
    if WIN:  # the DLLs sit in site-packages\nvidia\<lib>\bin
        try: roots = [Path(p) for p in (importlib.util.find_spec("nvidia").submodule_search_locations or [])]
        except (ImportError, ValueError, AttributeError): return
        for root in roots:
            for d in sorted(root.glob("*/bin")):
                if any(d.glob("*.dll")):
                    try: _dll_dirs.append(os.add_dll_directory(str(d)))
                    except (OSError, AttributeError): pass
                    os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        return
    try:  # skip nvblas, it breaks numpy
        import nvidia.cublas.lib as _cb, nvidia.cudnn.lib as _cd
    except ImportError:
        return
    for d in (list(_cb.__path__)[0], list(_cd.__path__)[0]):
        for f in sorted(glob.glob(os.path.join(d, "*.so*"))):
            if "nvblas" not in os.path.basename(f):
                try: ctypes.CDLL(f, mode=ctypes.RTLD_GLOBAL)
                except OSError: pass

preload_cuda()

def which(name):
    """Return the executable path visible on the PATH, or None."""
    return shutil.which(name)

# ───────────────────── system checks and installers (standard library only) ─────────────────────
# Kept free of tkinter and third-party imports so they also work on a brand-new machine:
#   python clipper_gui.py --version    print the version
#   python clipper_gui.py --check      show what is installed
#   python3 clipper_gui.py --models     list installed Ollama models (and test the connection)
#   python3 clipper_gui.py --install    install everything that is missing
#   python3 clipper_gui.py --script     print a setup script for this kind of system
def sh(cmd, timeout=None):
    # Resolve commands through the refreshed PATH. The first launch of a program on Windows can be slow (virus scan).
    timeout = timeout or (20 if WIN else 6)
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

if any(a in sys.argv for a in ("--version", "-V")):
    print(f"Clipper {VERSION}")
    sys.exit(0)

if any(a in sys.argv for a in ("--check", "--install", "--script", "--models")):
    if "--models" in sys.argv:
        cli_models(sys.argv)
    else:
        cli(sys.argv)
    sys.exit(0)

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import tkinter.font as tkfont
except ImportError:
    _n, _m = system_info()
    sys.exit("tkinter is missing, so the window can't open.\nInstall it with:  " + plan_for(DEPS[0], _m, False)["text"] + "\nOr install everything at once:  python3 clipper_gui.py --install")
import webbrowser

# ───────────────────────────── settings schema ─────────────────────────────
# One line per setting. The UI, saving, presets and the pipeline all read from this.
# (key, label, kind, default, options, hint, advanced)
def Opt(key, label, kind, default, opts=None, hint="", adv=False): return (key, label, kind, default, opts, hint, adv)

SCHEMA = {
    "Source": [
        Opt("max_height", "Max download quality (p)", "choice", 1080, [360, 480, 720, 1080, 1440, 2160], "Lower is a faster download"),
        Opt("use_cache", "Reuse earlier download and transcript", "bool", True, hint="Skips work already done for the same video"),
        Opt("cookies_browser", "Browser cookies for yt-dlp", "choice", "none", ["none", "firefox", "chrome", "chromium", "brave", "edge"], "Try this if YouTube blocks the download" + (". On Windows Firefox is the most reliable: Chrome and Edge often can't share their cookies" if WIN else ""), True),
        Opt("ytdlp_extra", "Extra yt-dlp arguments", "str", "", hint="For example: --proxy http://host:port", adv=True),
    ],
    "Transcribe": [
        Opt("device", "Run Whisper on", "choice", "auto", ["auto", "cuda", "cpu"], "auto uses the GPU when available and falls back to the CPU"),
        Opt("whisper_model", "Whisper model", "choice", "small", ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"], "Bigger is more accurate and slower"),
        Opt("language", "Spoken language", "str", "", hint="Leave empty to detect it. Or type a code like en, es, hi. Type hinglish for Hindi written in English letters (use the small model or bigger)"),
        Opt("vad_filter", "Skip silence (VAD)", "bool", False, hint="Can reduce made-up text in quiet parts", adv=True),
        Opt("beam_size", "Beam size", "int", 5, (1, 10), "Higher is slightly more accurate and slower", True),
    ],
    "AI picker": [
        Opt("use_ai", "Use local AI to pick moments", "bool", True, hint="Off spreads the clips evenly across the video"),
        Opt("ollama_model", "Ollama model", "ollama", "llama3.1:8b", hint="Press the refresh button to list installed models"),
        Opt("focus", "What should it look for?", "text", "", hint="Optional. For example: funny moments, or advice for beginners"),
        Opt("ollama_host", "Ollama address", "str", "http://127.0.0.1:11434", adv=True),
        Opt("num_ctx", "Context size (tokens)", "int", 8192, (2048, 65536), "Higher uses more VRAM. Long videos are split into parts automatically", True),
        Opt("temperature", "Creativity", "float", 0.4, (0, 1.5, 0.1), adv=True),
        Opt("ai_retries", "Retries on bad AI output", "int", 3, (1, 10), adv=True),
    ],
    "Growth": [
        Opt("smart_pick", "Smart moment picker", "bool", True, hint="Scores every possible clip on hook strength, audio energy, pacing and a clean start and end, then the AI rates the best of them. Off lets the AI choose alone"),
        Opt("pick_min_score", "Minimum AI score (1-10)", "float", 5.0, (1, 9, 0.5), "Moments the AI rates lower are skipped, even if that means fewer clips"),
        Opt("audience", "Your niche or audience", "text", "", hint="For example: GTA RP highlights, or personal finance tips. Used for titles, captions and hashtags"),
        Opt("hashtag_count", "Hashtags per clip", "int", 4, (1, 10), "3 to 5 relevant tags work better than a long list of generic ones"),
        Opt("extra_hashtags", "Always add these hashtags", "str", "", hint="Your brand or community tag, for example #ohnepixelclips"),
        Opt("credit_line", "Credit the original creator in the caption", "bool", True, hint="Adds 'Credit: @handle' from the video's channel, or from the watermark text"),
        Opt("cover_export", "Save a cover image for each clip", "bool", True, hint="A JPG taken just after the hook appears, ready to use as the cover"),
    ],
    "Clips": [
        Opt("num_clips", "Number of clips", "int", 5, (1, 30), "Ignored while auto-edit is on (see the Editing page)"),
        Opt("min_len", "Shortest clip (seconds)", "int", 35, (5, 180)),
        Opt("max_len", "Longest clip (seconds)", "int", 60, (10, 300)),
        Opt("review", "Review and edit clips before rendering", "bool", True),
        Opt("snap", "Start and end on sentence boundaries", "bool", True, adv=True),
        Opt("pad_start", "Extra time before (seconds)", "float", 0.0, (0, 5, 0.1), adv=True),
        Opt("pad_end", "Extra time after (seconds)", "float", 0.0, (0, 5, 0.1), adv=True),
    ],
    "Captions": [
        Opt("captions", "Burn captions into the video", "bool", True),
        Opt("cap_position", "Position", "choice", "bottom", ["bottom", "middle", "top"]),
        Opt("cap_size", "Font size", "int", 14, (6, 40)),
        Opt("cap_color", "Text color (hex)", "str", "FFFFFF", adv=True),
        Opt("cap_outline", "Outline thickness", "int", 2, (0, 8), adv=True),
        Opt("cap_bold", "Bold", "bool", True, adv=True),
        Opt("cap_margin", "Distance from edge", "int", 20, (0, 200), adv=True),
    ],
    "Editing": [
        Opt("edit_enabled", "Auto-edit clips with AI", "bool", True, hint="The AI plans zooms, pans, animated captions, text and sound effects for each clip"),
        Opt("edit_clips", "Edited clips per video", "int", 2, (1, 10), "Replaces 'Number of clips' while auto-edit is on"),
        Opt("edit_style", "Style", "choice", "punchy", ["subtle", "punchy"], "subtle: gentle zooms, slower captions. punchy: stronger and faster"),
        Opt("edit_zoom", "Zoom punch-ins", "bool", True),
        Opt("edit_pan", "Slow pan while zoomed", "bool", True),
        Opt("edit_words", "Highlight each word as it is spoken", "bool", True, hint="Needs 'Burn captions into the video' on the Captions page"),
        Opt("edit_text", "Hook title and keyword pop-ups", "bool", True),
        Opt("edit_bar", "Progress bar", "bool", True),
        Opt("edit_sfx", "Sound effects", "bool", True, hint="A quiet whoosh on zooms and a pop on text"),
        Opt("edit_hl_color", "Highlight color (hex)", "str", "FFD400", adv=True),
        Opt("edit_upper", "ALL CAPS captions", "bool", True, adv=True),
        Opt("edit_font", "Caption font", "str", DEFAULT_FONT, hint="Any font installed on this computer", adv=True),
    ],
    "Watermark": [
        Opt("wm_enabled", "Credit the original creator", "bool", True, hint="Shows their name on every clip. Local files have no channel, so type the name below"),
        Opt("wm_text", "Text", "str", "", hint="For example @ohnepixel. Leave empty to use the video's channel handle automatically"),
        Opt("wm_position", "Position", "choice", "top right", ["top right", "top left", "bottom right", "bottom left"]),
        Opt("wm_size", "Size", "int", 8, (4, 20)),
        Opt("wm_opacity", "Opacity (%)", "int", 80, (10, 100)),
    ],
    "Split screen": [
        Opt("split_enabled", "Split face cam and content", "bool", False, hint="For streams: an enlarged face cam on one half and the main content on the other. After download, mark the face-cam area and (optionally) the content area"),
        Opt("split_cam_pos", "Face cam position", "choice", "top", ["top", "bottom"]),
        Opt("split_cam_pct", "Face cam height (% of the screen)", "int", 40, (20, 70)),
        Opt("split_ask", "Ask where they are for every video", "bool", True, hint="Off reuses the face-cam and content areas you marked last time, which is handy when your layout never changes"),
        Opt("split_overlap", "If the face cam is on top of the content", "choice", "auto", ["auto", "crop", "fill", "keep"],
            "Keeps the face out of the content panel. crop: cut away the part of the content that holds the cam. fill: paint over the cam. auto: crop if that keeps at least half of the content, otherwise fill. keep: do nothing"),
        Opt("split_cam", "Face cam area (saved)", "str", "", hint="x, y, width, height as fractions of the video. Filled in by the picker window", adv=True),
        Opt("split_main", "Main content area (saved)", "str", "0,0,1,1", hint="x, y, width, height as fractions of the video. 0,0,1,1 is the full frame. Filled in by the picker window", adv=True),
    ],
    "Video": [
        Opt("fit", "Fit mode", "choice", "blur", ["blur", "crop", "bars", "squish"], "blur: whole video over a blurred copy. crop: fill the frame and cut the sides. bars: black bars. squish: stretch (distorts)"),
        Opt("aspect", "Aspect ratio", "choice", "9:16", ["9:16", "4:5", "1:1", "16:9"]),
        Opt("out_width", "Output width (px)", "int", 1080, (240, 3840), adv=True),
        Opt("blur", "Blur strength", "int", 20, (1, 60), adv=True),
        Opt("encoder", "Video encoder", "choice", "auto", ["auto", "nvenc", "x264"], "auto uses the NVIDIA GPU encoder when available", True),
        Opt("quality", "Quality (lower is better)", "int", 23, (14, 35), adv=True),
        Opt("audio_bitrate", "Audio bitrate", "str", "192k", adv=True),
        Opt("loudnorm", "Even out loudness", "bool", False, adv=True),
        Opt("extra_ffmpeg", "Extra ffmpeg arguments", "str", "", hint="Added before the output file", adv=True),
    ],
    "Output": [
        Opt("output_dir", "Save clips to", "path", str(Path.home() / "clipper_output")),
        Opt("prefix", "File name prefix", "str", "clip"),
        Opt("subfolder", "Give each video its own folder", "bool", True),
        Opt("write_txt", "Save Instagram descriptions (.txt)", "bool", True),
        Opt("open_done", "Open the folder when finished", "bool", True),
        Opt("write_json", "Save a manifest (.json)", "bool", False, adv=True),
        Opt("write_srt", "Also save subtitle files (.srt)", "bool", False, adv=True),
    ],
    "YouTube": [
        Opt("yt_enabled", "Upload clips to YouTube", "bool", False, hint="Requires a Google Cloud client_secret.json. Click 'Get credentials guide' below for steps."),
        Opt("yt_privacy", "Privacy status", "choice", "private", ["private", "unlisted", "public"], "private: only you. unlisted: anyone with the link. public: everyone."),
        Opt("yt_category", "Category", "choice", "People & Blogs", ["People & Blogs", "Gaming", "Entertainment", "Comedy", "Education", "Howto & Style", "Science & Technology", "Sports", "News & Politics", "Film & Animation", "Music", "Pets & Animals", "Travel & Events", "Autos & Vehicles"], "Helps YouTube decide who to show the clip to"),
        Opt("yt_hashtags_in_title", "Move hashtags to title", "bool", True, hint="Appends hashtags from the description to the end of the title"),
        Opt("yt_auto_upload", "Auto-upload after render", "bool", False, hint="Uploads each clip automatically when rendering finishes"),
        Opt("yt_default_tags", "Default tags (comma-separated)", "str", "shorts", hint="Generic tags like viral or fyp do not help on YouTube; the AI keywords are added automatically", adv=True),
        Opt("yt_client_secret", "client_secret.json path", "path", str(Path.home() / "client_secret.json"), adv=True),
    ],
}
PRESETS = {
    "Fast": {"whisper_model": "base", "review": False, "quality": 26},
    "Balanced": {"whisper_model": "small", "review": True, "quality": 23},
    "Best quality": {"whisper_model": "large-v3-turbo", "review": True, "quality": 20, "beam_size": 5},
}
STEPS = ["Get video", "Read audio", "Transcribe", "Pick moments", "Plan edits", "Render clips"]
WEIGHTS = [0.10, 0.03, 0.35, 0.12, 0.10, 0.30]
# Per-user settings: %APPDATA%\Clipper on Windows, ~/.config/clipper on Linux.
APP_DIR = (Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Clipper") if WIN else Path.home() / ".config" / "clipper"
SETTINGS_FILE = APP_DIR / "settings.json"

def cast(kind, default, v):
    if kind == "bool": return bool(v)
    if kind == "int": return int(float(v))
    if kind == "float": return float(v)
    if kind == "choice" and isinstance(default, int): return int(v)
    return str(v).strip()

def parse_t(s):  # "90", "1:30" or "1:02:03" -> seconds
    try: p = [float(x) for x in s.strip().split(":")]
    except ValueError: raise ValueError("Use seconds (90) or m:ss (1:30).") from None
    return sum(v * 60 ** i for i, v in enumerate(reversed(p)))

def fmt_t(sec):
    sec = int(sec); return f"{sec // 60}:{sec % 60:02d}"

def fmt_clip_t(sec):
    """Format editable clip times without discarding sub-second precision."""
    sec = max(0.0, float(sec))
    minutes = int(sec // 60)
    rem = sec - minutes * 60
    if round(rem, 2) >= 60: minutes, rem = minutes + 1, 0.0
    return f"{minutes}:{rem:05.2f}"

def output_stem(output_dir, prefix, index):
    """Create a safe, collision-free output basename inside output_dir."""
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", str(prefix)).strip("._-") or "clip"
    base = f"{safe_prefix}_{int(index)}"
    candidate, suffix = base, 2
    directory = Path(output_dir)
    while any((directory / f"{candidate}.{ext}").exists() for ext in ("mp4", "srt", "txt")):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate

def transcript_cache_name(cfg):
    """Include every transcription-affecting option in a safe cache filename."""
    model = re.sub(r"[^A-Za-z0-9_-]+", "_", str(cfg.get("whisper_model", "small"))) or "small"
    language = re.sub(r"[^A-Za-z0-9_-]+", "_", str(cfg.get("language") or "auto")).strip("_") or "auto"
    words = "_words" if cfg.get("edit_enabled") else ""  # edited clips need word timestamps, so they get their own cache
    return f"segments_{model}_{language}_beam{int(cfg['beam_size'])}_vad{int(bool(cfg['vad_filter']))}{words}.json"

def validate_clip_times(start, end, total, min_len, max_len):
    """Raise ValueError when a reviewed clip cannot safely be rendered."""
    values = (start, end, total, min_len, max_len)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("Times must be finite numbers.")
    if start < 0:
        raise ValueError("The start cannot be before the beginning of the video.")
    if end <= start:
        raise ValueError("The end must come after the start.")
    if end > total + 0.05:
        raise ValueError(f"The end cannot exceed the video duration ({fmt_clip_t(total)}).")
    length = end - start
    if length < min_len - 0.05 or length > max_len + 0.05:
        raise ValueError(f"Clip length must be between {min_len:g} and {max_len:g} seconds; this clip is {length:.2f} seconds.")

def srt_t(t):
    ms = max(0, int(round(float(t) * 1000)))
    h, rem = divmod(ms, 3600000); m, sec = divmod(rem, 60000)
    return f"{h:02}:{m:02}:{sec // 1000:02},{sec % 1000:03}"

# ───────────────────────────── auto-edit helpers (pure functions, no UI) ─────────────────────────────
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
ZOOM_AMP = {"punchy": (0.07, 0.11, 0.16), "subtle": (0.04, 0.06, 0.09)}  # zoom strength 1, 2, 3

def words_ok(seg):
    """True when a cached segment carries usable Whisper word timestamps."""
    ws = seg.get("words")
    return isinstance(ws, list) and all(
        isinstance(w, dict) and isinstance(w.get("word"), str)
        and math.isfinite(float(w["start"])) and math.isfinite(float(w["end"])) for w in ws)

def hexcol(s, default):
    h = str(s).lstrip("#").upper()
    return h if re.fullmatch(r"[0-9A-F]{6}", h) else default

def ass_col(h):  # RRGGBB -> ASS &H00BBGGRR
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}"

def ass_t(t):
    cs = max(0, int(round(float(t) * 100)))
    h, rem = divmod(cs, 360000); m, rem = divmod(rem, 6000)
    return f"{h}:{m:02}:{rem // 100:02}.{rem % 100:02}"

def ass_text(s):
    return re.sub(r"\s+", " ", re.sub(r"[{}\\]", "", str(s))).strip()

def clip_words(segs, a, b):
    """Words of the clip as (start, end, text) in clip time. Uses Whisper word timestamps when present,
    otherwise spreads each segment's words evenly over the segment."""
    out = []
    for s in segs:
        if s["end"] <= a or s["start"] >= b: continue
        if s.get("words"):
            items = [(float(w["start"]), float(w["end"]), str(w["word"]).strip()) for w in s["words"] if str(w.get("word", "")).strip()]
        else:
            toks = s["text"].split()
            step = max(s["end"] - s["start"], 0.01) / max(len(toks), 1)
            items = [(s["start"] + i * step, s["start"] + (i + 1) * step, t) for i, t in enumerate(toks)]
        for st, en, tx in items:
            if en <= a or st >= b: continue
            s0 = max(0.0, st - a)
            out.append((s0, max(s0, min(b, en) - a), tx))
    return sorted(out)

def caption_chunks(words, max_words, max_chars):
    """Group words into short caption lines: break on length, pauses and punctuation."""
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or sum(len(x[2]) + 1 for x in cur) + len(w[2]) > max_chars
                    or w[0] - cur[-1][1] > 0.7 or re.search(r"[.!?,;:…]$", cur[-1][2])):
            chunks.append(cur); cur = []
        cur.append(w)
    if cur: chunks.append(cur)
    return chunks

def _num(v, lo, hi):
    try: x = float(v)
    except (TypeError, ValueError): return None
    return x if math.isfinite(x) and lo <= x <= hi else None

def overlay_text(s, max_words=4, max_chars=24):
    out = ""
    for w in re.sub(r"[{}\\\r\n\"]+", " ", str(s)).split()[:max_words]:
        if len(out) + len(w) + (1 if out else 0) > max_chars: break
        out = (out + " " + w).strip()
    return out.upper()

def make_plan(raw, words, length, c):
    """Turn the AI's suggestions (or None) into a safe edit plan: clamps times, spaces effects out,
    caps their number, and falls back to simple rules when the AI gave nothing usable."""
    raw = raw if isinstance(raw, dict) else {}
    punchy = c["edit_style"] != "subtle"
    amps, r = ZOOM_AMP["punchy" if punchy else "subtle"], (0.45 if punchy else 0.65)
    cands = []
    if isinstance(raw.get("zooms"), list):
        for z in raw["zooms"]:
            if not isinstance(z, dict): continue
            t, s = _num(z.get("t"), 0, length), _num(z.get("strength", 2), 1, 3)
            if t is not None: cands.append((t, int(round(s)) if s is not None else 2))
    source = "ai" if cands else "auto"
    if not cands:  # no AI plan: zoom at the start of phrases, at least ~5 s apart
        last, k = -99.0, 0
        for i, w in enumerate(words):
            if (i == 0 or re.search(r"[.!?]$", words[i - 1][2]) or w[0] - words[i - 1][1] > 0.5) and w[0] - last >= 4.8:
                cands.append((w[0], (2, 1, 2, 3)[k % 4])); k += 1; last = w[0]
    zooms = []
    if c["edit_zoom"]:
        limit = min(8, max(1, int(length // 4.5)))
        for t, s in sorted(cands):
            t0 = max(0.0, t - 0.25); t1 = t0 + r + 1.2 + 0.4 * s
            if zooms and t0 < zooms[-1]["end"] + 1.0: continue
            if t1 + r > length - 0.1: continue
            zooms.append({"t0": round(t0, 2), "t1": round(t1, 2), "end": round(t1 + r, 2), "r": r, "amp": amps[s - 1],
                          "pan": ((1 if len(zooms) % 2 == 0 else -1) if c["edit_pan"] else 0)})
            if len(zooms) >= limit: break
    hook, callouts = None, []
    if c["edit_text"]:
        h = overlay_text(raw.get("hook", ""), 6, 28)
        hook = h if h and length > 6 else None
        items = []
        if isinstance(raw.get("callouts"), list):
            for it in raw["callouts"]:
                if not isinstance(it, dict): continue
                t, txt = _num(it.get("t"), 0, length), overlay_text(it.get("text", ""))
                if t is not None and txt: items.append((t, txt))
        for t, txt in sorted(items):
            if t < (2.9 if hook else 0.3) or t + 1.9 > length: continue
            if callouts and t < callouts[-1]["t"] + 3.0: continue
            callouts.append({"t": round(t, 2), "len": 1.8, "text": txt})
            if len(callouts) >= min(6, int(length // 8)): break
    return {"hook": hook, "zooms": zooms, "callouts": callouts, "source": source}

def plan_prompt(words, length, focus=""):
    lines, cur, cur_t = [], [], 0.0
    for w in words:
        if cur and w[0] - cur_t >= 3.0:
            lines.append("[%.1f] %s" % (cur_t, " ".join(cur))); cur = []
        if not cur: cur_t = w[0]
        cur.append(w[2])
    if cur: lines.append("[%.1f] %s" % (cur_t, " ".join(cur)))
    nz, nc = max(1, min(8, int(length // 5))), max(0, min(6, int(length // 9)))
    return ("You are a short-form video editor planning the edits for a %.0f second vertical clip. "
            "The transcript below has times in seconds from the start of the clip.%s\n"
            "Return valid JSON only, in this shape: "
            '{"hook":"...","zooms":[{"t":4.2,"strength":2}],"callouts":[{"t":9.5,"text":"..."}]}\n'
            "Rules:\n"
            "- hook: a punchy on-screen title of 3 to 6 words that makes a viewer keep watching. Do not copy a whole sentence.\n"
            "- zooms: up to %d moments of emphasis (a punchline, bold claim, surprising fact or emotional beat). t is the second the words are spoken. "
            "strength is 1 (subtle), 2 (medium) or 3 (strong). Keep at least 5 seconds between zooms.\n"
            "- callouts: up to %d very short on-screen texts (1 to 4 words) naming a key term, number or idea at the moment it is said. t is the second it is spoken. "
            "Leave the list empty if nothing deserves it.\n"
            "- Only use times that fall inside the transcript. Do not invent facts.\n\nTranscript:\n%s"
            % (length, (" Focus: " + focus + ".") if focus else "", nz, nc, "\n".join(lines)))

def build_ass(c, W, H, length, words, plan, ys=None, lay=None):
    """Build the ASS subtitle file with animated captions, hook/keyword boxes and the progress bar.
    Everything is drawn by libass in the same ffmpeg pass. Returns None when there is nothing to draw."""
    k = H / 288.0  # caption sizes are expressed on a 288 px tall canvas, like the plain captions
    fs, outline = max(8, round(c["cap_size"] * k)), round(c["cap_outline"] * k)
    base, hl = hexcol(c["cap_color"], "FFFFFF"), hexcol(c["edit_hl_color"], "FFD400")
    align, mvu = cap_place(c, H, lay); mv = round(mvu * k)
    font = re.sub(r"[,\r\n]", " ", str(c["edit_font"])).strip() or DEFAULT_FONT
    side, ev = round(W * 0.07), []
    if c["captions"] and words:
        upper, animate = c["edit_upper"], c["edit_words"]
        ws = [(a, b, t.upper() if upper else t) for a, b, t in words]
        maxc = max(8, min(30, int(W * 0.84 / (fs * (0.68 if upper else 0.6)))))
        chunks, cur = caption_chunks(ws, 3 if c["edit_style"] != "subtle" else 4, maxc), 0.0
        for ci, ch in enumerate(chunks):
            nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else length
            t_end, start = min(ch[-1][1] + 0.25, nxt, length), max(ch[0][0], cur)
            if t_end - start < 0.03: continue
            for n, i in enumerate(range(len(ch)) if animate else (0,)):
                s = start if n == 0 else max(ch[i][0], cur)
                e = t_end if (not animate or i + 1 == len(ch)) else min(max(ch[i + 1][0], s), t_end)
                if e - s < 0.02: continue
                if animate:
                    txt = " ".join("{\\1c" + ass_col(hl) + "&}" + ass_text(w[2]) + "{\\1c" + ass_col(base) + "&}" if j == i else ass_text(w[2]) for j, w in enumerate(ch))
                else:
                    txt = " ".join(ass_text(w[2]) for w in ch)
                pop = "{\\fscx88\\fscy88\\t(0,100,\\fscx100\\fscy100)}" if n == 0 else ""
                ev.append("Dialogue: 0,%s,%s,Cap,,0,0,0,,%s%s" % (ass_t(s), ass_t(e), pop, txt))
                cur = e
    y_hook, y_call = ys or ((0.16, 0.27) if c["cap_position"] != "top" else (0.84, 0.74))  # keep clear of the captions
    def box(t, dur, text, y):
        fx = ("\\an5\\pos(%d,%d)\\frz-3\\fad(90,160)\\fscx70\\fscy70\\t(0,130,\\fscx108\\fscy108)\\t(130,230,\\fscx100\\fscy100)" % (W // 2, round(H * y)))
        ev.append("Dialogue: 2,%s,%s,Box,,0,0,0,,{%s}%s" % (ass_t(t), ass_t(min(t + dur, length)), fx, ass_text(text)))
    if plan.get("hook"): box(0.2, 2.4, plan["hook"], y_hook)
    for q in plan.get("callouts") or []: box(q["t"], q["len"], q["text"], y_call)
    if c["edit_bar"] and length > 1:
        bh = max(4, round(H * 0.007))
        ev.append("Dialogue: 3,%s,%s,Cap,,0,0,0,,{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c%s&\\clip(0,0,1,%d)\\t(0,%d,\\clip(0,0,%d,%d))}m 0 0 l %d 0 %d %d 0 %d"
                  % (ass_t(0), ass_t(length), ass_col(hl), bh, int(length * 1000), W, bh, W, W, bh, bh))
    if not ev: return None
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Cap,%s,%d,%s,&H000000FF,&H00000000,&H64000000,%d,0,0,0,100,100,0,0,1,%d,0,%d,%d,%d,%d,1\n"
            "Style: Box,%s,%d,&H00181818,&H000000FF,%s,&H00000000,-1,0,0,0,100,100,0,0,3,%d,0,5,%d,%d,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            % (W, H, font, fs, ass_col(base), -1 if c["cap_bold"] else 0, outline, align, side, side, mv,
               font, round(fs * 1.05), ass_col(hl), round(fs * 0.3), side, side))
    return head + "\n".join(ev) + "\n"

def fit_filter(c, W, H):
    b = c["blur"]
    return {
        "blur": f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur={b}:5[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1",
        "crop": f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1",
        "bars": f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
        "squish": f"scale={W}:{H},setsar=1",
    }[c["fit"]]

def split_filter(c, W, H, pre, lay):
    """Face cam enlarged in its own panel, main content (with the usual fit mode and zoom) in the other."""
    cx, cy, cw, ch = lay["cam"]; mx, my, mw, mh = lay["main"]; cam_h = lay["cam_h"]
    # Preserve the entire user-selected face-cam rectangle even when it is not the
    # same aspect ratio as the output panel; pad rather than silently cropping it.
    cam = f"crop={cw}:{ch}:{cx}:{cy},scale={W}:{cam_h}:force_original_aspect_ratio=decrease,pad={W}:{cam_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    px, py, pw, ph = lay.get("patch") or (0, 0, 0, 0)  # area of the content that still shows the cam: painted over
    fix = f"delogo=x={px}:y={py}:w={pw}:h={ph}," if lay.get("patch") else ""
    main = f"crop={mw}:{mh}:{mx}:{my}," + fix + (pre + "," if pre else "") + fit_filter(c, W, H - cam_h)
    stack = "[cam][main]vstack" if lay["top"] else "[main][cam]vstack"
    return f"split[sc][sm];[sc]{cam}[cam];[sm]{main}[main];{stack},setsar=1"

def cap_place(c, H, lay=None):
    """(ASS alignment, margin in 288 px units) for captions. In split mode 'bottom' stays on the content panel
    (never over the face cam) and 'middle' sits on the seam between the two panels."""
    pos = c["cap_position"]; align, mv = {"bottom": 2, "middle": 5, "top": 8}[pos], c["cap_margin"]
    if lay:
        k = 288.0 / H; seam = lay["cam_h"] if lay["top"] else H - lay["cam_h"]
        if pos == "bottom" and not lay["top"]: mv += lay["cam_h"] * k
        elif pos == "middle": align, mv = 2, max(0.0, (H - seam) * k - 0.6 * c["cap_size"])
    return align, mv

# ───────────────────────────── smart moment picking and post text (pure functions) ─────────────────────────────
# ───────────────────── Hinglish (Hindi written in English letters) ─────────────────────
# Whisper has no "Hinglish" language. Clipper transcribes it as Hindi, nudges the model to write English letters with a
# Roman-script prompt, and converts any Devanagari that still comes out. English words come out of Whisper as normal letters.
HINGLISH_NAMES = {"hinglish", "hi-latn", "hi-en", "hi_en", "hindi-english", "hien"}
HINGLISH_PROMPT = ("Aaj hum is video mein baat karenge. Haan bhai, yeh kaafi mazedaar hai, toh chalo shuru karte hain. "
                   "Matlab dekho, mujhe lagta hai ki sab log isse pasand karenge, kyunki yeh bilkul alag hai.")

def is_hinglish(lang):
    return str(lang or "").strip().lower().replace(" ", "") in HINGLISH_NAMES

def hinglish_note(c):
    return " The video is in Hinglish (Hindi written in English letters). Write the title, hook and caption in the same natural Hinglish style, never in Devanagari." if is_hinglish(c.get("language")) else ""

_DEV_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh",
             "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l",
             "ळ": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h"}
_DEV_NUKTA = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f"}
_DEV_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_DEV_MATRAS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e"}
_DEV_PUNCT = str.maketrans({"।": ".", "॥": ".", **{chr(0x966 + i): str(i) for i in range(10)}})
# Words whose usual Hinglish spelling differs from a letter-by-letter conversion: common Hindi words and English words Whisper writes in Devanagari.
_DEV_WORDS = {w: r for r, ws in {
    "mein": "में", "main": "मैं", "hai": "है", "hain": "हैं", "nahi": "नहीं नही", "ki": "कि की", "ka": "का", "ke": "के", "ko": "को", "se": "से", "aur": "और", "yeh": "यह",
    "ye": "ये", "woh": "वो वह", "kya": "क्या", "kuch": "कुछ", "toh": "तो", "bhi": "भी", "hi": "ही", "hum": "हम", "aap": "आप", "tum": "तुम", "mujhe": "मुझे", "tujhe": "तुझे",
    "hamein": "हमें", "use": "उसे", "isse": "इसे इससे", "kaise": "कैसे", "kyun": "क्यों", "kahan": "कहाँ कहां", "yahan": "यहाँ यहां", "wahan": "वहाँ वहां", "jahan": "जहाँ जहां",
    "kab": "कब", "jab": "जब", "tab": "तब", "ab": "अब", "bahut": "बहुत", "accha": "अच्छा", "theek": "ठीक", "lekin": "लेकिन", "par": "पर", "tha": "था", "thi": "थी", "the": "थे",
    "ho": "हो", "hoon": "हूँ हूं", "raha": "रहा", "rahi": "रही", "rahe": "रहे", "kar": "कर", "karo": "करो", "karna": "करना", "sab": "सब", "koi": "कोई", "ek": "एक", "do": "दो",
    "bhai": "भाई", "yaar": "यार", "arre": "अरे", "okay": "ओके ओके।", "haan": "हाँ हां", "na": "ना", "ji": "जी", "matlab": "मतलब", "bilkul": "बिल्कुल", "aaj": "आज", "kal": "कल",
    "video": "वीडियो विडियो", "videos": "वीडियोज", "audio": "ऑडियो", "phone": "फोन फ़ोन", "mobile": "मोबाइल", "computer": "कंप्यूटर कम्प्यूटर", "internet": "इंटरनेट",
    "youtube": "यूट्यूब यूट्यूब", "channel": "चैनल", "subscribe": "सब्सक्राइब", "like": "लाइक", "share": "शेयर", "comment": "कमेंट कमेन्ट", "reel": "रील", "reels": "रील्स",
    "story": "स्टोरी", "post": "पोस्ट", "online": "ऑनलाइन", "offline": "ऑफलाइन", "game": "गेम", "gaming": "गेमिंग", "gameplay": "गेमप्ले", "live": "लाइव", "stream": "स्ट्रीम",
    "top": "टॉप", "best": "बेस्ट", "life": "लाइफ", "time": "टाइम", "please": "प्लीज प्लीज़", "sorry": "सॉरी", "thank you": "थैंक्यू", "friend": "फ्रेंड", "friends": "फ्रेंड्स",
    "family": "फैमिली", "message": "मैसेज", "call": "कॉल", "number": "नंबर", "bank": "बैंक", "car": "कार", "bus": "बस", "train": "ट्रेन", "hotel": "होटल", "movie": "मूवी",
    "film": "फिल्म", "song": "सॉन्ग", "music": "म्यूजिक", "party": "पार्टी", "boss": "बॉस", "office": "ऑफिस", "job": "जॉब", "project": "प्रोजेक्ट", "problem": "प्रॉब्लम",
    "serious": "सीरियस", "start": "स्टार्ट", "stop": "स्टॉप", "school": "स्कूल", "college": "कॉलेज", "doctor": "डॉक्टर", "police": "पुलिस", "special": "स्पेशल", "level": "लेवल",
    "result": "रिजल्ट", "real": "रियल", "first": "फर्स्ट", "next": "नेक्स्ट", "full": "फुल", "free": "फ्री", "paise": "पैसे", "log": "लोग", "kam": "कम", "zyada": "ज़्यादा ज्यादा",
}.items() for w in ws.split()}

def _dev_word(w):
    """One Devanagari word to Hinglish: letter by letter, with the schwa dropped where Hindi drops it (kamla, not kamala)."""
    import unicodedata
    known = _DEV_WORDS.get(unicodedata.normalize("NFC", w))
    if known: return known
    w = unicodedata.normalize("NFD", w).replace("ज्ञ", "ग्य")
    units, i, n = [], 0, len(w)  # unit = [consonant, vowel, tail, kept_vowel]
    while i < n:
        ch = w[i]; i += 1
        if ch in _DEV_CONS:
            cons = _DEV_CONS[ch]
            if i < n and w[i] == "\u093c": cons = _DEV_NUKTA.get(ch, cons); i += 1
            if i < n and w[i] in _DEV_MATRAS: units.append([cons, _DEV_MATRAS[w[i]], "", False]); i += 1
            elif i < n and w[i] == "\u094d": units.append([cons, "", "", False]); i += 1
            else: units.append([cons, "a", "", True])  # the inherent "a" is the only vowel that may be dropped
        elif ch in _DEV_VOWELS: units.append(["", _DEV_VOWELS[ch], "", False])
        elif ch in "\u0902\u0901" and units: units[-1][2] += "n"
        elif ch == "\u0903" and units: units[-1][2] += "h"
        elif ch in _DEV_MATRAS and units and not units[-1][1]: units[-1][1] = _DEV_MATRAS[ch]
    if len(units) > 1 and units[-1][3] and not units[-1][2]: units[-1][1] = ""  # final "a" is silent: kamal
    for k in range(1, len(units) - 1):  # a middle "a" between two vowel sounds is silent: kamla, not kamala
        if units[k][3] and not units[k][2] and units[k - 1][1] and units[k + 1][1]: units[k][1] = ""
    for k, u in enumerate(units):
        if u[0] == "v" and k and not units[k - 1][1]: u[0] = "w"  # swagat, dwar
        if u[0] and u[1] == "e" and u[2].startswith("n"): u[1] = "ei"  # ismein, hamein
    last = units[-1] if units else None
    if last and last[0]:  # word endings are short in Hinglish: lagta, nahi, tu
        if last[1] == "ee": last[1] = "i"
        elif last[1] == "oo" and not last[2]: last[1] = "u"
        elif last[1] == "aa" and not last[2]: last[1] = "a"
    return "".join(c + v + t for c, v, t, _ in units)

def hinglish_text(text, sentence=False):
    """Convert any Devanagari in the text to Hinglish. Text already in English letters is returned unchanged.
    sentence=True also capitalises the first letter, for a whole caption line (not for single words)."""
    if not re.search(r"[\u0900-\u097F]", text): return text
    def run(m):
        pieces = re.split(r"([.0-9]+)", m.group(0).translate(_DEV_PUNCT))
        return "".join(p if re.fullmatch(r"[.0-9]*", p) else _dev_word(p) for p in pieces)
    out = re.sub(r"[\u0900-\u097F]+", run, text)
    if sentence: out = re.sub(r"(^\s*|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)  # capital letters after . ? !
    return out

STOP = frozenset("a an the and or but if of to in on at for with is are was were be been being it its this that these those i you he she we they me him her us them my your our "
                 "their so as by from not no do does did have has had will would can could just very really like um uh yeah okay oh well there here then than too also about into "
                 "out up down over one get got go going gonna know think mean say said".split())
STOP |= frozenset("hai hain tha thi the ho hoon toh ki ka ke ko se mein par pe ye yeh wo woh vo aur bhi hi nahi nahin na kya kuch koi hum tum aap main mai mera meri mere tera teri tere "
                  "uska uski uske isme usme jo jab tab ab phir fir ek bahut kar karo karna kiya hua hue raha rahi rahe lekin magar agar haan matlab bas bhai yaar".split())  # Hinglish
HOOK_WORDS = frozenset("secret secrets mistake mistakes truth never always nobody everyone everybody stop wrong worst best biggest hidden crazy insane shocking unbelievable actually reason "
                       "why how what imagine warning dangerous free easy fastest simple trick hack lesson changed regret wish scam lie lied exposed banned problem "
                       "raaz sach galti kabhi sabse khatarnak jhooth dhoka asli sacchai".split())
EMO_WORDS = frozenset("love hate amazing awesome incredible crazy insane wild scary terrifying funny hilarious laugh cry angry furious shocked shocking disgusting beautiful perfect terrible "
                      "horrible worst best unbelievable won lost died dead killed scared excited epic legend nightmare dream heartbreaking proud embarrassing wtf omg damn holy "
                      "pagal kamaal zabardast mast maza mazedaar dard khushi gussa darr badhiya ekdum".split())
WEAK_START = re.compile(r"^(and|but|so|um|uh|well|yeah|yes|no|okay|ok|anyway|because|which|then|also|or|like|right|hmm|oh)\b", re.I)
DANGLING = re.compile(r"^(it|that|this|they|he|she|them|those|these|there)\b", re.I)  # points back at something said earlier
GREETING = re.compile(r"\b(welcome back|what'?s up|hey guys|hi guys|hello everyone|hello guys|how'?s everybody|good morning|good evening|thanks for watching|thank you for watching|"
                      r"see you (in|on) the next|subscribe|smash that|hit the like|like and subscribe|before we (get|start|begin)|in today'?s (video|stream)|let'?s get into it)\b", re.I)
SPONSOR = re.compile(r"\b(sponsor(ed)?|promo code|discount code|use code|link in the (description|bio)|affiliate|brought to you by|patreon|merch)\b", re.I)
FILLERS = frozenset("um uh er ah so and but like okay ok yeah well right hmm".split())
SPAM_TAGS = frozenset("fyp foryou foryoupage viral trending explore follow like instagood reels shorts tiktok trend followme viralvideo".split())
YT_CATEGORIES = {"People & Blogs": "22", "Gaming": "20", "Entertainment": "24", "Comedy": "23", "Education": "27", "Howto & Style": "26", "Science & Technology": "28",
                 "Sports": "17", "News & Politics": "25", "Film & Animation": "1", "Music": "10", "Pets & Animals": "15", "Travel & Events": "19", "Autos & Vehicles": "2"}
RATE_KEYS = (("s_hook", 1.5), ("s_payoff", 1.2), ("s_alone", 1.2), ("s_emotion", 1.0), ("s_share", 1.0))
WHY = {"hook": "strong opening", "emo": "emotional language", "idf": "specific, information-dense", "wps": "good speaking pace", "dead": "little dead air",
       "quest": "asks a question", "len": "good length", "peaks": "loud, high-energy peaks", "loud": "energetic audio", "dyn": "dynamic delivery", "quiet": "no quiet stretches"}

def parse_loudness(lines):
    """RMS level in dB for each analysis frame, from ffmpeg's astats metadata output."""
    out = []
    for ln in lines:
        m = re.search(r"lavfi\.astats\.Overall\.RMS_level=(\S+)", ln)
        if m:
            try: v = float(m.group(1))
            except ValueError: v = -80.0
            out.append(max(-80.0, v) if math.isfinite(v) else -80.0)
    return out

def hook_score(text):
    """How strongly the opening of a moment grabs attention (higher is better; penalties are applied separately)."""
    t = text.strip(); ws = re.findall(r"[a-z']+", t.lower())
    s = 1.2 if "?" in t[:220] else 0.0
    s += 0.5 * min(3, sum(w in HOOK_WORDS for w in ws))
    s += 0.5 if re.search(r"\d", t) else 0.0
    s += 0.4 if re.search(r"\byou(r|'re|'ll)?\b", t, re.I) else 0.0
    s += 0.3 if "!" in t else 0.0
    s += 0.4 if len(ws) >= 8 else 0.0  # something is actually being said
    return s

def rank_windows(segs, total, min_len, max_len, loud=None, hop=0.5, relaxed=False):
    """Score every sentence-aligned window of the allowed length on transcript and audio signals.
    Returns dicts (i, j = first and last segment, start, end, raw, heur 0-10, why) sorted best first."""
    n = len(segs)
    if n == 0: return []
    texts = [str(s["text"]).strip() for s in segs]
    toks = [re.findall(r"[a-z0-9']+", t.lower()) for t in texts]
    df = Counter(w for ts in toks for w in set(ts) if w not in STOP and len(w) > 2)
    idf = {w: math.log((n + 1) / (k + 1)) + 1 for w, k in df.items()}
    cont = [[w for w in ts if w not in STOP and len(w) > 2] for ts in toks]
    ends = [bool(re.search(r"[.!?…][\"')\]]*$", t)) for t in texts]
    gap = [segs[k + 1]["start"] - segs[k]["end"] if k + 1 < n else 9.0 for k in range(n)]
    pre = lambda v: [0.0] + list(itertools.accumulate(v))  # prefix sums: any window total in O(1)
    P_wc, P_sp = pre(len(ts) for ts in toks), pre(max(0.0, s["end"] - s["start"]) for s in segs)
    P_idf, P_ct = pre(sum(idf[w] for w in ct) for ct in cont), pre(len(ct) for ct in cont)
    P_emo = pre(sum(w in EMO_WORDS for w in ts) + 0.5 * t.count("!") for ts, t in zip(toks, texts))
    P_q, P_bad = pre(min(2, t.count("?")) for t in texts), pre(1 if (GREETING.search(t) or SPONSOR.search(t)) else 0 for t in texts)
    hook = []
    for i in range(n):
        j, parts = i, []
        while j < n and len(parts) < 3 and segs[j]["start"] - segs[i]["start"] < 8: parts.append(texts[j]); j += 1
        hook.append(hook_score(" ".join(parts)))
    weak, dang = [bool(WEAK_START.match(t)) for t in texts], [bool(DANGLING.match(t)) for t in texts]
    if loud:
        med = sorted(loud)[len(loud) // 2]  # judge loudness against this video's own normal level
        P_db, P_db2 = pre(loud), pre(v * v for v in loud)
        P_pk, P_qt = pre(1 if v > med + 6 else 0 for v in loud), pre(1 if v < med - 15 else 0 for v in loud)
    rows = []
    for i in range(n):
        a = segs[i]["start"]
        if not relaxed and i > 0 and not (ends[i - 1] or gap[i - 1] >= 0.5): continue  # start where a sentence or pause begins
        for j in range(i, n):
            b = segs[j]["end"]; L = b - a
            if L > max_len: break
            if L < min_len: continue
            if not relaxed and not (ends[j] or gap[j] >= 0.6): continue  # end where a sentence or pause ends
            rows.append((i, j, a, b, L))
    if not rows: return []
    F = {k: [] for k in ("hook", "emo", "idf", "wps", "dead", "quest", "len", "peaks", "loud", "dyn", "quiet")}
    target = min_len + 0.35 * (max_len - min_len)
    for i, j, a, b, L in rows:
        words, sp = P_wc[j + 1] - P_wc[i], P_sp[j + 1] - P_sp[i]
        F["hook"].append(hook[i]); F["emo"].append((P_emo[j + 1] - P_emo[i]) / max(words, 1) * 100)
        F["idf"].append((P_idf[j + 1] - P_idf[i]) / max(P_ct[j + 1] - P_ct[i], 1))
        F["wps"].append(1 - min(1.0, abs(words / L - 2.7) / 2.7)); F["dead"].append(max(0.0, 1 - sp / L))
        F["quest"].append(P_q[j + 1] - P_q[i]); F["len"].append(-abs(L - target) / max(max_len - min_len, 1))
        if loud:
            fa = min(int(a / hop), len(loud) - 1); fb = min(len(loud), max(int(math.ceil(b / hop)), fa + 1)); nf = fb - fa
            mean = (P_db[fb] - P_db[fa]) / nf; var = max((P_db2[fb] - P_db2[fa]) / nf - mean * mean, 0.0)
            F["peaks"].append((P_pk[fb] - P_pk[fa]) / nf); F["loud"].append(mean); F["dyn"].append(math.sqrt(var)); F["quiet"].append((P_qt[fb] - P_qt[fa]) / nf)
    def z(v):
        m = sum(v) / len(v); sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v)) or 1.0
        return [max(-2.5, min(2.5, (x - m) / sd)) for x in v]
    W = {"hook": 1.5, "emo": 0.7, "idf": 0.5, "wps": 0.4, "dead": -0.8, "quest": 0.3, "len": 0.2, "peaks": 0.9, "loud": 0.4, "dyn": 0.3, "quiet": -0.4}
    Z = {k: z(v) for k, v in F.items() if v}
    wins = []
    for r, (i, j, a, b, L) in enumerate(rows):
        raw = sum(W[k] * Z[k][r] for k in Z)
        if P_bad[j + 1] - P_bad[i]: raw -= 3.0  # greeting, sponsor read or "subscribe" inside the clip
        raw -= 1.0 if weak[i] else (0.8 if dang[i] else 0.0)  # opens mid-thought
        raw += (0.6 if ends[j] else 0.0) + (0.3 if gap[j] >= 0.6 else 0.0)  # ends cleanly
        wins.append({"i": i, "j": j, "start": a, "end": b, "raw": raw, "heur": 10 / (1 + math.exp(-raw / 2.5)), "r": r})
    wins.sort(key=lambda w: -w["raw"])
    for w in wins[:2000]:
        contrib = sorted(((W[k] * Z[k][w["r"]], k) for k in Z), reverse=True)
        w["why"] = [WHY[k] for v, k in contrib[:3] if v > 0.3]
    return wins

def select_candidates(wins, k, max_overlap=0.25):
    """The k best windows that do not substantially overlap each other."""
    out = []
    for w in wins:
        if all(min(w["end"], o["end"]) - max(w["start"], o["start"]) <= max_overlap * min(w["end"] - w["start"], o["end"] - o["start"]) for o in out):
            out.append(w)
            if len(out) >= k: break
    return out

def refine_bounds(w, segs, min_len, total):
    """Final start and end of a chosen window: skip a few leading filler words, keep a little room around the speech."""
    i, j = w["i"], w["j"]; a, b = float(segs[i]["start"]), float(segs[j]["end"])
    words = segs[i].get("words") or []
    k = 0
    while (k < min(3, len(words) - 1) and re.sub(r"[^\w']", "", str(words[k].get("word", "")).lower()) in FILLERS
           and b - float(words[k + 1]["start"]) >= min_len):
        k += 1
    prev_end = float(segs[i - 1]["end"]) if i > 0 else 0.0
    if k: a = max(float(words[k - 1]["end"]), float(words[k]["start"]) - 0.12)
    else: a = max(prev_end, a - 0.15)
    last = segs[j].get("words") or []
    if last: b = max(b, float(last[-1]["end"]))
    nxt = float(segs[j + 1]["start"]) if j + 1 < len(segs) else total
    return max(0.0, a), min(total, b + 0.3, max(b, nxt - 0.02))

def clip_text(t, n=1400):
    return t if len(t) <= n else t[:n * 7 // 10].rstrip() + " ... " + t[-n * 27 // 100:].lstrip()

def rerank_prompt(items, c):
    """items: (id, seconds, text). Asks the model to rate each candidate and write its title, hook, caption, hashtags and keywords."""
    niche = (f" The channel's niche: {c['audience']}." if c["audience"] else "") + hinglish_note(c)
    focus = f" Prefer moments about: {c['focus']}." if c["focus"] else ""
    body = "\n\n".join(f"[id {i} | {sec:.0f}s]\n{txt}" for i, sec, txt in items)
    return ("You are an expert short-form video editor and growth strategist." + niche + focus + "\n"
            "Below are candidate moments from a longer video, each with an id and its transcript. Judge each one the way a viewer scrolling Reels, Shorts or TikTok would: "
            "would they stop, watch to the end, and share it?\n"
            "Rate every candidate from 1 to 10 on: s_hook (how strong the first 3 seconds are: curiosity, tension, a bold claim, a question), "
            "s_payoff (a complete, satisfying point or story by the end), s_alone (makes sense with no other context), "
            "s_emotion (funny, surprising, inspiring or intense), s_share (people would send it to a friend or comment).\n"
            "Be honest: most moments are average. Use the whole 1-10 range and give weak moments low scores.\n"
            "For every candidate also write:\n"
            "- title: at most 60 characters, starts with the most searchable keyword or the hook, specific, honest, no hashtags, no emojis\n"
            "- hook: on-screen text for the first 2 seconds, 3 to 6 words\n"
            "- caption: one or two short sentences. The first one is a hook. End with a question that invites comments. At most 220 characters, no hashtags\n"
            "- hashtags: 3 to 5 lowercase words without the # sign: one broad topic tag and two to four specific niche tags. No generic spam like fyp or viral\n"
            "- keywords: 4 to 8 phrases people would search to find this\n"
            "Only describe what is in the transcript; never invent facts. Write in the language of the transcript.\n"
            "Return valid JSON only: "
            '{"clips":[{"id":1,"s_hook":7,"s_payoff":8,"s_alone":9,"s_emotion":6,"s_share":7,"title":"...","hook":"...","caption":"...","hashtags":["..."],"keywords":["..."]}]}\n\n'
            "Candidates:\n" + body)

def parse_rating(item, valid_ids):
    """One rated candidate from the AI -> (id, score 1-10, metadata), or None when unusable."""
    if not isinstance(item, dict): return None
    try: cid = int(item.get("id"))
    except (TypeError, ValueError): return None
    if cid not in valid_ids: return None
    vals = [(_num(item.get(k), 1, 10), wgt) for k, wgt in RATE_KEYS]
    if all(v is None for v, _ in vals): return None
    score = sum((5.0 if v is None else v) * wgt for v, wgt in vals) / sum(wgt for _, wgt in vals)
    tags, kws = item.get("hashtags"), item.get("keywords")
    return cid, score, {"title": str(item.get("title") or ""), "hook": str(item.get("hook") or ""), "caption": str(item.get("caption") or ""),
                        "hashtags": [str(t) for t in tags] if isinstance(tags, list) else [], "keywords": [str(k) for k in kws] if isinstance(kws, list) else [],
                        "ratings": {k: v for (k, _), (v, _w) in zip(RATE_KEYS, vals) if v is not None}}

def clean_title(s, limit=60):
    if s is None: return ""
    s = re.sub(r"\s+", " ", re.sub(r"https?://\S+|#\w+", "", str(s))).strip(" \"'-–—:;,.")
    if len(s) > 6 and s.isupper(): s = s.capitalize()
    if len(s) > limit: s = s[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-–—")
    return s

def clean_tags(tags, extra, n):
    """Lowercase, de-duplicated hashtag words. The user's own tags come first and are never filtered; generic spam tags are dropped."""
    out, seen = [], set()
    for t, own in [(t, True) for t in extra] + [(t, False) for t in tags or []]:
        t = re.sub(r"\W", "", str(t).lstrip("#").lower())
        if not (3 <= len(t) <= 30) or t in seen or t.isdigit() or (not own and t in SPAM_TAGS): continue
        seen.add(t); out.append(t)
    return out[:max(n, 1)]

def compose_caption(caption, tags, credit=""):
    parts = [str(caption).strip(), credit, " ".join("#" + t for t in tags)]
    return "\n\n".join(p for p in parts if p)

BORING_WORDS = STOP | frozenset("what when where which while would should because every thing things something anything people actually still even much many more most some such only other "
                                "been after before again never always around little being their them then told tell said says okay yeah right".split())

def keyword_idf(segs):
    """How rare each word is across the whole video (rare words make better hashtags than common ones)."""
    docs = [set(re.findall(r"[a-z]{4,}", str(s["text"]).lower())) for s in segs]
    df = Counter(w for d in docs for w in d)
    return {w: math.log((len(docs) + 1) / (k + 1)) + 1 for w, k in df.items()}

def fallback_meta(text, idf=None):
    """Title, caption and hashtags from the transcript alone, for when the AI is off or gave nothing usable."""
    first = re.split(r"(?<=[.!?])\s+", text.strip())[0] if text.strip() else ""
    tf = Counter(w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in BORING_WORDS)
    words = [w for w, _ in sorted(tf.items(), key=lambda kv: -kv[1] * (idf or {}).get(kv[0], 1.0))]
    return {"title": clean_title(first) or "Highlight", "caption": clean_title(first, 200) or "Highlight", "hashtags": words[:4], "keywords": words[:6]}

def youtube_description(desc, src):
    """Caption + original link + hashtags with #Shorts first (YouTube shows the first three hashtags above the title)."""
    paras = [q.strip() for q in re.split(r"\n\s*\n", str(desc).strip()) if q.strip()]
    tag_i = next((k for k in range(len(paras) - 1, -1, -1) if re.fullmatch(r"(#\w+\s*)+", paras[k])), None)
    if tag_i is None: paras.append("#Shorts"); tag_i = len(paras) - 1
    elif "#shorts" not in paras[tag_i].lower(): paras[tag_i] = "#Shorts " + paras[tag_i]
    if str(src).startswith("http"): paras.insert(tag_i, "Original video: " + str(src))
    return "\n\n".join(paras)

def youtube_fields(title, description, keywords, src):
    desc = youtube_description(description, src)
    tags, size = [], 0
    for t in [str(k) for k in keywords or []] + re.findall(r"#(\w+)", desc):
        t = t.strip().lstrip("#")
        if t and t.lower() not in {x.lower() for x in tags} and size + len(t) + 1 <= 450: tags.append(t); size += len(t) + 1
    return {"title": re.sub(r"\s+", " ", str(title)).strip()[:100], "description": desc, "tags": tags}

def post_text(cl, yt):
    out = [f"TITLE\n{cl['title']}", f"CAPTION (Instagram Reels / TikTok)\n{cl['description']}",
           f"YOUTUBE SHORTS\nTitle: {yt['title']}\nTags: {', '.join(yt['tags'])}\nDescription:\n{yt['description']}"]
    if cl.get("hook"): out.append(f"ON-SCREEN HOOK\n{cl['hook']}")
    if cl.get("score") is not None: out.append(f"WHY THIS MOMENT\nScore {cl['score']}/10" + (": " + ", ".join(cl["why"]) if cl.get("why") else ""))
    return "\n\n".join(out) + "\n"

def exclude_cam(main, cam, mode):
    """Keep the face cam out of the content panel when the cam box sits on top of the content box.
    main and cam are (x, y, w, h) in the same pixel space. Returns (content crop, patch, how):
    'crop' = the biggest part of the content box that doesn't touch the cam,
    'fill' = the whole content box with the cam area (patch, relative to the crop) painted over,
    'none' = no overlap, or mode 'keep'. Mode 'auto' crops when that keeps at least half of the content, else fills."""
    mx, my, mw, mh = main; cx, cy, cw, ch = cam
    pad = max(2, round(mw * 0.012))  # a little margin around the cam so no edge of the face is left
    ox0, oy0, ox1, oy1 = max(mx, cx - pad), max(my, cy - pad), min(mx + mw, cx + cw + pad), min(my + mh, cy + ch + pad)
    if mode == "keep" or ox0 >= ox1 or oy0 >= oy1: return main, None, "none"
    def usable(r):
        x, y, w, h = r; w, h = w // 2 * 2, h // 2 * 2
        return (x, y, w, h) if w >= 16 and h >= 16 else None
    cands = [usable(r) for r in ((mx, my, mw, oy0 - my), (mx, oy1, mw, my + mh - oy1), (mx, my, ox0 - mx, mh), (ox1, my, mx + mw - ox1, mh))]
    best = max((r for r in cands if r), key=lambda r: r[2] * r[3], default=None)
    if mode != "fill" and best and (mode == "crop" or best[2] * best[3] >= 0.5 * mw * mh): return best, None, "crop"
    px, py = max(1, ox0 - mx), max(1, oy0 - my)
    pw, ph = min(ox1 - mx, mw - 1) - px, min(oy1 - my, mh - 1) - py
    return (main, (px, py, pw, ph), "fill") if pw >= 2 and ph >= 2 else (main, None, "none")

def parse_region(s):
    """'x,y,w,h' as fractions of the video -> tuple, or None when missing or invalid."""
    try: v = [float(x) for x in str(s).split(",")]
    except ValueError: return None
    if len(v) != 4 or not all(math.isfinite(x) for x in v): return None
    x, y, w, h = v
    return (x, y, w, h) if 0 <= x < 1 and 0 <= y < 1 and 0.02 <= w <= 1 and 0.02 <= h <= 1 and x + w <= 1.001 and y + h <= 1.001 else None

def fmt_region(r): return ",".join(f"{v:.4f}" for v in r)

def region_px(frac, sw, sh):
    """A fractional region as an even-sized crop (x, y, w, h) that stays inside a sw x sh picture."""
    fx, fy, fw, fh = frac
    w, h = min(max(16, int(fw * sw) // 2 * 2), sw // 2 * 2), min(max(16, int(fh * sh) // 2 * 2), sh // 2 * 2)
    return max(0, min(int(round(fx * sw)), sw - w)), max(0, min(int(round(fy * sh)), sh - h)), w, h

def layout_sizes(c):
    """Output width, height and the height of the face cam panel in split mode."""
    rw, rh = map(int, c["aspect"].split(":"))
    W = c["out_width"] // 2 * 2; H = round(W * rh / rw / 2) * 2
    return W, H, min(max(round(H * c["split_cam_pct"] / 100 / 2) * 2, 2), H - 2)

def creator_handle(info):
    """The uploader's @handle from yt-dlp's info, or '@ChannelName' when there is no handle."""
    uid = str(info.get("uploader_id") or "")
    if uid.startswith("@"): return uid
    name = re.sub(r"\s+", "", str(info.get("channel") or info.get("uploader") or ""))
    return "@" + name if name else ""

def build_wm_ass(c, W, H, text):
    """A one-line ASS file that shows the creator credit for the whole clip."""
    k = H / 288.0
    fs = max(8, round(c["wm_size"] * k))
    alpha = "%02X" % round(255 * (1 - max(10, min(100, c["wm_opacity"])) / 100))
    pos = c["wm_position"]
    align = {"top right": 9, "top left": 7, "bottom right": 3, "bottom left": 1}[pos]
    mv, mh = round(H * (0.05 if pos.startswith("top") else 0.07)), round(W * 0.04)
    font = re.sub(r"[,\r\n]", " ", str(c["edit_font"])).strip() or DEFAULT_FONT
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Wm,%s,%d,&H%sFFFFFF,&H000000FF,&H%s000000,&H00000000,-1,0,0,0,100,100,0,0,1,%d,0,%d,%d,%d,%d,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 5,0:00:00.00,9:59:59.00,Wm,,0,0,0,,%s\n"
            % (W, H, font, fs, alpha, alpha, max(1, round(0.8 * k)), align, mh, mh, mv, ass_text(text)))

class Cancelled(Exception): pass
class OllamaError(RuntimeError): pass  # a real model error (out of memory, bad request) is not worth retrying

# ───────────────────────────── pipeline (no UI code) ─────────────────────────────
class Pipeline:
    def __init__(self, cfg, src, emit, cancel):
        self.c, self.src, self.emit, self.cancel = cfg, src, emit, cancel
        self.proc, self.i = None, 1
        self.wm_text, self.regions = "", None
        self.video, self.work, self.creator, self.src_url = None, None, "", ""

    # helpers
    def log(self, m): self.emit("log", m)
    def step(self, i, name): self.i = i; self.emit("step", i, name)
    def prog(self, frac, text=""): self.emit("prog", self.i, frac, text)
    def check(self):
        if self.cancel.is_set(): raise Cancelled()
    def kill(self): kill_tree(self.proc)

    def run(self, cmd, cwd=None, on_line=None):
        self.check()
        # Resolve executables after refreshing the user PATH.
        refresh_paths()
        if cmd:
            resolved = which(cmd[0])
            if not resolved:
                raise RuntimeError(
                    f"'{cmd[0]}' is not installed or not on PATH. "
                    "Open the Setup page in Clipper to install it, then try again."
                )
            cmd = [resolved] + list(cmd[1:])
        try:
            self.proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, **proc_kw())
        except FileNotFoundError as e:
            raise RuntimeError(
                f"'{cmd[0]}' is not installed or not on PATH. "
                "Open the Setup page in Clipper to install it, then try again."
            ) from e
        out = []
        for line in self.proc.stdout:
            self.check()
            line = line.rstrip(); out.append(line)
            if on_line: on_line(line)
        self.proc.wait(); rc = self.proc.returncode; self.proc = None
        self.check()
        return rc, out

    def duration(self, path):
        ffprobe = which("ffprobe")
        if not ffprobe:
            raise RuntimeError("ffprobe (part of ffmpeg) is not installed or not on PATH. Open the Setup page to install it, then try again.")
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, timeout=20, **proc_kw())
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffprobe timed out while reading the video duration.") from None
        if result.returncode != 0:
            raise RuntimeError("ffprobe could not read the video duration: " + (result.stderr or "unknown error")[:300])
        try:
            duration = float(result.stdout.strip())
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError
            return duration
        except ValueError:
            raise RuntimeError("Couldn't read a valid video duration. Check that the file is valid and ffmpeg is working.") from None

    @staticmethod
    def find_video(work):
        v = [p for p in work.glob("video.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
        return v[0] if v else None

    # main flow
    def go(self):
        c, src = self.c, self.src
        if c["edit_enabled"]:
            c["num_clips"] = c["edit_clips"]
            self.log(f"Auto-edit is on: making {c['num_clips']} edited clip(s).")
        root = Path(c["output_dir"]).expanduser(); root.mkdir(parents=True, exist_ok=True)
        is_url = bool(re.match(r"https?://", src))
        if not is_url and not Path(src).is_file(): raise RuntimeError("That file doesn't exist.")
        # Fail fast with a clear message when required tools are missing.
        refresh_paths()
        missing = [t for t in ("ffmpeg", "ffprobe") if not which(t)]
        if is_url and not ytdlp_cmd(): missing.append("yt-dlp")
        if missing:
            names = ", ".join(missing)
            raise RuntimeError(
                f"{names} {'is' if len(missing) == 1 else 'are'} not installed or not on PATH. "
                "Open the Setup page in Clipper to install what's missing, then try again."
            )
        source_identity = src
        if not is_url:
            source_stat = Path(src).stat()
            source_identity = f"{src}:{source_stat.st_size}:{source_stat.st_mtime_ns}"
        # Quality is part of the key so changing it re-downloads instead of reusing the old file.
        key = hashlib.sha1(f"{source_identity}|q{c['max_height']}".encode()).hexdigest()[:10]
        work = root / ".cache" / key; work.mkdir(parents=True, exist_ok=True)

        self.step(1, STEPS[0])
        if is_url:
            video = self.find_video(work) if c["use_cache"] else None
            if video: self.log("Using the earlier download."); self.prog(1)
            else: video = self.download(work)
        else:
            video = Path(src); self.prog(1)
        total = self.duration(video)
        title = video.stem
        info, meta = work / "video.info.json", {}
        if info.exists():
            try: meta = json.loads(info.read_text(encoding="utf-8"))
            except ValueError: pass
        if not isinstance(meta, dict): meta = {}
        title = meta.get("title") or title
        self.wm_text = (c["wm_text"] or creator_handle(meta)) if c["wm_enabled"] else ""
        if c["wm_enabled"]: self.log(f"Watermark: {self.wm_text}" if self.wm_text else "Watermark: no creator name found (type one on the Watermark page), so it is skipped.")
        self.log(f"Video: {title} ({total / 60:.1f} min)")
        self.video, self.work = video, work
        self.creator = c["wm_text"] or creator_handle(meta)
        self.src_url = str(src) if str(src).startswith("http") else ""
        if c["split_enabled"]: self.regions = self.pick_regions(video, total, work)

        seg_file = work / transcript_cache_name(c)
        cached = False
        if c["use_cache"] and seg_file.exists():
            try:
                candidate_segs = json.loads(seg_file.read_text(encoding="utf-8"))
                if isinstance(candidate_segs, list) and all(
                    isinstance(x, dict) and math.isfinite(float(x["start"])) and math.isfinite(float(x["end"]))
                    and 0 <= float(x["start"]) <= float(x["end"]) <= total + 0.05
                    and isinstance(x.get("text"), str) and (not c["edit_enabled"] or words_ok(x)) for x in candidate_segs):
                    segs, cached = candidate_segs, True
                else:
                    self.log("Cached transcript was invalid; transcribing again.")
            except (OSError, ValueError, TypeError, KeyError):
                self.log("Cached transcript was unreadable; transcribing again.")
        if cached:
            self.step(2, STEPS[1]); self.prog(1); self.step(3, STEPS[2] + " (cached)"); self.prog(1)
            self.log("Using the earlier transcript.")
        else:
            self.step(2, STEPS[1]); audio = self.audio(video); self.prog(1)
            self.step(3, STEPS[2])
            try:
                segs = self.transcribe(audio, total)
            finally:
                try: os.unlink(audio)
                except OSError: pass
            seg_file.write_text(json.dumps(segs), encoding="utf-8")
        if not segs: raise RuntimeError("No speech found in this video.")
        self.log(f"Transcript: {len(segs)} segments.")

        self.step(4, STEPS[3]); clips = self.pick(segs, total)
        if not clips: raise RuntimeError("The AI didn't return usable clips. Try again, or turn off the AI picker.")
        self.prog(1)
        if c["review"]:
            clips = self.ask_review(clips, video, total)
            if not clips: raise RuntimeError("No clips were selected.")

        if c["edit_enabled"]:
            self.step(5, STEPS[4]); self.plan_edits(clips, segs); self.prog(1)

        safe = safe_dirname(title) or key
        out = root / safe if c["subfolder"] else root
        out.mkdir(parents=True, exist_ok=True)
        self.step(6, STEPS[5]); files = self.render(clips, segs, video, work, out)
        if c["write_json"]:
            manifest = out / "manifest.json"
            suffix = 2
            while manifest.exists():
                manifest = out / f"manifest_{suffix}.json"; suffix += 1
            manifest.write_text(json.dumps({"title": title, "source": src, "clips": clips}, indent=2), encoding="utf-8")
        return out, files

    def pick_regions(self, video, total, work):
        """Ask for the face-cam box and the content box. The content box is optional: unmarked means the full frame."""
        c = self.c
        saved_cam, saved_main = parse_region(c["split_cam"]), parse_region(c["split_main"])
        full_frame = (0.0, 0.0, 1.0, 1.0)
        if not c["split_ask"] and saved_cam:
            self.log("Split layout: using the saved face-cam and content areas.")
            return saved_cam, saved_main or full_frame
        holder = {"ev": threading.Event(), "res": None, "video": str(video), "duration": total,
                  "saved": saved_cam, "saved_main": saved_main, "work": str(work), "cfg": c}
        self.log("Split layout: mark the face cam and the content area in the window that opened. Leave the content box unmarked to use the full frame.")
        self.emit("regions", holder)
        while not holder["ev"].wait(0.2):
            try: self.check()
            except Cancelled: self.emit("regions_cancel"); raise
        if holder["res"] is None: raise Cancelled()
        return holder["res"]

    def download(self, work):
        c, h = self.c, self.c["max_height"]
        for f in work.glob("video.*"): f.unlink()
        cmd = (ytdlp_cmd() or ["yt-dlp"]) + ["--newline", "--no-playlist", "--write-info-json", "-f", f"bv*[height<={h}]+ba/b[height<={h}]/b",
               "--merge-output-format", "mp4", "-o", "video.%(ext)s"]
        ff, fp = which("ffmpeg"), which("ffprobe")  # tell yt-dlp exactly where ffmpeg is, so merging never depends on PATH
        if ff and fp and Path(ff).parent == Path(fp).parent: cmd += ["--ffmpeg-location", str(Path(ff).parent)]
        if c["cookies_browser"] != "none": cmd += ["--cookies-from-browser", c["cookies_browser"]]
        cmd += split_args(c["ytdlp_extra"]) + [self.src]
        n, parts = [0], [2]
        def on(line):
            # Count merged streams ("Downloading 1 format(s): 137+140") so a single-file
            # download fills the whole bar instead of stopping at half of it.
            f = re.search(r"format\(s\):\s*([\w.+=/-]+)", line)
            if f: parts[0] = f.group(1).count("+") + 1
            if "Destination:" in line: n[0] += 1
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m: self.prog(min(1, (max(n[0], 1) - 1 + float(m.group(1)) / 100) / parts[0]), f"{m.group(1)}%")
            elif "ERROR" in line: self.log(line)
        rc, out = self.run(cmd, cwd=work, on_line=on)
        v = self.find_video(work)
        if rc != 0 or not v: raise RuntimeError("Download failed:\n" + "\n".join(out[-4:]))
        return v

    def audio(self, video):
        """Extract a temporary mono 16 kHz WAV; avoid holding long audio in RAM."""
        self.check()
        ffmpeg = which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not installed or not on PATH. Open the Setup page to install it, then try again.")
        fd, audio_path = tempfile.mkstemp(prefix="clipper_audio_", suffix=".wav")
        os.close(fd)
        try:
            rc, lines = self.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio_path])
            if rc != 0 or not Path(audio_path).is_file() or Path(audio_path).stat().st_size <= 44:
                raise RuntimeError("Couldn't extract audio from the video: " + " | ".join(lines[-3:]))
            return audio_path
        except Exception:
            try: os.unlink(audio_path)
            except OSError: pass
            raise

    def transcribe(self, audio, total):
        from faster_whisper import WhisperModel
        import ctranslate2
        c = self.c
        # Decode the audio once, before any model is loaded. Reading the audio does not depend on the GPU, CPU or compute type,
        # so a failure here can never be fixed by the fallbacks below. Fail once with a clear message instead.
        try:
            from faster_whisper import decode_audio
            if ensure_av_compat(): self.log("PyAV 19 or newer found: working around the option it removed (Setup > PyAV can switch to a matching version).")
            self.emit("prog", self.i, None, "Reading audio")
            wave = decode_audio(str(audio), sampling_rate=16000)
        except Cancelled: raise
        except Exception as e:
            if is_av_mismatch(e): raise RuntimeError(av_mismatch_help()) from e
            raise RuntimeError(f"Whisper could not read the audio: {e}") from e
        gpu = c["device"] == "cuda" or (c["device"] == "auto" and ctranslate2.get_cuda_device_count() > 0)
        # CPU compute types to try in order: int8 (fastest) -> int8_float32 (compat) -> float32 (safest).
        # Fall back through compatible CPU compute types if the installed CTranslate2 build rejects one.
        cpu_types = ["int8", "int8_float32", "float32"]
        last_err = None
        for use_gpu in ([True, False] if gpu else [False]):
            if use_gpu:
                attempts = [("float16", "GPU")]
            else:
                attempts = [(ct, "CPU") for ct in cpu_types]
            for compute_type, label in attempts:
                try:
                    self.log(f"Loading Whisper '{c['whisper_model']}' on {label} (compute_type={compute_type}; first use downloads it)...")
                    self.emit("prog", self.i, None, "Loading model")
                    cpu_threads = max(1, min(8, (os.cpu_count() or 4) - 1))
                    model = WhisperModel(
                        c["whisper_model"], device="cuda" if use_gpu else "cpu",
                        compute_type=compute_type, cpu_threads=cpu_threads, num_workers=1)
                    hing = is_hinglish(c["language"])
                    it, info = model.transcribe(wave, language="hi" if hing else (c["language"] or None), initial_prompt=HINGLISH_PROMPT if hing else None,
                                                beam_size=c["beam_size"], vad_filter=c["vad_filter"], word_timestamps=bool(c["edit_enabled"]))
                    segs = []
                    for s in it:
                        self.check()
                        seg = {"start": s.start, "end": s.end, "text": hinglish_text(s.text.strip(), sentence=True) if hing else s.text.strip()}
                        if c["edit_enabled"]: seg["words"] = [{"start": w.start, "end": w.end, "word": hinglish_text(w.word) if hing else w.word} for w in (s.words or [])]
                        segs.append(seg)
                        self.prog(min(s.end / total, 1), f"{s.end / 60:.1f} of {total / 60:.1f} min")
                    return segs
                except Cancelled: raise
                except Exception as e:
                    if is_av_mismatch(e): raise RuntimeError(av_mismatch_help()) from e
                    last_err = e
                    if use_gpu:
                        self.log(f"GPU failed ({e}). Falling back to the CPU.")
                        break  # break inner loop, outer loop continues to CPU
                    else:
                        # On CPU, try the next compute_type before giving up.
                        if compute_type != cpu_types[-1]:
                            self.log(f"CPU with {compute_type} failed ({e}). Trying next mode...")
                        # If this was the last attempt, just let the loop end;
                        # the clear error message below will be shown.
        # If we get here, every attempt failed. Show a clear, actionable message.
        if last_err:
            raise RuntimeError(
                f"Whisper could not run. Last error: {last_err}\n"
                "Common fixes:\n"
                "  - If you have an NVIDIA GPU: install the CUDA 12 libraries from the Setup page (nvidia-cublas-cu12, nvidia-cudnn-cu12), then restart Clipper.\n"
                "  - If you don't have an NVIDIA GPU: go to the Transcribe settings page and set 'Run Whisper on' to 'cpu' (not 'auto').\n"
                "  - If you still get 'mkl_malloc: failed to allocate memory', close other apps to free RAM, or use a smaller Whisper model (tiny or base)."
                + ("\n  - If the error mentions a missing DLL: install the Microsoft Visual C++ Redistributable (winget install -e --id Microsoft.VCRedist.2015+.x64), then restart Clipper." if WIN else "")
            ) from last_err
        return []

    # picking moments
    def pick(self, segs, total):
        c, n = self.c, self.c["num_clips"]
        if c["smart_pick"]: return self.smart_pick(segs, total)
        if not c["use_ai"]:
            self.log("AI is off: spreading clips evenly.")
            return self.finish([{"start": total * (k + .5) / n} for k in range(n)], segs, total)
        host = c["ollama_host"].rstrip("/")
        if not ollama_up(host):
            raise RuntimeError("Ollama isn't running. Press Start on the Setup page, or run: ollama serve (or turn off the AI picker).")
        # group segments into ~20 second lines, then split into chunks that fit the context window
        lines, cur = [], None
        for s in segs:
            if cur and s["end"] - cur[0] < 20: cur[1] += " " + s["text"]
            else:
                if cur: lines.append(f"[{int(cur[0])}s] {cur[1]}")
                cur = [s["start"], s["text"]]
        lines.append(f"[{int(cur[0])}s] {cur[1]}")
        budget, chunks, part, size = int(max(c["num_ctx"] - 1500, 1000) * 3), [], [], 0
        for ln in lines:
            if size + len(ln) > budget and part: chunks.append(part); part, size = [], 0
            part.append(ln); size += len(ln) + 1
        chunks.append(part)
        per = n if len(chunks) == 1 else max(2, math.ceil(n * 2 / len(chunks)))
        if len(chunks) > 1: self.log(f"Long video: asking the AI in {len(chunks)} parts.")
        focus = (f"\nFocus on: {c['focus']}." if c["focus"] else "") + hinglish_note(c)
        cands = []
        for k, ch in enumerate(chunks, 1):
            prompt = (f"You select short-form video moments from a transcript.\n"
                      f"Choose up to {per} distinct, self-contained moments from THIS transcript part.{focus}\n"
                      "Prioritize: (1) a compelling hook in the first seconds, (2) a complete thought or story, "
                      "(3) useful, surprising, emotional, funny, or actionable content, and (4) clear context without invented facts.\n"
                      "Avoid greetings, dead air, incomplete sentences, repeated points, and moments that need missing context. "
                      "Do not invent dialogue or timestamps. Use the timestamp nearest the beginning of each moment. "
                      "Score consistently: 9-10 exceptional standalone moment; 7-8 strong; 5-6 usable; 1-4 weak (do not return weak moments).\n"
                      "For each return start time in seconds, score 1-10, concise title, and an accurate Instagram caption "
                      "with a hook, 1-2 useful sentences, and up to 5 relevant hashtags. Return valid JSON only in this shape: "
                      '{"clips":[{"start":123,"score":8,"title":"...","description":"..."}]}\n\nTranscript:\n' + "\n".join(ch))
            cands += self.ask(prompt, f"part {k}/{len(chunks)}")
            self.prog(k / len(chunks), f"part {k}/{len(chunks)}")
        return self.finish(cands, segs, total)

    def ask(self, prompt, tag, key="clips"):  # key=None returns the whole JSON object instead of one list
        c, host = self.c, self.c["ollama_host"].rstrip("/")
        for attempt in range(1, c["ai_retries"] + 1):
            text, tokens = "", 0
            body = {"model": c["ollama_model"], "prompt": prompt, "format": "json", "stream": True,
                    "options": {"num_ctx": c["num_ctx"], "temperature": c["temperature"]}}
            try:
                with ollama_post(host, "/api/generate", body, 900) as r:
                    for line in r:
                        self.check()
                        if not line.strip(): continue
                        d = json.loads(line)
                        if "error" in d: raise OllamaError("Ollama: " + d["error"])
                        text += d.get("response", ""); tokens += 1
                        if tokens % 5 == 0: self.emit("prog", self.i, None, f"AI thinking, {tag}, {tokens} tokens")
                        if d.get("done"): break
            except Cancelled:
                raise
            except OllamaError:
                raise  # a real model error will not fix itself; don't waste retries on it
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise RuntimeError(f"Model '{c['ollama_model']}' isn't installed. Download it on the AI picker page, or run: ollama pull {c['ollama_model']}") from None
                detail = e.read(200).decode(errors="replace")
                if attempt < c["ai_retries"]:
                    self.log(f"Ollama request failed ({e.code}; attempt {attempt}/{c['ai_retries']}); retrying...")
                    self.check(); time.sleep(min(attempt, 3)); self.check(); continue
                raise RuntimeError(f"Ollama error {e.code}: {detail}") from None
            except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
                if attempt < c["ai_retries"]:
                    self.log(f"Ollama stream failed ({e}; attempt {attempt}/{c['ai_retries']}); retrying...")
                    self.check(); time.sleep(min(attempt, 3)); self.check(); continue
                raise RuntimeError(f"Ollama request failed after {attempt} attempt(s): {e}") from e
            try:
                payload = json.loads(text)
                if key is None:
                    if isinstance(payload, dict): return payload
                    raise ValueError("JSON response was not an object")
                clips = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(clips, list):
                    return clips
                raise ValueError(f"JSON response did not contain a {key} list")
            except (ValueError, TypeError, AttributeError):
                self.log(f"Bad AI output (attempt {attempt}). Retrying...")
        return []

    # smart picker -------------------------------------------------------------------------------
    def loudness(self, hop=0.5):
        """RMS level every `hop` seconds from the video's audio (cached next to the video); None when it can't be measured."""
        f = self.work / f"loudness_{hop}.json"
        if self.c["use_cache"] and f.is_file():
            try:
                v = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(v, list) and len(v) >= 10 and all(isinstance(x, (int, float)) for x in v): return v
            except (OSError, ValueError): pass
        try:
            rc, lines = self.run(["ffmpeg", "-v", "error", "-i", str(self.video), "-vn", "-af",
                                  f"aresample=16000,asetnsamples=n={int(16000 * hop)}:p=0,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                                  "-f", "null", "-"])
        except RuntimeError: return None
        vals = parse_loudness(lines)
        if rc != 0 or len(vals) < 10:
            self.log("Audio energy couldn't be measured, so moments are ranked on the transcript only."); return None
        try: f.write_text(json.dumps([round(x, 1) for x in vals]), encoding="utf-8")
        except OSError: pass
        return vals

    def rerank(self, cands, segs):
        """Ask the AI to rate the candidates and write their metadata. Returns {candidate index: (score, metadata)}."""
        c = self.c
        items = [(k + 1, w["end"] - w["start"], clip_text(" ".join(s["text"].strip() for s in segs[w["i"]:w["j"] + 1]))) for k, w in enumerate(cands)]
        budget, batches, cur, size = int(max(c["num_ctx"] - 2500, 1500) * 3), [], [], 0
        for it in items:
            if cur and size + len(it[2]) > budget: batches.append(cur); cur, size = [], 0
            cur.append(it); size += len(it[2]) + 40
        batches.append(cur)
        ids, out = {it[0] for it in items}, {}
        for b, batch in enumerate(batches, 1):
            self.check()
            for raw in self.ask(rerank_prompt(batch, c), f"rating {b}/{len(batches)}"):
                r = parse_rating(raw, {it[0] for it in batch} & ids)
                if r and r[0] - 1 not in out: out[r[0] - 1] = (r[1], r[2])
            self.prog(0.2 + 0.7 * b / len(batches), f"rating {b}/{len(batches)}")
        self.log(f"AI rated {len(out)} of {len(cands)} candidates.")
        return out

    def smart_pick(self, segs, total):
        c, n = self.c, self.c["num_clips"]
        loud = self.loudness() if self.video else None
        self.prog(0.1, "scoring moments")
        wins = rank_windows(segs, total, c["min_len"], c["max_len"], loud)
        if len(wins) < max(3, n): wins = rank_windows(segs, total, c["min_len"], c["max_len"], loud, relaxed=True) or wins  # transcript without sentence punctuation
        if not wins:
            self.log("Smart picker: no clip of the requested length fits this transcript (check the shortest and longest clip settings). Spreading clips evenly.")
            return self.finish([{"start": total * (k + .5) / n} for k in range(n)], segs, total)
        cands = select_candidates(wins, min(24, max(n * 3, 8)))
        self.log(f"Smart picker: scored {len(wins)} possible clips ({'audio and transcript' if loud else 'transcript only'}); {len(cands)} strong candidates.")
        rated = {}
        if c["use_ai"]:
            if ollama_up(c["ollama_host"].rstrip("/")):
                try: rated = self.rerank(cands, segs)
                except (OllamaError, RuntimeError) as e: self.log(f"The AI rating failed ({e}). Using the audio and transcript scores only.")
            else: self.log("Ollama isn't reachable, so clips are chosen from the audio and transcript scores only.")
        ranked = []
        for k, w in enumerate(cands):
            llm = rated[k][0] if k in rated else None
            ranked.append((0.35 * w["heur"] + 0.65 * llm if llm is not None else w["heur"] * (0.85 if rated else 1.0), k, llm))
        ranked.sort(reverse=True)
        gate, out, used, sigs, skipped = c["pick_min_score"], [], [], [], 0
        for final, k, llm in ranked:
            if len(out) >= n: break
            if rated and (llm if llm is not None else final) < gate: skipped += 1; continue
            clip = self.make_clip(cands[k], segs, total, final, rated.get(k, (None, None))[1])
            sig = self._content_signature(clip["start"], clip["end"], segs)
            if any(clip["start"] < ue and clip["end"] > us for us, ue in used) or any(self._similar_content(sig, s) for s in sigs): continue
            out.append(clip); used.append((clip["start"], clip["end"])); sigs.append(sig)
        if not out and ranked:
            self.log("No moment reached the minimum AI score, so the best one is used anyway.")
            clip = self.make_clip(cands[ranked[0][1]], segs, total, ranked[0][0], rated.get(ranked[0][1], (None, None))[1]); out.append(clip)
        if len(out) < n: self.log(f"Only {len(out)} moment(s) were strong enough" + (f" ({skipped} rated below {gate:g})" if skipped else "") + ".")
        for k, cl in enumerate(out, 1): self.log(f"Clip {k}: {cl['score']}/10, {fmt_t(cl['start'])} to {fmt_t(cl['end'])}: {cl['title']}")
        return out

    def idf(self, segs):
        if getattr(self, "_idf", None) is None or self._idf[0] is not segs: self._idf = (segs, keyword_idf(segs))
        return self._idf[1]

    def make_clip(self, w, segs, total, final, meta):
        """A finished clip dict: refined start and end plus title, hook, caption, hashtags and keywords."""
        c = self.c
        a, b = refine_bounds(w, segs, c["min_len"], total)
        a, b = max(0.0, a - max(0.0, float(c["pad_start"]))), min(total, b + max(0.0, float(c["pad_end"])))
        fb, m = fallback_meta(" ".join(s["text"].strip() for s in segs[w["i"]:w["j"] + 1]), self.idf(segs)), meta or {}
        title = clean_title(m.get("title")) or fb["title"]
        caption = (m.get("caption") or "").strip() or fb["caption"]
        extras = [t for t in re.split(r"[\s,]+", c["extra_hashtags"]) if t.strip("#")]
        tags = clean_tags(m.get("hashtags") or fb["hashtags"], extras, c["hashtag_count"])
        credit = f"Credit: {self.creator}" if c["credit_line"] and self.creator else ""
        return {"start": a, "end": b, "title": title, "description": compose_caption(caption, tags, credit), "use": True, "score": round(final, 1),
                "hook": m.get("hook", "").strip(), "hashtags": tags, "keywords": m.get("keywords") or fb["keywords"], "why": w.get("why", []), "ratings": m.get("ratings", {})}

    def snap(self, start, segs, total):
        c = self.c
        i = min(range(len(segs)), key=lambda k: abs(segs[k]["start"] - start))
        if not c["snap"]: return start, min(total, start + (c["min_len"] + c["max_len"]) / 2), i
        a, j = segs[i]["start"], i
        while j < len(segs) - 1 and segs[j]["end"] - a < c["min_len"]: j += 1
        while j > i and segs[j]["end"] - a > c["max_len"]: j -= 1
        return a, segs[j]["end"], i

    @staticmethod
    def _content_signature(start, end, segs):
        """Build a normalized transcript signature for a candidate time range."""
        words = []
        for segment in segs:
            try:
                if float(segment["end"]) > start and float(segment["start"]) < end:
                    words.extend(re.findall(r"\w+", str(segment.get("text", "")).lower()))
            except (KeyError, TypeError, ValueError):
                continue
        return words

    @staticmethod
    def _similar_content(left, right):
        """Conservative duplicate test; short clips need near-identical wording."""
        if not left or not right:
            return False
        a, b = " ".join(left), " ".join(right)
        if min(len(left), len(right)) < 8:
            return a == b
        # Compare token sets and sequence similarity; require a high threshold to
        # avoid discarding distinct moments that merely share common vocabulary.
        sa, sb = set(left), set(right)
        jaccard = len(sa & sb) / max(1, len(sa | sb))
        sequence = SequenceMatcher(None, a, b, autojunk=False).ratio()
        return jaccard >= 0.82 or sequence >= 0.90

    def finish(self, cands, segs, total):
        c, out, used, used_content = self.c, [], [], []
        rejected = {}

        def reject(reason):
            rejected[reason] = rejected.get(reason, 0) + 1

        # A missing transcript cannot be snapped safely. Return no AI-picked clips
        # rather than letting an empty segment list crash the whole pipeline.
        if not segs:
            self.log("AI selection skipped: the transcript contains no usable segments.")
            return []
        try:
            total = float(total)
            if not math.isfinite(total) or total <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            self.log("AI selection skipped: the video duration is invalid.")
            return []

        # Normalize and validate before sorting. In particular, do not call .get()
        # on arbitrary Ollama output or allow NaN scores to affect sort ordering.
        valid = []
        if not isinstance(cands, (list, tuple)):
            cands = []
            reject("response was not a candidate list")
        for candidate in cands:
            if not isinstance(candidate, dict):
                reject("candidate was not an object")
                continue
            try:
                start = float(candidate["start"])
                if not math.isfinite(start) or start < 0 or start >= total:
                    reject("start timestamp was outside the video")
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                reject("start timestamp was missing or invalid")
                continue

            raw_score = candidate.get("score", 5)
            try:
                score = float(raw_score)
                if not math.isfinite(score) or not 1 <= score <= 10:
                    reject("score was outside 1-10 or invalid")
                    continue
            except (TypeError, ValueError, OverflowError):
                reject("score was outside 1-10 or invalid")
                continue
            valid.append((score, start, candidate))

        for score, start, candidate in sorted(valid, key=lambda item: -item[0]):
            try:
                a, b, i = self.snap(start, segs, total)
                a, b = float(a), float(b)
                if not (math.isfinite(a) and math.isfinite(b) and 0 <= a < b <= total):
                    reject("snapped clip boundaries were invalid")
                    continue
                signature = self._content_signature(a, b, segs)
                if any(self._similar_content(signature, previous) for previous in used_content):
                    reject("repeated transcript content")
                    continue
                title = str(candidate.get("title") or " ".join(segs[i]["text"].split()[:6])).strip()
                desc = str(candidate.get("description") or f"{title}\n\n#reels #shorts #fyp #viral #video").strip()
                pad_start = max(0.0, float(c["pad_start"]))
                pad_end = max(0.0, float(c["pad_end"]))
                clip_start = max(0.0, a - pad_start)
                clip_end = min(total, b + pad_end)
                if not (math.isfinite(clip_start) and math.isfinite(clip_end) and clip_start < clip_end):
                    reject("final clip boundaries were invalid")
                    continue
                # A candidate that overlaps an already selected clip is trimmed to fit,
                # losing as little as possible; only what still collides is dropped.
                for used_start, used_end in used:
                    if clip_start < used_end and clip_end > used_start:
                        if b <= used_start: clip_end = used_start           # sits before the old clip: shave the tail
                        elif a >= used_end: clip_start = used_end           # starts after the old clip: shave the head
                        elif used_end - clip_start <= clip_end - used_start: clip_start = used_end
                        else: clip_end = used_start
                if clip_end - clip_start < min(c["min_len"], b - a):
                    reject("duplicate or overlapping clip after trimming")
                    continue
                if any(clip_start < used_end and clip_end > used_start for used_start, used_end in used):
                    reject("duplicate or overlapping clip after padding")  # safety net
                    continue
            except (IndexError, KeyError, TypeError, ValueError, OverflowError):
                reject("candidate could not be converted into a clip")
                continue
            used.append((clip_start, clip_end))
            used_content.append(signature)
            out.append({"start": clip_start, "end": clip_end, "title": title, "description": desc, "use": True})
            if len(out) >= c["num_clips"]: break

        if rejected:
            details = "; ".join(f"{count} {reason}" for reason, count in rejected.items())
            self.log(f"AI selection: rejected {sum(rejected.values())} candidate(s) ({details}).")
        self.log(f"AI selection: kept {len(out)} of {len(cands)} candidate(s).")
        return sorted(out, key=lambda x: x["start"])

    def ask_review(self, clips, video, total):
        holder = {"ev": threading.Event(), "res": None,
                  "duration": total,
                  "min_len": self.c["min_len"], "max_len": self.c["max_len"]}
        self.emit("review", clips, str(video), holder)
        while not holder["ev"].wait(0.2):
            try:
                self.check()
            except Cancelled:
                self.emit("review_cancel")
                raise
        if holder["res"] is None: raise Cancelled()
        return [x for x in holder["res"] if x["use"]]

    # rendering
    def fit_expr(self, W, H): return fit_filter(self.c, W, H)
    def split_expr(self, W, H, pre, lay): return split_filter(self.c, W, H, pre, lay)

    def vf(self, W, H, sub, pre="", ass=None, wm=None, layout=None):
        c = self.c
        if layout: fit = self.split_expr(W, H, pre, layout)
        else:
            fit = self.fit_expr(W, H)
            if pre: fit = pre + "," + fit
        if sub:
            h = c["cap_color"].lstrip("#").upper()
            h = h if re.fullmatch(r"[0-9A-F]{6}", h) else "FFFFFF"
            al, mv = cap_place(c, H, layout)
            fit += (f",subtitles={sub}:force_style='FontSize={c['cap_size']},Alignment={al},Outline={c['cap_outline']},"
                    f"Bold={int(c['cap_bold'])},MarginV={mv:.0f},PrimaryColour=&H00{h[4:6]}{h[2:4]}{h[0:2]}'")
        if ass: fit += ",ass=" + ass
        if wm: fit += ",ass=" + wm
        return fit

    # auto-edit: planning
    def plan_edits(self, clips, segs):
        c = self.c; host = c["ollama_host"].rstrip("/")
        ai = c["use_ai"] and ollama_up(host)
        if c["use_ai"] and not ai: self.log("Ollama isn't reachable, so the edits are planned with simple rules.")
        for k, cl in enumerate(clips, 1):
            self.check()
            length = cl["end"] - cl["start"]
            words = clip_words(segs, cl["start"], cl["end"])
            raw = None
            if ai and words:
                try: raw = self.ask(plan_prompt(words, length, c["focus"]), f"edit plan {k}/{len(clips)}", key=None)
                except (OllamaError, RuntimeError) as e: self.log(f"The AI edit plan failed ({e}). Using simple rules for this clip.")
                if not isinstance(raw, dict): raw = None
            if cl.get("hook"): raw = dict(raw or {}, hook=cl["hook"])  # the hook written together with the title and caption
            plan = make_plan(raw, words, length, c)
            cl["edit"] = plan
            self.log(f"Clip {k}: {len(plan['zooms'])} zoom(s), {len(plan['callouts'])} text pop-up(s)" + (", hook title" if plan["hook"] else "")
                     + (" (AI plan)." if plan["source"] == "ai" else " (simple rules)."))
            self.prog(k / len(clips), f"clip {k} of {len(clips)}")

    # auto-edit: ffmpeg pieces
    @staticmethod
    def has_filter(name):
        return re.search(r"^\s*\S+\s+%s\s" % re.escape(name), sh(["ffmpeg", "-hide_banner", "-filters"], timeout=20)[1], re.M) is not None

    @staticmethod
    def dims(path):
        """(width, height) of the video as ffmpeg sees it, honouring phone rotation; None when unreadable."""
        ffprobe = which("ffprobe")
        if not ffprobe: return None
        try:
            r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:stream_side_data=rotation:stream_tags=rotate",
                                "-of", "json", str(path)], capture_output=True, timeout=20, **proc_kw())
            s = json.loads(r.stdout)["streams"][0]
            w, h, rot = int(s["width"]), int(s["height"]), 0
            for sd in s.get("side_data_list") or []:
                if "rotation" in sd: rot = int(float(sd["rotation"]))
            rot = rot or int(float((s.get("tags") or {}).get("rotate", 0)))
            return (h, w) if abs(rot) % 180 == 90 else (w, h)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, TypeError): return None

    def sfx_files(self, work):
        """Synthesise two tiny sound effects once (no files to ship); returns {name: path} or None."""
        specs = {"whoosh": "anoisesrc=color=pink:amplitude=0.7:duration=0.55:sample_rate=48000,highpass=f=500,lowpass=f=6000,afade=t=in:st=0:d=0.28,afade=t=out:st=0.28:d=0.27",
                 "pop": "aevalsrc=0.9*sin(2*PI*680*t)*exp(-26*t):s=48000:d=0.18"}
        paths = {}
        for n, graph in specs.items():
            p = work / f"sfx_{n}.wav"
            if not (p.is_file() and p.stat().st_size > 1000):
                rc, _ = self.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", graph, "-ac", "2", "-ar", "48000", str(p)])
                if rc != 0 or not p.is_file(): return None
            paths[n] = p
        return paths

    @staticmethod
    def zoom_chain(plan, dims):
        """Smooth zoom (and slow pan) as one scale+crop pair driven by the frame time, in the source picture."""
        zs = plan.get("zooms") or []
        if not zs or not dims: return ""
        sw, sh = dims
        ease = lambda x: "(0.5-0.5*cos(PI*clip(%s,0,1)))" % x
        z, pan = "1", ""
        for q in zs:
            up, down = "(t-%.3f)/%.3f" % (q["t0"], q["r"]), "(t-%.3f)/%.3f" % (q["t1"], q["r"])
            z += "+%.3f*(%s-%s)" % (q["amp"], ease(up), ease(down))
            if q["pan"]:
                pan += "%+.3f*between(t,%.3f,%.3f)*(2*clip((t-%.3f)/%.3f,0,1)-1)" % (0.45 * q["pan"], q["t0"], q["end"], q["t0"], q["end"] - q["t0"])
        return ("scale=w='2*ceil(iw*(%s)/2)':h='2*ceil(ih*(%s)/2)':eval=frame:flags=bicubic,"
                "crop=%d:%d:x='(in_w-out_w)/2*(1%s)':y='(in_h-out_h)*0.4'" % (z, z, sw, sh, pan))

    def audio_graph(self, plan, sfx):
        """Mix the sound effects into the clip's audio. Returns (extra ffmpeg inputs, filter graph) or ([], None)."""
        wh = [max(0, int(round((q["t0"] - 0.05) * 1000))) for q in plan.get("zooms") or []]
        pp = [int(round(t * 1000)) for t in ([0.2] if plan.get("hook") else []) + [q["t"] for q in plan.get("callouts") or []]]
        if not wh and not pp: return [], None
        parts, mix, inputs, n = ["[0:a:0]aformat=sample_rates=48000:channel_layouts=stereo[am]"], ["[am]"], [], 1
        for tag, path, delays, vol in (("w", sfx["whoosh"], wh, 0.30), ("p", sfx["pop"], pp, 0.45)):
            if not delays: continue
            inputs += ["-i", str(path)]
            labs = ["[%s%d]" % (tag, j) for j in range(len(delays))]
            parts.append("[%d:a]" % n + (("asplit=%d" % len(delays)) + "".join(labs) if len(delays) > 1 else "anull" + labs[0]))
            for j, d in enumerate(delays):
                parts.append("%sadelay=%d|%d,volume=%.2f[%sd%d]" % (labs[j], d, d, vol, tag, j)); mix.append("[%sd%d]" % (tag, j))
            n += 1
        parts.append("".join(mix) + "amix=inputs=%d:duration=first:normalize=0,alimiter=limit=0.97%s[aout]" % (len(mix), ("," + LOUDNORM) if self.c["loudnorm"] else ""))
        return inputs, ";".join(parts)

    def edit_cmd(self, a, length, video, W, H, ass, plan, dims, sfx, codec, out_path, wm=None, layout=None):
        c = self.c
        graph = "[0:v]" + self.vf(W, H, None, self.zoom_chain(plan, dims), ass, wm, layout) + "[v]"
        inputs, ag = self.audio_graph(plan, sfx) if sfx else ([], None)
        if ag: graph += ";" + ag
        aud = ["-c:a", "aac", "-b:a", c["audio_bitrate"]] + (["-af", LOUDNORM] if c["loudnorm"] and not ag else [])
        return ["ffmpeg", "-n", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-ss", str(a), "-t", str(length), "-i", str(video), *inputs,
                "-filter_complex", graph, "-map", "[v]", "-map", "[aout]" if ag else "0:a:0?", *codec, "-pix_fmt", "yuv420p", *aud,
                "-movflags", "+faststart", *split_args(c["extra_ffmpeg"]), str(out_path)]

    def render(self, clips, segs, video, work, out):
        c = self.c
        rw, rh = map(int, c["aspect"].split(":"))
        W = c["out_width"] // 2 * 2; H = round(W * rh / rw / 2) * 2
        have_nv = "h264_nvenc" in sh(["ffmpeg", "-hide_banner", "-encoders"])[1]
        order = {"auto": ["nvenc", "x264"] if have_nv else ["x264"], "nvenc": ["nvenc", "x264"], "x264": ["x264"]}[c["encoder"]]
        q = str(c["quality"])
        codecs = {"nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", q, "-b:v", "0"],
                  "x264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", q]}
        audio = ["-c:a", "aac", "-b:a", c["audio_bitrate"]] + (["-af", LOUDNORM] if c["loudnorm"] else [])
        edit_on = bool(c["edit_enabled"]) and any(cl.get("edit") for cl in clips)
        has_ass = (edit_on or bool(self.wm_text)) and self.has_filter("ass")
        ass_ok = edit_on and has_ass
        if edit_on and not has_ass: self.log("This ffmpeg build has no ASS (libass) support, so animated captions and graphics are skipped.")
        wm = None
        if self.wm_text:
            if has_ass: wm = "wm.ass"; (work / wm).write_text(build_wm_ass(c, W, H, self.wm_text), encoding="utf-8")
            else: self.log("This ffmpeg build has no ASS (libass) support, so the watermark is skipped.")
        split = bool(c["split_enabled"] and self.regions)
        sd = self.dims(video) if split or (edit_on and c["edit_zoom"]) else None
        layout, ys = None, None
        if split:
            if sd:
                cam_px = region_px(self.regions[0], *sd)
                main_frac = self.regions[1] if len(self.regions) > 1 and self.regions[1] else (0.0, 0.0, 1.0, 1.0)
                main_px, patch, how = exclude_cam(region_px(main_frac, *sd), cam_px, c["split_overlap"])
                layout = {"cam": cam_px, "main": main_px, "patch": patch, "cam_h": layout_sizes(c)[2], "top": c["split_cam_pos"] == "top"}
                if how == "crop": self.log("The face cam is on top of the content box, so the content panel uses only the part without the cam.")
                elif how == "fill": self.log("The face cam is on top of the content box, so it is painted over in the content panel.")
                fy = layout["cam_h"] / H  # keep the hook and pop-ups next to the seam, off the face
                # distance of the hook / pop-ups from the seam (as a fraction of the screen), kept clear of wherever the captions are
                d1, d2 = {("top", "middle"): (0.14, 0.26), ("bottom", "middle"): (0.17, 0.29), ("bottom", "bottom"): (0.26, 0.38)}.get(
                    ("top" if layout["top"] else "bottom", c["cap_position"]), (0.09, 0.20))
                ys = (fy + d1, fy + d2) if layout["top"] else (1 - fy - d1, 1 - fy - d2)
                if layout["cam"][2] * 2.5 < W:
                    self.log(f"The face cam is only {layout['cam'][2]} px wide in the source and gets stretched to {W} px, so it will look soft. A higher download quality helps.")
            else: self.log("Couldn't read the video size, so the split layout is skipped.")
        dims = (layout["main"][2:] if layout else sd) if edit_on and c["edit_zoom"] else None
        if edit_on and c["edit_zoom"] and not dims: self.log("Couldn't read the video size, so zoom is skipped.")
        sfx = self.sfx_files(work) if edit_on and c["edit_sfx"] else None
        if edit_on and c["edit_sfx"] and not sfx: self.log("Couldn't create the sound effects, so they are skipped.")
        files = []
        for k, cl in enumerate(clips, 1):
            a, b = cl["start"], cl["end"]; length = b - a
            name = output_stem(out, c.get("prefix", "clip"), k)
            rows = [s for s in segs if s["end"] > a and s["start"] < b]
            sub = None
            if c["captions"] and rows:
                sub = f"sub_{k}.srt"
                (work / sub).write_text("".join(f"{n}\n{srt_t(s['start'] - a)} --> {srt_t(min(s['end'], b) - a)}\n{s['text']}\n\n" for n, s in enumerate(rows, 1)), encoding="utf-8")
                if c["write_srt"]: shutil.copy(work / sub, out / f"{name}.srt")
            plan, ass = (cl.get("edit") if edit_on else None), None
            if plan and ass_ok:
                text = build_ass(c, W, H, length, clip_words(segs, a, b), plan, ys, layout)
                if text: ass = f"sub_{k}.ass"; (work / ass).write_text(text, encoding="utf-8")
            output_path = out / f"{name}.mp4"
            # edited render first; if it fails on every encoder, fall back to the plain captioned clip
            attempts = ([(e, True) for e in order] if plan else []) + [(e, False) for e in order]
            done = False
            for enc, edited in attempts:
                self.log(f"Rendering {name} ({fmt_t(a)} to {fmt_t(b)}, {'GPU' if enc == 'nvenc' else 'CPU'} encoder{', with edits' if edited else ''})...")
                def on(line, k=k, length=length):
                    m = re.match(r"out_time_us=(\d+)", line)
                    if m: self.prog(min(1, (k - 1 + int(m.group(1)) / 1e6 / length) / len(clips)), f"clip {k} of {len(clips)}")
                if edited:
                    cmd = self.edit_cmd(a, length, video, W, H, ass, plan, dims, sfx, codecs[enc], output_path, wm, layout)
                else:
                    cmd = ["ffmpeg", "-n", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-ss", str(a), "-t", str(length), "-i", str(video),
                           "-vf", self.vf(W, H, sub, wm=wm, layout=layout), *codecs[enc], "-pix_fmt", "yuv420p", *audio, "-movflags", "+faststart", *split_args(c["extra_ffmpeg"]), str(output_path)]
                rc, lines = self.run(cmd, cwd=work, on_line=on)
                if rc == 0 and output_path.is_file() and output_path.stat().st_size > 0:
                    done = True; break
                try: output_path.unlink()  # remove an incomplete file before trying the fallback
                except OSError: pass
                self.log(f"{enc} failed: " + " | ".join(x for x in lines if not re.match(r"\w+=", x))[-300:])
                if edited and enc == order[-1]: self.log("The edited render failed, so this clip is rendered without edits.")
            if not done: self.log(f"Skipped {name}: it couldn't be rendered."); continue
            yt = youtube_fields(cl["title"], cl["description"], cl.get("keywords"), self.src_url)
            if c["write_txt"]: (out / f"{name}.txt").write_text(post_text(cl, yt), encoding="utf-8")
            cover = None
            if c["cover_export"]:  # a frame just after the hook has appeared
                cover = out / f"{name}_cover.jpg"
                rc, _ = self.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(1.0, max(0.2, length * 0.3)):.2f}", "-i", str(output_path), "-frames:v", "1", "-q:v", "2", str(cover)])
                if rc != 0 or not cover.is_file(): cover = None
            files.append({"path": str(out / f"{name}.mp4"), "title": cl["title"], "description": cl["description"], "cover": str(cover) if cover else "",
                          "yt_title": yt["title"], "yt_description": yt["description"], "yt_tags": yt["tags"]})
        return files

# ───────────────────────────── UI ─────────────────────────────
BG, SIDE, PANEL, FIELD, BORDER = "#0b1220", "#101a2b", "#151f30", "#1c2a3d", "#2a3a50"
FG, MUTED, ACC, ACC_HI, ACC_FG, DANGER = "#edf3fb", "#9aaac0", "#35c7b2", "#50dfc9", "#071a18", "#f07878"
SUCCESS, WARNING, LOG_FG = "#55d6a4", "#e8b86d", "#c5d1e1"
FAM = "Segoe UI" if WIN else "Sans"
SUBTITLES = {
    "Source": "Where the video comes from and how it is downloaded.",
    "Transcribe": "Turns speech into text with Whisper. The GPU is used when available.",
    "AI picker": "A local Ollama model chooses the best moments.",
    "Growth": "Finds the moments most likely to hold attention and writes the title, caption, hashtags and cover.",
    "Clips": "How many clips to make and how long they are.",
    "Captions": "Subtitles burned into the video.",
    "Split screen": "Face cam enlarged on one half, main content on the other, like a streamer short.",
    "Watermark": "A small credit for the original creator, shown on every clip.",
    "Editing": "The AI plans zooms, animated captions, text and sound for each clip, then renders it finished.",
    "Video": "Shape, quality and encoder of the finished clips.",
    "Output": "Where files are saved and what gets saved.",
    "YouTube": "Upload finished clips to your YouTube channel. The title, description and hashtags are filled from the AI picker.",
}

def pick_font():
    fams = set(tkfont.families())
    for f in (("Segoe UI",) if WIN else ()) + ("Inter", "Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans"):
        if f in fams: return f
    return "Segoe UI" if WIN else "Sans"

def rrect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)

class Tip:
    """A single, delayed tooltip whose callbacks are cleaned up with its widget."""
    DELAY_MS = 400

    def __init__(self, w, text):
        self.w, self.t, self.tw = w, text, None
        self.show_job = self.hide_job = None
        if text:
            w.bind("<Enter>", self.schedule_show, add="+")
            w.bind("<Leave>", self.hide, add="+")
            w.bind("<Destroy>", self.on_destroy, add="+")

    def _cancel_job(self, attr):
        job = getattr(self, attr)
        if job is not None:
            try: self.w.after_cancel(job)
            except tk.TclError: pass
            setattr(self, attr, None)

    def schedule_show(self, _=None):
        self._cancel_job("hide_job")
        self._cancel_job("show_job")
        try: self.show_job = self.w.after(self.DELAY_MS, self.show)
        except tk.TclError: self.show_job = None

    def show(self):
        self.show_job = None
        try:
            if not self.w.winfo_exists() or not self.w.winfo_ismapped(): return
            px, py = self.w.winfo_pointerxy()
            x, y = self.w.winfo_rootx(), self.w.winfo_rooty()
            if not (x <= px < x + self.w.winfo_width() and y <= py < y + self.w.winfo_height()): return
            if self.tw is not None:
                try:
                    if self.tw.winfo_exists(): return
                except tk.TclError: pass
            self.tw = tk.Toplevel(self.w)
            self.tw.wm_overrideredirect(True)
            self.tw.wm_geometry(f"+{x + 10}+{y + self.w.winfo_height() + 4}")
            tk.Label(self.tw, text=self.t, bg=FIELD, fg=FG, padx=8, pady=5,
                     justify="left", wraplength=380).pack()
        except tk.TclError:
            self.tw = None

    def hide(self, _=None):
        self._cancel_job("show_job")
        self._cancel_job("hide_job")
        self._destroy_tip()

    def _destroy_tip(self):
        tip, self.tw = self.tw, None
        if tip is not None:
            try:
                if tip.winfo_exists(): tip.destroy()
            except tk.TclError: pass

    def on_destroy(self, event=None):
        if event is not None and event.widget is not self.w: return
        self._cancel_job("show_job")
        self._cancel_job("hide_job")
        self._destroy_tip()

def mix_color(a, b, t):
    """Blend two #RRGGBB colors for lightweight Tk hover transitions."""
    t = max(0.0, min(1.0, float(t)))
    try:
        av = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
        bv = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
        return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(av, bv))
    except (TypeError, ValueError):
        return b


class Btn(tk.Canvas):
    """Flat rounded button. state(["disabled"]) / state(["!disabled"]) work like ttk."""
    KINDS = {"primary": (ACC, ACC_HI, ACC_FG, "#1f4a47", "#6f9a95"), "secondary": (FIELD, "#27384f", FG, PANEL, MUTED)}
    def __init__(self, master, text, command, kind="secondary", bg=BG, small=False, state="normal"):
        self.font = tkfont.Font(family=FAM, size=10, weight="bold" if kind == "primary" else "normal")
        self.bh = 30 if small else 38; self.bw = self.font.measure(text) + (28 if small else 38)
        super().__init__(master, width=self.bw, height=self.bh, bg=bg, highlightthickness=0, bd=0)
        self.text, self.cmd, self.kind, self.hover, self.disabled, self.r = text, command, kind, 0.0, state == "disabled", 8 if small else 10
        self._hover_job = None
        self.bind("<Enter>", lambda _: self._hv(True)); self.bind("<Leave>", lambda _: self._hv(False)); self.bind("<Button-1>", self._click)
        self.draw()
    def _hv(self, on):
        if self.disabled: return
        if self._hover_job:
            try: self.after_cancel(self._hover_job)
            except tk.TclError: pass
            self._hover_job = None
        target = 1.0 if on else 0.0
        def tick():
            delta = target - self.hover
            if abs(delta) < 0.04:
                self.hover = target; self._hover_job = None; self.draw(); return
            self.hover += delta * 0.48
            self.draw()
            self._hover_job = self.after(12, tick)
        tick()
    def _click(self, _=None):
        if not self.disabled: self.cmd()
    def state(self, spec):
        for s in spec:
            if s == "disabled": self.disabled = True
            elif s == "!disabled": self.disabled = False
        self.draw()
    def draw(self):
        if not self.winfo_exists(): return
        self.delete("all"); fill, hi, fg, dfill, dfg = self.KINDS[self.kind]
        c, t = (dfill, dfg) if self.disabled else (mix_color(fill, hi, self.hover), fg)
        rrect(self, 1, 1, self.bw - 1, self.bh - 1, self.r, fill=c, outline=BORDER if self.kind == "secondary" else c)
        self.create_text(self.bw / 2, self.bh / 2, text=self.text, fill=t, font=self.font)
        self.configure(cursor="arrow" if self.disabled else "hand2")

class Switch(tk.Canvas):
    def __init__(self, master, variable, bg=PANEL):
        super().__init__(master, width=46, height=26, bg=bg, highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self.var = variable; self.bind("<Button-1>", self.toggle); self.bind("<space>", self.toggle)
        variable.trace_add("write", lambda *_: self.draw()); self.draw()
    def toggle(self, _=None): self.var.set(not self.var.get()); return "break"
    def draw(self):
        if not self.winfo_exists(): return
        self.delete("all"); on = bool(self.var.get()); col = ACC if on else "#3a4a60"
        self.create_oval(2, 2, 24, 24, fill=col, outline=col); self.create_oval(22, 2, 44, 24, fill=col, outline=col); self.create_rectangle(13, 2, 33, 24, fill=col, outline=col)
        x = 33 if on else 13; self.create_oval(x - 9, 4, x + 9, 22, fill="#ffffff" if on else "#c3cfdf", outline="")

class Scrolled(tk.Frame):
    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.columnconfigure(0, weight=1); self.rowconfigure(0, weight=1)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview); self.cv.configure(yscrollcommand=self.sb.set)
        self.cv.grid(row=0, column=0, sticky="nsew"); self.sb.grid(row=0, column=1, sticky="ns")
        self.body = tk.Frame(self.cv, bg=BG); self.win = self.cv.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self.fit); self.cv.bind("<Configure>", self.fit)
    def fit(self, _=None):
        self.cv.itemconfigure(self.win, width=self.cv.winfo_width()); self.cv.configure(scrollregion=self.cv.bbox("all"))
        if self.body.winfo_reqheight() <= self.cv.winfo_height(): self.sb.grid_remove(); self.cv.yview_moveto(0)
        else: self.sb.grid()

class NavItem(tk.Frame):
    def __init__(self, master, text, command):
        super().__init__(master, bg=SIDE, cursor="hand2")
        self.active = False
        self.hovered = False
        self.hover_t = 0.0
        self._anim_job = None
        self.bar = tk.Frame(self, width=3, bg=SIDE)
        self.bar.pack(side="left", fill="y")
        self.lbl = tk.Label(self, text=text, bg=SIDE, fg=MUTED, anchor="w", padx=19, pady=9, font=(FAM, 10))
        self.lbl.pack(side="left", fill="x", expand=True)
        self.badge = tk.Label(self, text="●", bg=SIDE, fg=DANGER, font=(FAM, 7))
        for w in (self, self.lbl, self.badge):
            w.bind("<Button-1>", lambda _, fn=command: fn())
            w.bind("<Enter>", lambda _: self.paint(True))
            w.bind("<Leave>", lambda _: self.paint(False))
        self.paint(False, animate=False)

    def paint(self, hover=False, animate=True):
        self.hovered = bool(hover)
        target = 1.0 if (self.active or self.hovered) else 0.0
        if self._anim_job:
            try: self.after_cancel(self._anim_job)
            except tk.TclError: pass
            self._anim_job = None
        if not animate:
            self.hover_t = target
            self._paint_colors()
            return
        def tick():
            delta = target - self.hover_t
            if abs(delta) < 0.04:
                self.hover_t = target
                self._anim_job = None
                self._paint_colors()
                return
            self.hover_t += delta * 0.45
            self._paint_colors()
            self._anim_job = self.after(12, tick)
        tick()

    def _paint_colors(self):
        bg = mix_color(SIDE, "#1a2740", self.hover_t)
        fg = mix_color(MUTED, FG, self.hover_t)
        self.configure(bg=bg)
        self.lbl.configure(bg=bg, fg=fg)
        self.bar.configure(bg=ACC if self.active else bg)
        self.badge.configure(bg=bg)

    def set_active(self, on):
        self.active = bool(on)
        self.paint(self.hovered)
    def set_badge(self, on):
        self.badge.pack(side="right", padx=16) if on else self.badge.pack_forget()

class Review(tk.Toplevel):
    def __init__(self, app, clips, video, holder):
        super().__init__(app); self.title("Review and select clips"); self.geometry("1120x700"); self.minsize(900, 580); self.configure(bg=BG)
        self.clips, self.video, self.holder, self.cur, self.busy, self.shown = clips, video, holder, None, False, ("", "")
        self.transient(app); self.protocol("WM_DELETE_WINDOW", self.cancel)
        left = ttk.Frame(self, padding=12); left.pack(side="left", fill="y")
        ttk.Label(left, text="Pick the clips to render", font=(FAM, 11, "bold")).pack(anchor="w", pady=(0, 4))
        self.count_var = tk.StringVar()
        ttk.Label(left, textvariable=self.count_var, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        self.tree = ttk.Treeview(left, columns=("use", "start", "end", "length", "title"), show="headings", height=15, selectmode="browse")
        for col, w, t in (("use", 42, "Use"), ("start", 72, "Start"), ("end", 72, "End"), ("length", 62, "Length"), ("title", 220, "Title")):
            self.tree.heading(col, text=t); self.tree.column(col, width=w, stretch=(col == "title"))
        self.tree.pack(fill="y", expand=True); self.tree.bind("<<TreeviewSelect>>", self.pick)
        self.tree.bind("<Double-1>", self.toggle_selected)
        right = ttk.Frame(self, padding=12); right.pack(side="right", fill="both", expand=True); right.columnconfigure(1, weight=1)
        self.use, self.s, self.e, self.t = tk.BooleanVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Checkbutton(right, text="Include this clip", variable=self.use, command=self.update_count).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        for r, (lab, var) in enumerate((("Start", self.s), ("End", self.e), ("Title", self.t)), 1):
            ttk.Label(right, text=lab).grid(row=r, column=0, sticky="w", pady=4); ttk.Entry(right, textvariable=var).grid(row=r, column=1, sticky="ew", pady=4)
        ttk.Label(right, text="Times accept seconds (90) or m:ss (1:30).", style="Muted.TLabel").grid(row=4, column=1, sticky="w")
        ttk.Label(right, text="Instagram description").grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.d = tk.Text(right, height=9, bg=FIELD, fg=FG, relief="flat", wrap="word", insertbackground=FG, highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACC, padx=8, pady=6, font=(FAM, 10)); self.d.grid(row=6, column=0, columnspan=2, sticky="nsew"); right.rowconfigure(6, weight=1)
        bar = ttk.Frame(right); bar.grid(row=7, column=0, columnspan=2, sticky="e", pady=(12, 0))
        Btn(bar, "Preview", self.preview).pack(side="left", padx=4)
        Btn(bar, "Cancel", self.cancel).pack(side="left", padx=4)
        Btn(bar, "Render selected", self.ok, kind="primary").pack(side="left", padx=4)
        for i in range(len(clips)): self.tree.insert("", "end", iid=str(i), values=self.row(i))
        self.update_count()
        if clips: self.tree.selection_set("0")

    def row(self, i):
        c = self.clips[i]
        return ("yes" if c["use"] else "no", fmt_clip_t(c["start"]), fmt_clip_t(c["end"]), f"{c['end'] - c['start']:.1f}s", c["title"])

    def update_count(self):
        selected = sum(bool(c["use"]) for c in self.clips)
        if self.cur is not None:
            selected += int(bool(self.use.get())) - int(bool(self.clips[self.cur]["use"]))
        self.count_var.set(f"{selected} of {len(self.clips)} selected · double-click a row to toggle")

    def toggle_selected(self, event=None):
        item = self.tree.identify_row(event.y) if event is not None else ""
        if not item: return
        if self.cur is not None and not self.commit(): return
        idx = int(item); self.clips[idx]["use"] = not self.clips[idx]["use"]
        self.tree.item(item, values=self.row(idx))
        if self.cur == idx: self.use.set(self.clips[idx]["use"])
        self.update_count()
        self.tree.selection_set(item)
    def commit(self):
        if self.cur is None: return True
        c = self.clips[self.cur]; a, b = c["start"], c["end"]
        # The editor shows rounded values, and AI clips can legitimately fall outside min/max
        # (e.g. the last clip of a video). So only re-parse and validate times the user edited,
        # and don't block un-ticking a clip.
        if self.use.get() and (self.s.get(), self.e.get()) != self.shown:
            try:
                if self.s.get() != self.shown[0]: a = parse_t(self.s.get())
                if self.e.get() != self.shown[1]: b = parse_t(self.e.get())
                if 0 < b - self.holder["duration"] < 0.05: b = self.holder["duration"]
                validate_clip_times(a, b, self.holder["duration"], self.holder["min_len"], self.holder["max_len"])
            except (ValueError, TypeError, OverflowError) as exc:
                messagebox.showerror("Invalid clip times", str(exc) or "Use seconds (90) or m:ss (1:30).", parent=self)
                return False
        c.update(start=a, end=b, title=self.t.get().strip(), description=self.d.get("1.0", "end").strip(), use=self.use.get())
        self.tree.item(str(self.cur), values=self.row(self.cur)); self.update_count(); return True
    def pick(self, _=None):
        if self.busy or not self.tree.selection(): return
        if not self.commit(): self.busy = True; self.tree.selection_set(str(self.cur)); self.busy = False; return
        self.cur = int(self.tree.selection()[0]); c = self.clips[self.cur]
        self.use.set(c["use"]); self.s.set(f"{c['start']:.2f}"); self.e.set(f"{c['end']:.2f}"); self.t.set(c["title"])
        self.d.delete("1.0", "end"); self.d.insert("1.0", c["description"]); self.shown = (self.s.get(), self.e.get()); self.update_count()
    def preview(self):
        if self.cur is None or not self.commit(): return
        c = self.clips[self.cur]
        ffplay = which("ffplay")
        if not ffplay:
            messagebox.showerror("ffplay not found", "ffplay ships with ffmpeg. Install ffmpeg from the Setup page to preview.", parent=self)
            return
        try: subprocess.Popen([ffplay, "-hide_banner", "-loglevel", "error", "-autoexit", "-ss", str(c["start"]), "-t", str(c["end"] - c["start"]), self.video], **quiet_kw())
        except OSError as e: messagebox.showerror("Couldn't start ffplay", str(e), parent=self)
    def ok(self):
        if self.commit(): self.holder["res"] = self.clips; self.holder["ev"].set(); self.destroy()
    def cancel(self): self.holder["res"] = None; self.holder["ev"].set(); self.destroy()

class RegionPicker(tk.Toplevel):
    """Pick two freely resizable rectangles: the face cam and the main content. An unmarked content box means the full frame."""
    COLORS = {"cam": "#35c7b2", "main": "#e8b86d"}
    NAMES = {"cam": "FACE CAM", "main": "CONTENT"}
    PV_EMPTY = "Draw the face-cam\nbox to see the result"
    HANDLE, MIN = 10, 8
    ORDER = ("tl", "tr", "bl", "br", "t", "b", "l", "r")

    def __init__(self, app, holder):
        super().__init__(app)
        self.title("Mark the face cam and content")
        self.configure(bg=BG)
        self.transient(app)
        self.app, self.hold, self.dw, self.dh, self.img, self.act = app, holder, 0, 0, None, None
        self.pv_job, self.pv_img = None, None
        W, H, cam_h = layout_sizes(holder["cfg"])
        scale = 450 / H
        self.pv_size = (round(W * scale / 2) * 2, 450, min(max(round(cam_h * scale / 2) * 2, 2), 448))
        self.rect = {"cam": None, "main": None}
        self.key = tk.StringVar(value="cam")  # which box the mouse and arrow keys edit
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=18, pady=(16, 4))
        tk.Label(top, text="Mark the face cam and the content", bg=BG, fg=FG, font=(FAM, 10, "bold")).pack(anchor="w")
        tk.Label(top, text="Pick which box you are editing, then drag to draw it. Drag inside a box to move it. Drag any edge or corner handle to resize only that side. "
                           "Both boxes can be any shape. The content box is optional: leave it unmarked to use the whole frame.",
                 bg=BG, fg=MUTED, font=(FAM, 9), wraplength=780, justify="left").pack(anchor="w", pady=(3, 8))
        sel = tk.Frame(top, bg=BG)
        sel.pack(anchor="w")
        tk.Label(sel, text="Editing:", bg=BG, fg=MUTED, font=(FAM, 9)).pack(side="left", padx=(0, 8))
        for key, label in (("cam", "Face cam"), ("main", "Content")):
            tk.Radiobutton(sel, text=label, variable=self.key, value=key, command=self.draw, bg=BG, fg=self.COLORS[key], selectcolor=BG,
                           activebackground=BG, activeforeground=self.COLORS[key], highlightthickness=0, bd=0, font=(FAM, 10, "bold"),
                           cursor="hand2").pack(side="left", padx=(0, 14))
        body = tk.Frame(self, bg=BG)
        body.pack(padx=18, pady=8)
        self.cv = tk.Canvas(body, bg="#000000", highlightthickness=0, cursor="crosshair", takefocus=True)
        self.cv.pack(side="left")
        side = tk.Frame(body, bg=BG)
        side.pack(side="left", padx=(16, 0), anchor="n")
        tk.Label(side, text="Result", bg=BG, fg=FG, font=(FAM, 10, "bold")).pack(anchor="w")
        tk.Label(side, text="face cam + content", bg=BG, fg=MUTED, font=(FAM, 8)).pack(anchor="w", pady=(0, 6))
        self.pv = tk.Label(side, text=self.PV_EMPTY, bg="#000000", fg=MUTED, font=(FAM, 9), width=28, height=14, justify="center")
        self.pv.pack()
        self.cv.bind("<ButtonPress-1>", self.press)
        self.cv.bind("<B1-Motion>", self.drag)
        self.cv.bind("<ButtonRelease-1>", self.release)
        self.cv.bind("<Motion>", self.hover)
        for name, (dx, dy) in {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}.items():
            self.cv.bind(f"<{name}>", lambda e, dx=dx, dy=dy: self.nudge(dx, dy))
            self.cv.bind(f"<Shift-{name}>", lambda e, dx=dx, dy=dy: self.nudge(dx * 10, dy * 10))
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=18, pady=(0, 16))
        Btn(bar, "Different frame", self.other).pack(side="left")
        Btn(bar, "Clear this box", self.clear).pack(side="left", padx=(10, 0))
        Btn(bar, "Use this layout", self.ok, kind="primary").pack(side="right")
        Btn(bar, "Cancel", self.cancel).pack(side="right", padx=(0, 10))

        dur = holder["duration"]
        if self.load(min(dur * 0.25, max(dur - 1, 0.0))):
            for key, r in (("cam", holder.get("saved")), ("main", holder.get("saved_main"))):
                if r and not (key == "main" and r[2] > 0.995 and r[3] > 0.995):  # a full-frame content box is the default, so it is not drawn
                    self.rect[key] = (r[0] * self.dw, r[1] * self.dh, (r[0] + r[2]) * self.dw, (r[1] + r[3]) * self.dh)
            self.draw()
        # On Windows, a transient Toplevel can otherwise appear behind the main window or
        # fail to receive the first mouse interaction. Activate it after Tk has mapped it.
        self.after(80, self._activate_picker)

    def _activate_picker(self):
        try:
            if not self.winfo_exists():
                return
            self.lift()
            self.focus_force()
            self.cv.focus_set()
            self.grab_set()
        except tk.TclError:
            pass

    def load(self, t):
        png = Path(self.hold["work"]) / "split_frame.png"
        try:
            r = subprocess.run([which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", self.hold["video"], "-frames:v", "1",
                                "-vf", "scale=800:450:force_original_aspect_ratio=decrease", str(png)], capture_output=True, timeout=60, **proc_kw())
            ok = r.returncode == 0 and png.is_file()
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            messagebox.showerror("Clipper", "Couldn't read a picture from the video.", parent=self)
            return False
        try:
            self.img = tk.PhotoImage(file=str(png))
            self.dw, self.dh = self.img.width(), self.img.height()
            self.cv.configure(width=self.dw, height=self.dh)
            self.cv.delete("all")
            self.cv.create_image(0, 0, image=self.img, anchor="nw")
            self.draw()
            return True
        except (OSError, tk.TclError) as e:
            messagebox.showerror("Clipper", f"Couldn't display the video frame: {e}", parent=self)
            return False

    def other(self):
        self.act = None
        if self.load(random.uniform(0.05, 0.95) * self.hold["duration"]):
            self.rect["cam"] = self.rect["main"] = None
            self.draw()

    @staticmethod
    def handles(r):
        x0, y0, x1, y1 = r
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        return {"tl": (x0, y0), "t": (mx, y0), "tr": (x1, y0), "l": (x0, my),
                "r": (x1, my), "bl": (x0, y1), "b": (mx, y1), "br": (x1, y1)}

    def hit(self, r, x, y):
        if not r:
            return None
        hs = self.handles(r)
        for name in self.ORDER:
            if abs(x - hs[name][0]) <= self.HANDLE and abs(y - hs[name][1]) <= self.HANDLE:
                return name
        return "in" if r[0] <= x <= r[2] and r[1] <= y <= r[3] else None

    def pick(self, x, y):
        key = self.key.get()
        return key, self.hit(self.rect[key], x, y)

    def draw(self):
        self.cv.delete("r")
        active = self.key.get()
        for key in ("main", "cam"):  # the face cam is drawn on top
            r = self.rect[key]
            if not r:
                continue
            col, on = self.COLORS[key], key == active
            self.cv.create_rectangle(*r, outline=col, width=3 if on else 2, tags="r")
            self.cv.create_text(r[0] + 6, r[1] + 4, text=self.NAMES[key], anchor="nw", fill=col, font=(FAM, 10, "bold"), tags="r")
            if on:
                for hx, hy in self.handles(r).values():
                    self.cv.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4, fill=col, outline="#000000", tags="r")
        eff = self.effective()
        if eff and eff[2] != "none":  # the face cam overlaps the content: show what the content panel will really use
            (x, y, w, h), patch, how = eff
            label = "CONTENT USED (cam cut away)"
            if how == "fill" and patch:
                x, y, w, h = x + patch[0], y + patch[1], patch[2], patch[3]
                label = "PAINTED OVER"
            self.cv.create_rectangle(x, y, x + w, y + h, outline=self.COLORS["main"], width=2, dash=(6, 4), tags="r")
            self.cv.create_text(x + 6, y + 20, text=label, anchor="nw", fill=self.COLORS["main"], font=(FAM, 9, "bold"), tags="r")
        self.schedule_preview()

    def clamp(self, e):
        # Canvas coordinates are used rather than raw screen coordinates to avoid
        # DPI/window-border offsets on Windows.
        x, y = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        return min(max(x, 0), self.dw), min(max(y, 0), self.dh)

    def press(self, e):
        self.cv.focus_set()
        x, y = self.clamp(e)
        key, part = self.pick(x, y)
        r = self.rect[key]
        self.act = ("move", (x, y), r, key) if part == "in" else ("size", part, r, key) if part else ("new", (x, y), r, key)
        return "break"

    def drag(self, e):
        if not self.act:
            return "break"
        x, y = self.clamp(e)
        kind, a, r0, key = self.act
        if kind == "move":
            w, h = r0[2] - r0[0], r0[3] - r0[1]
            nx = min(max(r0[0] + x - a[0], 0), self.dw - w)
            ny = min(max(r0[1] + y - a[1], 0), self.dh - h)
            self.rect[key] = (nx, ny, nx + w, ny + h)
        elif kind == "size":
            self.rect[key] = self.resized(a, r0, x, y)
        else:
            self.rect[key] = self.drawn(a, x, y)
        self.draw()
        return "break"

    def drawn(self, start, x, y):
        x0, y0 = start
        x0, x1 = sorted((min(max(x0, 0), self.dw), min(max(x, 0), self.dw)))
        y0, y1 = sorted((min(max(y0, 0), self.dh), min(max(y, 0), self.dh)))
        return (x0, y0, x1, y1)

    def resized(self, hnd, r0, x, y):
        x0, y0, x1, y1 = r0
        x = min(max(x, 0), self.dw)
        y = min(max(y, 0), self.dh)
        if "l" in hnd:
            x0 = min(x, x1 - self.MIN)
        if "r" in hnd:
            x1 = max(x, x0 + self.MIN)
        if "t" in hnd:
            y0 = min(y, y1 - self.MIN)
        if "b" in hnd:
            y1 = max(y, y0 + self.MIN)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.dw, x1), min(self.dh, y1)
        return (x0, y0, max(x0 + self.MIN, x1), max(y0 + self.MIN, y1))

    def release(self, _=None):
        act, self.act = self.act, None
        if act and act[0] == "new":
            key = act[3]
            r = self.rect[key]
            if not r or r[2] - r[0] < self.MIN or r[3] - r[1] < self.MIN:
                self.rect[key] = act[2]
                self.draw()
        return "break"

    def hover(self, e):
        x, y = self.clamp(e)
        part = self.pick(x, y)[1]
        cur = {"in": "fleur", "tl": "top_left_corner", "tr": "top_right_corner", "bl": "bottom_left_corner", "br": "bottom_right_corner",
               "l": "sb_h_double_arrow", "r": "sb_h_double_arrow", "t": "sb_v_double_arrow", "b": "sb_v_double_arrow"}.get(part, "crosshair")
        try:
            self.cv.configure(cursor=cur)
        except tk.TclError:
            self.cv.configure(cursor="crosshair")

    def nudge(self, dx, dy):
        key = self.key.get()
        r = self.rect[key]
        if not r:
            return "break"
        w, h = r[2] - r[0], r[3] - r[1]
        nx = min(max(r[0] + dx, 0), self.dw - w)
        ny = min(max(r[1] + dy, 0), self.dh - h)
        self.rect[key] = (nx, ny, nx + w, ny + h)
        self.draw()
        return "break"

    def crop_of(self, r):
        return region_px(((r[0] / self.dw), (r[1] / self.dh), (r[2] - r[0]) / self.dw, (r[3] - r[1]) / self.dh), self.dw, self.dh)

    def main_px(self):
        r = self.rect["main"]
        return self.crop_of(r) if r else (0, 0, self.dw // 2 * 2, self.dh // 2 * 2)

    def effective(self):
        cam = self.rect["cam"]
        if not (cam and self.dw and self.dh):
            return None
        return exclude_cam(self.main_px(), self.crop_of(cam), self.hold["cfg"]["split_overlap"])

    def schedule_preview(self):
        if self.pv_job:
            try:
                self.after_cancel(self.pv_job)
            except tk.TclError:
                pass
        self.pv_job = self.after(220, self.preview)

    def preview(self):
        self.pv_job = None
        if not self.winfo_exists():
            return
        cam = self.rect["cam"]
        if not (cam and self.dw):
            self.pv_img = None
            self.pv.configure(image="", text=self.PV_EMPTY, width=28, height=14)
            return
        c, (W, H, cam_h), work = self.hold["cfg"], self.pv_size, Path(self.hold["work"])
        main, patch, _ = self.effective()
        lay = {"cam": self.crop_of(cam), "main": main, "patch": patch, "cam_h": cam_h, "top": c["split_cam_pos"] == "top"}
        out = work / "split_preview.png"
        try:
            r = subprocess.run([which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-i", str(work / "split_frame.png"), "-vf", split_filter(c, W, H, "", lay),
                                "-frames:v", "1", str(out)], capture_output=True, timeout=30, **proc_kw())
            ok = r.returncode == 0 and out.is_file()
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            try:
                self.pv_img = tk.PhotoImage(file=str(out))
                self.pv.configure(image=self.pv_img, text="", width=0, height=0)
            except (OSError, tk.TclError):
                self.pv.configure(image="", text="Preview unavailable", width=28, height=14)

    def destroy(self):
        if self.pv_job:
            try:
                self.after_cancel(self.pv_job)
            except tk.TclError:
                pass
            self.pv_job = None
        try:
            if self.grab_current() == self:
                self.grab_release()
        except tk.TclError:
            pass
        super().destroy()

    def clear(self):
        self.rect[self.key.get()] = None
        self.draw()

    def frac(self, r):
        return (r[0] / self.dw, r[1] / self.dh, (r[2] - r[0]) / self.dw, (r[3] - r[1]) / self.dh)

    def too_small(self, r):
        return not r or r[2] - r[0] < self.MIN or r[3] - r[1] < self.MIN

    def ok(self):
        cam, main = self.rect["cam"], self.rect["main"]
        if self.too_small(cam):
            self.key.set("cam")
            messagebox.showinfo("Clipper", "Draw the face-cam rectangle first.", parent=self)
            return
        if main and self.too_small(main):
            self.key.set("main")
            messagebox.showinfo("Clipper", "The content box is too small. Redraw it, or use Clear this box to use the full frame.", parent=self)
            return
        cam_f, main_f = self.frac(cam), self.frac(main) if main else (0.0, 0.0, 1.0, 1.0)
        self.app.set["split_cam"](fmt_region(cam_f))
        self.app.set["split_main"](fmt_region(main_f))
        self.hold["res"] = (cam_f, main_f)
        self.hold["ev"].set()
        self.destroy()

    def cancel(self):
        self.hold["res"] = None
        self.hold["ev"].set()
        self.destroy()

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title(f"Clipper {VERSION} — Video to Short Clips"); self.geometry("1280x860"); self.minsize(1040, 700)
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, wh = min(1280, sw - 80), min(860, sh - 100)
        self.geometry(f"{ww}x{wh}+{max(0, (sw-ww)//2)}+{max(0, (sh-wh)//2)}")
        self.q, self.cancel_ev, self.pipe = queue.Queue(), threading.Event(), None
        self.get, self.set, self.rows, self.t_step, self.running = {}, {}, [], time.time(), False
        self.pages, self.nav, self.chips, self.scroller = {}, {}, [], None
        self.model_cbs, self.pulling, self.pull_cancel, self.job, self.job_busy, self.models_loaded, self.setup_info = [], False, threading.Event(), None, False, False, None
        self.last_files = []  # finished clips (for the YouTube upload button)
        self.asked_setup = False  # the "install what is missing?" question is asked once per launch
        self.style_ui(); self.build(); self.build_menus(); self.load(); self.toggle_adv()
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"): self.bind_all(seq, self.wheel)
        self.bind_all("<Control-o>", self.main_key(self.browse))
        self.bind_all("<Control-Return>", self.main_key(self.start))
        self.bind_all("<Escape>", self.main_key(lambda: self.stop() if self.running else None))
        self.bind_all("<Control-1>", self.main_key(lambda: self.show_page("Progress")))
        for idx, page_name in enumerate(SCHEMA, 2):
            if idx <= 9:
                self.bind_all(f"<Control-{idx}>", self.main_key(lambda n=page_name: self.show_page(n)))
        self.after(100, self.poll); self.after(600, self.setup_check); self.protocol("WM_DELETE_WINDOW", self.on_close)

    def style_ui(self):
        global FAM
        FAM = pick_font()
        for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(n).configure(family=FAM, size=10)
        tkfont.nametofont("TkHeadingFont").configure(size=12, weight="bold")
        s = ttk.Style(self); s.theme_use("clam"); self.configure(bg=BG)
        s.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, troughcolor=FIELD, font=(FAM, 10), focuscolor=BG)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Card.TFrame", background=PANEL)
        for st in ("TEntry", "TSpinbox"):
            s.configure(st, padding=(8, 6), fieldbackground=FIELD, insertcolor=FG, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD, arrowcolor=FG, background=FIELD)
            s.map(st, bordercolor=[("focus", ACC)], lightcolor=[("focus", ACC)], darkcolor=[("focus", ACC)], fieldbackground=[("readonly", FIELD), ("disabled", PANEL)], foreground=[("disabled", MUTED)])
        s.configure("TCombobox", padding=(8, 5), arrowcolor=FG, background=FIELD, selectbackground=FIELD, selectforeground=FG, bordercolor=BORDER, lightcolor=FIELD, darkcolor=FIELD)
        s.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", PANEL)], foreground=[("readonly", FG), ("disabled", MUTED)], selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)],
              background=[("active", "#2e3441"), ("readonly", FIELD)], arrowcolor=[("readonly", FG)], bordercolor=[("focus", ACC)], lightcolor=[("focus", ACC)], darkcolor=[("focus", ACC)])
        s.configure("TCheckbutton", background=BG, indicatorbackground=FIELD, indicatorforeground=ACC_FG, indicatormargin=4)
        s.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", ACC)])
        s.configure("Slim.Horizontal.TProgressbar", background=ACC, troughcolor=FIELD, borderwidth=0, thickness=8, lightcolor=ACC, darkcolor=ACC)
        s.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG, borderwidth=0, rowheight=34, font=(FAM, 10))
        s.map("Treeview", background=[("selected", "#245b60")], foreground=[("selected", FG)])
        s.configure("Treeview.Heading", background=SIDE, foreground=MUTED, borderwidth=0, padding=(9, 8), font=(FAM, 9, "bold"))
        for name, trough in (("Vertical.TScrollbar", BG), ("Log.Vertical.TScrollbar", PANEL)):
            s.layout(name, [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
            s.configure(name, background="#2c313c", troughcolor=trough, bordercolor=trough, lightcolor="#2c313c", darkcolor="#2c313c", relief="flat", width=10)
            s.map(name, background=[("active", "#3a4150")])
        for k, v in (("background", FIELD), ("foreground", FG), ("selectBackground", ACC), ("selectForeground", ACC_FG)): self.option_add(f"*TCombobox*Listbox.{k}", v)

    @staticmethod
    def card(parent): return tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
    def header(self, parent, title, sub, row):
        h = tk.Frame(parent, bg=BG); h.grid(row=row, column=0, sticky="ew", padx=30, pady=(8, 20))
        tk.Label(h, text=title, bg=BG, fg=FG, font=(FAM, 21, "bold")).pack(anchor="w")
        tk.Label(h, text=sub, bg=BG, fg=MUTED, font=(FAM, 10)).pack(anchor="w", pady=(2, 0))

    def build(self):
        side = tk.Frame(self, bg=SIDE, width=214); side.pack(side="left", fill="y"); side.pack_propagate(False)
        brand = tk.Frame(side, bg=SIDE); brand.pack(fill="x", padx=22, pady=(26, 0))
        mark = tk.Canvas(brand, width=30, height=30, bg=SIDE, highlightthickness=0); mark.pack(side="left", padx=(0, 10))
        mark.create_oval(1, 1, 29, 29, fill=ACC, outline=ACC)
        mark.create_polygon(12, 8, 22, 15, 12, 22, fill=ACC_FG, outline=ACC_FG)
        tk.Label(brand, text="CLIPPER", bg=SIDE, fg=FG, font=(FAM, 17, "bold")).pack(side="left")
        tk.Label(side, text="VIDEO WORKSPACE", bg=SIDE, fg=MUTED, font=(FAM, 8, "bold")).pack(anchor="w", padx=23, pady=(5, 25))
        self.nav["Progress"] = NavItem(side, "Progress", lambda: self.show_page("Progress")); self.nav["Progress"].pack(fill="x")
        tk.Label(side, text="WORKSPACE SETTINGS", bg=SIDE, fg=MUTED, font=(FAM, 8, "bold")).pack(anchor="w", padx=22, pady=(22, 8))
        for name in SCHEMA:
            self.nav[name] = NavItem(side, name, lambda n=name: self.show_page(n)); self.nav[name].pack(fill="x")
        tk.Label(side, text="TOOLS", bg=SIDE, fg=MUTED, font=(FAM, 8, "bold")).pack(anchor="w", padx=22, pady=(22, 8))
        self.nav["Setup"] = NavItem(side, "Setup", lambda: self.show_page("Setup")); self.nav["Setup"].pack(fill="x")
        bot = tk.Frame(side, bg=SIDE); bot.pack(side="bottom", fill="x", padx=20, pady=22)
        tk.Label(bot, text="QUICK PRESET", bg=SIDE, fg=MUTED, font=(FAM, 8, "bold")).pack(anchor="w")
        pc = ttk.Combobox(bot, values=list(PRESETS), state="readonly"); pc.set("Choose a preset"); pc.pack(fill="x", pady=(5, 18)); pc.bind("<<ComboboxSelected>>", lambda _: self.preset(pc.get()))
        Tip(pc, "Applies a group of settings. You can still change each one afterwards.")
        row = tk.Frame(bot, bg=SIDE); row.pack(fill="x")
        tk.Label(row, text="Advanced options", bg=SIDE, fg=FG, font=(FAM, 9)).pack(side="left")
        self.adv = tk.BooleanVar(value=False); Switch(row, self.adv, bg=SIDE).pack(side="right")
        self.adv.trace_add("write", lambda *_: self.toggle_adv())

        main = tk.Frame(self, bg=BG); main.pack(side="left", fill="both", expand=True)
        top = tk.Frame(main, bg=BG); top.pack(fill="x", padx=30, pady=(24, 18)); top.columnconfigure(0, weight=1)
        self.url = tk.StringVar()
        box = tk.Frame(top, bg=FIELD, highlightthickness=1, highlightbackground=BORDER); box.grid(row=0, column=0, sticky="ew")
        e = tk.Entry(box, textvariable=self.url, bg=FIELD, fg=FG, insertbackground=FG, relief="flat", bd=0, highlightthickness=0, font=(FAM, 11), selectbackground=ACC, selectforeground=ACC_FG)
        e.pack(fill="x", padx=16, ipady=12)
        ph = tk.Label(box, text="Paste a video URL or select a local video file", bg=FIELD, fg=MUTED, font=(FAM, 10)); ph.place(x=16, rely=0.5, anchor="w"); ph.bind("<Button-1>", lambda _: e.focus_set())
        self.url.trace_add("write", lambda *_: ph.place_forget() if self.url.get() else ph.place(x=16, rely=0.5, anchor="w"))
        e.bind("<Return>", lambda _: self.start()); e.bind("<FocusIn>", lambda _: box.configure(highlightbackground=ACC)); e.bind("<FocusOut>", lambda _: box.configure(highlightbackground=BORDER)); e.focus_set()
        Btn(top, "Choose file", self.browse).grid(row=0, column=1, padx=(10, 0))
        self.go_btn = Btn(top, "Make clips", self.start, kind="primary"); self.go_btn.grid(row=0, column=2, padx=(10, 0))
        self.stop_btn = Btn(top, "Cancel", self.stop, state="disabled"); self.stop_btn.grid(row=0, column=3, padx=(10, 0))

        self.content = tk.Frame(main, bg=BG); self.content.pack(fill="both", expand=True, padx=(0, 4)); self.content.rowconfigure(0, weight=1); self.content.columnconfigure(0, weight=1)
        self.build_progress()
        for name, items in SCHEMA.items(): self.build_page(name, items)
        self.build_setup(); self.show_page("Progress")

    def main_key(self, fn):
        """Wrap a keyboard shortcut so it only fires when the main window has focus, not a picker or review window."""
        def handler(e):
            try:
                if e.widget.winfo_toplevel() is not self: return None
            except (AttributeError, KeyError, tk.TclError): return None
            fn()
            return "break"
        return handler

    def build_menus(self):
        """Native menus make common actions discoverable and keyboard-accessible."""
        bar = tk.Menu(self, tearoff=False)
        file_menu = tk.Menu(bar, tearoff=False)
        file_menu.add_command(label="Choose video…", accelerator="Ctrl+O", command=self.browse)
        file_menu.add_command(label="Make clips", accelerator="Ctrl+Enter", command=self.start)
        file_menu.add_command(label="Cancel current job", accelerator="Esc", command=self.stop)
        file_menu.add_separator()
        file_menu.add_command(label="Open output folder", command=lambda: self.open_path(self.last_out()))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", accelerator="Alt+F4", command=self.on_close)
        bar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(bar, tearoff=False)
        view_menu.add_command(label="Progress", accelerator="Ctrl+1", command=lambda: self.show_page("Progress"))
        for idx, page_name in enumerate(SCHEMA, 2):
            view_menu.add_command(label=page_name, accelerator=f"Ctrl+{idx}" if idx <= 9 else "", command=lambda n=page_name: self.show_page(n))
        view_menu.add_command(label="Setup", command=lambda: self.show_page("Setup"))
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Advanced options", variable=self.adv)
        bar.add_cascade(label="View", menu=view_menu)

        tools_menu = tk.Menu(bar, tearoff=False)
        tools_menu.add_command(label="AI model picker", command=lambda: self.show_page("AI picker"))
        tools_menu.add_command(label="YouTube settings", command=lambda: self.show_page("YouTube"))
        tools_menu.add_command(label="Setup and dependencies", command=lambda: self.show_page("Setup"))
        tools_menu.add_separator()
        tools_menu.add_command(label="Clear download cache…", command=self.clear_cache)
        tools_menu.add_command(label="Reset settings…", command=self.reset)
        bar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(bar, tearoff=False)
        help_menu.add_command(label="Keyboard shortcuts", command=self.show_shortcuts)
        help_menu.add_command(label="About Clipper", command=self.show_about)
        bar.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=bar)
        self.app_menu = bar

    def show_shortcuts(self):
        messagebox.showinfo(
            "Keyboard shortcuts",
            "Ctrl + O       Choose a video file\n"
            "Ctrl + Enter   Start making clips\n"
            "Escape         Cancel the current job\n"
            "Ctrl + 1       Open Progress\n"
            "Ctrl + 2…      Open settings pages\n"
            "Alt + F4       Close Clipper",
            parent=self,
        )

    def show_about(self):
        messagebox.showinfo(
            "About Clipper",
            f"Clipper {VERSION} — Video to Short Clips\n\n"
            "A local video clipping workspace powered by yt-dlp, faster-whisper, "
            "Ollama and FFmpeg.\n\n"
            f"Python {sys.version.split()[0]} on {platform.system()} {platform.release()}\n"
            f"Settings: {SETTINGS_FILE}",
            parent=self,
        )

    def build_progress(self):
        pg = tk.Frame(self.content, bg=BG); pg.columnconfigure(0, weight=1); pg.rowconfigure(2, weight=1)
        self.header(pg, "Progress", "Each step shows its own progress and an estimate of the time left.", 0)
        c = self.card(pg); c.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 18))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=24, pady=20); inn.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="Ready."); tk.Label(inn, textvariable=self.status, bg=PANEL, fg=FG, font=(FAM, 16, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        self.eta = tk.StringVar(); tk.Label(inn, textvariable=self.eta, bg=PANEL, fg=MUTED, font=(FAM, 10), anchor="w").grid(row=1, column=0, sticky="w", pady=(2, 14))
        self.step_pb = ttk.Progressbar(inn, maximum=100, style="Slim.Horizontal.TProgressbar"); self.step_pb.grid(row=2, column=0, sticky="ew")
        chips = tk.Frame(inn, bg=PANEL); chips.grid(row=3, column=0, sticky="w", pady=(12, 0))
        for i, n in enumerate(STEPS, 1):
            lab = tk.Label(chips, text=f"{i}  {n}", bg=PANEL, fg=MUTED, font=(FAM, 9)); lab.pack(side="left", padx=(0, 16)); self.chips.append(lab)
        tk.Label(inn, text="Overall", bg=PANEL, fg=MUTED, font=(FAM, 9)).grid(row=4, column=0, sticky="w", pady=(18, 5))
        self.all_pb = ttk.Progressbar(inn, maximum=100, style="Slim.Horizontal.TProgressbar"); self.all_pb.grid(row=5, column=0, sticky="ew")
        lc = self.card(pg); lc.grid(row=2, column=0, sticky="nsew", padx=28); lc.columnconfigure(0, weight=1); lc.rowconfigure(0, weight=1)
        self.logbox = tk.Text(lc, bg=PANEL, fg=LOG_FG, relief="flat", wrap="word", state="disabled", padx=16, pady=12, font=(MONO, 9), highlightthickness=0, spacing1=2)
        self.logbox.grid(row=0, column=0, sticky="nsew"); sb = ttk.Scrollbar(lc, command=self.logbox.yview, style="Log.Vertical.TScrollbar"); sb.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4); self.logbox["yscrollcommand"] = sb.set
        self.logbox.tag_configure("err", foreground=DANGER); self.logbox.tag_configure("ok", foreground=SUCCESS)
        bt = tk.Frame(pg, bg=BG); bt.grid(row=3, column=0, sticky="e", padx=28, pady=16)
        Btn(bt, "Open output folder", lambda: self.open_path(self.last_out())).pack(side="left", padx=(0, 10))
        self.yt_btn = Btn(bt, "Upload to YouTube", self.upload_button_clicked); self.yt_btn.pack(side="left", padx=(0, 10))
        self.yt_stop = Btn(bt, "Stop upload", self.yt_stop_clicked, state="disabled"); self.yt_stop.pack(side="left", padx=(0, 10))
        Btn(bt, "Clear cache", self.clear_cache).pack(side="left", padx=(0, 10)); Btn(bt, "Reset settings", self.reset).pack(side="left")
        self.pages["Progress"] = pg

    def build_page(self, name, items):
        sc = Scrolled(self.content); body = sc.body; body.columnconfigure(0, weight=1)
        self.header(body, name, SUBTITLES.get(name, ""), 0)
        c = self.card(body); c.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 28))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=28, pady=16); inn.columnconfigure(0, weight=1, minsize=300); inn.columnconfigure(1, weight=0)
        for r, it in enumerate(items): self.add_row(inn, r, *it)
        if name == "AI picker": c.grid_configure(pady=(0, 16)); self.build_models_card(body, 2)
        if name == "YouTube": c.grid_configure(pady=(0, 16)); self.build_yt_guide_card(body, 2)
        self.pages[name] = sc

    def show_page(self, name):
        for p in self.pages.values(): p.grid_remove()
        self.pages[name].grid(row=0, column=0, sticky="nsew")
        for n, item in self.nav.items(): item.set_active(n == name)
        self.scroller = self.pages[name] if isinstance(self.pages[name], Scrolled) else None
        if name == "AI picker" and not self.models_loaded: self.models_loaded = True; self.refresh_models(quiet=True)
    def wheel(self, e):
        try:
            if self.scroller is None or e.widget.winfo_toplevel() is not self or isinstance(e.widget, tk.Text): return
        except (AttributeError, KeyError, tk.TclError): return
        self.scroller.cv.yview_scroll((-1 if (e.num == 4 or getattr(e, "delta", 0) > 0) else 1), "units")

    def add_row(self, f, r, key, label, kind, default, opts, hint, adv):
        blk = tk.Frame(f, bg=PANEL); blk.grid(row=r, column=0, sticky="nw", pady=15, padx=(0, 30))
        tk.Label(blk, text=label, bg=PANEL, fg=FG, font=(FAM, 10), anchor="w").pack(anchor="w")
        if hint: tk.Label(blk, text=hint, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=390, justify="left", anchor="w").pack(anchor="w", pady=(3, 0))
        if kind == "bool":
            v = tk.BooleanVar(value=default); w = Switch(f, v); self.get[key], self.set[key] = v.get, v.set
        elif kind == "choice":
            v = tk.StringVar(value=str(default)); w = ttk.Combobox(f, textvariable=v, values=[str(o) for o in opts], state="readonly", width=22); self.get[key], self.set[key] = v.get, v.set
        elif kind in ("int", "float"):
            v = tk.StringVar(value=str(default)); w = ttk.Spinbox(f, textvariable=v, from_=opts[0], to=opts[1], increment=opts[2] if len(opts) > 2 else 1, width=10); self.get[key], self.set[key] = v.get, v.set
        elif kind == "text":
            w = tk.Text(f, height=3, width=34, bg=FIELD, fg=FG, relief="flat", wrap="word", insertbackground=FG, highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACC, padx=8, pady=6, font=(FAM, 10))
            self.get[key] = lambda w=w: w.get("1.0", "end").strip(); self.set[key] = lambda s, w=w: (w.delete("1.0", "end"), w.insert("1.0", s))
        elif kind == "path":
            v = tk.StringVar(value=default); w = tk.Frame(f, bg=PANEL); ttk.Entry(w, textvariable=v, width=30).pack(side="left")
            Btn(w, "Browse", lambda v=v: v.set(filedialog.askdirectory(initialdir=v.get()) or v.get()), bg=PANEL, small=True).pack(side="left", padx=(8, 0)); self.get[key], self.set[key] = v.get, v.set
        elif kind == "ollama":
            v = tk.StringVar(value=default); w = tk.Frame(f, bg=PANEL); cb = ttk.Combobox(w, textvariable=v, width=24); cb.pack(side="left")
            # Pre-populate with the suggested models so the dropdown is never empty, even before
            # Ollama is started or any model is downloaded. The user can pick a suggestion and
            # download it from the "Download more models" card below.
            cb["values"] = list(SUGGESTED)
            self.model_cbs.append(cb)
            Btn(w, "Refresh", lambda cb=cb: self.refresh_models(cb), bg=PANEL, small=True).pack(side="left", padx=(8, 0)); self.get[key], self.set[key] = v.get, v.set
        else:
            v = tk.StringVar(value=default); w = ttk.Entry(f, textvariable=v, width=36); self.get[key], self.set[key] = v.get, v.set
        w.grid(row=r, column=1, sticky="ne" if kind == "text" else "e", pady=12); self.rows.append(([blk, w], adv))

    def toggle_adv(self):
        for ws, adv in self.rows:
            if adv:
                for w in ws: w.grid() if self.adv.get() else w.grid_remove()

    # ───────── AI picker: download more Ollama models ─────────
    def build_models_card(self, body, row):
        c = self.card(body); c.grid(row=row, column=0, sticky="ew", padx=30, pady=(0, 28))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=26, pady=18); inn.columnconfigure(0, weight=1)
        tk.Label(inn, text="Download more models", bg=PANEL, fg=FG, font=(FAM, 11, "bold")).grid(row=0, column=0, sticky="w", columnspan=2)
        self.gpu_note = tk.StringVar(value="Checking your GPU...")
        tk.Label(inn, textvariable=self.gpu_note, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=620, justify="left", anchor="w").grid(row=1, column=0, sticky="w", columnspan=2, pady=(3, 14))
        tk.Label(inn, text="Installed", bg=PANEL, fg=FG, font=(FAM, 10)).grid(row=2, column=0, sticky="w")
        self.installed_var = tk.StringVar(value="Press Refresh in the Ollama model row to list them.")
        tk.Label(inn, textvariable=self.installed_var, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=620, justify="left", anchor="w").grid(row=3, column=0, sticky="w", columnspan=2, pady=(2, 14))
        tk.Label(inn, text="Model to download", bg=PANEL, fg=FG, font=(FAM, 10)).grid(row=4, column=0, sticky="w")
        ctl = tk.Frame(inn, bg=PANEL); ctl.grid(row=5, column=0, sticky="w", pady=(6, 0), columnspan=2)
        self.pull_var = tk.StringVar(value="llama3.2:3b")
        pc = ttk.Combobox(ctl, textvariable=self.pull_var, values=list(SUGGESTED), width=30); pc.pack(side="left")
        self.pull_btn = Btn(ctl, "Download", self.pull_model, kind="primary", bg=PANEL, small=True); self.pull_btn.pack(side="left", padx=(10, 0))
        self.pull_stop = Btn(ctl, "Cancel", lambda: self.pull_cancel.set(), bg=PANEL, small=True, state="disabled"); self.pull_stop.pack(side="left", padx=(8, 0))
        self.pull_hint = tk.StringVar()
        tk.Label(inn, textvariable=self.pull_hint, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=620, justify="left", anchor="w").grid(row=6, column=0, sticky="w", pady=(6, 0), columnspan=2)
        self.pull_pb = ttk.Progressbar(inn, maximum=100, style="Slim.Horizontal.TProgressbar"); self.pull_pb.grid(row=7, column=0, sticky="ew", pady=(14, 4), columnspan=2)
        self.pull_status = tk.StringVar()
        tk.Label(inn, textvariable=self.pull_status, bg=PANEL, fg=MUTED, font=(FAM, 9), anchor="w").grid(row=8, column=0, sticky="w", columnspan=2)
        def hint(*_):
            n = self.pull_var.get().strip(); g = SUGGESTED.get(n)
            self.pull_hint.set(f"About {g[0]} GB. {g[1]}" if g else "Any model name from ollama.com/library works, for example llama3.2:1b.")
        self.pull_var.trace_add("write", hint); hint()

    def pull_model(self):
        name = self.pull_var.get().strip()
        if not name: return
        if self.pulling: return
        self.pulling = True; self.pull_cancel.clear(); self.pull_btn.state(["disabled"]); self.pull_stop.state(["!disabled"]); self.pull_pb["value"] = 0; self.pull_status.set("Starting...")
        host = self.get["ollama_host"]().rstrip("/")
        def run():
            layers, top, err = {}, 0.0, None
            try:
                with ollama_post(host, "/api/pull", {"model": name, "stream": True}, 120) as r:
                    for line in r:
                        if self.pull_cancel.is_set(): err = "Cancelled. Run it again to resume where it stopped."; break
                        if not line.strip(): continue
                        d = json.loads(line)
                        if "error" in d: err = d["error"]; break
                        if d.get("total"): layers[d.get("digest", "?")] = (d.get("completed", 0), d["total"])
                        tot, done = sum(t for _, t in layers.values()), sum(c for c, _ in layers.values())
                        top = max(top, done / tot) if tot else top
                        self.q.put(("pull_prog", top if tot else None, f"{d.get('status', '')}  {done / 1e9:.2f} of {tot / 1e9:.2f} GB" if tot else d.get("status", "")))
            except urllib.error.HTTPError as e: err = f"Ollama error {e.code}: {e.read()[:150].decode(errors='replace')}"
            except urllib.error.URLError: err = "Couldn't reach Ollama. Start it on the Setup page first."
            except (OSError, ValueError) as e: err = str(e)
            self.q.put(("pull_done", name, err))
        threading.Thread(target=run, daemon=True).start()

    def refresh_models(self, cb=None, quiet=False):
        host = self.get["ollama_host"]().rstrip("/")
        threading.Thread(target=lambda: self.q.put(("models", ollama_models(host), quiet)), daemon=True).start()

    def on_models(self, models, quiet):
        if models is None:
            self.installed_var.set("Couldn't reach Ollama. Start it on the Setup page.")
            if not quiet: messagebox.showwarning("Ollama", "Couldn't reach Ollama. Is it running? You can start it on the Setup page.")
            return
        installed_names = [n for n, _ in models]
        # Build the dropdown list: installed models first, then suggested models that aren't
        # already installed. This way the dropdown is never empty, and installed models are
        # easy to find at the top.
        values = list(installed_names)
        for s in SUGGESTED:
            if s not in values: values.append(s)
        for cb in self.model_cbs: cb["values"] = values
        if installed_names:
            self.installed_var.set(", ".join(f"{n} ({s / 1e9:.1f} GB)" for n, s in models))
        else:
            self.installed_var.set("No models installed yet. Pick a suggestion from the dropdown, or download one below.")
    def on_pull_prog(self, frac, text):
        if frac is None:
            if str(self.pull_pb["mode"]) != "indeterminate": self.pull_pb.configure(mode="indeterminate"); self.pull_pb.start(12)
        else:
            if str(self.pull_pb["mode"]) == "indeterminate": self.pull_pb.stop(); self.pull_pb.configure(mode="determinate")
            self.pull_pb["value"] = frac * 100
        self.pull_status.set(text)
    def on_pull_done(self, name, err):
        self.pulling = False; self.pull_pb.stop(); self.pull_pb.configure(mode="determinate"); self.pull_btn.state(["!disabled"]); self.pull_stop.state(["disabled"])
        self.pull_pb["value"] = 0 if err else 100
        self.pull_status.set(err or f"Downloaded {name}. Pick it in the Ollama model row above to use it.")
        if not err: self.refresh_models(quiet=True)

    # ───────── YouTube upload ─────────
    def build_yt_guide_card(self, body, row):
        c = self.card(body); c.grid(row=row, column=0, sticky="ew", padx=30, pady=(0, 28))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=26, pady=18)
        tk.Label(inn, text="Get YouTube upload credentials", bg=PANEL, fg=FG, font=(FAM, 11, "bold")).pack(anchor="w")
        tk.Label(inn, text="Clipper needs a client_secret.json from Google Cloud to upload. Here is how to get one:",
                 bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=620, justify="left", anchor="w").pack(anchor="w", pady=(4, 10))
        steps = [
            "1.  Go to console.cloud.google.com  →  create a new project (or pick an existing one).",
            "2.  APIs & Services  →  Library  →  search 'YouTube Data API v3'  →  Enable it.",
            "3.  APIs & Services  →  OAuth consent screen  →  External  →  fill in a name. Add yourself as a Test user.",
            "4.  APIs & Services  →  Credentials  →  Create Credentials  →  OAuth client ID.",
            "5.  Application type: Desktop app.  Give it a name, then Create.",
            "6.  Click 'Download JSON'.  Rename the file to client_secret.json.",
            "7.  Put client_secret.json in your home folder (or set its path above).",
            "8.  Turn on 'Upload clips to YouTube' above. The first upload opens a browser to sign in.",
        ]
        for s in steps:
            tk.Label(inn, text=s, bg=PANEL, fg=FG, font=(FAM, 9), anchor="w", justify="left").pack(anchor="w", pady=1)
        btns = tk.Frame(inn, bg=PANEL); btns.pack(anchor="w", pady=(12, 0))
        Btn(btns, "Open Google Cloud Console", lambda: webbrowser.open("https://console.cloud.google.com/"), kind="primary", bg=PANEL, small=True).pack(side="left", padx=(0, 8))
        Btn(btns, "Test connection", self.yt_test_connection, bg=PANEL, small=True).pack(side="left")

    def yt_test_connection(self):
        """Quick check: are the YouTube packages installed, and does the client_secret exist?"""
        if not have_mod("googleapiclient") or not have_mod("google_auth_oauthlib"):
            messagebox.showwarning("YouTube", "The YouTube packages aren't installed.\nGo to the Setup page and install 'YouTube upload packages', then try again.", parent=self)
            return
        cs = Path(self.get["yt_client_secret"]()).expanduser()
        if not cs.is_file():
            messagebox.showwarning("YouTube", f"client_secret.json not found at:\n{cs}\n\nDownload it from Google Cloud Console (see the steps above) and place it there, or set its path in the YouTube settings.", parent=self)
            return
        messagebox.showinfo("YouTube", f"client_secret.json found at:\n{cs}\n\nEverything looks ready. The first upload will open a browser to sign in to Google.", parent=self)

    def upload_button_clicked(self):
        if self.running:
            messagebox.showinfo("Clipper", "Wait for the current job to finish, or cancel it first.", parent=self)
            return
        files = getattr(self, "last_files", None)
        if not files:
            messagebox.showinfo("Clipper", "No clips to upload. Make some clips first.", parent=self)
            return
        # Filter to only valid file paths
        valid = [f for f in files if Path(f["path"] if isinstance(f, dict) else f).is_file()]
        if not valid:
            messagebox.showinfo("Clipper", "The clip files were moved or deleted. Render clips first.", parent=self)
            return
        if not messagebox.askyesno("Upload to YouTube", f"Upload {len(valid)} clip(s) to YouTube?"):
            return
        self.upload_youtube(valid)

    def upload_youtube(self, files):
        """Upload clips to YouTube in a background thread. 'files' is a list of dicts with path/title/description."""
        c = self.cfg()
        if not have_mod("googleapiclient") or not have_mod("google_auth_oauthlib"):
            self.on_log("YouTube upload: packages not installed. Go to Setup to install them.")
            return
        cs_path = Path(c["yt_client_secret"]).expanduser()
        if not cs_path.is_file():
            self.on_log(f"YouTube upload: client_secret.json not found at {cs_path}. See the YouTube settings page for steps.")
            return
        token_path = SETTINGS_FILE.parent / "youtube_token.json"
        privacy = c["yt_privacy"]
        hashtags_in_title = c["yt_hashtags_in_title"]
        default_tags = [t.strip() for t in c["yt_default_tags"].split(",") if t.strip()]
        cancel_ev = threading.Event(); self.yt_cancel = cancel_ev
        self.yt_btn.state(["disabled"]); self.yt_stop.state(["!disabled"])
        self.status.set(f"Uploading {len(files)} clip(s) to YouTube...")
        def emit(*a): self.q.put(a)
        def run():
            try:
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow
                from google.auth.transport.requests import Request
                from googleapiclient.discovery import build
                from googleapiclient.http import MediaFileUpload
            except ImportError as e:
                self.q.put(("log", f"YouTube upload: missing package ({e}). Install 'YouTube upload packages' on the Setup page."))
                self.q.put(("yt_done",))
                return
            SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
            # --- authenticate (load saved token, or run OAuth browser flow) ---
            creds = None
            if token_path.is_file():
                try: creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                except Exception: creds = None
            if creds and creds.expired and creds.refresh_token:
                try: creds.refresh(Request())
                except Exception: creds = None
            if not creds or not creds.valid:
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(str(cs_path), SCOPES)
                    creds = flow.run_local_server(port=0)  # opens the default browser
                except Exception as e:
                    self.q.put(("log", f"YouTube auth failed: {e}"))
                    self.q.put(("yt_done",))
                    return
                try:
                    token_path.parent.mkdir(parents=True, exist_ok=True)
                    token_path.write_text(creds.to_json(), encoding="utf-8")
                    os.chmod(token_path, 0o600)  # the token grants upload access; keep it private
                except OSError: pass
            try:
                youtube = build("youtube", "v3", credentials=creds, static_discovery=False)
            except Exception as e:
                self.q.put(("log", f"YouTube: couldn't connect to the API: {e}"))
                self.q.put(("yt_done",))
                return
            # --- upload each clip ---
            uploaded = 0
            for i, f in enumerate(files, 1):
                if cancel_ev.is_set(): break
                path = f["path"] if isinstance(f, dict) else str(f)
                title = (f.get("yt_title") or f.get("title") or Path(path).stem) if isinstance(f, dict) else Path(path).stem
                desc = (f.get("yt_description") or f.get("description") or "") if isinstance(f, dict) else ""
                # Move hashtags from description to title if enabled
                desc_tags = re.findall(r"#\w+", desc)
                if hashtags_in_title and desc_tags:
                    tag_str = " " + " ".join(desc_tags[:5])
                    if tag_str.strip() not in title:
                        title = (title + tag_str)[:100]  # YouTube title limit is 100 chars
                tags, size = [], 0
                for t in (list(f.get("yt_tags") or []) if isinstance(f, dict) else []) + default_tags + [t.lstrip("#") for t in desc_tags]:
                    if t and t.lower() not in {x.lower() for x in tags} and size + len(t) + 1 <= 450: tags.append(t); size += len(t) + 1
                body = {
                    "snippet": {"title": title[:100], "description": desc[:5000], "tags": tags, "categoryId": YT_CATEGORIES.get(c["yt_category"], "22")},
                    "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
                }
                self.q.put(("log", f"YouTube: uploading {Path(path).name} ({i}/{len(files)}) as {privacy}..."))
                try:
                    media = MediaFileUpload(path, mimetype="video/mp4", resumable=True, chunksize=10 * 1024 * 1024)
                    request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
                    response = None
                    while response is None:
                        if cancel_ev.is_set(): break
                        status, response = request.next_chunk()
                        if status:
                            self.q.put(("log", f"  {Path(path).name}: {int(status.progress() * 100)}%"))
                    if response and response.get("id"):
                        vid = response["id"]
                        self.q.put(("log", f"YouTube: uploaded {Path(path).name} → https://www.youtube.com/watch?v={vid}"))
                        uploaded += 1
                    elif response and response.get("error"):
                        self.q.put(("log", f"YouTube: error uploading {Path(path).name}: {response['error'].get('message', '?')}"))
                except Exception as e:
                    self.q.put(("log", f"YouTube: error uploading {Path(path).name}: {e}"))
            note = " (cancelled)" if cancel_ev.is_set() else ""
            self.q.put(("log", f"YouTube: done{note}. {uploaded} of {len(files)} clip(s) uploaded." if uploaded else "YouTube: no clips were uploaded."))
            self.q.put(("yt_done",))
        threading.Thread(target=run, daemon=True).start()

    def yt_stop_clicked(self):
        if getattr(self, "yt_cancel", None): self.yt_cancel.set()
        self.yt_stop.state(["disabled"])
        self.on_log("YouTube upload: stopping after the current chunk...")
    def on_yt_done(self):
        self.yt_btn.state(["!disabled"]); self.yt_stop.state(["disabled"])
        self.status.set("Ready.")

    # ───────── Setup page ─────────
    def section(self, parent, row, title, sub=""):
        c = self.card(parent); c.grid(row=row, column=0, sticky="ew", padx=30, pady=(0, 18))
        tk.Label(c, text=title, bg=PANEL, fg=FG, font=(FAM, 11, "bold")).pack(anchor="w", padx=24, pady=(16, 0))
        if sub: tk.Label(c, text=sub, bg=PANEL, fg=MUTED, font=(FAM, 9), wraplength=700, justify="left").pack(anchor="w", padx=24, pady=(2, 0))
        inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=24, pady=(6, 12)); inn.columnconfigure(1, weight=1); return inn

    def status_row(self, f, r, state, name, why, detail, buttons):
        col = {"ok": ACC, "missing": DANGER, "warn": WARNING, "skip": MUTED}[state]
        tk.Label(f, text="●", bg=PANEL, fg=col, font=(FAM, 10)).grid(row=r, column=0, sticky="nw", padx=(0, 12), pady=(11, 0))
        blk = tk.Frame(f, bg=PANEL); blk.grid(row=r, column=1, sticky="w", pady=8)
        tk.Label(blk, text=name, bg=PANEL, fg=FG, font=(FAM, 10), anchor="w").pack(anchor="w")
        tk.Label(blk, text=why, bg=PANEL, fg=MUTED, font=(FAM, 9), anchor="w", justify="left", wraplength=270).pack(anchor="w")
        tk.Label(f, text=detail, bg=PANEL, fg=MUTED, font=(FAM, 9), anchor="e", justify="right", wraplength=130).grid(row=r, column=2, sticky="e", padx=14)
        bf = tk.Frame(f, bg=PANEL); bf.grid(row=r, column=3, sticky="e")
        for text, cmd in buttons: Btn(bf, text, cmd, bg=PANEL, small=True).pack(side="left", padx=(6, 0))

    def build_setup(self):
        sc = Scrolled(self.content); body = sc.body; body.columnconfigure(0, weight=1)
        self.header(body, "Setup", "See what is installed and install what is missing. Nothing runs unless you press a button.", 0)
        c = self.card(body); c.grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 18)); inn = tk.Frame(c, bg=PANEL); inn.pack(fill="x", padx=24, pady=18)
        self.sys_var = tk.StringVar(value="Checking this computer...")
        tk.Label(inn, textvariable=self.sys_var, bg=PANEL, fg=FG, font=(FAM, 10), justify="left", anchor="w").pack(anchor="w")
        bt = tk.Frame(inn, bg=PANEL); bt.pack(anchor="w", pady=(14, 0))
        for text, cmd, kind in (("Check again", self.setup_check, "secondary"), ("Install enabled dependencies", self.setup_install_all, "primary"), ("Save setup script", self.setup_save_script, "secondary"), ("Copy all commands", self.setup_copy_all, "secondary")):
            Btn(bt, text, cmd, kind=kind, bg=PANEL).pack(side="left", padx=(0, 10))
        self.dep_f = self.section(body, 2, "Requirements", "Programs and Python packages Clipper uses.")
        self.svc_f = self.section(body, 3, "Ollama", "Optional local AI service. Required only when the AI picker is enabled.")
        self.wh_f = self.section(body, 4, "Whisper models", "Speech models. Each is downloaded once and reused. The size you use is set on the Transcribe page.")
        lc = self.card(body); lc.grid(row=5, column=0, sticky="ew", padx=30, pady=(0, 28))
        top = tk.Frame(lc, bg=PANEL); top.pack(fill="x", padx=24, pady=(16, 6))
        tk.Label(top, text="Install log", bg=PANEL, fg=FG, font=(FAM, 11, "bold")).pack(side="left")
        self.job_stop = Btn(top, "Cancel", self.job_cancel, bg=PANEL, small=True, state="disabled"); self.job_stop.pack(side="right")
        self.job_pb = ttk.Progressbar(lc, maximum=100, mode="indeterminate", style="Slim.Horizontal.TProgressbar")
        self.setup_log = tk.Text(lc, height=10, bg=PANEL, fg=LOG_FG, relief="flat", wrap="word", state="disabled", padx=10, pady=6, font=(MONO, 9), highlightthickness=0)
        self.setup_log.pack(fill="x", padx=14, pady=(0, 14)); self.setup_log.tag_configure("err", foreground=DANGER); self.setup_log.tag_configure("ok", foreground=SUCCESS)
        self.pages["Setup"] = sc

    def slog(self, t):
        self.setup_log.configure(state="normal"); self.setup_log.insert("end", t + "\n", "err" if t.startswith(("Error", "Couldn't", "FAILED")) else "")
        try:
            if int(self.setup_log.index("end-1c").split(".")[0]) > 2000:
                self.setup_log.delete("1.0", "301.0")
        except (ValueError, tk.TclError): pass
        self.setup_log.see("end"); self.setup_log.configure(state="disabled")
    def on_setup_log(self, t): self.slog(t)

    def setup_check(self):
        host = self.get["ollama_host"]().rstrip("/")
        def run():
            nv = gpu_info(); name, mgr = system_info(); rows = check_all(nv is not None)
            fw = any(r["id"] == "fw" and r["state"] == "ok" for r in rows)
            wgpu = None
            if nv and fw:
                try: import ctranslate2; wgpu = ctranslate2.get_cuda_device_count() > 0
                except Exception: wgpu = False
            self.q.put(("setup_checked", dict(sysname=name, mgr=mgr, gpu=nv, py=sys.version.split()[0], rows=rows, up=ollama_up(host), models=ollama_models(host), wgpu=wgpu,
                                             whisper={n: whisper_cached(n) for n in WHISPER_REPOS}, fw=fw, host=host)))
        threading.Thread(target=run, daemon=True).start()

    def on_setup_checked(self, i):
        self.setup_info = i; nv = i["gpu"]
        gpu = f"NVIDIA GPU: {nv[0]}" + (f", {nv[1]} GB VRAM" if nv[1] else "") if nv else "No NVIDIA GPU found, so Whisper and encoding use the CPU"
        if nv and i["wgpu"] is not None: gpu += ". Whisper can use it." if i["wgpu"] else ". Whisper can't use it yet: install the CUDA 12 libraries below."
        self.sys_var.set(f"{i['sysname']}  ·  package manager: {mgr_label(i['mgr'])}  ·  Python {i['py']}\n{gpu}")
        self.gpu_note.set(f"Your GPU: {nv[0]}" + (f", {nv[1]} GB. Models up to about {max(nv[1] - 2, 1):.0f} GB fit fully on it. Larger ones run partly on the CPU and are slower." if nv[1] else ".") if nv else "No NVIDIA GPU found, so models run on the CPU. Prefer the small ones.")
        can_elevate = WIN or is_admin() or bool(shutil.which("pkexec")); mgr = i["mgr"]
        for w in self.dep_f.winfo_children(): w.destroy()
        for r, d in enumerate(i["rows"]):
            tag = {"recommended": "  ·  recommended", "optional": "  ·  optional"}.get(d["level"], "")
            pl = plan_for(d, mgr, nv is not None); btns = []
            if d["state"] in ("missing", "warn"):
                if pl["kind"] in ("pip", "winget", "download") or (pl["kind"] == "sys" and (not pl["root"] or can_elevate)):
                    btns.append(("Install", lambda d=d: self.setup_install([d])))
                btns.append(("Copy command", lambda t=pl["text"]: self.copy_text(t)))
                if pl.get("url"): btns.append(("Website", lambda u=pl["url"]: webbrowser.open(u)))
            elif d["state"] == "ok" and d.get("updatable"): btns.append(("Update", lambda d=d: self.setup_install([d], upgrade=True)))
            self.status_row(self.dep_f, r, d["state"], d["name"] + tag, d["why"], d["detail"], btns)
        for w in self.svc_f.winfo_children(): w.destroy()
        have = any(d["id"] == "ollama" and d["state"] == "ok" for d in i["rows"]); b = []
        if i["up"]: st, det = "ok", f"Running at {i['host']}" + (f"  ·  {len(i['models'])} model(s) installed" if i["models"] is not None else "")
        elif have: st, det = "warn", "Installed but not running"; b.append(("Start", self.setup_start_ollama))
        else: st, det = "missing", "Not installed"
        b.append(("Manage models", lambda: self.show_page("AI picker")))
        self.status_row(self.svc_f, 0, st, "Ollama service", "Optional; needed only when the AI picker is enabled.", det, b)
        for w in self.wh_f.winfo_children(): w.destroy()
        for r, n in enumerate(WHISPER_REPOS):
            got = i["whisper"][n]; btn = [] if got or not i["fw"] else [("Download", lambda n=n: self.setup_whisper(n))]
            self.status_row(self.wh_f, r, "ok" if got else "warn", n, f"About {WHISPER_MB[n] / 1000:.1f} GB" if WHISPER_MB[n] >= 1000 else f"About {WHISPER_MB[n]} MB", "downloaded" if got else ("not downloaded yet" if i["fw"] else "install faster-whisper first"), btn)
        self.nav["Setup"].set_badge(any(d["state"] == "missing" for d in i["rows"]))
        if not self.asked_setup and any(d["state"] == "missing" for d in i["rows"]): self.asked_setup = True; self.after(300, self.offer_install)
        # on_models() merges installed + suggested models into the dropdown, so we just delegate
        # to it when we have a valid (possibly empty) model list.
        if i["models"] is not None: self.on_models(i["models"], True)

    def offer_install(self):
        """On launch, offer to install whatever the app can't run without (and the recommended extras), in one click."""
        i = self.setup_info
        if not i or self.job_busy or self.running: return
        todo = [d for d in i["rows"] if d["state"] in ("missing", "warn")]
        if not self.get["use_ai"](): todo = [d for d in todo if d["id"] != "ollama"]
        if not self.get["yt_enabled"](): todo = [d for d in todo if d["id"] != "yt"]
        if not any(d["state"] == "missing" for d in todo): return
        names = "\n".join("   -  " + d["name"] + (" (large download, about 1 GB)" if d["id"] == "cuda" else "") for d in todo)
        if messagebox.askyesno("Clipper setup", "Clipper needs a few things that aren't installed yet:\n\n" + names + "\n\nInstall them now? You can follow the progress on the Setup page.", parent=self):
            self.show_page("Setup"); self.setup_install(todo)

    def copy_text(self, t):
        self.clipboard_clear(); self.clipboard_append(t); self.update(); self.slog("Copied: " + t)
    def setup_copy_all(self):
        i = getattr(self, "setup_info", None)
        if not i: return
        todo = [d for d in i["rows"] if d["state"] in ("missing", "warn")]
        if not self.get["use_ai"]():
            todo = [d for d in todo if d["id"] != "ollama"]
        if not self.get["yt_enabled"]():
            todo = [d for d in todo if d["id"] != "yt"]
        if not todo: self.slog("Everything required by the current settings is installed."); return
        self.copy_text("\n".join(pl["text"] for pl in merge_plans([plan_for(d, i["mgr"], i["gpu"] is not None) for d in todo], i["mgr"])))
    def setup_save_script(self):
        mgr = (getattr(self, "setup_info", None) or {}).get("mgr") or system_info()[1]
        if WIN: path = filedialog.asksaveasfilename(initialfile="setup_clipper.ps1", defaultextension=".ps1", filetypes=[("PowerShell script", "*.ps1")])
        else: path = filedialog.asksaveasfilename(initialfile="setup_clipper.sh", defaultextension=".sh", filetypes=[("Shell script", "*.sh")])
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as fh: fh.write(setup_script(mgr))
        if WIN: self.slog(f"Saved {path}. Run it with: powershell -ExecutionPolicy Bypass -File \"{Path(path).name}\"")
        else:
            os.chmod(path, 0o755)
            self.slog(f"Saved {path}. Run it with: bash {Path(path).name}")

    def start_job(self, title, fn, cancellable=True):
        if self.job_busy: messagebox.showinfo("Setup", "Another task is still running. Wait for it or press Cancel in the install log."); return
        self.job_busy = True; self.job = {"cancel": False, "p": None}; self.job_pb.pack(fill="x", padx=24, pady=(0, 8), before=self.setup_log); self.job_pb.start(12); self.job_stop.state(["!disabled"] if cancellable else ["disabled"]); self.slog("— " + title)
        job = self.job
        def run():
            ok = False
            try: ok = bool(fn(lambda t: self.q.put(("setup_log", t)), job))
            except Exception as e: self.q.put(("setup_log", f"Error: {e}"))
            self.q.put(("setup_done", title, ok))
        threading.Thread(target=run, daemon=True).start()
    def job_cancel(self):
        if self.job: self.job["cancel"] = True; p = self.job.get("p")
        if self.job and p and p.poll() is None: kill_tree(p)
    def on_setup_done(self, title, ok):
        self.job_busy = False; self.job_pb.stop(); self.job_pb.pack_forget(); self.job_stop.state(["disabled"]); self.slog(("Done: " if ok else "FAILED: ") + title)
        if ok and "CUDA" in title: self.slog("Restart Clipper so the GPU libraries are loaded.")
        self.setup_check()

    def setup_install(self, rows, upgrade=False):
        i = self.setup_info; mgr = i["mgr"]
        esc = elevation(gui=True)
        plans = merge_plans([plan_for(d, mgr, i["gpu"] is not None, upgrade) for d in rows], mgr)
        auto = [pl for pl in plans if pl["kind"] in ("pip", "sys", "winget", "download")]; manual = [pl for pl in plans if pl["kind"] == "manual"]
        for pl in manual: self.slog("Install this one yourself: " + pl["text"])
        if any(pl["kind"] == "sys" and pl["root"] for pl in auto) and esc is None:
            for pl in auto:
                if pl["kind"] == "sys":
                    self.slog("Needs admin rights and 'pkexec' wasn't found. Run in a terminal: " + pl["text"])
            auto = [pl for pl in auto if pl["kind"] != "sys"]
        if not auto: return
        def fn(emit, job):
            ok = True
            for pl in auto:
                if job["cancel"]: return False
                ok = run_plan(pl, emit, esc or [], job) and ok
            return ok
        self.start_job("Install " + ", ".join(d["name"] for d in rows), fn)
    def setup_install_all(self):
        i = getattr(self, "setup_info", None)
        if not i: return
        todo = [d for d in i["rows"] if d["state"] in ("missing", "warn")]
        if not self.get["use_ai"]():
            todo = [d for d in todo if d["id"] != "ollama"]
        if not self.get["yt_enabled"]():
            todo = [d for d in todo if d["id"] != "yt"]
        if not todo: self.slog("Everything required by the current settings is installed."); return
        self.setup_install(todo)
    def setup_whisper(self, name):
        def fn(emit, job):
            from faster_whisper import download_model
            emit(f"Downloading Whisper '{name}' (about {WHISPER_MB[name]} MB). This can't be cancelled and shows no progress until it finishes.")
            download_model(name); emit("Downloaded."); return True
        self.start_job(f"Download Whisper model {name}", fn, cancellable=False)
    def setup_start_ollama(self):
        host = self.get["ollama_host"]().rstrip("/")
        def fn(emit, job):
            ollama_bin = which("ollama")
            if not ollama_bin:
                emit("Ollama is not installed or not on PATH. Install it from the Setup page first.")
                return False
            if ollama_up(host): emit("Ollama is already running."); return True
            emit("Starting: ollama serve")
            if WIN: subprocess.Popen([ollama_bin, "serve"], **dict(quiet_kw(), creationflags=NOWIN | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP))
            else: subprocess.Popen([ollama_bin, "serve"], start_new_session=True, **quiet_kw())
            for _ in range(30):
                if ollama_up(host): emit("Ollama is running."); return True
                time.sleep(0.5)
            emit("Ollama didn't start in time. Try: ollama serve  (in a terminal)"); return False
        self.start_job("Start Ollama", fn, cancellable=False)

    # settings
    def cfg(self):
        d = {}
        for items in SCHEMA.values():
            for key, label, kind, default, *_ in items:
                try: d[key] = cast(kind, default, self.get[key]())
                except ValueError: raise ValueError(f"'{label}' needs a number.")
        return d
    def save(self):
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {k: self.get[k]() for k in self.get}; data["_adv"] = self.adv.get(); data["_version"] = VERSION
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8"); os.replace(tmp, SETTINGS_FILE)
    def load(self):
        try: data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError): return
        if "ollama_host" in data and "localhost" in str(data["ollama_host"]):
            data["ollama_host"] = "http://127.0.0.1:11434"  # migrate old IPv6-prone setting
        for k, v in data.items():
            if k in self.set:
                try: self.set[k](v)
                except (ValueError, TypeError, tk.TclError): pass  # ignore one bad saved value
        self.adv.set(bool(data.get("_adv", False)))
    def preset(self, name):
        for k, v in PRESETS[name].items():
            if k in self.set: self.set[k](v)
        self.on_log(f"Preset applied: {name}")
    def reset(self):
        if not messagebox.askyesno("Reset settings", "Restore every setting to its default?"): return
        for items in SCHEMA.values():
            for key, _, _, default, *_ in items: self.set[key](default)
    # actions
    def browse(self):
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mkv *.mov *.webm *.avi"), ("All files", "*.*")])
        if p: self.url.set(p)
    def last_out(self): return Path(self.get["output_dir"]()).expanduser()
    def open_path(self, p):
        Path(p).mkdir(parents=True, exist_ok=True)
        if WIN:
            try: os.startfile(str(p))
            except OSError as e: self.on_log(f"Couldn't open the folder: {e}")
            return
        opener = which("xdg-open")
        if not opener:
            self.on_log("Saved folder: " + str(p) + " (xdg-open is not installed).")
            return
        subprocess.Popen([opener, str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def clear_cache(self):
        cache = self.last_out() / ".cache"
        if cache.exists() and messagebox.askyesno("Clear cache", "Delete downloaded videos and transcripts? Finished clips are kept."): shutil.rmtree(cache, ignore_errors=True); self.on_log("Cache cleared.")
    def start(self):
        src = self.url.get().strip().strip('"\'')
        if self.running: return
        if not src: messagebox.showinfo("Clipper", "Paste a video link or choose a file first."); return
        try: cfg = self.cfg()
        except ValueError as e: messagebox.showerror("Invalid setting", str(e)); return
        if cfg["min_len"] >= cfg["max_len"]: messagebox.showerror("Invalid setting", "The shortest clip must be shorter than the longest."); return
        self.save(); self.cancel_ev.clear(); self.running = True
        self.pipe = Pipeline(cfg, src, lambda *a: self.q.put(a), self.cancel_ev)
        self.go_btn.state(["disabled"]); self.stop_btn.state(["!disabled"]); self.show_page("Progress")
        self.logbox.configure(state="normal"); self.logbox.delete("1.0", "end"); self.logbox.configure(state="disabled")
        self.step_pb["value"] = self.all_pb["value"] = 0; self.eta.set(""); self.paint_chips(0)
        threading.Thread(target=self.work, daemon=True).start()
    def work(self):
        try: out, files = self.pipe.go(); self.q.put(("done", str(out), files))
        except Cancelled: self.q.put(("cancelled",))
        except Exception as e: self.q.put(("error", str(e), traceback.format_exc()))
    def stop(self):
        if not self.running: return  # the File menu can call this when nothing is running
        self.cancel_ev.set(); self.stop_btn.state(["disabled"]); self.status.set("Cancelling...")
        if self.pipe: self.pipe.kill()
    def on_close(self):
        if self.running and not messagebox.askyesno("Clipper", "A job is running. Cancel it and quit?"): return
        self.cancel_ev.set(); self.pull_cancel.set()
        if getattr(self, "yt_cancel", None): self.yt_cancel.set()
        if self.job: self.job["cancel"] = True
        if self.pipe: self.pipe.kill()
        try: self.save()
        except OSError: pass
        self.destroy()

    # events from the worker thread
    def poll(self):
        # Drain a bounded batch: large log/progress bursts must not monopolize Tk's event loop.
        processed = 0
        while processed < 250:
            try: m = self.q.get_nowait()
            except queue.Empty: break
            try: getattr(self, "on_" + m[0])(*m[1:])
            except Exception as e:
                try: self.on_log(f"UI event error ({m[0]}): {e}")
                except Exception: pass
            processed += 1
        self.after(20 if not self.q.empty() else 100, self.poll)
    def paint_chips(self, cur):
        for k, lab in enumerate(self.chips, 1): lab.configure(fg=ACC if k < cur else (FG if k == cur else MUTED))
    def on_log(self, t):
        tag = "err" if t.startswith(("Error", "Couldn't", "GPU failed")) else ("ok" if t.startswith("Saved") else "")
        self.logbox.configure(state="normal"); self.logbox.insert("end", t + "\n", tag)
        try:
            if int(self.logbox.index("end-1c").split(".")[0]) > 4000:
                self.logbox.delete("1.0", "501.0")
        except (ValueError, tk.TclError): pass
        self.logbox.see("end"); self.logbox.configure(state="disabled")
    def on_step(self, i, name):
        self.t_step = time.time(); self.status.set(f"Step {i} of {len(STEPS)}: {name}"); self.step_pb.stop(); self.step_pb.configure(mode="determinate"); self.step_pb["value"] = 0
        self.all_pb["value"] = sum(WEIGHTS[:i - 1]) * 100; self.eta.set(""); self.paint_chips(i)
    def on_prog(self, i, frac, text):
        if frac is None:
            if str(self.step_pb["mode"]) != "indeterminate": self.step_pb.configure(mode="indeterminate"); self.step_pb.start(12)
            self.eta.set(text); return
        if str(self.step_pb["mode"]) == "indeterminate": self.step_pb.stop(); self.step_pb.configure(mode="determinate")
        frac = max(0.0, min(1.0, frac)); el = time.time() - self.t_step
        left = f"   about {fmt_t(el / frac - el)} left" if 0.03 < frac < 1 else ""
        self.step_pb["value"] = frac * 100; self.eta.set(f"{frac * 100:.0f}%   {text}{left}")
        self.all_pb["value"] = (sum(WEIGHTS[:i - 1]) + WEIGHTS[i - 1] * frac) * 100
    def on_review(self, clips, video, holder):
        self.status.set("Waiting for your review..."); self.eta.set("Choose which clips to render")
        self.review_window = Review(self, clips, video, holder)
    def on_regions(self, holder):
        self.status.set("Waiting for you to mark the face cam and content..."); self.eta.set("The content box is optional: unmarked uses the full video frame")
        self.review_window = RegionPicker(self, holder)
    def on_regions_cancel(self): self.on_review_cancel()
    def on_review_cancel(self):
        window = getattr(self, "review_window", None)
        if window and window.winfo_exists():
            window.destroy()
        self.review_window = None
    def finished(self, msg):
        self.running = False; self.go_btn.state(["!disabled"]); self.stop_btn.state(["disabled"]); self.step_pb.stop(); self.step_pb.configure(mode="determinate"); self.status.set(msg)
    def on_done(self, out, files):
        self.step_pb["value"] = self.all_pb["value"] = 100; self.eta.set(""); self.paint_chips(len(STEPS) + 1)
        self.finished(f"Done. {len(files)} clip(s) saved.")
        self.last_files = files  # keep for the Upload button
        for f in files: self.on_log("Saved " + (f["path"] if isinstance(f, dict) else str(f)))
        if files and self.get["open_done"](): self.open_path(out)
        # Auto-upload to YouTube if enabled
        if files and self.get["yt_enabled"]() and self.get["yt_auto_upload"]():
            self.upload_youtube(files)
    def on_cancelled(self): self.finished("Cancelled."); self.eta.set(""); self.on_log("Cancelled.")
    def on_error(self, msg, tb):
        self.finished("Something went wrong."); self.eta.set(""); self.on_log("Error: " + msg); self.on_log(tb)

if __name__ == "__main__":
    App().mainloop()
