# ───────────────────── platform helpers ─────────────────────
import ctypes, glob, importlib, importlib.util, os, shlex, shutil, subprocess, sys, sysconfig, unicodedata
from pathlib import Path

VERSION = "2.1.0"
WIN = os.name == "nt"
NOWIN = subprocess.CREATE_NO_WINDOW if WIN else 0  # keeps black console windows from flashing up on Windows

def local_appdata():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")

FROZEN = bool(getattr(sys, "frozen", False))  # True inside the PyInstaller exe / binary
# Frozen builds install extras (CUDA libraries, yt-dlp updates, YouTube upload packages) here with "pip install --target".
PYLIBS = (local_appdata() / "Clipper" / "pylibs") if WIN else (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Clipper" / "pylibs")
UPDATABLE = ("yt_dlp", "yt_dlp_ejs")  # bundled packages that a newer copy in PYLIBS may replace

if FROZEN and not WIN:
    # PyInstaller points LD_LIBRARY_PATH at its unpacked folder, which holds its own (older) libstdc++, libssl, libz...
    # Programs started from here (ffmpeg, ffprobe, deno, ollama) would load those instead of the system's and fail.
    # This process has already captured what it needs, so restoring the original value only changes its children.
    _orig_ld = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if _orig_ld is not None: os.environ["LD_LIBRARY_PATH"] = _orig_ld
    else: os.environ.pop("LD_LIBRARY_PATH", None)

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

class _PylibsFirst:
    """PyInstaller's importer beats sys.path, so a newer yt-dlp installed into PYLIBS would be ignored.
    This finder runs first and hands the UPDATABLE packages to the normal path finder when a copy exists in PYLIBS."""
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name.split(".")[0] not in UPDATABLE: return None
        import importlib.machinery
        return importlib.machinery.PathFinder.find_spec(name, path or [str(PYLIBS)], target)

def refresh_paths():  # expose freshly installed user-level programs and Python packages
    import site
    if FROZEN and PYLIBS.is_dir():
        if str(PYLIBS) not in sys.path: sys.path.append(str(PYLIBS))
        if not any(f is _PylibsFirst for f in sys.meta_path): sys.meta_path.insert(0, _PylibsFirst)
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

