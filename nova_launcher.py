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
APP_VERSION = "0.1"
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
                  "snapshots": False, "old": False}


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
    import tkinter as tk
    from tkinter import ttk, messagebox

    BG, CARD, FG, MUTED = "#0f1117", "#181b24", "#e8eaf0", "#8b90a0"
    ACCENT, GREEN, GREEN_H = "#5b8cff", "#2fbf71", "#3bd983"

    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    root.geometry("760x620")
    root.minsize(680, 560)
    root.configure(bg=BG)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=FG,
                    arrowcolor=FG, bordercolor=CARD, lightcolor=CARD, darkcolor=CARD)
    style.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", FG)])
    style.configure("Horizontal.TProgressbar", troughcolor=CARD, background=ACCENT,
                    bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("TScale", background=BG, troughcolor=CARD)
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    cfg = load_config()
    msgs = queue.Queue()
    state = {"manifest": None, "busy": False}

    # --- cabecera
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=28, pady=(24, 6))
    tk.Label(header, text="NOVA", font=("Segoe UI", 26, "bold"), fg=ACCENT, bg=BG).pack(side="left")
    tk.Label(header, text="LAUNCHER", font=("Segoe UI", 26), fg=FG, bg=BG).pack(side="left", padx=(6, 0))
    tk.Label(header, text="Minecraft Java Edition", font=("Segoe UI", 10), fg=MUTED, bg=BG).pack(
        side="left", padx=14, pady=(14, 0))

    # --- tarjeta de ajustes
    card = tk.Frame(root, bg=CARD)
    card.pack(fill="x", padx=28, pady=10)
    card.columnconfigure(1, weight=1)

    def lbl(text, row):
        tk.Label(card, text=text, font=("Segoe UI", 10), fg=MUTED, bg=CARD).grid(
            row=row, column=0, sticky="w", padx=(18, 14), pady=10)

    lbl("Usuario", 0)
    user_var = tk.StringVar(value=cfg["username"])
    tk.Entry(card, textvariable=user_var, font=("Segoe UI", 11), bg=BG, fg=FG, insertbackground=FG,
             relief="flat", highlightthickness=1, highlightbackground="#2a2f3d",
             highlightcolor=ACCENT).grid(row=0, column=1, sticky="ew", padx=(0, 18), pady=10, ipady=5)

    lbl("Versión", 1)
    ver_var = tk.StringVar(value=cfg["version"])
    ver_box = ttk.Combobox(card, textvariable=ver_var, state="readonly", font=("Segoe UI", 11))
    ver_box.grid(row=1, column=1, sticky="ew", padx=(0, 18), pady=10, ipady=3)

    flt = tk.Frame(card, bg=CARD)
    flt.grid(row=2, column=1, sticky="w", pady=(0, 6))
    snap_var = tk.BooleanVar(value=cfg["snapshots"])
    old_var = tk.BooleanVar(value=cfg["old"])
    for text, var in (("Snapshots", snap_var), ("Versiones antiguas", old_var)):
        tk.Checkbutton(flt, text=text, variable=var, command=lambda: refresh_versions(),
                       fg=FG, bg=CARD, selectcolor=BG, activebackground=CARD, activeforeground=FG,
                       font=("Segoe UI", 9), bd=0, highlightthickness=0).pack(side="left", padx=(0, 14))

    lbl("Memoria RAM", 3)
    max_ram = max(2048, min(32768, int(total_ram_mb() * 0.75)))
    ram_var = tk.IntVar(value=min(cfg["ram_mb"], max_ram))
    ram_text = tk.StringVar()
    ram_row = tk.Frame(card, bg=CARD)
    ram_row.grid(row=3, column=1, sticky="ew", padx=(0, 18), pady=(6, 16))

    def on_ram(_=None):
        v = int(round(float(ram_var.get()) / 256) * 256)
        ram_var.set(v)
        ram_text.set(f"{v / 1024:.2f} GB".replace(".00", ".0"))

    ttk.Scale(ram_row, from_=1024, to=max_ram, variable=ram_var, command=on_ram).pack(
        side="left", fill="x", expand=True)
    tk.Label(ram_row, textvariable=ram_text, width=8, font=("Segoe UI", 10, "bold"),
             fg=FG, bg=CARD).pack(side="left", padx=(12, 0))
    on_ram()

    # --- botón jugar
    play_btn = tk.Button(root, text="JUGAR", font=("Segoe UI", 16, "bold"), bg=GREEN, fg="white",
                         activebackground=GREEN_H, activeforeground="white", relief="flat",
                         cursor="hand2", bd=0)
    play_btn.pack(fill="x", padx=28, pady=(8, 4), ipady=10)
    play_btn.bind("<Enter>", lambda e: play_btn.config(bg=GREEN_H) if not state["busy"] else None)
    play_btn.bind("<Leave>", lambda e: play_btn.config(bg=GREEN) if not state["busy"] else None)

    status_var = tk.StringVar(value="Cargando versiones...")
    tk.Label(root, textvariable=status_var, font=("Segoe UI", 9), fg=MUTED, bg=BG,
             anchor="w").pack(fill="x", padx=30)
    bar = ttk.Progressbar(root, mode="determinate", style="Horizontal.TProgressbar")
    bar.pack(fill="x", padx=28, pady=(4, 8))

    # --- consola
    log_frame = tk.Frame(root, bg=CARD)
    log_frame.pack(fill="both", expand=True, padx=28, pady=(0, 10))
    log_text = tk.Text(log_frame, bg=CARD, fg="#b7bccb", font=("Consolas", 9), relief="flat",
                       wrap="word", state="disabled", height=8, bd=0, padx=10, pady=8)
    sb = tk.Scrollbar(log_frame, command=log_text.yview)
    log_text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    log_text.pack(side="left", fill="both", expand=True)

    footer = tk.Frame(root, bg=BG)
    footer.pack(fill="x", padx=28, pady=(0, 14))
    tk.Button(footer, text="Abrir carpeta del juego", font=("Segoe UI", 9), bg=CARD, fg=FG,
              activebackground="#222634", activeforeground=FG, relief="flat", bd=0, cursor="hand2",
              command=lambda: open_folder(GAME_DIR)).pack(side="left", ipadx=10, ipady=4)

    def open_folder(path):
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def append_log(text):
        log_text.config(state="normal")
        log_text.insert("end", text.rstrip("\n") + "\n")
        log_text.see("end")
        log_text.config(state="disabled")

    # --- versiones
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
            msgs.put(("status", "Sin conexión"))

    # --- jugar
    def set_busy(b):
        state["busy"] = b
        play_btn.config(state="disabled" if b else "normal", bg="#2a2f3d" if b else GREEN,
                        text="TRABAJANDO..." if b else "JUGAR")

    def worker(version, username, ram):
        launcher = Launcher(
            log=lambda t: msgs.put(("log", t)),
            progress=lambda d, t, l: msgs.put(("progress", d, t, l)),
        )
        try:
            proc = launcher.launch(version, username, ram, state["manifest"])
            msgs.put(("status", f"Minecraft {version} en ejecución"))
            msgs.put(("progress", 0, 1, ""))
            for line in proc.stdout:
                msgs.put(("log", line))
            code = proc.wait()
            msgs.put(("log", f"El juego se cerró (código {code})."))
            msgs.put(("status", "Listo"))
        except Exception as e:
            msgs.put(("log", f"ERROR: {e}"))
            msgs.put(("status", "Error — revisa la consola"))
        finally:
            msgs.put(("idle",))

    def on_play():
        if state["busy"]:
            return
        name = user_var.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
            messagebox.showwarning(APP_NAME, "El usuario debe tener 3 a 16 caracteres "
                                             "(letras, números o guion bajo).")
            return
        version = ver_var.get()
        if not version:
            messagebox.showwarning(APP_NAME, "Elige una versión.")
            return
        cfg.update(username=name, version=version, ram_mb=int(ram_var.get()),
                   snapshots=snap_var.get(), old=old_var.get())
        save_config(cfg)
        set_busy(True)
        status_var.set("Preparando...")
        threading.Thread(target=worker, args=(version, name, int(ram_var.get())), daemon=True).start()

    play_btn.config(command=on_play)

    def pump():
        try:
            while True:
                m = msgs.get_nowait()
                if m[0] == "log":
                    append_log(m[1])
                elif m[0] == "status":
                    status_var.set(m[1])
                elif m[0] == "progress":
                    _, d, t, label = m
                    bar["maximum"] = max(t, 1)
                    bar["value"] = d
                    if label:
                        status_var.set(f"{label}: {d}/{t}")
                elif m[0] == "manifest":
                    refresh_versions()
                    status_var.set("Listo")
                elif m[0] == "idle":
                    set_busy(False)
        except queue.Empty:
            pass
        root.after(100, pump)

    pump()
    threading.Thread(target=load_versions, daemon=True).start()
    append_log(f"Datos guardados en: {ROOT}")
    root.mainloop()


if __name__ == "__main__":
    run_gui()
