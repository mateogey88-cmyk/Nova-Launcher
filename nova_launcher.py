#!/usr/bin/env python3
"""
NovaLauncher 0.6 - launcher de Minecraft Java Edition en un solo archivo.

Novedades de la 0.6:
  * Interfaz estilo Battly: barra dorada, fondo nocturno, tarjetas de novedades y estado,
    "Jugador activo" y barra flotante inferior (Inicio, Mods, Consola, Ajustes, Instancias,
    Descargar, Jugar)

Requisitos: Python 3.8+ (con tkinter)
Uso:        python nova_launcher.py
"""

import glob
import hashlib
import json
import os
import platform
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

APP_NAME = "NovaLauncher"
APP_VERSION = "0.6"
UA = f"NovaLauncher/{APP_VERSION} (launcher de Minecraft para amigos)"

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RUNTIME_ALL_URL = (
    "https://launchermeta.mojang.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)
ASSET_BASE = "https://resources.download.minecraft.net/"

FABRIC_META = "https://meta.fabricmc.net/v2/versions/loader"
QUILT_META = "https://meta.quiltmc.org/v3/versions/loader"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_API = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"

LOADER_NAMES = {"vanilla": "Vanilla", "fabric": "Fabric", "quilt": "Quilt",
                "forge": "Forge", "neoforge": "NeoForge"}

JVM_PRESETS = {
    "Por defecto": [],
    "Rendimiento (G1GC)": ["-XX:+UseG1GC", "-XX:+UnlockExperimentalVMOptions",
                           "-XX:G1NewSizePercent=20", "-XX:G1ReservePercent=20",
                           "-XX:MaxGCPauseMillis=50", "-XX:G1HeapRegionSize=32M"],
    "Bajo consumo (SerialGC)": ["-XX:+UseSerialGC", "-Xss1M"],
}


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
INSTALLERS = os.path.join(ROOT, "installers")
CONFIG_FILE = os.path.join(ROOT, "config.json")
MANIFEST_CACHE = os.path.join(ROOT, "version_manifest.json")


def game_dir(inst):
    return os.path.join(ROOT, "instances", inst["id"])


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
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"El servidor respondió {e.code} ({url.split('?')[0]})") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"No hay conexión ({e.reason})") from None


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


def stream_process(cmd, cwd, log):
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", **kw)
    for line in p.stdout:
        log(line)
    return p.wait()


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
        if os.path.isfile(p) and os.path.exists(os.path.join(base, ".ok")):
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
    raise RuntimeError(f"No encontré Java {required}. Instalalo desde https://adoptium.net")


# --------------------------------------------------------------------------
# Versiones, herencia (inheritsFrom) y mod loaders
# --------------------------------------------------------------------------
def offline_uuid(name):
    digest = hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest()
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


def _lib_key(lib):
    parts = lib.get("name", "").split(":")
    return ":".join(parts[:2] + parts[3:])


def merge_libraries(child, parent):
    """Las librerías del hijo ganan a las del padre (mismo group:artifact)."""
    seen, out = set(), []
    for lib in list(child) + list(parent):
        k = _lib_key(lib)
        if k and k in seen:
            continue
        seen.add(k)
        out.append(lib)
    return out


def resolve_version(vid):
    """Lee versions/<id>/<id>.json y resuelve 'inheritsFrom' recursivamente."""
    path = os.path.join(VERSIONS, vid, f"{vid}.json")
    with open(path, encoding="utf-8") as f:
        v = json.load(f)
    parent_id = v.get("inheritsFrom")
    if not parent_id:
        v["_root"] = vid
        return v
    p = resolve_version(parent_id)
    merged = dict(p)
    for k, val in v.items():
        if k not in ("libraries", "arguments", "inheritsFrom"):
            merged[k] = val
    merged["libraries"] = merge_libraries(v.get("libraries", []), p.get("libraries", []))
    pa, ca = p.get("arguments"), v.get("arguments")
    if pa or ca:
        pa, ca = pa or {}, ca or {}
        merged["arguments"] = {"game": pa.get("game", []) + ca.get("game", []),
                               "jvm": pa.get("jvm", []) + ca.get("jvm", [])}
    merged["id"] = vid
    merged["_root"] = p["_root"]
    return merged


def loader_versions(kind, mc):
    """Versiones del loader para una versión de Minecraft (la mejor primero)."""
    if kind in ("fabric", "quilt"):
        base = FABRIC_META if kind == "fabric" else QUILT_META
        data = fetch_json(f"{base}/{urllib.parse.quote(mc)}")
        vers, stable = [], []
        for e in data:
            l = e.get("loader", {})
            v = l.get("version")
            if not v or any(t in v for t in ("beta", "pre", "alpha")) and kind == "quilt":
                continue
            vers.append(v)
            if l.get("stable"):
                stable.append(v)
        return (stable or vers)[:25]
    if kind == "forge":
        promos = fetch_json(FORGE_PROMOS).get("promos", {})
        out = []
        for k in ("recommended", "latest"):
            v = promos.get(f"{mc}-{k}")
            if v and v not in out:
                out.append(v)
        return out
    if kind == "neoforge":
        prefix = neoforge_prefix(mc)
        if not prefix:
            return []
        vs = fetch_json(NEOFORGE_API).get("versions", [])
        vs = [v for v in vs if v.startswith(prefix) and not any(t in v for t in ("beta", "alpha"))]
        return list(reversed(vs))[:25]
    return []


def neoforge_prefix(mc):
    parts = mc.split(".")
    try:
        first = int(parts[0])
    except ValueError:
        return None
    if first == 1:
        if len(parts) == 2:
            return f"{parts[1]}.0."
        if len(parts) >= 3:
            return f"{parts[1]}.{parts[2]}."
        return None
    if len(parts) == 2:
        return f"{mc}.0."
    return mc + "."


def install_fabric_like(kind, mc, lv):
    base = FABRIC_META if kind == "fabric" else QUILT_META
    profile = fetch_json(f"{base}/{urllib.parse.quote(mc)}/{urllib.parse.quote(lv)}/profile/json")
    vid = profile["id"]
    path = os.path.join(VERSIONS, vid, f"{vid}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f)
    return vid


def find_loader_dir(kind, lv):
    if not os.path.isdir(VERSIONS):
        return None
    for d in sorted(os.listdir(VERSIONS)):
        low = d.lower()
        if lv.lower() not in low:
            continue
        if kind == "forge" and ("neoforge" in low or "forge" not in low):
            continue
        if kind == "neoforge" and "neoforge" not in low:
            continue
        if os.path.isfile(os.path.join(VERSIONS, d, d + ".json")):
            return d
    return None


# --------------------------------------------------------------------------
# Mods (Modrinth)
# --------------------------------------------------------------------------
MODRINTH = "https://api.modrinth.com/v2"
MOD_DIRS = {"mod": "mods", "resourcepack": "resourcepacks", "shader": "shaderpacks"}


def loader_tags(kind):
    return {"fabric": ["fabric"], "quilt": ["quilt", "fabric"],
            "forge": ["forge"], "neoforge": ["neoforge"]}.get(kind, [])


def modrinth_search(query, mc, kind, ptype="mod", limit=20):
    facets = [[f"project_type:{ptype}"], [f"versions:{mc}"]]
    if ptype == "mod":
        facets.append([f"categories:{t}" for t in loader_tags(kind)])
    url = (f"{MODRINTH}/search?query={urllib.parse.quote(query)}&limit={limit}"
           f"&index={'relevance' if query else 'downloads'}"
           f"&facets={urllib.parse.quote(json.dumps(facets))}")
    return fetch_json(url).get("hits", [])


def modrinth_pick_version(project, mc, kind, ptype="mod"):
    url = (f"{MODRINTH}/project/{urllib.parse.quote(project)}/version"
           f"?game_versions={urllib.parse.quote(json.dumps([mc]))}")
    if ptype == "mod":
        url += f"&loaders={urllib.parse.quote(json.dumps(loader_tags(kind)))}"
    vs = fetch_json(url)
    pref = [v for v in vs if v.get("version_type") == "release"] or vs
    return pref[0] if pref else None


def modrinth_install(project, mc, kind, dest_dir, _seen=None, depth=0, ptype="mod"):
    """Instala un proyecto y sus dependencias obligatorias. Devuelve los archivos instalados."""
    seen = _seen if _seen is not None else set()
    if project in seen or depth > 3:
        return []
    seen.add(project)
    v = modrinth_pick_version(project, mc, kind, ptype)
    if not v:
        return []
    files = v.get("files", [])
    f = next((x for x in files if x.get("primary")), files[0] if files else None)
    if not f:
        return []
    name = os.path.basename(f["filename"])
    os.makedirs(dest_dir, exist_ok=True)
    download(f["url"], os.path.join(dest_dir, name), (f.get("hashes") or {}).get("sha1"))
    installed = [name]
    for dep in v.get("dependencies", []):
        if dep.get("dependency_type") == "required" and dep.get("project_id"):
            installed += modrinth_install(dep["project_id"], mc, kind, dest_dir, seen, depth + 1, ptype)
    return installed


# --------------------------------------------------------------------------
# Lanzador
# --------------------------------------------------------------------------
class Launcher:
    def __init__(self, log, progress, on_change=None):
        self.log = log
        self.progress = progress
        self.on_change = on_change or (lambda: None)

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
                # formato maven simple (Fabric / Quilt / versiones antiguas)
                rel = maven_path(lib["name"])
                base = lib.get("url", "https://libraries.minecraft.net/").rstrip("/") + "/"
                dest = os.path.join(LIBS, *rel.split("/"))
                tasks.append((base + rel, dest, None))
                classpath.append(dest)
                continue
            art = d.get("artifact")
            if art and art.get("path"):
                dest = os.path.join(LIBS, *art["path"].split("/"))
                if art.get("url"):
                    tasks.append((art["url"], dest, art.get("sha1")))
                classpath.append(dest)  # (los de Forge los crea el instalador, sin url)
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
        shutil.rmtree(natives_dir, ignore_errors=True)
        os.makedirs(natives_dir, exist_ok=True)
        for jar, exclude in native_jobs:
            with zipfile.ZipFile(jar) as z:
                for name in z.namelist():
                    if name.endswith("/") or any(name.startswith(e) for e in exclude):
                        continue
                    z.extract(name, natives_dir)
        seen, uniq = set(), []
        for c in classpath:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    def prepare_assets(self, vjson, gdir):
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
            target_root = (os.path.join(gdir, "resources") if idx.get("map_to_resources")
                           else os.path.join(ASSETS, "virtual", ai["id"]))
            for name, info in objs.items():
                src = os.path.join(ASSETS, "objects", info["hash"][:2], info["hash"])
                dst = os.path.join(target_root, *name.split("/"))
                if not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
            game_assets = target_root
        return assets_root, game_assets, ai["id"]

    # ---- mod loaders
    def install_forge_like(self, kind, mc, lv, java):
        existing = find_loader_dir(kind, lv)
        if existing:
            return existing
        if kind == "forge":
            slugs = [f"{mc}-{lv}", f"{mc}-{lv}-{mc}"]
            urls = [f"{FORGE_MAVEN}/{s}/forge-{s}-installer.jar" for s in slugs]
        else:
            urls = [f"{NEOFORGE_MAVEN}/{lv}/neoforge-{lv}-installer.jar"]
        installer = os.path.join(INSTALLERS, f"{kind}-{lv}-installer.jar")
        last = None
        self.log(f"Descargando instalador de {LOADER_NAMES[kind]} {lv}...")
        for u in urls:
            try:
                download(u, installer)
                last = None
                break
            except Exception as e:
                last = e
        if last:
            raise RuntimeError(f"No pude descargar el instalador de {LOADER_NAMES[kind]}: {last}")
        os.makedirs(ROOT, exist_ok=True)
        prof = os.path.join(ROOT, "launcher_profiles.json")
        if not os.path.exists(prof):
            with open(prof, "w", encoding="utf-8") as f:
                f.write('{"profiles":{}}\n')
        self.log(f"Instalando {LOADER_NAMES[kind]} (puede tardar un par de minutos)...")
        code = stream_process([java, "-jar", installer, "--installClient", ROOT], ROOT, self.log)
        found = find_loader_dir(kind, lv)
        if code != 0 or not found:
            raise RuntimeError(f"El instalador de {LOADER_NAMES[kind]} falló (código {code}). "
                               f"Mirá la consola para ver el detalle.")
        return found

    def ensure_loader(self, inst, java):
        kind, mc = inst["loader"], inst["mc"]
        if kind == "vanilla":
            return mc
        lv = inst.get("loader_version") or ""
        if not lv:
            self.log(f"Buscando la última versión de {LOADER_NAMES[kind]} para {mc}...")
            vs = loader_versions(kind, mc)
            if not vs:
                raise RuntimeError(f"{LOADER_NAMES[kind]} no está disponible para Minecraft {mc}.")
            lv = vs[0]
            inst["loader_version"] = lv
            self.on_change()
        lid = inst.get("loader_id") or ""
        if lid and os.path.isfile(os.path.join(VERSIONS, lid, lid + ".json")):
            return lid
        if kind in ("fabric", "quilt"):
            self.log(f"Instalando {LOADER_NAMES[kind]} {lv}...")
            lid = install_fabric_like(kind, mc, lv)
        else:
            lid = self.install_forge_like(kind, mc, lv, java)
        inst["loader_id"] = lid
        self.on_change()
        return lid

    def pick_java(self, inst, required, component):
        jp = (inst.get("java_path") or "").strip()
        if jp:
            if os.path.isfile(jp):
                return jp
            self.log("El Java personalizado no existe, busco uno automáticamente.")
        return find_java(required, component, self.log, self.progress)

    # ---- lanzar
    def launch(self, inst, username, manifest):
        gdir = game_dir(inst)
        os.makedirs(gdir, exist_ok=True)
        if inst["loader"] != "vanilla":
            os.makedirs(os.path.join(gdir, "mods"), exist_ok=True)
        mc = inst["mc"]
        self.log(f"Preparando {inst['name']} (Minecraft {mc})...")
        base = self.load_version_json(mc, manifest)

        # cliente vanilla
        client = base["downloads"]["client"]
        client_jar = os.path.join(VERSIONS, mc, f"{mc}.jar")
        self.log("Descargando cliente...")
        download(client["url"], client_jar, client.get("sha1"))

        # java
        jv = base.get("javaVersion", {})
        required = jv.get("majorVersion", 8)
        component = jv.get("component", "jre-legacy" if required <= 8 else "java-runtime-gamma")
        self.log(f"Buscando Java {required}...")
        java = self.pick_java(inst, required, component)
        self.log(f"Java: {java}")

        # loader
        vid = self.ensure_loader(inst, java)
        vjson = resolve_version(vid)
        root_id = vjson["_root"]
        root_jar = os.path.join(VERSIONS, root_id, f"{root_id}.jar")

        # librerías
        natives_dir = os.path.join(VERSIONS, vid, "natives")
        self.log("Descargando librerías...")
        classpath = self.prepare_libraries(vjson, natives_dir)
        if root_jar not in classpath:
            classpath.append(root_jar)

        # assets
        self.log("Descargando assets (sonidos, idiomas...)...")
        assets_root, game_assets, assets_id = self.prepare_assets(vjson, gdir)

        # log4j
        log_arg = None
        lc = vjson.get("logging", {}).get("client")
        if lc and lc.get("file"):
            lpath = os.path.join(ASSETS, "log_configs", lc["file"]["id"])
            download(lc["file"]["url"], lpath, lc["file"].get("sha1"))
            log_arg = lc["argument"].replace("${path}", lpath)

        variables = {
            "auth_player_name": username,
            "version_name": vid,
            "game_directory": gdir,
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
        if log_arg:
            jvm.append(log_arg)

        # opciones de la instancia
        ram = int(inst.get("ram_mb", 4096))
        cmd = [java, f"-Xmx{ram}M", f"-Xms{min(512, ram)}M"]
        cmd += JVM_PRESETS.get(inst.get("jvm_preset", "Por defecto"), [])
        extra = (inst.get("jvm_args") or "").strip()
        if extra:
            cmd += shlex.split(extra, posix=(sys.platform != "win32"))
        cmd += [substitute(a, variables) for a in jvm]
        cmd.append(vjson["mainClass"])
        cmd += [substitute(a, variables) for a in game]
        w, h = int(inst.get("width") or 0), int(inst.get("height") or 0)
        if w > 0 and h > 0:
            cmd += ["--width", str(w), "--height", str(h)]
        if inst.get("fullscreen"):
            cmd.append("--fullscreen")

        self.log("Iniciando Minecraft...")
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000
        return subprocess.Popen(cmd, cwd=gdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", **kw)


# --------------------------------------------------------------------------
# Config e instancias
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


DEFAULT_CONFIG = {"username": "Player", "last_instance": "", "snapshots": False, "old": False,
                  "minimize": False, "show_console": False, "ui_scale": 0, "instances": []}


def new_instance(name, mc, loader="vanilla", loader_version="", ram_mb=4096):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "instancia"
    return {"id": f"{slug}-{uuid.uuid4().hex[:6]}", "name": name, "mc": mc, "loader": loader,
            "loader_version": loader_version, "loader_id": "", "ram_mb": ram_mb,
            "jvm_preset": "Por defecto", "jvm_args": "", "width": 0, "height": 0,
            "fullscreen": False, "java_path": "", "playtime": 0}


def unique_name(name, instances, ignore_id=None):
    names = {i["name"].lower() for i in instances if i["id"] != ignore_id}
    if name.lower() not in names:
        return name
    n = 2
    while f"{name} ({n})".lower() in names:
        n += 1
    return f"{name} ({n})"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg["instances"] = []
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    # migración desde la versión 0.1/0.2 (una sola instalación)
    if not cfg["instances"] and cfg.get("version"):
        inst = new_instance("Principal", cfg["version"], ram_mb=int(cfg.get("ram_mb", 4096)))
        inst["id"] = "default"
        cfg["instances"].append(inst)
        cfg["last_instance"] = "default"
    return cfg


def save_config(cfg):
    os.makedirs(ROOT, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def describe(inst):
    loader = LOADER_NAMES.get(inst["loader"], inst["loader"])
    if inst["loader"] != "vanilla" and inst.get("loader_version"):
        loader += f" {inst['loader_version']}"
    return f"{loader}  ·  Minecraft {inst['mc']}"


PALETTE = ["#3a8ad8", "#2fbf71", "#e0a030", "#c0589a", "#38b6c9", "#9b6bff", "#e0603c"]


def accent_for(iid):
    return PALETTE[sum(map(ord, iid)) % len(PALETTE)]


def fmt_time(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "Sin jugar" if sec == 0 else "< 1 min"
    h, m = divmod(sec // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
         "Septiembre", "Octubre", "Noviembre", "Diciembre"]

NEWS = {
    "Novedades de Nova": (
        "¡Nueva interfaz en NovaLauncher!",
        "Pantalla de inicio renovada, barra de acciones flotante y tienda de mods, "
        "texturas y shaders con Modrinth.",
        "Nova Staff"),
    "Consejos": (
        "Mods en un solo clic",
        "Si tu instancia es Vanilla, entrá a Mods y creá una de Fabric automáticamente. "
        "Cada instancia tiene sus propios mundos y mods.",
        "Nova Staff"),
}


# --------------------------------------------------------------------------
# Interfaz
# --------------------------------------------------------------------------
def set_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def run_gui():
    import copy
    import math
    import random
    set_dpi_awareness()
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    BG, CARD, CARD2, LINE = "#080b14", "#121726", "#1a2036", "#252d47"
    CARDH = "#0d1328"          # tarjetas de la pantalla de inicio
    DOCK, PILL = "#070911", "#161a28"
    FG, MUTED, ACCENT, ACCENT_H = "#e9ecf4", "#8a90a2", "#3a8ad8", "#5aa2ee"
    GOLD = "#d9a21b"
    GREEN, GREEN_H, DIS = "#2fbf71", "#3bd983", "#262b38"
    RED, RED_H = "#c0455a", "#e0556c"
    OK, BAD = "#38c172", "#ff6b81"
    FONT = "Segoe UI"
    LOADER_LABELS = ["Vanilla (sin mods)", "Fabric", "Quilt", "Forge", "NeoForge"]
    LOADER_KEYS = ["vanilla", "fabric", "quilt", "forge", "neoforge"]
    SCALES = ["Automático", "100%", "125%", "150%", "175%", "200%"]

    root = tk.Tk()
    cfg = load_config()

    # --- escala / resolución de la interfaz
    us = int(cfg.get("ui_scale") or 0)
    if us > 0:
        SC = us / 100.0
        try:
            root.tk.call("tk", "scaling", SC * 96 / 72.0)
        except tk.TclError:
            pass
    else:
        SC = root.winfo_fpixels("1i") / 96.0
    SC = max(0.8, min(SC, 3.0))

    def px(n):
        return int(round(n * SC))

    def blend(c1, c2, t):
        t = max(0.0, min(1.0, t))
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    def round_rect(cv, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return cv.create_polygon(pts, smooth=True, **kw)

    class RoundButton(tk.Canvas):
        """Botón con esquinas redondeadas."""

        def __init__(self, parent, text, command, color, hover, parent_bg,
                     font=(FONT, 15, "bold"), height=54, radius=14, fg="white", width=None):
            kw = {"width": px(width)} if width else {}
            super().__init__(parent, height=px(height), bg=parent_bg, highlightthickness=0,
                             bd=0, cursor="hand2", **kw)
            self.text, self.command, self.color, self.hover = text, command, color, hover
            self.fg, self.font, self.r = fg, font, px(radius)
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
            round_rect(self, 1, 1, w - 1, h - 1, r, fill=fill, outline=fill)
            self.create_text(w / 2, h / 2, text=self.text, font=self.font,
                             fill=self.fg if self.enabled else MUTED)

    class ScrollFrame(tk.Frame):
        def __init__(self, parent, bg):
            super().__init__(parent, bg=bg)
            self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
            self.sb = tk.Scrollbar(self, command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=self.sb.set)
            self.sb.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.inner = tk.Frame(self.canvas, bg=bg)
            self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
            self.inner.bind("<Configure>", lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")))
            self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
            self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
            self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        def _wheel(self, e):
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    # --- ventana
    root.title(f"{APP_NAME} {APP_VERSION}")
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    W, H = min(px(1180), sw - px(60)), min(px(760), sh - px(110))
    root.geometry(f"{W}x{H}+{max(0, (sw - W) // 2)}+{max(0, (sh - H) // 2 - px(20))}")
    root.minsize(min(px(980), W), min(px(640), H))
    root.configure(bg=BG)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TCombobox", fieldbackground=CARD2, background=CARD2, foreground=FG,
                    arrowcolor=FG, bordercolor=LINE, lightcolor=CARD2, darkcolor=CARD2,
                    selectbackground=CARD2, selectforeground=FG, disabledbackground=CARD,
                    disabledforeground=MUTED)
    style.map("TCombobox", fieldbackground=[("readonly", CARD2), ("disabled", CARD)],
              foreground=[("readonly", FG), ("disabled", MUTED)],
              selectbackground=[("readonly", CARD2)])
    style.configure("Nova.Horizontal.TProgressbar", troughcolor=DOCK, background=ACCENT,
                    bordercolor=DOCK, lightcolor=ACCENT, darkcolor=ACCENT, thickness=px(4))
    style.configure("Nova.Horizontal.TScale", background=BG, troughcolor=CARD2)
    root.option_add("*TCombobox*Listbox.background", CARD2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.font", (FONT, 11))

    msgs = queue.Queue()
    state = {"manifest": None, "busy": False, "page": "play", "lv_cb": None, "playing": "",
             "hits": [], "net": {"mojang": None, "modrinth": None}}
    max_ram = max(2048, min(32768, int(total_ram_mb() * 0.75)))

    user_var = tk.StringVar(value=cfg["username"])
    snap_var = tk.BooleanVar(value=cfg["snapshots"])
    old_var = tk.BooleanVar(value=cfg["old"])
    min_var = tk.BooleanVar(value=cfg["minimize"])
    con_var = tk.BooleanVar(value=cfg["show_console"])
    scale_var = tk.StringVar(value=SCALES[0] if not us else f"{us}%")
    sel_var = tk.StringVar()
    news_var = tk.StringVar(value=list(NEWS)[0])
    status_var = tk.StringVar(value="Cargando versiones...")

    def instances():
        return cfg["instances"]

    def get_inst(iid):
        return next((i for i in instances() if i["id"] == iid), None)

    def cur():
        return get_inst(cfg.get("last_instance")) or (instances()[0] if instances() else None)

    def on_inst(iid, fn):
        inst = get_inst(iid)
        if inst:
            fn(inst)

    def open_folder(path):
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def flat_btn(parent, text, command, bg=CARD, hover=CARD2, fg=FG, size=9):
        b = tk.Button(parent, text=text, font=(FONT, size), bg=bg, fg=fg, activebackground=hover,
                      activeforeground=fg, relief="flat", bd=0, cursor="hand2", command=command)
        b.bind("<Enter>", lambda e: b.config(bg=hover))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    def card(parent, **kw):
        return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE, **kw)

    def small_title(parent, text, bg=CARD):
        return tk.Label(parent, text=text, font=(FONT, 8, "bold"), fg=MUTED, bg=bg)

    def page_title(parent, text):
        return tk.Label(parent, text=text, font=(FONT, 22, "bold"), fg=FG, bg=BG)

    # ================================================================== barra dorada
    header = tk.Canvas(root, height=px(32), bg=BG, highlightthickness=0, bd=0)
    header.pack(side="top", fill="x")

    def draw_header(_=None):
        w, h = header.winfo_width(), header.winfo_height()
        if w < 20:
            return
        header.delete("all")
        for x in range(0, w, 3):
            t = x / w
            col = blend("#c98f10", "#7a5208", t * 0.6) if t < 0.5 else blend("#a87a0e", "#14110a", (t - 0.5) * 1.5)
            header.create_rectangle(x, 0, x + 3, h, fill=col, outline="")
        header.create_text(px(12), h / 2, anchor="w", text=f"{APP_NAME} {APP_VERSION} Premium Edition",
                           fill="white", font=(FONT, 10, "bold"))

    header.bind("<Configure>", draw_header)

    # ================================================================== barra flotante inferior
    dockf = tk.Frame(root, bg=DOCK)
    dockf.pack(side="bottom", fill="x")
    bar = ttk.Progressbar(dockf, mode="determinate", style="Nova.Horizontal.TProgressbar")
    bar.pack(fill="x")
    tk.Label(dockf, textvariable=status_var, font=(FONT, 9), fg=MUTED, bg=DOCK).pack(pady=(px(6), 0))
    pill = tk.Canvas(dockf, height=px(62), bg=DOCK, highlightthickness=0, bd=0)
    pill.pack(fill="x")

    content = tk.Frame(root, bg=BG)
    content.pack(side="top", fill="both", expand=True)
    pages = {}

    def show_page(name):
        for f in pages.values():
            f.pack_forget()
        state["page"] = name
        pages[name].pack(fill="both", expand=True)
        if name == "mods":
            mods_enter()

    dock_items = [("⌂  Inicio", lambda: show_page("play")),
                  ("✦  Mods", lambda: show_page("mods")),
                  ("≡  Consola", lambda: show_page("console")),
                  ("⚙  Ajustes", lambda: show_page("settings")),
                  ("▣  Instancias", lambda: show_page("instances")),
                  ("⬇  Descargar", lambda: open_instance_dialog(None))]
    dock_btns = [RoundButton(pill, t, fn, ACCENT, ACCENT_H, PILL, font=(FONT, 10, "bold"),
                             height=34, radius=10, width=len(t) * 7 + 34) for t, fn in dock_items]
    play_btn = RoundButton(pill, "▶  Jugar", lambda: on_play(), GREEN, GREEN_H, PILL,
                           font=(FONT, 10, "bold"), height=34, radius=10, width=130)
    dock_btns.append(play_btn)

    def draw_dock(_=None):
        w = pill.winfo_width()
        if w < 100:
            return
        pill.delete("all")
        gap, ph, y0 = px(8), px(48), px(6)
        widths = [px(int(b.winfo_reqwidth() / SC)) if False else int(float(b.cget("width")))
                  for b in dock_btns]
        total = sum(widths) + gap * (len(widths) + 1)
        x = (w - total) // 2
        round_rect(pill, x, y0, x + total, y0 + ph, px(16), fill=PILL, outline=LINE)
        x += gap
        for b, wd in zip(dock_btns, widths):
            pill.create_window(x, y0 + ph // 2, window=b, anchor="w", width=wd, height=px(34))
            x += wd + gap

    pill.bind("<Configure>", draw_dock)

    # ================================================================== INICIO
    scene = tk.Canvas(content, bg=BG, highlightthickness=0, bd=0)
    pages["play"] = scene

    news_box = ttk.Combobox(scene, textvariable=news_var, values=list(NEWS), state="readonly",
                            font=(FONT, 9), width=18)
    inst_box = ttk.Combobox(scene, textvariable=sel_var, state="readonly", font=(FONT, 11), width=24)
    ver_btn = RoundButton(scene, "Ver instancias →", lambda: show_page("instances"), ACCENT, ACCENT_H,
                          "#0d1328", font=(FONT, 9, "bold"), height=30, radius=9, width=140)

    def heights(w, seed, base, amp, step):
        r = random.Random(seed)
        p1, p2, p3 = r.random() * 6, r.random() * 6, r.random() * 6
        out = []
        for i in range(w // step + 2):
            v = 0.55 * math.sin(i * 0.17 + p1) + 0.3 * math.sin(i * 0.41 + p2) + 0.15 * math.sin(i * 0.93 + p3)
            out.append(int((base + amp * v) // step * step))
        return out

    def net_status(key):
        v = state["net"][key]
        if key == "mojang" and v is None and state["manifest"]:
            v = True
        if v is None:
            return "Comprobando...", MUTED
        return ("Operativo", OK) if v else ("Sin conexión", BAD)

    def draw_scene(_=None):
        w, h = scene.winfo_width(), scene.winfo_height()
        if w < 300 or h < 260:
            return
        scene.delete("all")
        state["hits"] = []

        def txt(x, y, s, size, fill=FG, bold=False, anchor="w", width=None):
            kw = {"width": width} if width else {}
            return scene.create_text(x, y, anchor=anchor, text=s, fill=fill,
                                     font=(FONT, size, "bold" if bold else "normal"), **kw)

        # --- cielo nocturno y vía láctea
        sky0, sky1 = "#04060e", "#101d3f"
        for y in range(0, h, 4):
            scene.create_line(0, y, w, y, fill=blend(sky0, sky1, y / h), width=4)
        rng = random.Random(21)
        ax, ay, bx, by = w * 0.80, -h * 0.05, w * 0.40, h * 0.85
        for _i in range(520):
            t = rng.random()
            x = ax + (bx - ax) * t + rng.gauss(0, w * 0.055)
            y = ay + (by - ay) * t + rng.gauss(0, h * 0.05)
            z = px(rng.choice([1, 1, 2]))
            scene.create_rectangle(x, y, x + z, y + z,
                                   fill=blend(sky1, "#9db2e6", 0.15 + rng.random() * 0.5), outline="")
        for _i in range(130):
            x, y, z = rng.randrange(w), rng.randrange(int(h * 0.7)), px(rng.choice([1, 2]))
            scene.create_rectangle(x, y, x + z, y + z,
                                   fill=blend(sky0, "#ffffff", 0.4 + rng.random() * 0.6), outline="")
        # --- terreno nevado en bloques
        step = px(22)
        for seed, base, amp, col in ((5, 0.74, 0.05, "#0e1730"), (9, 0.80, 0.04, "#14213f")):
            for i, hy in enumerate(heights(w, seed, h * base, h * amp, step)):
                scene.create_rectangle(i * step, hy, i * step + step, h, fill=col, outline="")
        rr = random.Random(7)
        for i, hy in enumerate(heights(w, 17, h * 0.88, h * 0.03, step)):
            x0 = i * step
            scene.create_rectangle(x0, hy, x0 + step, hy + step // 2,
                                   fill=blend("#8fa3c9", "#c9d6ee", rr.random() * 0.6), outline="")
            scene.create_rectangle(x0, hy + step // 2, x0 + step, h,
                                   fill=blend("#101a30", "#18233f", rr.random()), outline="")

        # --- geometría
        m = px(28)
        lw = int(w * 0.60)
        x0 = m + lw + px(28)
        rw = max(px(240), w - m - x0)

        # --- columna izquierda: novedades
        txt(m + 2, px(42) + 2, "Últimas novedades de Nova", 22, "#02030a", True)
        txt(m, px(42), "Últimas novedades de Nova", 22, "white", True)
        scene.create_window(m + lw, px(42), window=news_box, anchor="e", width=px(170))
        n_title, n_body, n_author = NEWS.get(news_var.get(), list(NEWS.values())[0])
        y1, y2 = px(78), int(h * 0.64)
        round_rect(scene, m, y1, m + lw, y2, px(14), fill=CARDH, outline=LINE)
        txt(m + px(18), y1 + px(30), n_title, 17, "#c9ced9", True)
        txt(m + lw - px(18), y1 + px(26), time.strftime("%d").lstrip("0"), 15, "#c9ced9", True, "e")
        txt(m + lw - px(18), y1 + px(46), MESES[int(time.strftime("%m")) - 1], 9, "#aeb5c6", True, "e")
        txt(m + px(18), y1 + px(62), n_body, 11, "#aeb5c6", False, "nw", lw - px(36))
        iy = y1 + px(112)
        ih = max(px(50), y2 - iy - px(40))
        iw = min(px(300), lw - px(36))
        round_rect(scene, m + px(18), iy, m + px(18) + iw, iy + ih, px(10), fill="#080d1c", outline=LINE)
        s = min(ih - px(24), px(80))
        cx, cy = m + px(18) + iw // 2, iy + ih // 2
        scene.create_rectangle(cx - s - px(34), cy - s // 2, cx - px(34), cy + s // 2,
                               outline=GOLD, width=px(5), fill="")
        txt(cx - s // 2 - px(34), cy, "N", max(14, int(s / SC * 0.45)), GOLD, True, "center")
        txt(cx, cy, "×", 22, "#9aa3b8", True, "center")
        c0 = cx + px(34)
        scene.create_rectangle(c0, cy - s // 2, c0 + s, cy - s // 2 + s // 3, fill="#4fb552", outline="")
        scene.create_rectangle(c0, cy - s // 2 + s // 3, c0 + s, cy + s // 2, fill="#6d4b31", outline="")
        txt(m + lw - px(18), y2 - px(18), f"⚒ {n_author}", 9, "#9aa3b8", True, "e")

        # --- jugador activo
        ty = y2 + px(44)
        txt(m + 2, ty + 2, "Jugador activo", 20, "#02030a", True)
        txt(m, ty, "Jugador activo", 20, "white", True)
        scene.create_window(m + lw, ty, window=ver_btn, anchor="e", width=px(140), height=px(30))
        name = user_var.get().strip() or "Jugador"
        inst = cur()
        ry = ty + px(24)
        rh = px(56)
        round_rect(scene, m, ry, m + px(320), ry + rh, px(12), fill=CARDH, outline=LINE)
        scene.create_rectangle(m + px(10), ry + px(10), m + px(10) + px(36), ry + px(46),
                               fill=accent_for(name), outline="")
        txt(m + px(28), ry + px(28), name[:1].upper(), 14, "white", True, "center")
        txt(m + px(58), ry + px(19), name[:20], 11, FG, True)
        stt = f"Jugando {state['playing']}" if state["playing"] else "En el menú de inicio"
        txt(m + px(58), ry + px(39), stt, 9, "#9aa3b8")
        state["hits"].append((m, ry, m + px(320), ry + rh, "settings"))
        txt(m + px(340), ry + px(10), "INSTANCIA", 8, "#9aa3b8", True)
        scene.create_window(m + px(340), ry + px(38), window=inst_box, anchor="w", width=px(240))
        if not inst:
            txt(m, ry + rh + px(14), "Creá una instancia para empezar (botón Descargar).", 9, "#ffe1c9")
        else:
            txt(m, ry + rh + px(14), describe(inst), 9, "#9aa3b8")

        # --- columna derecha: estado
        txt(x0 + 2, px(46) + 2, "Estado de Nova", 22, "#02030a", True)
        txt(x0, px(46), "Estado de Nova", 22, "white", True)

        def mini(x, y, cw, chh, title, sub, subcol, glyph="N", bold_title=True, tsize=11):
            round_rect(scene, x, y, x + cw, y + chh, px(12), fill=CARDH, outline=LINE)
            if glyph:
                scene.create_rectangle(x + px(12), y + (chh - px(26)) // 2, x + px(38),
                                       y + (chh + px(26)) // 2, outline=GOLD, width=2, fill="")
                txt(x + px(25), y + chh // 2, glyph, 11, GOLD, True, "center")
            tx = x + (px(50) if glyph else px(14))
            if sub:
                txt(tx, y + chh * 0.36, title, tsize, "#c9ced9", bold_title)
                txt(tx, y + chh * 0.72, sub, 8, subcol)
            else:
                txt(tx, y + chh // 2, title, tsize, "#c9ced9", bold_title)

        cw2 = (rw - px(10)) // 2
        st, sc_ = net_status("mojang")
        mini(x0, px(78), cw2, px(56), "Mojang", st, sc_)
        st, sc_ = net_status("modrinth")
        mini(x0 + cw2 + px(10), px(78), cw2, px(56), "Modrinth", st, sc_)
        n = len(instances())
        total = sum(int(i.get("playtime", 0)) for i in instances())
        mini(x0, px(146), rw, px(50), f"{n} instancia{'s' if n != 1 else ''}  ·  {fmt_time(total)}",
             "", MUTED, "▣", True, 12)
        state["hits"].append((x0, px(146), x0 + rw, px(196), "instances"))
        if state["playing"]:
            mini(x0, px(208), rw, px(66), "♪ Jugando ahora", state["playing"], "#9aa3b8", "", True, 12)
        else:
            mini(x0, px(208), rw, px(66), "♪ Jugando ahora", "Nada en ejecución", "#9aa3b8", "", True, 12)

    news_box.bind("<<ComboboxSelected>>", lambda e: draw_scene())
    scene.bind("<Configure>", draw_scene)
    scene.bind("<Button-1>", lambda e: next(
        (show_page(p) for (a, b, c, d, p) in state["hits"] if a <= e.x <= c and b <= e.y <= d), None))
    user_var.trace_add("write", lambda *a: draw_scene())

    # ================================================================== INSTANCIAS
    inst_page = tk.Frame(content, bg=BG)
    pages["instances"] = inst_page
    ih = tk.Frame(inst_page, bg=BG)
    ih.pack(fill="x", padx=px(36), pady=(px(22), px(10)))
    page_title(ih, "Instancias").pack(side="left")
    RoundButton(ih, "＋  Nueva instancia", lambda: open_instance_dialog(None), ACCENT, ACCENT_H, BG,
                font=(FONT, 11, "bold"), height=42, radius=12).pack(side="right", ipadx=px(26))
    tk.Label(inst_page, text="Cada instancia tiene su propia versión, mods, mundos y opciones.",
             font=(FONT, 10), fg=MUTED, bg=BG).pack(anchor="w", padx=px(38), pady=(0, px(10)))
    ilist = ScrollFrame(inst_page, BG)
    ilist.pack(fill="both", expand=True, padx=(px(36), px(20)), pady=(0, px(10)))

    def build_instances():
        for w in ilist.inner.winfo_children():
            w.destroy()
        if not instances():
            tk.Label(ilist.inner, text="Todavía no hay instancias. Tocá “Nueva instancia”.",
                     font=(FONT, 11), fg=MUTED, bg=BG).pack(pady=px(40))
            return
        for inst in instances():
            c = card(ilist.inner)
            c.pack(fill="x", pady=(0, px(10)), padx=(0, px(8)))
            tk.Frame(c, bg=accent_for(inst["id"]), width=px(5)).pack(side="left", fill="y")
            left = tk.Frame(c, bg=CARD)
            left.pack(side="left", fill="x", expand=True, padx=px(18), pady=px(14))
            tk.Label(left, text=inst["name"], font=(FONT, 13, "bold"), fg=FG, bg=CARD,
                     anchor="w").pack(fill="x")
            extra = (f"{describe(inst)}  ·  {inst.get('ram_mb', 4096) / 1024:.1f} GB RAM"
                     f"  ·  {fmt_time(inst.get('playtime', 0))}")
            tk.Label(left, text=extra, font=(FONT, 9), fg=MUTED, bg=CARD, anchor="w").pack(fill="x")
            right = tk.Frame(c, bg=CARD)
            right.pack(side="right", padx=px(14))
            iid = inst["id"]
            flat_btn(right, "Jugar", lambda i=iid: play_instance(i), bg=GREEN, hover=GREEN_H,
                     fg="white", size=10).pack(side="left", padx=px(3), ipadx=px(14), ipady=px(6))
            for txt_, fn in (("Editar", lambda i=iid: on_inst(i, open_instance_dialog)),
                             ("Mods", lambda i=iid: open_mods_for(i)),
                             ("Duplicar", lambda i=iid: on_inst(i, duplicate_instance)),
                             ("Carpeta", lambda i=iid: on_inst(i, lambda x: open_folder(game_dir(x))))):
                flat_btn(right, txt_, fn, bg=CARD2, hover=LINE).pack(
                    side="left", padx=px(3), ipadx=px(10), ipady=px(6))
            flat_btn(right, "Eliminar", lambda i=iid: delete_instance(i), bg=CARD2, hover=RED,
                     fg="#ff8a9b").pack(side="left", padx=px(3), ipadx=px(10), ipady=px(6))

    def play_instance(iid):
        cfg["last_instance"] = iid
        refresh_all()
        show_page("play")
        on_play()

    def delete_instance(iid):
        inst = get_inst(iid)
        if not inst:
            return
        if not messagebox.askokcancel(
                APP_NAME, f"¿Eliminar la instancia “{inst['name']}”?\n\n"
                          "Se borran también sus mundos, mods y configuración. "
                          "No se puede deshacer."):
            return
        shutil.rmtree(game_dir(inst), ignore_errors=True)
        cfg["instances"] = [i for i in instances() if i["id"] != iid]
        if cfg.get("last_instance") == iid:
            cfg["last_instance"] = instances()[0]["id"] if instances() else ""
        save_all()
        refresh_all()

    def duplicate_instance(inst):
        new = copy.deepcopy(inst)
        new["id"] = new_instance(inst["name"], inst["mc"])["id"]
        new["name"] = unique_name(inst["name"] + " (copia)", instances())
        new["playtime"] = 0
        try:
            if os.path.isdir(game_dir(inst)):
                shutil.copytree(game_dir(inst), game_dir(new))
        except Exception as e:
            messagebox.showwarning(APP_NAME, f"No se pudo copiar la instancia: {e}")
            return
        instances().append(new)
        cfg["last_instance"] = new["id"]
        save_all()
        refresh_all()

    # ================================================================== TIENDA (mods)
    mods_page = tk.Frame(content, bg=BG)
    pages["mods"] = mods_page
    mt = tk.Frame(mods_page, bg=BG)
    mt.pack(fill="x", padx=px(36), pady=(px(20), px(4)))
    page_title(mt, "Mods").pack(side="left")
    mods_target = tk.StringVar(value="")
    tk.Label(mt, textvariable=mods_target, font=(FONT, 10), fg=MUTED, bg=BG).pack(
        side="left", padx=px(16), pady=(px(12), 0))
    mods_state = {"tok": 0, "for": None, "ptype": "mod"}

    tabs = tk.Frame(mods_page, bg=BG)
    tabs.pack(fill="x", padx=px(36), pady=(px(6), px(8)))
    tab_btns = {}

    def style_tabs():
        for k, b in tab_btns.items():
            on = k == mods_state["ptype"]
            b.config(bg=ACCENT if on else CARD, fg="white" if on else FG)

    def set_ptype(k):
        mods_state["ptype"] = k
        style_tabs()
        mods_search()

    for k, label in (("mod", "Mods"), ("resourcepack", "Texturas"), ("shader", "Shaders")):
        b = tk.Button(tabs, text=label, font=(FONT, 10, "bold"), bg=CARD, fg=FG, activebackground=CARD2,
                      activeforeground=FG, relief="flat", bd=0, cursor="hand2",
                      command=lambda kk=k: set_ptype(kk))
        b.pack(side="left", padx=(0, px(6)), ipadx=px(20), ipady=px(7))
        tab_btns[k] = b
    flat_btn(tabs, "Instalados", lambda: mods_installed(), size=10).pack(
        side="right", ipadx=px(16), ipady=px(7))

    mrow = tk.Frame(mods_page, bg=BG)
    mrow.pack(fill="x", padx=px(36), pady=(0, px(6)))
    mods_q = tk.StringVar()
    mq = tk.Entry(mrow, textvariable=mods_q, font=(FONT, 12), bg=CARD2, fg=FG, insertbackground=FG,
                  relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT)
    mq.pack(side="left", fill="x", expand=True, ipady=px(7))
    mq.bind("<Return>", lambda e: mods_search())
    flat_btn(mrow, "Buscar", lambda: mods_search(), bg=ACCENT, hover=ACCENT_H, fg="white", size=10).pack(
        side="left", padx=(px(8), 0), ipadx=px(20), ipady=px(7))
    mods_status = tk.StringVar(value="")
    mods_status_lbl = tk.Label(mods_page, textvariable=mods_status, font=(FONT, 10), fg=MUTED, bg=BG,
                               anchor="w", justify="left", wraplength=px(820))
    mods_status_lbl.pack(fill="x", padx=px(38), pady=(0, px(6)))
    mods_list = ScrollFrame(mods_page, BG)
    mods_list.pack(fill="both", expand=True, padx=(px(36), px(20)), pady=(0, px(10)))
    mods_list.inner.columnconfigure(0, weight=1, uniform="g")
    mods_list.inner.columnconfigure(1, weight=1, uniform="g")

    def set_mstat(text, err=False):
        mods_status.set(text)
        mods_status_lbl.config(fg="#ff8a9b" if err else MUTED)

    def clear_mods_list():
        for w in mods_list.inner.winfo_children():
            w.destroy()

    def ptype_dir(inst):
        return os.path.join(game_dir(inst), MOD_DIRS[mods_state["ptype"]])

    def mods_context():
        inst = cur()
        if not inst:
            set_mstat("Creá una instancia primero.", True)
        return inst

    def mods_enter():
        inst = cur()
        mods_target.set(f"Instancia: {inst['name']}  ·  {describe(inst)}" if inst else "")
        style_tabs()
        if inst and mods_state["for"] != inst["id"]:
            mods_state["for"] = inst["id"]
            mods_search()

    def open_mods_for(iid):
        cfg["last_instance"] = iid
        mods_state["for"] = None
        refresh_all()
        show_page("mods")

    def quick_create(kind):
        inst = cur()
        if not inst:
            return
        mc = inst["mc"]
        set_mstat(f"Buscando {LOADER_NAMES[kind]} para Minecraft {mc}...")

        def job():
            try:
                vs = loader_versions(kind, mc)
            except Exception as e:
                vs = e
            msgs.put(("quick", kind, mc, vs))
        threading.Thread(target=job, daemon=True).start()

    def finish_quick(kind, mc, vs):
        if isinstance(vs, Exception):
            set_mstat(f"No se pudo consultar {LOADER_NAMES[kind]}: {vs}", True)
            return
        if not vs:
            set_mstat(f"{LOADER_NAMES[kind]} no está disponible para Minecraft {mc}.", True)
            return
        base = cur()
        ni = new_instance(unique_name(f"{LOADER_NAMES[kind]} {mc}", instances()), mc, kind, vs[0],
                          ram_mb=(base or {}).get("ram_mb", 4096))
        instances().append(ni)
        cfg["last_instance"] = ni["id"]
        save_all()
        refresh_all()
        mods_state["for"] = None
        mods_enter()

    def show_vanilla_cta(inst):
        clear_mods_list()
        set_mstat("")
        c = card(mods_list.inner)
        c.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(0, px(8)), pady=px(6))
        tk.Label(c, text="Esta instancia es Vanilla y no acepta mods", font=(FONT, 14, "bold"), fg=FG,
                 bg=CARD).pack(anchor="w", padx=px(22), pady=(px(18), px(4)))
        tk.Label(c, text=f"Los mods necesitan un cargador. Creá en un clic una instancia de Minecraft "
                         f"{inst['mc']} con el que prefieras (te recomiendo Fabric):",
                 font=(FONT, 10), fg=MUTED, bg=CARD, wraplength=px(700), justify="left").pack(
            anchor="w", padx=px(22))
        r = tk.Frame(c, bg=CARD)
        r.pack(anchor="w", padx=px(22), pady=px(18))
        for k in ("fabric", "quilt", "forge", "neoforge"):
            main = k == "fabric"
            flat_btn(r, LOADER_NAMES[k], lambda kk=k: quick_create(kk), bg=ACCENT if main else CARD2,
                     hover=ACCENT_H if main else LINE, fg="white" if main else FG, size=10).pack(
                side="left", padx=(0, px(8)), ipadx=px(18), ipady=px(8))
        tk.Label(c, text="Las pestañas Texturas y Shaders sí funcionan en esta instancia.",
                 font=(FONT, 9), fg=MUTED, bg=CARD).pack(anchor="w", padx=px(22), pady=(0, px(16)))

    def mods_search():
        inst = mods_context()
        if not inst:
            return
        ptype = mods_state["ptype"]
        if ptype == "mod" and inst["loader"] == "vanilla":
            mods_state["tok"] += 1
            show_vanilla_cta(inst)
            return
        mods_state["tok"] += 1
        tok = mods_state["tok"]
        set_mstat("Buscando en Modrinth...")
        q, mc, kind = mods_q.get().strip(), inst["mc"], inst["loader"]

        def job():
            try:
                res = modrinth_search(q, mc, kind, ptype)
            except Exception as e:
                res = e
            msgs.put(("mods", tok, res))
        threading.Thread(target=job, daemon=True).start()

    def show_hits(tok, hits):
        if tok != mods_state["tok"]:
            return
        clear_mods_list()
        if isinstance(hits, Exception):
            set_mstat(f"No se pudo cargar la tienda: {hits}. Revisá tu internet y tocá Buscar para reintentar.",
                      True)
            append_log(f"[Tienda] Error: {hits}")
            return
        if not hits:
            set_mstat("Sin resultados para esta versión de Minecraft. Probá con otra búsqueda.")
            return
        set_mstat(f"{len(hits)} resultados" + ("" if mods_q.get().strip() else " · los más descargados"))
        for idx, h in enumerate(hits):
            c = card(mods_list.inner)
            c.grid(row=idx // 2, column=idx % 2, sticky="nsew", padx=(0, px(8)), pady=(0, px(8)))
            title = h.get("title") or "?"
            tk.Label(c, text=title[:1].upper(), font=(FONT, 20, "bold"), fg="white",
                     bg=accent_for(h.get("project_id", title)), width=3).pack(side="left", fill="y")
            mid2 = tk.Frame(c, bg=CARD)
            mid2.pack(side="left", fill="both", expand=True, padx=px(14), pady=px(12))
            tk.Label(mid2, text=title, font=(FONT, 12, "bold"), fg=FG, bg=CARD, anchor="w").pack(fill="x")
            tk.Label(mid2, text=(h.get("description") or "")[:110], font=(FONT, 9), fg=MUTED, bg=CARD,
                     anchor="w", justify="left", wraplength=px(300)).pack(fill="x")
            dl = f"{int(h.get('downloads', 0)):,}".replace(",", ".")
            tk.Label(mid2, text=f"{dl} descargas", font=(FONT, 8), fg=MUTED, bg=CARD, anchor="w").pack(
                fill="x", pady=(px(3), 0))
            flat_btn(c, "Instalar", lambda p=h.get("project_id"), t=title: mods_install(p, t),
                     bg=GREEN, hover=GREEN_H, fg="white", size=10).pack(
                side="right", padx=px(12), ipadx=px(14), ipady=px(7))

    def mods_install(project, title):
        inst = mods_context()
        if not inst or not project:
            return
        ptype = mods_state["ptype"]
        dest, mc, kind = ptype_dir(inst), inst["mc"], inst["loader"]
        set_mstat(f"Instalando {title}...")

        def job():
            try:
                res = modrinth_install(project, mc, kind, dest, ptype=ptype)
            except Exception as e:
                res = e
            msgs.put(("modinst", title, res))
        threading.Thread(target=job, daemon=True).start()

    def mods_installed():
        inst = mods_context()
        if not inst:
            return
        mods_state["tok"] += 1
        ptype = mods_state["ptype"]
        d = ptype_dir(inst)
        exts = {"mod": (".jar", ".disabled"), "resourcepack": (".zip",), "shader": (".zip",)}[ptype]
        files = sorted(f for f in os.listdir(d) if f.endswith(exts)) if os.path.isdir(d) else []
        clear_mods_list()
        set_mstat(f"{len(files)} archivo(s) instalados" if files else "Todavía no hay nada instalado acá.")
        for i, fn in enumerate(files):
            off = fn.endswith(".disabled")
            c = card(mods_list.inner)
            c.grid(row=i, column=0, columnspan=2, sticky="ew", padx=(0, px(8)), pady=(0, px(6)))
            tk.Label(c, text=fn.replace(".disabled", "") + ("   (desactivado)" if off else ""),
                     font=(FONT, 10), fg=MUTED if off else FG, bg=CARD, anchor="w").pack(
                side="left", padx=px(16), pady=px(11), fill="x", expand=True)

            def toggle(f=fn):
                src = os.path.join(d, f)
                dst = src[:-9] if f.endswith(".disabled") else src + ".disabled"
                try:
                    os.replace(src, dst)
                except OSError as e:
                    set_mstat(f"No se pudo cambiar: {e}", True)
                mods_installed()

            def remove(f=fn):
                try:
                    os.remove(os.path.join(d, f))
                except OSError as e:
                    set_mstat(f"No se pudo quitar: {e}", True)
                mods_installed()
            flat_btn(c, "Quitar", remove, bg=CARD2, hover=RED, fg="#ff8a9b").pack(
                side="right", padx=px(8), ipadx=px(10), ipady=px(5))
            if ptype == "mod":
                flat_btn(c, "Activar" if off else "Desactivar", toggle, bg=CARD2, hover=LINE).pack(
                    side="right", padx=px(4), ipadx=px(10), ipady=px(5))

    # ================================================================== AJUSTES
    sett = tk.Frame(content, bg=BG)
    pages["settings"] = sett
    page_title(sett, "Ajustes").pack(anchor="w", padx=px(36), pady=(px(22), px(12)))
    c1 = card(sett)
    c1.pack(fill="x", padx=px(36), pady=(0, px(12)))
    small_title(c1, "NOMBRE DE USUARIO").pack(anchor="w", padx=px(20), pady=(px(14), px(6)))
    tk.Entry(c1, textvariable=user_var, font=(FONT, 12), bg=CARD2, fg=FG, insertbackground=FG,
             relief="flat", highlightthickness=1, highlightbackground=LINE,
             highlightcolor=ACCENT).pack(fill="x", padx=px(20), pady=(0, px(16)), ipady=px(7))

    c2 = card(sett)
    c2.pack(fill="x", padx=px(36), pady=(0, px(12)))
    small_title(c2, "TAMAÑO DE LA INTERFAZ").pack(anchor="w", padx=px(20), pady=(px(14), px(6)))
    srow = tk.Frame(c2, bg=CARD)
    srow.pack(fill="x", padx=px(20), pady=(0, px(16)))
    scale_box = ttk.Combobox(srow, textvariable=scale_var, values=SCALES, state="readonly",
                             font=(FONT, 11), width=14)
    scale_box.pack(side="left", ipady=px(3))
    tk.Label(srow, text="Se aplica al reiniciar el launcher. Subilo si se ve chico o borroso.",
             font=(FONT, 9), fg=MUTED, bg=CARD).pack(side="left", padx=px(14))

    c3 = card(sett)
    c3.pack(fill="x", padx=px(36), pady=(0, px(12)))
    small_title(c3, "OPCIONES").pack(anchor="w", padx=px(20), pady=(px(14), px(4)))

    def check(parent, text, var, cmd=None):
        tk.Checkbutton(parent, text=text, variable=var, command=cmd, fg=FG, bg=CARD,
                       selectcolor=CARD2, activebackground=CARD, activeforeground=FG,
                       font=(FONT, 10), bd=0, highlightthickness=0, anchor="w").pack(
            fill="x", padx=px(16), pady=px(3))

    check(c3, "Mostrar snapshots al elegir versión", snap_var)
    check(c3, "Mostrar versiones antiguas (beta / alpha)", old_var)
    check(c3, "Minimizar el launcher mientras juego", min_var)
    check(c3, "Mostrar la consola al iniciar el juego", con_var)
    tk.Frame(c3, bg=CARD, height=px(10)).pack()
    drow = tk.Frame(sett, bg=BG)
    drow.pack(fill="x", padx=px(36), pady=(px(4), 0))
    tk.Label(drow, text=f"Datos guardados en:  {ROOT}", font=(FONT, 9), fg=MUTED, bg=BG,
             anchor="w").pack(side="left")
    flat_btn(drow, "Abrir carpeta", lambda: open_folder(ROOT)).pack(
        side="left", padx=px(12), ipadx=px(10), ipady=px(4))

    # ================================================================== CONSOLA
    cons = tk.Frame(content, bg=BG)
    pages["console"] = cons
    head = tk.Frame(cons, bg=BG)
    head.pack(fill="x", padx=px(36), pady=(px(22), px(10)))
    page_title(head, "Consola").pack(side="left")
    log_text = tk.Text(cons, bg=CARD, fg="#b7bccb", font=("Consolas", 9), relief="flat",
                       wrap="word", state="disabled", bd=0, padx=px(12), pady=px(10),
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
        flat_btn(head, label, fn).pack(side="right", padx=(px(8), 0), ipadx=px(12), ipady=px(5))
    sb = tk.Scrollbar(cons, command=log_text.yview)
    log_text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y", padx=(0, px(36)), pady=(0, px(14)))
    log_text.pack(side="left", fill="both", expand=True, padx=(px(36), 0), pady=(0, px(14)))

    def append_log(text):
        log_text.config(state="normal")
        log_text.insert("end", text.rstrip("\n") + "\n")
        log_text.see("end")
        log_text.config(state="disabled")

    # ================================================================== refresco
    def refresh_play():
        inst_box["values"] = [i["name"] for i in instances()]
        inst = cur()
        sel_var.set(inst["name"] if inst else "")
        draw_scene()

    def refresh_all():
        refresh_play()
        build_instances()

    def on_pick(_=None):
        for i in instances():
            if i["name"] == sel_var.get():
                cfg["last_instance"] = i["id"]
        refresh_play()

    inst_box.bind("<<ComboboxSelected>>", on_pick)

    def save_all():
        cfg.update(username=user_var.get().strip() or "Player",
                   snapshots=snap_var.get(), old=old_var.get(), minimize=min_var.get(),
                   show_console=con_var.get(),
                   ui_scale=0 if scale_var.get() == SCALES[0] else int(scale_var.get().rstrip("%")))
        save_config(cfg)

    # ================================================================== diálogo de instancia
    def mc_choices():
        m = state["manifest"]
        if not m:
            return []
        out = []
        for v in m["versions"]:
            t = v["type"]
            if t == "release" or (t == "snapshot" and snap_var.get()) or \
               (t in ("old_beta", "old_alpha") and old_var.get()):
                out.append(v["id"])
        return out

    def open_instance_dialog(inst):
        editing = inst is not None
        dlg = tk.Toplevel(root)
        dlg.title("Editar instancia" if editing else "Nueva instancia")
        dlg.configure(bg=BG)
        dlg.transient(root)
        dw, dh = px(640), min(px(800), root.winfo_screenheight() - px(90))
        dlg.geometry(f"{dw}x{dh}+{root.winfo_x() + px(90)}+{max(0, root.winfo_y() + px(10))}")
        try:
            dlg.grab_set()
        except tk.TclError:
            pass

        choices = mc_choices()
        latest = (state["manifest"] or {}).get("latest", {}).get("release", "")
        d = inst or new_instance("", latest or (choices[0] if choices else ""),
                                 ram_mb=min(4096, max_ram))
        name_v = tk.StringVar(value=d["name"])
        mc_v = tk.StringVar(value=d["mc"])
        loader_v = tk.StringVar(value=LOADER_LABELS[LOADER_KEYS.index(d["loader"])])
        lv_v = tk.StringVar(value=d.get("loader_version") or ("Automática" if editing else ""))
        ram_v = tk.IntVar(value=min(int(d.get("ram_mb", 4096)), max_ram))
        ramt_v = tk.StringVar()
        preset_v = tk.StringVar(value=d.get("jvm_preset", "Por defecto"))
        args_v = tk.StringVar(value=d.get("jvm_args", ""))
        w_v = tk.StringVar(value=str(d["width"]) if d.get("width") else "")
        h_v = tk.StringVar(value=str(d["height"]) if d.get("height") else "")
        full_v = tk.BooleanVar(value=bool(d.get("fullscreen")))
        java_v = tk.StringVar(value=d.get("java_path", ""))
        lv_vals = {"list": [], "tok": 0}

        footer = tk.Frame(dlg, bg=BG)
        footer.pack(side="bottom", fill="x", padx=px(24), pady=px(16))
        sf = ScrollFrame(dlg, BG)
        sf.pack(fill="both", expand=True, padx=px(24), pady=(px(20), 0))
        f = sf.inner

        def lbl(text):
            tk.Label(f, text=text, font=(FONT, 8, "bold"), fg=MUTED, bg=BG).pack(
                anchor="w", pady=(px(12), px(4)))

        def entry(var, **kw):
            e = tk.Entry(f, textvariable=var, font=(FONT, 11), bg=CARD2, fg=FG, insertbackground=FG,
                         relief="flat", highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=ACCENT, **kw)
            e.pack(fill="x", ipady=px(6))
            return e

        lbl("NOMBRE")
        entry(name_v)
        lbl("VERSIÓN DE MINECRAFT")
        mc_box = ttk.Combobox(f, textvariable=mc_v, values=choices, state="disabled" if editing else "readonly",
                              font=(FONT, 11))
        mc_box.pack(fill="x", ipady=px(3))
        lbl("CARGADOR DE MODS")
        ld_box = ttk.Combobox(f, textvariable=loader_v, values=LOADER_LABELS,
                              state="disabled" if editing else "readonly", font=(FONT, 11))
        ld_box.pack(fill="x", ipady=px(3))
        lbl("VERSIÓN DEL CARGADOR")
        lv_box = ttk.Combobox(f, textvariable=lv_v, values=[], state="disabled", font=(FONT, 11))
        lv_box.pack(fill="x", ipady=px(3))

        def load_lv(*_):
            if editing:
                return
            kind = LOADER_KEYS[LOADER_LABELS.index(loader_v.get())]
            lv_vals["tok"] += 1
            tok = lv_vals["tok"]
            lv_vals["list"] = []
            if kind == "vanilla":
                lv_box.configure(values=[], state="disabled")
                lv_v.set("")
                return
            lv_box.configure(values=[], state="disabled")
            lv_v.set("Buscando versiones...")
            mc = mc_v.get()

            def job():
                try:
                    vs = loader_versions(kind, mc)
                except Exception:
                    vs = None
                msgs.put(("lv", tok, vs))
            threading.Thread(target=job, daemon=True).start()

            def cb(t, vs):
                if t != lv_vals["tok"] or not dlg.winfo_exists():
                    return
                if vs is None:
                    lv_v.set("No se pudo consultar (¿sin internet?)")
                elif not vs:
                    lv_v.set("No disponible para esta versión")
                else:
                    lv_vals["list"] = vs
                    lv_box.configure(values=vs, state="readonly")
                    lv_v.set(vs[0])
            state["lv_cb"] = cb

        ld_box.bind("<<ComboboxSelected>>", load_lv)
        mc_box.bind("<<ComboboxSelected>>", load_lv)

        lbl("MEMORIA RAM")
        rr = tk.Frame(f, bg=BG)
        rr.pack(fill="x")

        def on_ram(_=None):
            v = int(round(float(ram_v.get()) / 256) * 256)
            ram_v.set(v)
            ramt_v.set(f"{v / 1024:.1f} GB")

        ttk.Scale(rr, from_=1024, to=max_ram, variable=ram_v, command=on_ram,
                  style="Nova.Horizontal.TScale").pack(side="left", fill="x", expand=True)
        tk.Label(rr, textvariable=ramt_v, width=8, font=(FONT, 11, "bold"), fg=FG, bg=BG).pack(
            side="left", padx=(px(12), 0))
        on_ram()

        lbl("OPTIMIZACIÓN DE JAVA")
        ttk.Combobox(f, textvariable=preset_v, values=list(JVM_PRESETS), state="readonly",
                     font=(FONT, 11)).pack(fill="x", ipady=px(3))
        lbl("ARGUMENTOS JVM EXTRA (opcional)")
        entry(args_v)
        lbl("RESOLUCIÓN DEL JUEGO (vacío = automática)")
        res = tk.Frame(f, bg=BG)
        res.pack(fill="x")
        for var in (w_v, h_v):
            tk.Entry(res, textvariable=var, width=8, font=(FONT, 11), bg=CARD2, fg=FG,
                     insertbackground=FG, relief="flat", highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=ACCENT).pack(
                side="left", ipady=px(6), padx=(0, px(6)))
            if var is w_v:
                tk.Label(res, text="×", fg=MUTED, bg=BG, font=(FONT, 11)).pack(side="left", padx=(0, px(6)))
        tk.Checkbutton(res, text="Pantalla completa", variable=full_v, fg=FG, bg=BG, selectcolor=CARD2,
                       activebackground=BG, activeforeground=FG, font=(FONT, 10), bd=0,
                       highlightthickness=0).pack(side="left", padx=px(14))
        lbl("JAVA PERSONALIZADO (opcional)")
        jr = tk.Frame(f, bg=BG)
        jr.pack(fill="x")
        tk.Entry(jr, textvariable=java_v, font=(FONT, 11), bg=CARD2, fg=FG, insertbackground=FG,
                 relief="flat", highlightthickness=1, highlightbackground=LINE,
                 highlightcolor=ACCENT).pack(side="left", fill="x", expand=True, ipady=px(6))

        def browse():
            p = filedialog.askopenfilename(parent=dlg, title="Elegí java.exe")
            if p:
                java_v.set(p)

        flat_btn(jr, "Buscar...", browse).pack(side="left", padx=(px(8), 0), ipadx=px(10), ipady=px(6))
        tk.Frame(f, bg=BG, height=px(10)).pack()

        def save():
            name = name_v.get().strip()
            if not name:
                messagebox.showwarning(APP_NAME, "Poné un nombre para la instancia.", parent=dlg)
                return
            try:
                w = int(w_v.get()) if w_v.get().strip() else 0
                h = int(h_v.get()) if h_v.get().strip() else 0
            except ValueError:
                messagebox.showwarning(APP_NAME, "La resolución tiene que ser numérica.", parent=dlg)
                return
            kind = LOADER_KEYS[LOADER_LABELS.index(loader_v.get())]
            if not editing:
                if not mc_v.get():
                    messagebox.showwarning(APP_NAME, "Elegí una versión de Minecraft.", parent=dlg)
                    return
                if kind != "vanilla" and not lv_vals["list"]:
                    messagebox.showwarning(
                        APP_NAME, f"{LOADER_NAMES[kind]} no está disponible para Minecraft "
                                  f"{mc_v.get()} (o no hay internet).", parent=dlg)
                    return
            target = d if editing else new_instance(name, mc_v.get(), kind,
                                                    lv_v.get() if kind != "vanilla" else "")
            target["name"] = unique_name(name, instances(), target["id"])
            target.update(ram_mb=int(ram_v.get()), jvm_preset=preset_v.get(), jvm_args=args_v.get().strip(),
                          width=w, height=h, fullscreen=full_v.get(), java_path=java_v.get().strip())
            if not editing:
                instances().append(target)
                cfg["last_instance"] = target["id"]
            save_all()
            refresh_all()
            dlg.destroy()

        flat_btn(footer, "Cancelar", dlg.destroy, size=10).pack(side="right", padx=(px(8), 0),
                                                                 ipadx=px(16), ipady=px(8))
        flat_btn(footer, "Guardar" if editing else "Crear instancia", save, bg=ACCENT, hover=ACCENT_H,
                 fg="white", size=10).pack(side="right", ipadx=px(18), ipady=px(8))
        load_lv()

    # ================================================================== jugar
    def worker(inst, username):
        launcher = Launcher(
            log=lambda t: msgs.put(("log", t)),
            progress=lambda dn, t, l: msgs.put(("progress", dn, t, l)),
            on_change=lambda: msgs.put(("save",)),
        )
        try:
            proc = launcher.launch(inst, username, state["manifest"])
            msgs.put(("started", inst["name"]))
            t0 = time.time()
            for line in proc.stdout:
                msgs.put(("log", line))
            code = proc.wait()
            msgs.put(("played", inst["id"], int(time.time() - t0)))
            msgs.put(("log", f"El juego se cerró (código {code})."))
            msgs.put(("status", "Listo" if code == 0 else f"El juego terminó con código {code}"))
            if code != 0:
                msgs.put(("crash", code))
        except Exception as e:
            msgs.put(("log", f"ERROR: {e}"))
            msgs.put(("error", str(e)))
        finally:
            msgs.put(("idle",))

    def on_play():
        if state["busy"]:
            return
        inst = cur()
        if not inst:
            messagebox.showinfo(APP_NAME, "Primero creá una instancia.")
            show_page("instances")
            return
        name = user_var.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name):
            messagebox.showwarning(APP_NAME, "El usuario debe tener entre 3 y 16 caracteres "
                                             "(letras, números o guion bajo).")
            show_page("settings")
            return
        save_all()
        state["busy"] = True
        play_btn.set_state("Preparando…", False)
        status_var.set("Preparando...")
        threading.Thread(target=worker, args=(inst, name), daemon=True).start()

    play_btn.command = on_play

    def load_versions():
        try:
            state["manifest"] = load_manifest()
            msgs.put(("manifest",))
            msgs.put(("net", "mojang", True))
        except Exception as e:
            msgs.put(("log", f"No se pudo cargar la lista de versiones: {e}"))
            msgs.put(("status", "Sin conexión con Mojang"))
            msgs.put(("net", "mojang", False))
        try:
            fetch_json(MODRINTH, timeout=10)
            msgs.put(("net", "modrinth", True))
        except Exception:
            msgs.put(("net", "modrinth", False))

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
                    _, dn, t, label = m
                    bar["maximum"] = max(t, 1)
                    bar["value"] = dn
                    if label:
                        status_var.set(f"Descargando {label.lower()}:  {dn} / {t}")
                elif kind == "manifest":
                    if not instances():
                        latest = (state["manifest"] or {}).get("latest", {}).get("release")
                        if latest:
                            first = new_instance(f"Minecraft {latest}", latest, ram_mb=min(4096, max_ram))
                            instances().append(first)
                            cfg["last_instance"] = first["id"]
                            save_all()
                    refresh_all()
                    status_var.set("Listo para jugar")
                elif kind == "net":
                    state["net"][m[1]] = m[2]
                    draw_scene()
                elif kind == "lv":
                    if state["lv_cb"]:
                        state["lv_cb"](m[1], m[2])
                elif kind == "save":
                    save_all()
                    refresh_all()
                elif kind == "played":
                    i = get_inst(m[1])
                    if i:
                        i["playtime"] = int(i.get("playtime", 0)) + m[2]
                        save_all()
                        refresh_all()
                elif kind == "crash":
                    status_var.set(f"El juego se cerró con error (código {m[1]}). Revisá la consola.")
                    show_page("console")
                elif kind == "mods":
                    show_hits(m[1], m[2])
                elif kind == "modinst":
                    if isinstance(m[2], Exception):
                        set_mstat(f"Error instalando {m[1]}: {m[2]}", True)
                        append_log(f"[Tienda] Error instalando {m[1]}: {m[2]}")
                    elif not m[2]:
                        set_mstat(f"{m[1]}: no hay una versión compatible con esta instancia.", True)
                    else:
                        set_mstat(f"Instalado: {m[1]} ({len(m[2])} archivo(s), con dependencias)")
                        append_log("Instalado: " + ", ".join(m[2]))
                elif kind == "quick":
                    finish_quick(m[1], m[2], m[3])
                elif kind == "started":
                    bar["value"] = 0
                    state["playing"] = m[1]
                    play_btn.set_state("Jugando", False)
                    status_var.set(f"{m[1]} en ejecución")
                    draw_scene()
                    if con_var.get():
                        show_page("console")
                    if min_var.get():
                        root.iconify()
                elif kind == "error":
                    status_var.set("Error: revisá la consola")
                    show_page("console")
                elif kind == "idle":
                    state["busy"] = False
                    state["playing"] = ""
                    bar["value"] = 0
                    play_btn.set_state("▶  Jugar", True)
                    draw_scene()
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
    refresh_all()
    show_page("play")
    pump()
    threading.Thread(target=load_versions, daemon=True).start()
    append_log(f"Datos guardados en: {ROOT}")
    root.mainloop()


if __name__ == "__main__":
    run_gui()
