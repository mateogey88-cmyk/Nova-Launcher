#!/usr/bin/env python3
"""
NovaLauncher - launcher de Minecraft Java Edition en un solo archivo.

Qué hace:
  * Lista todas las versiones oficiales (releases, snapshots, antiguas)
  * Descarga cliente, librerías, assets y nativos desde los servidores de Mojang
  * Detecta Java en tu PC o descarga el runtime oficial de Mojang automáticamente
  * Lanza el juego con la RAM que elijas
  * Modo offline (para singleplayer y servidores que lo permiten)

Requisitos: Python 3.8+ (con tkinter, ya viene en el instalador de python.org)
Uso:        python nova_launcher.py
"""

import glob
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

APP_NAME = "NovaLauncher"
APP_VERSION = "0.2"
UA = f"{APP_NAME}/{APP_VERSION}"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RUNTIME_ALL_URL = (
    "https://launchermeta.mojang.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)
ASSET_BASE = "https://resources.download.minecraft.net/"


# --------------------------------------------------------------------------
# Rutas
# --------------------------------------------------------------------------
def _base_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.path.expanduser("~/.local/share")
    return os.path.join(base, ".novalauncher")


ROOT = _base_dir()
LIBS = os.path.join(ROOT, "libraries")
ASSETS = os.path.join(ROOT, "assets")
VERSIONS = os.path.join(ROOT, "versions")
RUNTIME = os.path.join(ROOT, "runtime")
GAME_DIR = os.path.join(ROOT, "instances", "default")
CONFIG_FILE = os.path.join(ROOT, "config.json")
MANIFEST_CACHE = os.path.join(ROOT, "version_manifest.json")


# --------------------------------------------------------------------------
# Sistema operativo y reglas de Mojang
# --------------------------------------------------------------------------
def os_name():
    return {"win32": "windows", "darwin": "osx"}.get(sys.platform, "linux")


def os_arch():
    m = platform.machine().lower()
    if m in ("x86", "i386", "i686"):
        return "x86"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return "x86_64"


def rule_matches(rule):
    # No activamos ninguna "feature" (demo, resolución custom, quickplay...)
    if "features" in rule:
        return False
    o = rule.get("os")
    if o:
        if "name" in o and o["name"] != os_name():
            return False
        if "arch" in o and o["arch"] != os_arch():
            return False
        if "version" in o:
            try:
                if not re.search(o["version"], platform.version()):
                    return False
            except re.error:
                return False
    return True


def rules_allow(rules):
    if not rules:
        return True
    allowed = False
    for r in rules:
        if rule_matches(r):
            allowed = r.get("action") == "allow"
    return allowed


# --------------------------------------------------------------------------
# Red y descargas
# --------------------------------------------------------------------------
def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, sha1=None, retries=3):
    """Descarga un archivo. Si ya existe (y el hash coincide) no hace nada."""
    if os.path.exists(dest):
        if sha1 is None or sha1_of(dest) == sha1:
            return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            if sha1 and sha1_of(tmp) != sha1:
                raise IOError(f"Hash incorrecto para {url}")
            os.replace(tmp, dest)
            return True
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)


def maven_path(name):
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = f"-{parts[3]}" if len(parts) > 3 else ""
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}{classifier}.jar"


# --------------------------------------------------------------------------
# Java
# --------------------------------------------------------------------------
def java_major(java_path):
    try:
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000
        out = subprocess.run(
            [java_path, "-version"], capture_output=True, text=True, timeout=15, **kw
        )
        text = out.stderr + out.stdout
        m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
        if not m:
            return None
        major = int(m.group(1))
        if major == 1 and m.group(2):
            major = int(m.group(2))
        return major
    except Exception:
        return None


def java_candidates():
    exe = "java.exe" if sys.platform == "win32" else "java"
    cands = []
    found = shutil.which("java")
    if found:
        cands.append(found)
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cands.append(os.path.join(jh, "bin", exe))
    patterns = []
    if sys.platform == "win32":
        pf = [os.environ.get("ProgramFiles", r"C:\Program Files"),
              os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
        for p in pf:
            for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu",
                           "Amazon Corretto", "BellSoft", "Semeru", "AdoptOpenJDK"):
                patterns.append(os.path.join(p, vendor, "*", "bin", exe))
        appdata = os.environ.get("APPDATA", "")
        patterns.append(os.path.join(appdata, ".minecraft", "runtime", "*", "*", "*", "bin", exe))
        local = os.environ.get("LOCALAPPDATA", "")
        patterns.append(os.path.join(local, "Packages", "Microsoft.4297127D64EC6_*", "LocalCache",
                                     "Local", "runtime", "*", "*", "*", "bin", exe))
    elif sys.platform == "darwin":
        patterns += ["/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java",
                     os.path.expanduser("~/Library/Application Support/minecraft/runtime/*/*/*/jre.bundle/Contents/Home/bin/java")]
    else:
        patterns += ["/usr/lib/jvm/*/bin/java", os.path.expanduser("~/.minecraft/runtime/*/*/*/bin/java")]
    for pat in patterns:
        cands.extend(glob.glob(pat))
    seen, result = set(), []
    for c in cands:
        key = os.path.normcase(os.path.realpath(c))
        if key not in seen and os.path.isfile(c):
            seen.add(key)
            result.append(c)
    return result


def runtime_platform_key():
    arch = os_arch()
    if sys.platform == "win32":
        return {"x86": "windows-x86", "arm64": "windows-arm64"}.get(arch, "windows-x64")
    if sys.platform == "darwin":
        return "mac-os-arm64" if arch == "arm64" else "mac-os"
    return "linux-i386" if arch == "x86" else "linux"


def own_runtime_java(component):
    exe = "java.exe" if sys.platform == "win32" else "java"
    base = os.path.join(RUNTIME, component)
    if sys.platform == "darwin":
        p = os.path.join(base, "jre.bundle", "Contents", "Home", "bin", exe)
        if os.path.isfile(p):
            return p
    p = os.path.join(base, "bin", exe)
    return p if os.path.isfile(p) and os.path.exists(os.path.join(base, ".ok")) else None


def install_mojang_runtime(component, log, progress):
    key = runtime_platform_key()
    log(f"Descargando Java ({component}) desde Mojang...")
    allj = fetch_json(RUNTIME_ALL_URL)
    entry = allj[key][component][0]
    manifest = fetch_json(entry["manifest"]["url"])
    base = os.path.join(RUNTIME, component)
    tasks, execs, links = [], [], []
    for rel, info in manifest["files"].items():
        dest = os.path.join(base, *rel.split("/"))
        t = info["type"]
        if t == "directory":
            os.makedirs(dest, exist_ok=True)
        elif t == "file":
            raw = info["downloads"]["raw"]
            tasks.append((raw["url"], dest, raw["sha1"]))
            if info.get("executable"):
                execs.append(dest)
        elif t == "link":
            links.append((dest, info["target"]))
    done, total = 0, len(tasks)
    with ThreadPoolExecutor(12) as ex:
        futs = [ex.submit(download, *t) for t in tasks]
        for f in as_completed(futs):
            f.result()
            done += 1
            progress(done, total, "Java")
    for p in execs:
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if sys.platform != "win32":
        for dest, target in links:
            try:
                if not os.path.lexists(dest):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.symlink(target, dest)
            except OSError:
                pass
    with open(os.path.join(base, ".ok"), "w") as f:
        f.write("ok")


def find_java(required, component, log, progress):
    # 1) runtime propio ya instalado
    p = own_runtime_java(component)
    if p:
        return p
    # 2) Java del sistema con la versión exacta
    cands = [(java_major(c), c) for c in java_candidates()]
    cands = [(m, c) for m, c in cands if m]
    for m, c in cands:
        if m == required:
            return c
    # 3) descargar runtime oficial de Mojang
    try:
        install_mojang_runtime(component, log, progress)
        p = own_runtime_java(component)
        if p:
            return p
    except Exception as e:
        log(f"No se pudo descargar Java automáticamente: {e}")
    # 4) cualquier Java más nuevo
    newer = sorted([(m, c) for m, c in cands if m >= required])
    if newer:
        log(f"Aviso: usando Java {newer[0][0]} (se recomienda Java {required}).")
        return newer[0][1]
    raise RuntimeError(f"No encontré Java {required}. Instálalo desde https://adoptium.net")


# --------------------------------------------------------------------------
# Lanzador
# --------------------------------------------------------------------------
def offline_uuid(name):
    import hashlib as _h
    digest = _h.md5(("OfflinePlayer:" + name).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest, version=3))


def substitute(s, variables):
    return re.sub(r"\$\{(\w+)\}", lambda m: variables.get(m.group(1), m.group(0)), s)


def expand_args(items):
    out = []
    for a in items:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict) and rules_allow(a.get("rules")):
            v = a.get("value", [])
            out.extend(v if isinstance(v, list) else [v])
    return out


def load_manifest():
    try:
        m = fetch_json(MANIFEST_URL)
        os.makedirs(ROOT, exist_ok=True)
        with open(MANIFEST_CACHE, "w", encoding="utf-8") as f:
            json.dump(m, f)
        return m
    except Exception:
        if os.path.exists(MANIFEST_CACHE):
            with open(MANIFEST_CACHE, encoding="utf-8") as f:
                return json.load(f)
        raise


class Launcher:
    def __init__(self, log, progress):
        self.log = log
        self.progress = progress

    def _download_many(self, tasks, label):
        total = len(tasks)
        if not total:
            return
        done = 0
        with ThreadPoolExecutor(16) as ex:
            futs = [ex.submit(download, *t) for t in tasks]
            for f in as_completed(futs):
                f.result()
                done += 1
                self.progress(done, total, label)

    def load_version_json(self, version_id, manifest):
        path = os.path.join(VERSIONS, version_id, f"{version_id}.json")
        entry = next((v for v in manifest["versions"] if v["id"] == version_id), None) if manifest else None
        if entry:
            try:
                download(entry["url"], path, entry.get("sha1"))
            except Exception as e:
                if not os.path.exists(path):
                    raise
                self.log(f"Sin conexión con Mojang, usando copia local ({e})")
        if not os.path.exists(path):
            raise RuntimeError(f"No se encontró la versión {version_id}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def prepare_libraries(self, vjson, natives_dir):
        classpath, tasks, native_jobs = [], [], []
        for lib in vjson.get("libraries", []):
            if not rules_allow(lib.get("rules")):
                continue
            d = lib.get("downloads")
            if d is None:
                # formato antiguo/maven simple
                rel = maven_path(lib["name"])
                base = lib.get("url", "https://libraries.minecraft.net/")
                dest = os.path.join(LIBS, *rel.split("/"))
                tasks.append((base + rel, dest, None))
                classpath.append(dest)
                continue
            art = d.get("artifact")
            if art and art.get("url"):
                dest = os.path.join(LIBS, *art["path"].split("/"))
                tasks.append((art["url"], dest, art.get("sha1")))
                classpath.append(dest)
            nat = lib.get("natives")
            if nat:
                classifier = nat.get(os_name())
                if classifier:
                    classifier = classifier.replace("${arch}", "32" if os_arch() == "x86" else "64")
                    cinfo = d.get("classifiers", {}).get(classifier)
                    if cinfo:
                        dest = os.path.join(LIBS, *cinfo["path"].split("/"))
                        tasks.append((cinfo["url"], dest, cinfo.get("sha1")))
                        native_jobs.append((dest, lib.get("extract", {}).get("exclude", [])))
        self._download_many(tasks, "Librerías")
        # extraer nativos (versiones antiguas)
        shutil.rmtree(natives_dir, ignore_errors=True)
        os.makedirs(natives_dir, exist_ok=True)
        for jar, exclude in native_jobs:
            with zipfile.ZipFile(jar) as z:
                for name in z.namelist():
                    if name.endswith("/") or any(name.startswith(e) for e in exclude):
                        continue
                    z.extract(name, natives_dir)
        # sin duplicados, conservando el orden
        seen, uniq = set(), []
        for c in classpath:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    def prepare_assets(self, vjson):
        ai = vjson["assetIndex"]
        idx_path = os.path.join(ASSETS, "indexes", ai["id"] + ".json")
        download(ai["url"], idx_path, ai.get("sha1"))
        with open(idx_path, encoding="utf-8") as f:
            idx = json.load(f)
        objs = idx.get("objects", {})
        tasks, seen = [], set()
        for info in objs.values():
            h = info["hash"]
            if h in seen:
                continue
            seen.add(h)
            dest = os.path.join(ASSETS, "objects", h[:2], h)
            tasks.append((ASSET_BASE + h[:2] + "/" + h, dest, None))
        self._download_many(tasks, "Assets")
        assets_root, game_assets = ASSETS, ASSETS
        if idx.get("virtual") or idx.get("map_to_resources"):
            target_root = (os.path.join(GAME_DIR, "resources") if idx.get("map_to_resources")
                           else os.path.join(ASSETS, "virtual", ai["id"]))
            for name, info in objs.items():
                src = os.path.join(ASSETS, "objects", info["hash"][:2], info["hash"])
                dst = os.path.join(target_root, *name.split("/"))
                if not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
            game_assets = target_root
        return assets_root, game_assets, ai["id"]

    def launch(self, version_id, username, ram_mb, manifest):
        os.makedirs(GAME_DIR, exist_ok=True)
        self.log(f"Preparando {version_id}...")
        vjson = self.load_version_json(version_id, manifest)
        if "inheritsFrom" in vjson:
            raise RuntimeError("Versiones con 'inheritsFrom' (Fabric/Forge) aún no están soportadas.")

        # cliente
        client = vjson["downloads"]["client"]
        client_jar = os.path.join(VERSIONS, version_id, f"{version_id}.jar")
        self.log("Descargando cliente...")
        download(client["url"], client_jar, client.get("sha1"))

        # librerías
        natives_dir = os.path.join(VERSIONS, version_id, "natives")
        self.log("Descargando librerías...")
        classpath = self.prepare_libraries(vjson, natives_dir)
        classpath.append(client_jar)

        # assets
        self.log("Descargando assets (sonidos, idiomas...)...")
        assets_root, game_assets, assets_id = self.prepare_assets(vjson)

        # log4j
        log_arg = None
        lc = vjson.get("logging", {}).get("client")
        if lc and lc.get("file"):
            lpath = os.path.join(ASSETS, "log_configs", lc["file"]["id"])
            download(lc["file"]["url"], lpath, lc["file"].get("sha1"))
            log_arg = lc["argument"].replace("${path}", lpath)

        # java
        jv = vjson.get("javaVersion", {})
        required = jv.get("majorVersion", 8)
        component = jv.get("component", "jre-legacy" if required <= 8 else "java-runtime-gamma")
        self.log(f"Buscando Java {required}...")
        java = find_java(required, component, self.log, self.progress)
        self.log(f"Java: {java}")

        # argumentos
        variables = {
            "auth_player_name": username,
            "version_name": version_id,
            "game_directory": GAME_DIR,
            "assets_root": assets_root,
            "assets_index_name": assets_id,
            "game_assets": game_assets,
            "auth_uuid": offline_uuid(username),
            "auth_access_token": "0",
            "auth_session": "0",
            "auth_xuid": "0",
            "clientid": "0",
            "user_type": "legacy",
            "user_properties": "{}",
            "version_type": vjson.get("type", "release"),
            "natives_directory": natives_dir,
            "launcher_name": APP_NAME,
            "launcher_version": APP_VERSION,
            "classpath": os.pathsep.join(classpath),
            "library_directory": LIBS,
            "classpath_separator": os.pathsep,
        }
        if "arguments" in vjson:
            jvm = expand_args(vjson["arguments"].get("jvm", []))
            game = expand_args(vjson["arguments"].get("game", []))
        else:
            jvm = ["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"]
            game = vjson.get("minecraftArguments", "").split()
        if "-cp" not in jvm and "-classpath" not in jvm:
            jvm += ["-cp", "${classpath}"]

        cmd = [java, f"-Xmx{ram_mb}M", "-Xms512M"]
        if log_arg:
            jvm.append(log_arg)
        cmd += [substitute(a, variables) for a in jvm]
        cmd.append(vjson["mainClass"])
        cmd += [substitute(a, variables) for a in game]

        self.log("Iniciando Minecraft...")
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000  # sin ventana de consola
        proc = subprocess.Popen(
            cmd, cwd=GAME_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", **kw
        )
        return proc


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def total_ram_mb():
    try:
        if sys.platform == "win32":
            import ctypes

            class MEM(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEM()
            m.dwLength = ctypes.sizeof(MEM)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullTotalPhys // (1024 * 1024))
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except Exception:
        return 8192


DEFAULT_CONFIG = {"username": "Player", "version": "", "ram_mb": 4096,
                  "snapshots": False, "old": False, "minimize": False}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(ROOT, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# --------------------------------------------------------------------------
# Interfaz
# --------------------------------------------------------------------------
def run_gui():
    import random
    import tkinter as tk
    from tkinter import ttk, messagebox

    BG, SIDE, CARD, CARD2, LINE = "#0d0f14", "#11141b", "#171b25", "#202636", "#262c3b"
    FG, MUTED, ACCENT = "#e9ecf4", "#8a90a2", "#5b8cff"
    GREEN, GREEN_H, DIS = "#2fbf71", "#3bd983", "#262b38"
    FONT = "Segoe UI"

    def blend(c1, c2, t):
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    class RoundButton(tk.Canvas):
        """Botón con esquinas redondeadas."""

        def __init__(self, parent, text, command, color, hover, parent_bg,
                     font=(FONT, 15, "bold"), height=54, radius=14, fg="white"):
            super().__init__(parent, height=height, bg=parent_bg, highlightthickness=0,
                             bd=0, cursor="hand2")
            self.text, self.command, self.color, self.hover = text, command, color, hover
            self.fg, self.font, self.r = fg, font, radius
            self.enabled, self.over = True, False
            self.bind("<Configure>", lambda e: self.draw())
            self.bind("<Enter>", lambda e: self._set_over(True))
            self.bind("<Leave>", lambda e: self._set_over(False))
            self.bind("<ButtonRelease-1>", self._click)

        def _set_over(self, v):
            self.over = v
            self.draw()

        def _click(self, _):
            if self.enabled and self.command:
                self.command()

        def set_state(self, text, enabled):
            self.text, self.enabled = text, enabled
            self.configure(cursor="hand2" if enabled else "arrow")
            self.draw()

        def draw(self):
            self.delete("all")
            w, h, r = self.winfo_width(), self.winfo_height(), self.r
            if w < 4:
                return
            fill = DIS if not self.enabled else (self.hover if self.over else self.color)
            x1, y1, x2, y2 = 1, 1, w - 1, h - 1
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            self.create_polygon(pts, smooth=True, fill=fill, outline=fill)
            self.create_text(w / 2, h / 2, text=self.text, font=self.font,
                             fill=self.fg if self.enabled else MUTED)

    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    root.geometry("960x640")
    root.minsize(880, 580)
    root.configure(bg=BG)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TCombobox", fieldbackground=CARD2, background=CARD2, foreground=FG,
                    arrowcolor=FG, bordercolor=LINE, lightcolor=CARD2, darkcolor=CARD2,
                    selectbackground=CARD2, selectforeground=FG)
    style.map("TCombobox", fieldbackground=[("readonly", CARD2)],
              foreground=[("readonly", FG)], selectbackground=[("readonly", CARD2)])
    style.configure("Nova.Horizontal.TProgressbar", troughcolor=CARD, background=ACCENT,
                    bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)
    style.configure("Nova.Horizontal.TScale", background=CARD, troughcolor=CARD2)
    root.option_add("*TCombobox*Listbox.background", CARD2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.font", (FONT, 11))

    cfg = load_config()
    msgs = queue.Queue()
    state = {"manifest": None, "busy": False, "page": "play"}

    user_var = tk.StringVar(value=cfg["username"])
    ver_var = tk.StringVar(value=cfg["version"])
    snap_var = tk.BooleanVar(value=cfg["snapshots"])
    old_var = tk.BooleanVar(value=cfg["old"])
    min_var = tk.BooleanVar(value=cfg.get("minimize", False))
    max_ram = max(2048, min(32768, int(total_ram_mb() * 0.75)))
    ram_var = tk.IntVar(value=min(cfg["ram_mb"], max_ram))
    ram_text = tk.StringVar()
    mode_var = tk.StringVar(value="Offline")
    status_var = tk.StringVar(value="Cargando versiones...")

    # ------------------------------------------------------------------ barra lateral
    sidebar = tk.Frame(root, bg=SIDE, width=210)
    sidebar.pack(side="left", fill="y")
    sidebar.pack_propagate(False)

    logo = tk.Frame(sidebar, bg=SIDE)
    logo.pack(fill="x", padx=20, pady=(26, 22))
    tk.Label(logo, text="NOVA", font=(FONT, 22, "bold"), fg=ACCENT, bg=SIDE).pack(anchor="w")
    tk.Label(logo, text="LAUNCHER", font=(FONT, 9, "bold"), fg=MUTED, bg=SIDE).pack(anchor="w")

    nav = {}
    pages = {}

    def style_nav(name, hover=False):
        row, bar, lab = nav[name]
        active = state["page"] == name
        bg = CARD if (active or hover) else SIDE
        row.configure(bg=bg)
        bar.configure(bg=ACCENT if active else bg)
        lab.configure(bg=bg, fg=FG if (active or hover) else MUTED)

    def make_nav(text, name):
        row = tk.Frame(sidebar, bg=SIDE, cursor="hand2")
        row.pack(fill="x", pady=1)
        bar = tk.Frame(row, bg=SIDE, width=4)
        bar.pack(side="left", fill="y")
        lab = tk.Label(row, text=text, font=(FONT, 11), fg=MUTED, bg=SIDE, anchor="w",
                       padx=16, pady=12)
        lab.pack(side="left", fill="x", expand=True)
        nav[name] = (row, bar, lab)
        for w in (row, lab, bar):
            w.bind("<Button-1>", lambda e, n=name: show_page(n))
            w.bind("<Enter>", lambda e, n=name: style_nav(n, True))
            w.bind("<Leave>", lambda e, n=name: style_nav(n, False))

    make_nav("▶   Jugar", "play")
    make_nav("⚙   Ajustes", "settings")
    make_nav("≡   Consola", "console")

    side_bottom = tk.Frame(sidebar, bg=SIDE)
    side_bottom.pack(side="bottom", fill="x", padx=16, pady=16)

    def open_folder(path):
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    tk.Button(side_bottom, text="Abrir carpeta del juego", font=(FONT, 9), bg=CARD, fg=FG,
              activebackground=CARD2, activeforeground=FG, relief="flat", bd=0, cursor="hand2",
              command=lambda: open_folder(GAME_DIR)).pack(fill="x", ipady=7)
    tk.Label(side_bottom, text=f"v{APP_VERSION}", font=(FONT, 8), fg=MUTED, bg=SIDE).pack(pady=(10, 0))

    content = tk.Frame(root, bg=BG)
    content.pack(side="left", fill="both", expand=True)

    def card(parent, **kw):
        return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE, **kw)

    def small_title(parent, text, bg=CARD):
        return tk.Label(parent, text=text, font=(FONT, 8, "bold"), fg=MUTED, bg=bg)

    # ------------------------------------------------------------------ página JUGAR
    play = tk.Frame(content, bg=BG)
    pages["play"] = play

    hero = tk.Canvas(play, height=210, bg=BG, highlightthickness=0)
    hero.pack(fill="x")

    def draw_hero(_=None):
        w, h = hero.winfo_width(), hero.winfo_height()
        if w < 10 or h < 10:
            return
        hero.delete("all")
        top, bottom = "#274291", BG
        for y in range(0, h, 2):
            hero.create_line(0, y, w, y, fill=blend(top, bottom, y / h), width=2)
        rng = random.Random(7)
        size = 28
        cols, rows = w // size + 1, h // size + 1
        for cx in range(cols):
            for cy in range(rows):
                if rng.random() < (cx / cols) * 0.6:
                    base = blend(top, bottom, min(1.0, (cy * size) / h))
                    shade = blend(base, "#8fb0ff", 0.08 + 0.16 * rng.random())
                    hero.create_rectangle(cx * size, cy * size, cx * size + size - 3,
                                          cy * size + size - 3, fill=shade, outline="")
        name = user_var.get().strip() or "Jugador"
        ver = ver_var.get()
        sub = f"Minecraft {ver}   ·   Modo offline" if ver else "Elegí una versión para empezar"
        hero.create_text(40, h / 2 - 20, anchor="w", text=f"Hola, {name}", fill="white",
                         font=(FONT, 30, "bold"))
        hero.create_text(42, h / 2 + 24, anchor="w", text=sub, fill="#c5d3ff", font=(FONT, 12))

    hero.bind("<Configure>", draw_hero)
    user_var.trace_add("write", lambda *a: draw_hero())
    ver_var.trace_add("write", lambda *a: draw_hero())

    body = tk.Frame(play, bg=BG)
    body.pack(fill="both", expand=True, padx=32, pady=(20, 10))

    vcard = card(body)
    vcard.pack(fill="x")
    small_title(vcard, "VERSIÓN DE MINECRAFT").pack(anchor="w", padx=18, pady=(14, 6))
    ver_box = ttk.Combobox(vcard, textvariable=ver_var, state="readonly", font=(FONT, 12))
    ver_box.pack(fill="x", padx=18, pady=(0, 16), ipady=4)

    stats = tk.Frame(body, bg=BG)
    stats.pack(fill="x", pady=14)
    for i in range(3):
        stats.columnconfigure(i, weight=1, uniform="s")

    def stat_card(col, title, var):
        f = card(stats)
        f.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0 if col == 2 else 8))
        small_title(f, title).pack(anchor="w", padx=16, pady=(12, 0))
        tk.Label(f, textvariable=var, font=(FONT, 14, "bold"), fg=FG, bg=CARD).pack(
            anchor="w", padx=16, pady=(2, 12))

    stat_card(0, "MEMORIA RAM", ram_text)
    stat_card(1, "USUARIO", user_var)
    stat_card(2, "MODO", mode_var)

    play_btn = RoundButton(body, "JUGAR", lambda: on_play(), GREEN, GREEN_H, BG)
    play_btn.pack(fill="x", pady=(4, 12))

    tk.Label(body, textvariable=status_var, font=(FONT, 9), fg=MUTED, bg=BG, anchor="w").pack(fill="x")
    bar = ttk.Progressbar(body, mode="determinate", style="Nova.Horizontal.TProgressbar")
    bar.pack(fill="x", pady=(6, 0))

    # ------------------------------------------------------------------ página AJUSTES
    sett = tk.Frame(content, bg=BG)
    pages["settings"] = sett
    tk.Label(sett, text="Ajustes", font=(FONT, 22, "bold"), fg=FG, bg=BG).pack(
        anchor="w", padx=32, pady=(28, 14))

    c1 = card(sett)
    c1.pack(fill="x", padx=32, pady=(0, 14))
    small_title(c1, "NOMBRE DE USUARIO").pack(anchor="w", padx=18, pady=(14, 6))
    tk.Entry(c1, textvariable=user_var, font=(FONT, 12), bg=CARD2, fg=FG, insertbackground=FG,
             relief="flat", highlightthickness=1, highlightbackground=LINE,
             highlightcolor=ACCENT).pack(fill="x", padx=18, pady=(0, 16), ipady=7)

    c2 = card(sett)
    c2.pack(fill="x", padx=32, pady=(0, 14))
    small_title(c2, "MEMORIA RAM ASIGNADA").pack(anchor="w", padx=18, pady=(14, 6))
    ram_row = tk.Frame(c2, bg=CARD)
    ram_row.pack(fill="x", padx=18, pady=(0, 16))

    def on_ram(_=None):
        v = int(round(float(ram_var.get()) / 256) * 256)
        ram_var.set(v)
        ram_text.set(f"{v / 1024:.1f} GB")

    ttk.Scale(ram_row, from_=1024, to=max_ram, variable=ram_var, command=on_ram,
              style="Nova.Horizontal.TScale").pack(side="left", fill="x", expand=True)
    tk.Label(ram_row, textvariable=ram_text, width=8, font=(FONT, 11, "bold"), fg=FG,
             bg=CARD).pack(side="left", padx=(14, 0))
    on_ram()

    c3 = card(sett)
    c3.pack(fill="x", padx=32, pady=(0, 14))
    small_title(c3, "OPCIONES").pack(anchor="w", padx=18, pady=(14, 4))

    def check(parent, text, var, cmd=None):
        tk.Checkbutton(parent, text=text, variable=var, command=cmd, fg=FG, bg=CARD,
                       selectcolor=CARD2, activebackground=CARD, activeforeground=FG,
                       font=(FONT, 10), bd=0, highlightthickness=0, anchor="w").pack(
            fill="x", padx=14, pady=3)

    check(c3, "Mostrar snapshots en la lista de versiones", snap_var, lambda: refresh_versions())
    check(c3, "Mostrar versiones antiguas (beta / alpha)", old_var, lambda: refresh_versions())
    check(c3, "Minimizar el launcher mientras juego", min_var)
    tk.Frame(c3, bg=CARD, height=10).pack()

    tk.Label(sett, text=f"Los datos del juego se guardan en:  {ROOT}", font=(FONT, 9), fg=MUTED,
             bg=BG, anchor="w").pack(fill="x", padx=34, pady=(4, 0))

    # ------------------------------------------------------------------ página CONSOLA
    cons = tk.Frame(content, bg=BG)
    pages["console"] = cons
    head = tk.Frame(cons, bg=BG)
    head.pack(fill="x", padx=32, pady=(28, 12))
    tk.Label(head, text="Consola", font=(FONT, 22, "bold"), fg=FG, bg=BG).pack(side="left")

    log_text = tk.Text(cons, bg=CARD, fg="#b7bccb", font=("Consolas", 9), relief="flat",
                       wrap="word", state="disabled", bd=0, padx=12, pady=10,
                       highlightthickness=1, highlightbackground=LINE)

    def copy_log():
        root.clipboard_clear()
        root.clipboard_append(log_text.get("1.0", "end"))
        status_var.set("Consola copiada al portapapeles")

    def clear_log():
        log_text.config(state="normal")
        log_text.delete("1.0", "end")
        log_text.config(state="disabled")

    for label, fn in (("Limpiar", clear_log), ("Copiar todo", copy_log)):
        tk.Button(head, text=label, font=(FONT, 9), bg=CARD, fg=FG, activebackground=CARD2,
                  activeforeground=FG, relief="flat", bd=0, cursor="hand2",
                  command=fn).pack(side="right", padx=(8, 0), ipadx=12, ipady=5)

    sb = tk.Scrollbar(cons, command=log_text.yview)
    log_text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y", padx=(0, 32), pady=(0, 28))
    log_text.pack(side="left", fill="both", expand=True, padx=(32, 0), pady=(0, 28))

    def append_log(text):
        log_text.config(state="normal")
        log_text.insert("end", text.rstrip("\n") + "\n")
        log_text.see("end")
        log_text.config(state="disabled")

    # ------------------------------------------------------------------ navegación
    def show_page(name):
        for f in pages.values():
            f.pack_forget()
        state["page"] = name
        pages[name].pack(fill="both", expand=True)
        for n in nav:
            style_nav(n)

    # ------------------------------------------------------------------ versiones
    def refresh_versions():
        m = state["manifest"]
        if not m:
            return
        shown = []
        for v in m["versions"]:
            t = v["type"]
            if t == "release" or (t == "snapshot" and snap_var.get()) or \
               (t in ("old_beta", "old_alpha") and old_var.get()):
                shown.append(v["id"])
        ver_box["values"] = shown
        if ver_var.get() not in shown:
            latest = m.get("latest", {}).get("release")
            ver_var.set(latest if latest in shown else (shown[0] if shown else ""))

    def load_versions():
        try:
            state["manifest"] = load_manifest()
            msgs.put(("manifest",))
        except Exception as e:
            msgs.put(("log", f"No se pudo cargar la lista de versiones: {e}"))
            msgs.put(("status", "Sin conexión con Mojang"))

    # ------------------------------------------------------------------ jugar
    def save_all():
        cfg.update(username=user_var.get().strip() or "Player", version=ver_var.get(),
                   ram_mb=int(ram_var.get()), snapshots=snap_var.get(), old=old_var.get(),
                   minimize=min_var.get())
        save_config(cfg)

    def worker(version, username, ram):
        launcher = Launcher(
            log=lambda t: msgs.put(("log", t)),
            progress=lambda d, t, l: msgs.put(("progress", d, t, l)),
        )
        try:
            proc = launcher.launch(version, username, ram, state["manifest"])
            msgs.put(("started", version))
            for line in proc.stdout:
                msgs.put(("log", line))
            code = proc.wait()
            msgs.put(("log", f"El juego se cerró (código {code})."))
            msgs.put(("status", "Listo" if code == 0 else f"El juego terminó con código {code}"))
        except Exception as e:
            msgs.put(("log", f"ERROR: {e}"))
            msgs.put(("error", str(e)))
        finally:
            msgs.put(("idle",))

    def on_play():
        if state["busy"]:
            return
        name = user_var.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
            messagebox.showwarning(APP_NAME, "El usuario debe tener entre 3 y 16 caracteres "
                                             "(letras, números o guion bajo).")
            show_page("settings")
            return
        version = ver_var.get()
        if not version:
            messagebox.showwarning(APP_NAME, "Elegí una versión.")
            return
        save_all()
        state["busy"] = True
        play_btn.set_state("PREPARANDO...", False)
        status_var.set("Preparando...")
        threading.Thread(target=worker, args=(version, name, int(ram_var.get())),
                         daemon=True).start()

    def pump():
        try:
            while True:
                m = msgs.get_nowait()
                kind = m[0]
                if kind == "log":
                    append_log(m[1])
                elif kind == "status":
                    status_var.set(m[1])
                elif kind == "progress":
                    _, d, t, label = m
                    bar["maximum"] = max(t, 1)
                    bar["value"] = d
                    if label:
                        status_var.set(f"Descargando {label.lower()}:  {d} / {t}")
                elif kind == "manifest":
                    refresh_versions()
                    draw_hero()
                    status_var.set("Listo para jugar")
                elif kind == "started":
                    bar["value"] = 0
                    play_btn.set_state("JUGANDO", False)
                    status_var.set(f"Minecraft {m[1]} en ejecución")
                    if min_var.get():
                        root.iconify()
                elif kind == "error":
                    status_var.set("Error: revisá la consola")
                    show_page("console")
                elif kind == "idle":
                    state["busy"] = False
                    bar["value"] = 0
                    play_btn.set_state("JUGAR", True)
                    if min_var.get():
                        root.deiconify()
        except queue.Empty:
            pass
        root.after(100, pump)

    def on_close():
        save_all()
        if state["busy"] and not messagebox.askokcancel(
                APP_NAME, "Hay una descarga o el juego está en marcha. ¿Cerrar igual?"):
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    show_page("play")
    pump()
    threading.Thread(target=load_versions, daemon=True).start()
    append_log(f"Datos guardados en: {ROOT}")
    root.mainloop()


if __name__ == "__main__":
    run_gui()
