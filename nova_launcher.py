#!/usr/bin/env python3
"""
NovaLauncher 0.7 - launcher de Minecraft Java Edition en un solo archivo.

Novedades de la 0.7:
  * Cuentas con contraseña (locales de este PC) y roles: administrador / usuario
  * Panel de administración: cuentas, permisos, actividad, sistema

Novedades de la 0.6:
  * Pantalla de inicio nocturna con novedades, estado de servicios y tus instancias
  * Asistente IA (Claude): recomienda mods y explica los errores de la consola

Novedades de la 0.5:
  * Interfaz nueva: pantalla de inicio con paisaje, barra lateral de iconos y barra de juego
  * Tienda de mods, texturas y shaders (Modrinth) con cuadrícula; instancia Vanilla -> Fabric en 1 clic

Novedades de la 0.4:
  * Buscador e instalador de mods (Modrinth), con dependencias automáticas
  * Duplicar instancias, tiempo jugado, colores por instancia
  * Detección de cierres con error del juego

Novedades de la 0.3:
  * Instancias: varias versiones/instalaciones a la vez, cada una con sus mods,
    mundos, RAM y opciones propias
  * Mod loaders: Fabric, Quilt, Forge y NeoForge
  * Más opciones: resolución del juego, pantalla completa, argumentos JVM,
    perfil de rendimiento, Java personalizado
  * Interfaz más grande y nítida (soporte de pantallas de alta resolución)

Requisitos: Python 3.8+ (con tkinter, ya viene en el instalador de python.org)
Uso:        python nova_launcher.py
"""

import glob
import hashlib
import hmac
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
APP_VERSION = "0.7"
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
# Asistente IA (API de Anthropic - usa TU clave, se guarda solo en tu PC)
# --------------------------------------------------------------------------
AI_URL = "https://api.anthropic.com/v1/messages"
AI_MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5"]


def post_json(url, payload, headers, timeout=90):
    data = json.dumps(payload).encode("utf-8")
    hdr = {"Content-Type": "application/json", "User-Agent": UA}
    hdr.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        raise RuntimeError(f"{e.code}: {msg}" if msg else f"{e.code}: el servicio respondió con error") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"No hay conexión ({e.reason})") from None


def ai_chat(api_key, model, system, messages):
    out = post_json(AI_URL, {"model": model, "max_tokens": 1200, "system": system,
                             "messages": messages},
                    {"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    return text.strip() or "(respuesta vacía)"


def ai_system_prompt(inst, mods):
    lines = ["Sos el asistente de NovaLauncher, un launcher de Minecraft Java Edition. "
             "Respondé en español rioplatense, claro, corto y con pasos concretos. "
             "Si te pasan la consola del launcher, buscá la causa más probable del error y explicá cómo "
             "arreglarlo (RAM, versión de Java, mods incompatibles, falta de dependencias como Fabric API, etc.). "
             "No inventes mods ni versiones: si no estás seguro de que algo exista para esa versión, decilo y "
             "sugerí buscarlo en la pestaña Tienda del launcher. No uses tablas."]
    if inst:
        lines.append(f"Instancia actual del jugador: {inst.get('name')} | Minecraft {inst.get('mc')} | "
                     f"cargador: {LOADER_NAMES.get(inst.get('loader'), inst.get('loader'))} "
                     f"{inst.get('loader_version') or ''} | RAM asignada: {inst.get('ram_mb', 4096)} MB.")
        if mods:
            lines.append("Mods instalados: " + ", ".join(mods[:60]))
        elif inst.get("loader") != "vanilla":
            lines.append("Todavía no tiene mods instalados.")
    return "\n".join(lines)


def clean_md(text):
    out = []
    for ln in text.splitlines():
        ln = ln.replace("**", "").replace("__", "").replace("`", "")
        ln = re.sub(r"^\s{0,3}#{1,6}\s*", "", ln)
        ln = re.sub(r"^(\s*)[-*]\s+", r"\1• ", ln)
        out.append(ln)
    return "\n".join(out).strip()


def installed_mod_names(inst):
    if not inst:
        return []
    d = os.path.join(game_dir(inst), "mods")
    try:
        return sorted(f for f in os.listdir(d) if f.endswith(".jar"))
    except OSError:
        return []


# --------------------------------------------------------------------------
# Lanzador
# --------------------------------------------------------------------------
class Launcher:
    def __init__(self, log, progress, on_change=None, ram_cap=0):
        self.log = log
        self.progress = progress
        self.on_change = on_change or (lambda: None)
        self.ram_cap = int(ram_cap or 0)

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
        if self.ram_cap > 0:
            ram = min(ram, self.ram_cap)
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
                  "minimize": False, "show_console": False, "ui_scale": 0, "instances": [],
                  "ai_key": "", "ai_model": "claude-haiku-5-5",
                  "accounts": [], "remember": "", "allow_signup": True, "audit": []}


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
    cfg["accounts"] = []
    cfg["audit"] = []
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


PALETTE = ["#5b8cff", "#2fbf71", "#e0a030", "#c0589a", "#38b6c9", "#9b6bff", "#e0603c"]


def accent_for(iid):
    return PALETTE[sum(map(ord, iid)) % len(PALETTE)]


def fmt_time(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "Sin jugar" if sec == 0 else "< 1 min"
    h, m = divmod(sec // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


# --------------------------------------------------------------------------
# Cuentas locales (contraseñas con PBKDF2; los datos viven solo en este PC)
# --------------------------------------------------------------------------
USER_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
PW_MIN = 6
PBKDF2_ITERS = 200_000


def hash_pw(password, salt=None):
    salt = salt or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERS).hex()
    return salt, h


def check_pw(acct, password):
    try:
        _, h = hash_pw(password or "", acct.get("salt", ""))
    except ValueError:
        return False
    return hmac.compare_digest(h, acct.get("hash", ""))


def find_account(accounts, user):
    u = (user or "").strip().lower()
    return next((a for a in accounts if a.get("user", "").lower() == u), None)


def validate_new_account(accounts, user, password):
    if not USER_RE.match(user or ""):
        return "El usuario debe tener 3 a 16 caracteres (letras, números o guion bajo)."
    if find_account(accounts, user):
        return "Ese usuario ya existe."
    if len(password or "") < PW_MIN:
        return f"La contraseña debe tener al menos {PW_MIN} caracteres."
    return ""


def new_account(user, password, role="user"):
    salt, h = hash_pw(password)
    return {"user": user, "salt": salt, "hash": h, "role": role, "enabled": True, "mc_name": user,
            "created": time.strftime("%Y-%m-%d %H:%M"), "last_login": "", "playtime": 0,
            "perms": {"store": True, "ai": True, "console": True, "ram_mb": 0}}


def admin_count(accounts):
    return sum(1 for a in accounts if a.get("role") == "admin" and a.get("enabled", True))


def is_last_admin(accounts, acct):
    return acct.get("role") == "admin" and acct.get("enabled", True) and admin_count(accounts) <= 1


def audit_add(cfg, user, event, detail=""):
    log = cfg.setdefault("audit", [])
    log.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "u": user, "e": event, "d": detail})
    del log[:-300]


def dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


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
    from tkinter import ttk, messagebox, filedialog, simpledialog

    BG, SIDE, CARD, CARD2, LINE = "#0d0f14", "#11141b", "#171b25", "#202636", "#262c3b"
    FG, MUTED, ACCENT = "#e9ecf4", "#8a90a2", "#5b8cff"
    GREEN, GREEN_H, DIS = "#2fbf71", "#3bd983", "#262b38"
    RED, RED_H = "#c0455a", "#e0556c"
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
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    class RoundButton(tk.Canvas):
        """Botón con esquinas redondeadas."""

        def __init__(self, parent, text, command, color, hover, parent_bg,
                     font=(FONT, 15, "bold"), height=54, radius=14, fg="white"):
            super().__init__(parent, height=px(height), bg=parent_bg, highlightthickness=0,
                             bd=0, cursor="hand2")
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
            x1, y1, x2, y2 = 1, 1, w - 1, h - 1
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                   x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            self.create_polygon(pts, smooth=True, fill=fill, outline=fill)
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
    style.configure("Nova.Horizontal.TProgressbar", troughcolor=CARD, background=ACCENT,
                    bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=px(8))
    style.configure("Nova.Horizontal.TScale", background=CARD, troughcolor=CARD2)
    root.option_add("*TCombobox*Listbox.background", CARD2)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.font", (FONT, 11))

    msgs = queue.Queue()
    state = {"manifest": None, "busy": False, "page": "play", "lv_cb": None, "acct": None}
    max_ram = max(2048, min(32768, int(total_ram_mb() * 0.75)))

    user_var = tk.StringVar(value=cfg["username"])
    chip_var = tk.StringVar(value="")
    snap_var = tk.BooleanVar(value=cfg["snapshots"])
    old_var = tk.BooleanVar(value=cfg["old"])
    min_var = tk.BooleanVar(value=cfg["minimize"])
    con_var = tk.BooleanVar(value=cfg["show_console"])
    scale_var = tk.StringVar(value=SCALES[0] if not us else f"{us}%")
    sel_var = tk.StringVar()
    status_var = tk.StringVar(value="Cargando versiones...")
    ram_text = tk.StringVar(value="—")
    loader_text = tk.StringVar(value="—")
    time_text = tk.StringVar(value="—")
    mode_var = tk.StringVar(value="Offline")

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

    def is_admin():
        a = state.get("acct")
        return bool(a and a.get("role") == "admin")

    def allowed(perm):
        a = state.get("acct")
        if not a:
            return False
        return is_admin() or bool((a.get("perms") or {}).get(perm, True))

    def audit_event(event, detail=""):
        audit_add(cfg, (state.get("acct") or {}).get("user", "-"), event, detail)

    # ================================================================== barra superior
    DARK = "#0a0c11"
    nav, pages = {}, {}

    def style_nav(name, hover=False):
        return None

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

    topbar = tk.Frame(root, bg=DARK, height=px(56))
    topbar.pack(side="top", fill="x")
    topbar.pack_propagate(False)
    brand = tk.Frame(topbar, bg=DARK)
    brand.pack(side="left", padx=px(18))
    tk.Label(brand, text="N", font=(FONT, 14, "bold"), fg="white", bg=ACCENT).pack(
        side="left", ipadx=px(9), ipady=px(1))
    tk.Label(brand, text="NOVA LAUNCHER", font=(FONT, 11, "bold"), fg=FG, bg=DARK).pack(
        side="left", padx=(px(10), 0))
    tk.Label(brand, text=f"v{APP_VERSION}", font=(FONT, 8), fg=MUTED, bg=DARK).pack(
        side="left", padx=(px(8), 0), pady=(px(3), 0))
    back_btn = flat_btn(topbar, "←  Inicio", lambda: show_page("play"), bg=CARD, hover=CARD2, size=10)
    right = tk.Frame(topbar, bg=DARK)
    right.pack(side="right", padx=px(12))
    for txt, fn in (("▣ Datos", lambda: open_folder(ROOT)), ("≡ Consola", lambda: show_page("console")),
                    ("⚙ Ajustes", lambda: show_page("settings"))):
        flat_btn(right, txt, fn, bg=DARK, hover=CARD2, size=10).pack(
            side="right", padx=px(2), ipadx=px(10), ipady=px(6))
    chip = tk.Button(right, textvariable=chip_var, font=(FONT, 10, "bold"), bg=ACCENT, fg="white",
                     activebackground="#7ba3ff", activeforeground="white", relief="flat", bd=0,
                     cursor="hand2", command=lambda: show_page("settings"))
    chip.pack(side="right", padx=(px(10), px(8)), ipadx=px(14), ipady=px(5))
    logout_btn = flat_btn(right, "Salir", lambda: logout(), bg=DARK, hover=CARD2, size=10)
    logout_btn.pack(side="right", padx=px(2), ipadx=px(10), ipady=px(6))
    admin_btn = flat_btn(right, "★ Admin", lambda: show_page("admin"), bg="#3a2230", hover="#55304a",
                         fg="#ff9fb4", size=10)

    content = tk.Frame(root, bg=BG)
    content.pack(fill="both", expand=True)

    def card(parent, **kw):
        return tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE, **kw)

    def small_title(parent, text, bg=CARD):
        return tk.Label(parent, text=text, font=(FONT, 8, "bold"), fg=MUTED, bg=bg)

    def page_title(parent, text):
        return tk.Label(parent, text=text, font=(FONT, 22, "bold"), fg=FG, bg=BG)

    # ================================================================== INICIO
    HC, HL = "#0f1424", "#26304f"
    play = tk.Frame(content, bg=BG)
    pages["play"] = play
    scene = tk.Canvas(play, bg=BG, highlightthickness=0, bd=0)
    scene.place(x=0, y=0, relwidth=1.0, relheight=1.0)
    status_vals = {"mojang": None, "modrinth": None}

    def heights(w, seed, base, amp, step):
        r = random.Random(seed)
        p1, p2, p3 = r.random() * 6, r.random() * 6, r.random() * 6
        out = []
        for i in range(w // step + 2):
            v = 0.55 * math.sin(i * 0.17 + p1) + 0.3 * math.sin(i * 0.41 + p2) + 0.15 * math.sin(i * 0.93 + p3)
            out.append(int((base + amp * v) // step * step))
        return out

    def draw_night(cv):
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 60 or h < 60:
            return
        cv.delete("all")
        for y in range(0, h, 3):
            cv.create_line(0, y, w, y, fill=blend("#04050d", "#18275a", (y / h) ** 0.9), width=3)
        rng = random.Random(21)
        for _i in range(160):                      # vía láctea
            t = rng.random()
            x = w * (0.05 + 0.95 * t) + rng.gauss(0, w * 0.045)
            y = h * (0.80 - 0.62 * t) + rng.gauss(0, h * 0.06)
            z = px(rng.choice([1, 2, 2, 3]))
            cv.create_rectangle(x, y, x + z, y + z, fill=blend("#18275a", "#9fb2ff", 0.25 + rng.random() * 0.5),
                                   outline="")
        for _i in range(230):                      # estrellas
            x, y, z = rng.randrange(w), rng.randrange(int(h * 0.7)), px(rng.choice([1, 1, 2]))
            cv.create_rectangle(x, y, x + z, y + z, fill=blend("#18275a", "#ffffff", 0.35 + rng.random() * 0.65),
                                   outline="")
        mx, my, mr = int(w * 0.80), int(h * 0.60), px(30)
        for grow, col in ((2.0, "#141d45"), (1.45, "#1d2a5c"), (1.0, "#e9eefc")):
            rr = int(mr * grow)
            cv.create_rectangle(mx - rr, my - rr, mx + rr, my + rr, fill=col, outline="")
        cv.create_rectangle(mx - mr // 3, my - mr // 2, mx + mr // 4, my - mr // 6, fill="#c3cbe6", outline="")
        step = px(24)
        for seed, base, amp, col in ((4, 0.80, 0.05, "#0d1534"), (9, 0.86, 0.04, "#080d22")):
            hs = heights(w, seed, h * base, h * amp, step)
            for i, hy in enumerate(hs):
                cv.create_rectangle(i * step, hy, i * step + step, h, fill=col, outline="")
                if seed == 9 and i % 8 == 3:
                    cv.create_rectangle(i * step + step * 0.35, hy - step * 2, i * step + step * 0.65, hy,
                                           fill="#05081a", outline="")
                    cv.create_rectangle(i * step - step, hy - step * 4, i * step + step * 2, hy - step * 2,
                                           fill="#060b1f", outline="")
                    cv.create_rectangle(i * step, hy - step * 5, i * step + step, hy - step * 4,
                                           fill="#060b1f", outline="")

    def draw_scene(_=None):
        draw_night(scene)

    draw_hero = draw_scene
    scene.bind("<Configure>", draw_scene)

    def hub_card(**place):
        f = tk.Frame(play, bg=HC, highlightthickness=1, highlightbackground=HL)
        f.place(**place)
        return f

    def hub_title(parent, text):
        tk.Label(parent, text=text, font=(FONT, 13, "bold"), fg=FG, bg=HC, anchor="w").pack(
            fill="x", padx=px(20), pady=(px(14), px(8)))

    news = hub_card(x=px(30), y=px(20), relwidth=0.60, height=px(250))
    hub_title(news, "Últimas novedades de Minecraft")
    news_body = tk.Frame(news, bg=HC)
    news_body.pack(fill="both", expand=True, padx=px(20), pady=(0, px(10)))

    stat = hub_card(relx=0.64, y=px(20), relwidth=0.33, height=px(250))
    hub_title(stat, "Estado de NovaLauncher")
    stat_body = tk.Frame(stat, bg=HC)
    stat_body.pack(fill="both", expand=True, padx=px(20), pady=(0, px(10)))

    mine = hub_card(x=px(30), rely=1.0, y=-px(122), anchor="sw", relwidth=0.60, height=px(150))
    hub_title(mine, "Tus instancias")
    mine_body = tk.Frame(mine, bg=HC)
    mine_body.pack(fill="both", expand=True, padx=px(20), pady=(0, px(8)))

    def create_vanilla(vid):
        ni = new_instance(unique_name(f"Minecraft {vid}", instances()), vid, ram_mb=min(4096, max_ram))
        instances().append(ni)
        cfg["last_instance"] = ni["id"]
        save_all()
        refresh_all()

    def dot_row(parent, label, value, color):
        r = tk.Frame(parent, bg=HC)
        r.pack(fill="x", pady=px(3))
        tk.Label(r, text="●", font=(FONT, 9), fg=color, bg=HC).pack(side="left")
        tk.Label(r, text=label, font=(FONT, 10), fg=FG, bg=HC).pack(side="left", padx=(px(8), 0))
        tk.Label(r, text=value, font=(FONT, 10, "bold"), fg=color, bg=HC).pack(side="right")

    def refresh_home():
        for w_ in news_body.winfo_children():
            w_.destroy()
        m = state["manifest"]
        if not m:
            tk.Label(news_body, text="Cargando versiones...", font=(FONT, 10), fg=MUTED, bg=HC).pack(pady=px(24))
        else:
            for v in m["versions"][:4]:
                row = tk.Frame(news_body, bg=HC)
                row.pack(fill="x", pady=px(3))
                snap = v["type"] != "release"
                tk.Label(row, text="SNAPSHOT" if snap else "RELEASE", font=(FONT, 8, "bold"), fg="white",
                         bg="#c08a2c" if snap else GREEN, width=10).pack(side="left", ipady=px(3))
                col = tk.Frame(row, bg=HC)
                col.pack(side="left", fill="x", expand=True, padx=px(12))
                tk.Label(col, text=f"Minecraft {v['id']}", font=(FONT, 11, "bold"), fg=FG, bg=HC,
                         anchor="w").pack(fill="x")
                tk.Label(col, text=(v.get("releaseTime") or "")[:10], font=(FONT, 8), fg=MUTED, bg=HC,
                         anchor="w").pack(fill="x")
                flat_btn(row, "Crear instancia", lambda vid=v["id"]: create_vanilla(vid), bg=CARD2,
                         hover=LINE).pack(side="right", ipadx=px(10), ipady=px(4))
        for w_ in stat_body.winfo_children():
            w_.destroy()
        names = {None: ("Comprobando...", MUTED), True: ("En línea", GREEN), False: ("Sin conexión", "#ff6b81")}
        dot_row(stat_body, "Mojang · versiones", *names[status_vals["mojang"]])
        dot_row(stat_body, "Modrinth · tienda", *names[status_vals["modrinth"]])
        has_key = bool((cfg.get("ai_key") or "").strip())
        dot_row(stat_body, "Asistente IA", "Listo" if has_key else "Falta la clave", GREEN if has_key else "#e0a030")
        dot_row(stat_body, "Instancias", str(len(instances())), FG)
        dot_row(stat_body, "Tiempo jugado", fmt_time(sum(int(i.get("playtime", 0)) for i in instances())), FG)
        dot_row(stat_body, "RAM del PC", f"{total_ram_mb() / 1024:.0f} GB", FG)
        for w_ in mine_body.winfo_children():
            w_.destroy()
        if not instances():
            tk.Label(mine_body, text="Todavía no tenés instancias. Creá una desde las novedades.",
                     font=(FONT, 10), fg=MUTED, bg=HC).pack(pady=px(14))
        for inst in instances()[:3]:
            row = tk.Frame(mine_body, bg=HC)
            row.pack(fill="x", pady=px(2))
            tk.Frame(row, bg=accent_for(inst["id"]), width=px(5)).pack(side="left", fill="y", padx=(0, px(10)))
            tk.Label(row, text=inst["name"], font=(FONT, 10, "bold"), fg=FG, bg=HC, anchor="w").pack(side="left")
            tk.Label(row, text="   " + describe(inst), font=(FONT, 9), fg=MUTED, bg=HC).pack(side="left")
            flat_btn(row, "Jugar", lambda i=inst["id"]: play_instance(i), bg=GREEN, hover=GREEN_H, fg="white").pack(
                side="right", ipadx=px(12), ipady=px(3))

    def open_store(ptype):
        mods_state["ptype"] = ptype
        mods_state["for"] = None
        show_page("mods")

    bottom = tk.Frame(play, bg=DARK)
    bottom.place(x=0, rely=1.0, relwidth=1.0, anchor="sw", height=px(108))
    prog = tk.Frame(bottom, bg=DARK)
    prog.pack(fill="x", padx=px(24), pady=(px(10), 0))
    tk.Label(prog, textvariable=status_var, font=(FONT, 9), fg=MUTED, bg=DARK, anchor="w", width=34).pack(side="left")
    bar = ttk.Progressbar(prog, mode="determinate", style="Nova.Horizontal.TProgressbar")
    bar.pack(side="left", fill="x", expand=True)
    ctrl = tk.Frame(bottom, bg=DARK)
    ctrl.pack(fill="x", padx=px(24), pady=(px(8), 0))
    inst_box = ttk.Combobox(ctrl, textvariable=sel_var, state="readonly", font=(FONT, 11), width=20)
    inst_box.pack(side="left", ipady=px(5))
    for txt, fn in (("Mods", lambda: open_store("mod")), ("Texturas", lambda: open_store("resourcepack")),
                    ("Instancias", lambda: show_page("instances")), ("✧ Asistente IA", lambda: show_page("ai"))):
        flat_btn(ctrl, txt, fn, bg=CARD2, hover=LINE, size=10).pack(
            side="left", padx=(px(8), 0), ipadx=px(12), ipady=px(9))
    play_btn = RoundButton(ctrl, "JUGAR", lambda: on_play(), GREEN, GREEN_H, DARK, height=50)
    play_btn.configure(width=px(230))
    play_btn.pack(side="right")

    # ================================================================== INSTANCIAS
    inst_page = tk.Frame(content, bg=BG)
    pages["instances"] = inst_page
    ih = tk.Frame(inst_page, bg=BG)
    ih.pack(fill="x", padx=px(36), pady=(px(28), px(14)))
    page_title(ih, "Instancias").pack(side="left")
    RoundButton(ih, "＋  Nueva instancia", lambda: open_instance_dialog(None), ACCENT, "#7ba3ff", BG,
                font=(FONT, 11, "bold"), height=42, radius=12).pack(side="right", ipadx=px(26))
    tk.Label(inst_page, text="Cada instancia tiene su propia versión, mods, mundos y opciones.",
             font=(FONT, 10), fg=MUTED, bg=BG).pack(anchor="w", padx=px(38), pady=(0, px(10)))
    ilist = ScrollFrame(inst_page, BG)
    ilist.pack(fill="both", expand=True, padx=(px(36), px(20)), pady=(0, px(20)))

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
            for txt, fn in (("Editar", lambda i=iid: on_inst(i, open_instance_dialog)),
                            ("Mods", lambda i=iid: open_mods_for(i)),
                            ("Duplicar", lambda i=iid: on_inst(i, duplicate_instance)),
                            ("Carpeta", lambda i=iid: on_inst(i, lambda x: open_folder(game_dir(x))))):
                flat_btn(right, txt, fn, bg=CARD2, hover=LINE).pack(
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
        audit_event("instancia eliminada", inst["name"])
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

    # ================================================================== TIENDA
    mods_page = tk.Frame(content, bg=BG)
    pages["mods"] = mods_page
    mt = tk.Frame(mods_page, bg=BG)
    mt.pack(fill="x", padx=px(36), pady=(px(26), px(4)))
    page_title(mt, "Tienda").pack(side="left")
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
    flat_btn(mrow, "Buscar", lambda: mods_search(), bg=ACCENT, hover="#7ba3ff", fg="white", size=10).pack(
        side="left", padx=(px(8), 0), ipadx=px(20), ipady=px(7))
    mods_status = tk.StringVar(value="")
    mods_status_lbl = tk.Label(mods_page, textvariable=mods_status, font=(FONT, 10), fg=MUTED, bg=BG,
                               anchor="w", justify="left", wraplength=px(820))
    mods_status_lbl.pack(fill="x", padx=px(38), pady=(0, px(6)))
    mods_list = ScrollFrame(mods_page, BG)
    mods_list.pack(fill="both", expand=True, padx=(px(36), px(20)), pady=(0, px(20)))
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
                     hover="#7ba3ff" if main else LINE, fg="white" if main else FG, size=10).pack(
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

    # ================================================================== ASISTENTE IA
    ai_page = tk.Frame(content, bg=BG)
    pages["ai"] = ai_page
    ai_state = {"history": [], "busy": False}
    ah = tk.Frame(ai_page, bg=BG)
    ah.pack(fill="x", padx=px(36), pady=(px(26), px(4)))
    page_title(ah, "Asistente IA").pack(side="left")
    model_v = tk.StringVar(value=cfg.get("ai_model") or AI_MODELS[0])
    mbox = ttk.Combobox(ah, textvariable=model_v, values=AI_MODELS, font=(FONT, 10), width=26)
    mbox.pack(side="right", ipady=px(2))
    tk.Label(ah, text="Modelo:", font=(FONT, 9), fg=MUTED, bg=BG).pack(side="right", padx=(0, px(8)))

    def on_model(_=None):
        cfg["ai_model"] = model_v.get().strip() or AI_MODELS[0]
        save_config(cfg)

    mbox.bind("<<ComboboxSelected>>", on_model)
    mbox.bind("<FocusOut>", on_model)
    tk.Label(ai_page, text="Preguntale por mods, crashes, RAM o versiones. Conoce tu instancia actual.",
             font=(FONT, 10), fg=MUTED, bg=BG, anchor="w").pack(fill="x", padx=px(38), pady=(0, px(10)))

    setup = card(ai_page)
    tk.Label(setup, text="Conectá tu clave de API de Anthropic", font=(FONT, 14, "bold"), fg=FG, bg=CARD).pack(
        anchor="w", padx=px(22), pady=(px(18), px(4)))
    tk.Label(setup, text="La IA funciona con tu propia clave: se saca en console.anthropic.com (API Keys). "
                         "Las consultas se cobran a tu cuenta de Anthropic (son centavos). La clave se guarda "
                         "solo en tu PC, dentro de la carpeta de datos del launcher; nunca la compartas ni la "
                         "subas a GitHub.", font=(FONT, 10), fg=MUTED, bg=CARD, wraplength=px(720),
             justify="left").pack(anchor="w", padx=px(22))
    key_v = tk.StringVar()
    krow = tk.Frame(setup, bg=CARD)
    krow.pack(fill="x", padx=px(22), pady=px(18))
    tk.Entry(krow, textvariable=key_v, show="•", font=(FONT, 11), bg=CARD2, fg=FG, insertbackground=FG,
             relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT).pack(
        side="left", fill="x", expand=True, ipady=px(7))

    def save_key():
        k = key_v.get().strip()
        if not k:
            return
        cfg["ai_key"] = k
        save_config(cfg)
        key_v.set("")
        ai_enter()
        refresh_home()

    flat_btn(krow, "Guardar clave", save_key, bg=ACCENT, hover="#7ba3ff", fg="white", size=10).pack(
        side="left", padx=(px(8), 0), ipadx=px(16), ipady=px(7))

    chat = tk.Frame(ai_page, bg=BG)
    chat_text = tk.Text(chat, bg=CARD, fg="#d5d9e6", font=(FONT, 10), relief="flat", wrap="word",
                        state="disabled", bd=0, padx=px(14), pady=px(12), highlightthickness=1,
                        highlightbackground=LINE, spacing3=px(6))
    chat_sb = tk.Scrollbar(chat, command=chat_text.yview)
    chat_text.configure(yscrollcommand=chat_sb.set)
    chat_text.tag_configure("user", foreground="#9fc0ff", font=(FONT, 10, "bold"))
    chat_text.tag_configure("bot", foreground="#e9ecf4")
    chat_text.tag_configure("err", foreground="#ff8a9b")
    chat_text.tag_configure("note", foreground=MUTED, font=(FONT, 9, "italic"))
    chips = tk.Frame(chat, bg=BG)
    chips.pack(side="bottom", fill="x", pady=(px(6), 0))
    irow = tk.Frame(chat, bg=BG)
    irow.pack(side="bottom", fill="x", pady=(px(8), 0))
    ai_in_v = tk.StringVar()
    ai_in = tk.Entry(irow, textvariable=ai_in_v, font=(FONT, 11), bg=CARD2, fg=FG, insertbackground=FG,
                     relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT)
    ai_in.pack(side="left", fill="x", expand=True, ipady=px(8))
    chat_sb.pack(side="right", fill="y")
    chat_text.pack(side="top", fill="both", expand=True)

    def ai_append(role, text):
        chat_text.config(state="normal")
        label = {"user": "Vos\n", "bot": "Asistente\n", "err": "Error\n", "note": ""}[role]
        if label:
            chat_text.insert("end", label, "user" if role == "user" else role)
        chat_text.insert("end", text + "\n\n", role if role != "user" else "bot")
        chat_text.see("end")
        chat_text.config(state="disabled")

    def friendly_ai_error(e):
        t = str(e)
        if t.startswith("401"):
            return "La clave de API no es válida. Tocá “Cambiar clave” y pegala de nuevo."
        if t.startswith("404"):
            return "Ese modelo no existe en tu cuenta. Elegí otro en el selector de arriba."
        if t.startswith("429") or "credit" in t.lower():
            return "Demasiadas consultas o te quedaste sin saldo en tu cuenta de Anthropic. " + t
        return t

    def ai_send(text, with_log=False):
        text = (text or "").strip()
        if not text or ai_state["busy"]:
            return
        key = (cfg.get("ai_key") or "").strip()
        if not key:
            ai_enter()
            return
        ai_append("user", text)
        body = text
        if with_log:
            tail = "\n".join(log_text.get("1.0", "end").splitlines()[-150:])[-12000:]
            body += "\n\n--- CONSOLA DEL LAUNCHER (últimas líneas) ---\n" + tail
        ai_state["history"].append({"role": "user", "content": body})
        ai_state["busy"] = True
        status_var.set("El asistente está pensando...")
        inst = cur()
        system = ai_system_prompt(inst, installed_mod_names(inst))
        model = (cfg.get("ai_model") or AI_MODELS[0]).strip()
        hist = list(ai_state["history"][-16:])
        while hist and hist[0]["role"] != "user":
            hist.pop(0)

        def job():
            try:
                res = ai_chat(key, model, system, hist)
            except Exception as e:
                res = e
            msgs.put(("ai", res))
        threading.Thread(target=job, daemon=True).start()

    def ai_done(res):
        ai_state["busy"] = False
        status_var.set("Listo")
        if isinstance(res, Exception):
            if ai_state["history"] and ai_state["history"][-1]["role"] == "user":
                ai_state["history"].pop()
            ai_append("err", friendly_ai_error(res))
        else:
            ai_state["history"].append({"role": "assistant", "content": res})
            ai_append("bot", clean_md(res))

    def ai_submit(_=None):
        t = ai_in_v.get()
        ai_in_v.set("")
        ai_send(t)

    ai_in.bind("<Return>", ai_submit)
    flat_btn(irow, "Enviar", ai_submit, bg=ACCENT, hover="#7ba3ff", fg="white", size=10).pack(
        side="left", padx=(px(8), 0), ipadx=px(20), ipady=px(8))

    def ai_analyze_log():
        show_page("ai")
        ai_send("Mi juego falló o se cerró de forma rara. Analizá la consola y decime la causa más "
                "probable y cómo arreglarla.", with_log=True)

    for txt, q, lg in (("Recomendame mods de rendimiento", "Recomendame mods de rendimiento para mi instancia "
                        "actual (que existan para esta versión y cargador).", False),
                       ("Analizar la consola", None, True),
                       ("¿Cuánta RAM le doy?", "¿Cuánta RAM me conviene asignarle a esta instancia?", False),
                       ("Fabric vs Forge", "Explicame en corto la diferencia entre Fabric, Forge y NeoForge "
                        "y cuál me conviene.", False)):
        flat_btn(chips, txt, (ai_analyze_log if q is None else (lambda qq=q: ai_send(qq))),
                 bg=CARD, hover=CARD2, size=9).pack(side="left", padx=(0, px(6)), ipadx=px(10), ipady=px(6))
    flat_btn(chips, "Cambiar clave", lambda: (cfg.update(ai_key=""), save_config(cfg), ai_enter(), refresh_home()),
             bg=BG, hover=CARD, fg=MUTED).pack(side="right", ipadx=px(8), ipady=px(6))

    def ai_enter():
        if (cfg.get("ai_key") or "").strip():
            setup.pack_forget()
            chat.pack(fill="both", expand=True, padx=px(36), pady=(0, px(24)))
            if not ai_state["history"] and not chat_text.get("1.0", "end").strip():
                ai_append("note", "Hola, soy tu asistente. Puedo recomendarte mods, explicarte errores de la "
                                  "consola o ayudarte a elegir RAM y versión. Usá los botones o escribime.")
        else:
            chat.pack_forget()
            setup.pack(fill="x", padx=px(36), pady=(0, px(10)))

    # ================================================================== LOGIN
    login = tk.Frame(content, bg=BG)
    pages["login"] = login
    lbg = tk.Canvas(login, bg=BG, highlightthickness=0, bd=0)
    lbg.place(x=0, y=0, relwidth=1.0, relheight=1.0)
    lbg.bind("<Configure>", lambda e: draw_night(lbg))
    lcard = tk.Frame(login, bg=HC, highlightthickness=1, highlightbackground=HL)
    lcard.place(relx=0.5, rely=0.5, anchor="center", width=px(440))
    tk.Label(lcard, text="N", font=(FONT, 26, "bold"), fg="white", bg=ACCENT).pack(
        pady=(px(28), px(8)), ipadx=px(16), ipady=px(4))
    login_title = tk.StringVar(value="Iniciar sesión")
    login_sub = tk.StringVar(value="")
    tk.Label(lcard, textvariable=login_title, font=(FONT, 16, "bold"), fg=FG, bg=HC).pack()
    tk.Label(lcard, textvariable=login_sub, font=(FONT, 9), fg=MUTED, bg=HC, wraplength=px(360),
             justify="center").pack(pady=(px(2), px(10)))
    lu, lp, lp2 = tk.StringVar(), tk.StringVar(), tk.StringVar()
    remember_v = tk.BooleanVar(value=False)
    login_state = {"mode": "login", "fails": 0, "until": 0.0}

    def lentry(parent, label, var, show=""):
        tk.Label(parent, text=label, font=(FONT, 8, "bold"), fg=MUTED, bg=HC, anchor="w").pack(
            fill="x", padx=px(36), pady=(px(8), px(3)))
        e = tk.Entry(parent, textvariable=var, show=show, font=(FONT, 12), bg=CARD2, fg=FG,
                     insertbackground=FG, relief="flat", highlightthickness=1, highlightbackground=LINE,
                     highlightcolor=ACCENT)
        e.pack(fill="x", padx=px(36), ipady=px(7))
        e.bind("<Return>", lambda ev: login_submit())
        return e

    e_user = lentry(lcard, "USUARIO", lu)
    e_pass = lentry(lcard, "CONTRASEÑA", lp, "•")
    rep_frame = tk.Frame(lcard, bg=HC)
    e_rep = lentry(rep_frame, "REPETIR CONTRASEÑA", lp2, "•")
    rem_cb = tk.Checkbutton(lcard, text="Recordarme en este PC", variable=remember_v, fg=FG, bg=HC,
                            selectcolor=CARD2, activebackground=HC, activeforeground=FG, font=(FONT, 9),
                            bd=0, highlightthickness=0, anchor="w")
    rem_cb.pack(fill="x", padx=px(34), pady=(px(10), 0))
    login_err = tk.Label(lcard, text="", font=(FONT, 9), fg="#ff8a9b", bg=HC, wraplength=px(360), justify="left")
    login_err.pack(fill="x", padx=px(36), pady=(px(6), 0))
    submit_btn = RoundButton(lcard, "ENTRAR", lambda: login_submit(), ACCENT, "#7ba3ff", HC,
                             font=(FONT, 12, "bold"), height=46, radius=12)
    submit_btn.pack(fill="x", padx=px(36), pady=(px(10), px(6)))
    switch_btn = flat_btn(lcard, "Crear una cuenta", lambda: login_switch(), bg=HC, hover=CARD2, fg=MUTED)
    switch_btn.pack(pady=(0, px(22)), ipadx=px(12), ipady=px(4))

    def login_set_mode(mode):
        login_state["mode"] = mode
        lp.set("")
        lp2.set("")
        login_err.config(text="")
        if mode == "setup":
            login_title.set("Creá tu cuenta de administrador")
            login_sub.set("Es la primera vez. Esta cuenta será la dueña del launcher y tendrá el panel de "
                          "administración.")
            submit_btn.set_state("CREAR CUENTA", True)
            switch_btn.pack_forget()
        elif mode == "signup":
            login_title.set("Crear cuenta")
            login_sub.set("Vas a tener una cuenta de usuario. El administrador puede darte permisos.")
            submit_btn.set_state("REGISTRARME", True)
            switch_btn.config(text="Ya tengo cuenta")
            switch_btn.pack(pady=(0, px(22)), ipadx=px(12), ipady=px(4))
        else:
            login_title.set("Iniciar sesión")
            login_sub.set("Bienvenido de nuevo.")
            submit_btn.set_state("ENTRAR", True)
            if cfg.get("allow_signup", True):
                switch_btn.config(text="Crear una cuenta")
                switch_btn.pack(pady=(0, px(22)), ipadx=px(12), ipady=px(4))
            else:
                switch_btn.pack_forget()
        if mode in ("setup", "signup"):
            rep_frame.pack(fill="x", after=e_pass)
        else:
            rep_frame.pack_forget()

    def login_switch():
        login_set_mode("signup" if login_state["mode"] == "login" else "login")

    def show_login():
        topbar.pack_forget()
        admin_btn.pack_forget()
        chip_var.set("")
        login_set_mode("setup" if not cfg["accounts"] else "login")
        show_page("login")
        e_user.focus_set()

    def apply_session(acct, remember):
        state["acct"] = acct
        acct["last_login"] = time.strftime("%Y-%m-%d %H:%M")
        cfg["remember"] = acct["user"] if remember else ""
        user_var.set(acct.get("mc_name") or acct["user"])
        chip_var.set(acct["user"] + ("  ★" if acct.get("role") == "admin" else ""))
        acc_lbl.set(f"{acct['user']}  ·  {'Administrador' if acct.get('role') == 'admin' else 'Usuario'}")
        topbar.pack(side="top", fill="x", before=content)
        admin_btn.pack_forget()
        if acct.get("role") == "admin":
            admin_btn.pack(side="right", padx=px(6), ipadx=px(10), ipady=px(6))
        lp.set("")
        lp2.set("")
        audit_event("inicio de sesión")
        save_all()
        refresh_all()
        show_page("play")

    def login_submit():
        mode = login_state["mode"]
        u, pw = lu.get().strip(), lp.get()
        accts = cfg["accounts"]
        if mode in ("setup", "signup"):
            if mode == "signup" and not cfg.get("allow_signup", True):
                login_err.config(text="El registro está desactivado por el administrador.")
                return
            if pw != lp2.get():
                login_err.config(text="Las contraseñas no coinciden.")
                return
            err = validate_new_account(accts, u, pw)
            if err:
                login_err.config(text=err)
                return
            role = "admin" if (mode == "setup" or not accts) else "user"
            acct = new_account(u, pw, role)
            accts.append(acct)
            audit_add(cfg, u, "cuenta creada", f"rol: {role}")
            save_config(cfg)
            apply_session(acct, remember_v.get())
            return
        now = time.time()
        if now < login_state["until"]:
            login_err.config(text=f"Demasiados intentos. Esperá {int(login_state['until'] - now) + 1} s.")
            return
        acct = find_account(accts, u)
        if not acct or not check_pw(acct, pw):
            login_state["fails"] += 1
            if login_state["fails"] >= 5:
                login_state["fails"] = 0
                login_state["until"] = now + 30
                audit_add(cfg, u or "-", "bloqueo temporal por intentos fallidos")
                save_config(cfg)
                login_err.config(text="Demasiados intentos. Esperá 30 s.")
            else:
                login_err.config(text="Usuario o contraseña incorrectos.")
            lp.set("")
            return
        if not acct.get("enabled", True):
            login_err.config(text="Tu cuenta está desactivada. Pedile al administrador que la active.")
            return
        login_state["fails"] = 0
        apply_session(acct, remember_v.get())

    def logout():
        if state["busy"] and not messagebox.askokcancel(APP_NAME, "Hay algo en marcha. ¿Cerrar sesión igual?"):
            return
        audit_event("cierre de sesión")
        save_all()
        state["acct"] = None
        cfg["remember"] = ""
        save_config(cfg)
        show_login()

    def start_session():
        a = find_account(cfg["accounts"], cfg.get("remember", "")) if cfg.get("remember") else None
        if a and a.get("enabled", True):
            apply_session(a, True)
        else:
            show_login()

    def change_password():
        a = state.get("acct")
        if not a:
            return
        old = simpledialog.askstring(APP_NAME, "Contraseña actual:", show="•", parent=root)
        if old is None:
            return
        if not check_pw(a, old):
            messagebox.showwarning(APP_NAME, "La contraseña actual no es correcta.")
            return
        new = simpledialog.askstring(APP_NAME, f"Nueva contraseña (mínimo {PW_MIN} caracteres):", show="•",
                                     parent=root)
        if not new:
            return
        if len(new) < PW_MIN:
            messagebox.showwarning(APP_NAME, f"La contraseña debe tener al menos {PW_MIN} caracteres.")
            return
        a["salt"], a["hash"] = hash_pw(new)
        audit_event("contraseña cambiada")
        save_all()
        messagebox.showinfo(APP_NAME, "Contraseña actualizada.")

    # ================================================================== ADMIN
    admin_page = tk.Frame(content, bg=BG)
    pages["admin"] = admin_page
    ahd = tk.Frame(admin_page, bg=BG)
    ahd.pack(fill="x", padx=px(36), pady=(px(26), px(2)))
    page_title(ahd, "Panel de administración").pack(side="left")
    tk.Label(ahd, text="ADMIN", font=(FONT, 8, "bold"), fg="white", bg="#c0455a").pack(
        side="left", padx=px(12), pady=(px(10), 0), ipadx=px(8), ipady=px(2))
    tk.Label(admin_page, text="Las cuentas son locales de este PC: quien tenga acceso a los archivos de la "
                              "carpeta de datos puede modificarlas.", font=(FONT, 9), fg=MUTED, bg=BG,
             anchor="w").pack(fill="x", padx=px(38), pady=(0, px(6)))
    atabs = tk.Frame(admin_page, bg=BG)
    atabs.pack(fill="x", padx=px(36), pady=(px(4), px(8)))
    abody = tk.Frame(admin_page, bg=BG)
    abody.pack(fill="both", expand=True, padx=px(36), pady=(0, px(20)))
    admin_views, admin_tab_btns, admin_state = {}, {}, {"tab": "accounts"}

    def show_admin_tab(k):
        admin_state["tab"] = k
        for key, f in admin_views.items():
            f.pack_forget()
        admin_views[k].pack(fill="both", expand=True)
        for key, b in admin_tab_btns.items():
            on = key == k
            b.config(bg=ACCENT if on else CARD, fg="white" if on else FG)
        {"accounts": build_accounts, "audit": refresh_audit, "system": refresh_system,
         "global": refresh_global}[k]()

    for k, label in (("accounts", "Cuentas"), ("audit", "Actividad"), ("system", "Sistema"),
                     ("global", "Global")):
        admin_views[k] = tk.Frame(abody, bg=BG)
        b = tk.Button(atabs, text=label, font=(FONT, 10, "bold"), bg=CARD, fg=FG, activebackground=CARD2,
                      activeforeground=FG, relief="flat", bd=0, cursor="hand2",
                      command=lambda kk=k: show_admin_tab(kk))
        b.pack(side="left", padx=(0, px(6)), ipadx=px(20), ipady=px(7))
        admin_tab_btns[k] = b

    def admin_enter():
        if is_admin():
            show_admin_tab(admin_state["tab"])

    # ---- cuentas
    av = admin_views["accounts"]
    ar = tk.Frame(av, bg=BG)
    ar.pack(fill="x", pady=(0, px(8)))
    flat_btn(ar, "＋  Nueva cuenta", lambda: new_account_dialog(), bg=ACCENT, hover="#7ba3ff", fg="white",
             size=10).pack(side="left", ipadx=px(16), ipady=px(7))
    alist = ScrollFrame(av, BG)
    alist.pack(fill="both", expand=True)

    def admin_guard():
        return is_admin()

    def toggle_role(a):
        if not admin_guard():
            return
        if is_last_admin(cfg["accounts"], a):
            messagebox.showwarning(APP_NAME, "No podés quitarle el rol al último administrador.")
            return
        a["role"] = "user" if a.get("role") == "admin" else "admin"
        audit_event("rol cambiado", f"{a['user']} → {a['role']}")
        save_all()
        build_accounts()

    def toggle_enabled(a):
        if not admin_guard():
            return
        if a is state["acct"] or is_last_admin(cfg["accounts"], a):
            messagebox.showwarning(APP_NAME, "No podés desactivar tu propia cuenta ni al último administrador.")
            return
        a["enabled"] = not a.get("enabled", True)
        audit_event("cuenta " + ("activada" if a["enabled"] else "desactivada"), a["user"])
        save_all()
        build_accounts()

    def delete_account(a):
        if not admin_guard():
            return
        if a is state["acct"] or is_last_admin(cfg["accounts"], a):
            messagebox.showwarning(APP_NAME, "No podés eliminar tu propia cuenta ni al último administrador.")
            return
        if not messagebox.askokcancel(APP_NAME, f"¿Eliminar la cuenta “{a['user']}”? No se puede deshacer."):
            return
        cfg["accounts"] = [x for x in cfg["accounts"] if x is not a]
        if cfg.get("remember", "").lower() == a["user"].lower():
            cfg["remember"] = ""
        audit_event("cuenta eliminada", a["user"])
        save_all()
        build_accounts()

    def reset_pw(a):
        if not admin_guard():
            return
        new = simpledialog.askstring(APP_NAME, f"Nueva contraseña para {a['user']}:", show="•", parent=root)
        if not new:
            return
        if len(new) < PW_MIN:
            messagebox.showwarning(APP_NAME, f"La contraseña debe tener al menos {PW_MIN} caracteres.")
            return
        a["salt"], a["hash"] = hash_pw(new)
        audit_event("contraseña restablecida", a["user"])
        save_all()
        messagebox.showinfo(APP_NAME, f"Contraseña de {a['user']} actualizada.")

    def new_account_dialog():
        if not admin_guard():
            return
        u = simpledialog.askstring(APP_NAME, "Nombre de usuario de la nueva cuenta:", parent=root)
        if not u:
            return
        pw = simpledialog.askstring(APP_NAME, f"Contraseña para {u} (mínimo {PW_MIN} caracteres):", show="•",
                                    parent=root)
        if pw is None:
            return
        err = validate_new_account(cfg["accounts"], u.strip(), pw)
        if err:
            messagebox.showwarning(APP_NAME, err)
            return
        role = "admin" if messagebox.askyesno(APP_NAME, "¿Hacerla cuenta de administrador?") else "user"
        cfg["accounts"].append(new_account(u.strip(), pw, role))
        audit_event("cuenta creada por admin", f"{u.strip()} ({role})")
        save_all()
        build_accounts()

    def perms_dialog(a):
        if not admin_guard():
            return
        dlg = tk.Toplevel(root)
        dlg.title(f"Permisos de {a['user']}")
        dlg.configure(bg=BG)
        dlg.transient(root)
        dlg.geometry(f"{px(440)}x{px(360)}+{root.winfo_x() + px(160)}+{root.winfo_y() + px(120)}")
        pr = a.get("perms") or {}
        pv = {k: tk.BooleanVar(value=bool(pr.get(k, True))) for k in ("store", "ai", "console")}
        ram_v = tk.StringVar(value=str(round(int(pr.get("ram_mb", 0)) / 1024, 1)))
        tk.Label(dlg, text=f"Permisos de {a['user']}", font=(FONT, 14, "bold"), fg=FG, bg=BG).pack(
            anchor="w", padx=px(24), pady=(px(20), px(10)))
        for k, label in (("store", "Puede usar la tienda de mods, texturas y shaders"),
                         ("ai", "Puede usar el asistente IA"),
                         ("console", "Puede ver la consola")):
            tk.Checkbutton(dlg, text=label, variable=pv[k], fg=FG, bg=BG, selectcolor=CARD2,
                           activebackground=BG, activeforeground=FG, font=(FONT, 10), bd=0,
                           highlightthickness=0, anchor="w").pack(fill="x", padx=px(24), pady=px(3))
        tk.Label(dlg, text="Límite de RAM por partida en GB (0 = sin límite)", font=(FONT, 9), fg=MUTED,
                 bg=BG).pack(anchor="w", padx=px(26), pady=(px(14), px(4)))
        tk.Entry(dlg, textvariable=ram_v, font=(FONT, 11), bg=CARD2, fg=FG, insertbackground=FG,
                 relief="flat", highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT,
                 width=10).pack(anchor="w", padx=px(26), ipady=px(6))

        def save():
            try:
                gb = float((ram_v.get() or "0").replace(",", "."))
                if gb < 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning(APP_NAME, "El límite de RAM tiene que ser un número (0 = sin límite).",
                                       parent=dlg)
                return
            a["perms"] = {"store": pv["store"].get(), "ai": pv["ai"].get(), "console": pv["console"].get(),
                          "ram_mb": int(gb * 1024)}
            audit_event("permisos modificados", a["user"])
            save_all()
            build_accounts()
            dlg.destroy()

        fr = tk.Frame(dlg, bg=BG)
        fr.pack(side="bottom", fill="x", padx=px(24), pady=px(18))
        flat_btn(fr, "Cancelar", dlg.destroy, size=10).pack(side="right", padx=(px(8), 0), ipadx=px(14), ipady=px(7))
        flat_btn(fr, "Guardar", save, bg=ACCENT, hover="#7ba3ff", fg="white", size=10).pack(
            side="right", ipadx=px(16), ipady=px(7))

    def build_accounts():
        for w_ in alist.inner.winfo_children():
            w_.destroy()
        for a in cfg["accounts"]:
            c = card(alist.inner)
            c.pack(fill="x", pady=(0, px(8)), padx=(0, px(8)))
            tk.Label(c, text=a["user"][:1].upper(), font=(FONT, 18, "bold"), fg="white",
                     bg=accent_for(a["user"]), width=3).pack(side="left", fill="y")
            mid = tk.Frame(c, bg=CARD)
            mid.pack(side="left", fill="x", expand=True, padx=px(14), pady=px(12))
            top = tk.Frame(mid, bg=CARD)
            top.pack(fill="x")
            tk.Label(top, text=a["user"], font=(FONT, 12, "bold"), fg=FG, bg=CARD).pack(side="left")
            badges = []
            if a.get("role") == "admin":
                badges.append(("ADMIN", ACCENT))
            if not a.get("enabled", True):
                badges.append(("DESACTIVADA", "#c0455a"))
            if a is state["acct"]:
                badges.append(("VOS", "#2a8a5a"))
            for txt, col in badges:
                tk.Label(top, text=txt, font=(FONT, 7, "bold"), fg="white", bg=col).pack(
                    side="left", padx=(px(8), 0), ipadx=px(5), ipady=px(1))
            tk.Label(mid, text=f"Minecraft: {a.get('mc_name') or a['user']}  ·  Último acceso: "
                               f"{a.get('last_login') or 'nunca'}  ·  Jugado: {fmt_time(a.get('playtime', 0))}",
                     font=(FONT, 9), fg=MUTED, bg=CARD, anchor="w").pack(fill="x")
            pr = a.get("perms") or {}
            if a.get("role") == "admin":
                ptxt = "Acceso total"
            else:
                ck = lambda v: "sí" if v else "no"
                cap = int(pr.get("ram_mb", 0))
                ptxt = (f"Tienda: {ck(pr.get('store', True))}  ·  IA: {ck(pr.get('ai', True))}  ·  "
                        f"Consola: {ck(pr.get('console', True))}  ·  RAM máx: "
                        f"{'sin límite' if cap <= 0 else f'{cap / 1024:.1f} GB'}")
            tk.Label(mid, text=ptxt, font=(FONT, 9), fg=MUTED, bg=CARD, anchor="w").pack(fill="x")
            right = tk.Frame(c, bg=CARD)
            right.pack(side="right", padx=px(10))
            acts = []
            if a.get("role") != "admin":
                acts.append(("Permisos", lambda x=a: perms_dialog(x), CARD2, LINE, FG))
            acts += [("Clave", lambda x=a: reset_pw(x), CARD2, LINE, FG),
                     ("Quitar admin" if a.get("role") == "admin" else "Hacer admin", lambda x=a: toggle_role(x),
                      CARD2, LINE, FG),
                     ("Activar" if not a.get("enabled", True) else "Desactivar", lambda x=a: toggle_enabled(x),
                      CARD2, LINE, FG),
                     ("Eliminar", lambda x=a: delete_account(x), CARD2, RED, "#ff8a9b")]
            for txt, fn, bgc, hov, fgc in acts:
                flat_btn(right, txt, fn, bg=bgc, hover=hov, fg=fgc).pack(
                    side="left", padx=px(2), ipadx=px(8), ipady=px(5))

    # ---- actividad
    uv = admin_views["audit"]
    ur = tk.Frame(uv, bg=BG)
    ur.pack(fill="x", pady=(0, px(8)))
    audit_text = tk.Text(uv, bg=CARD, fg="#b7bccb", font=("Consolas", 9), relief="flat", wrap="none",
                         state="disabled", bd=0, padx=px(12), pady=px(10), highlightthickness=1,
                         highlightbackground=LINE)

    def refresh_audit():
        lines = [f"{e.get('t', '')}   {e.get('u', '-'):<14} {e.get('e', '')}" +
                 (f"  ({e['d']})" if e.get("d") else "") for e in reversed(cfg.get("audit", [])[-150:])]
        audit_text.config(state="normal")
        audit_text.delete("1.0", "end")
        audit_text.insert("end", "\n".join(lines) if lines else "Todavía no hay actividad registrada.")
        audit_text.config(state="disabled")

    def copy_audit():
        root.clipboard_clear()
        root.clipboard_append(audit_text.get("1.0", "end"))

    def clear_audit():
        if messagebox.askokcancel(APP_NAME, "¿Borrar todo el registro de actividad?"):
            cfg["audit"] = []
            audit_event("registro de actividad borrado")
            save_all()
            refresh_audit()

    for txt, fn in (("Actualizar", lambda: refresh_audit()), ("Copiar", copy_audit), ("Borrar registro", clear_audit)):
        flat_btn(ur, txt, fn, size=10).pack(side="left", padx=(0, px(6)), ipadx=px(12), ipady=px(6))
    audit_text.pack(fill="both", expand=True)

    # ---- sistema
    sv = admin_views["system"]
    sys_info = tk.StringVar(value="")
    sr = tk.Frame(sv, bg=BG)
    sr.pack(fill="x", pady=(0, px(10)))

    def backup_config():
        if not admin_guard():
            return
        save_all()
        d = os.path.join(ROOT, "backups")
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, time.strftime("config-%Y%m%d-%H%M%S.json"))
        shutil.copyfile(CONFIG_FILE, dest)
        audit_event("copia de seguridad", os.path.basename(dest))
        save_all()
        messagebox.showinfo(APP_NAME, f"Copia guardada en:\n{dest}\n\nOjo: incluye cuentas y la clave de la IA.")

    def clear_installers():
        if not admin_guard():
            return
        shutil.rmtree(INSTALLERS, ignore_errors=True)
        audit_event("instaladores de loaders borrados")
        save_all()
        refresh_system()

    for txt, fn in (("Abrir carpeta de datos", lambda: open_folder(ROOT)),
                    ("Copia de seguridad de la configuración", backup_config),
                    ("Borrar instaladores descargados", clear_installers),
                    ("Recalcular tamaños", lambda: refresh_system())):
        flat_btn(sr, txt, fn, size=10).pack(side="left", padx=(0, px(6)), ipadx=px(12), ipady=px(6))
    tk.Label(sv, textvariable=sys_info, font=("Consolas", 10), fg=FG, bg=CARD, anchor="nw", justify="left",
             padx=px(18), pady=px(14), highlightthickness=1, highlightbackground=LINE).pack(fill="both", expand=True)

    def refresh_system():
        sys_info.set("Calculando tamaños...")
        insts = [(i["name"], game_dir(i)) for i in instances()]

        def job():
            sizes = [(nm, dir_size(pth)) for nm, pth in insts]
            msgs.put(("sizes", sizes, dir_size(ROOT)))
        threading.Thread(target=job, daemon=True).start()

    def show_sizes(sizes, total):
        lines = [f"Carpeta de datos: {ROOT}", f"Tamaño total:     {fmt_size(total)}", "",
                 f"Cuentas: {len(cfg['accounts'])}   Instancias: {len(instances())}", "", "Instancias:"]
        lines += [f"  {nm:<28} {fmt_size(sz)}" for nm, sz in sizes] or ["  (ninguna)"]
        sys_info.set("\n".join(lines))

    # ---- global
    gv = admin_views["global"]
    signup_v = tk.BooleanVar(value=cfg.get("allow_signup", True))
    gcard = card(gv)
    gcard.pack(fill="x")
    small_title(gcard, "REGISTRO DE CUENTAS").pack(anchor="w", padx=px(20), pady=(px(14), px(4)))

    def on_signup():
        cfg["allow_signup"] = bool(signup_v.get())
        audit_event("registro de cuentas " + ("permitido" if cfg["allow_signup"] else "desactivado"))
        save_all()

    tk.Checkbutton(gcard, text="Permitir que cualquiera cree una cuenta de usuario desde la pantalla de inicio",
                   variable=signup_v, command=on_signup, fg=FG, bg=CARD, selectcolor=CARD2,
                   activebackground=CARD, activeforeground=FG, font=(FONT, 10), bd=0, highlightthickness=0,
                   anchor="w").pack(fill="x", padx=px(16), pady=(px(4), px(16)))

    def refresh_global():
        signup_v.set(cfg.get("allow_signup", True))

    # ================================================================== AJUSTES
    sett = tk.Frame(content, bg=BG)
    pages["settings"] = sett
    page_title(sett, "Ajustes").pack(anchor="w", padx=px(36), pady=(px(28), px(14)))
    c1 = card(sett)
    c1.pack(fill="x", padx=px(36), pady=(0, px(14)))
    small_title(c1, "NOMBRE EN MINECRAFT").pack(anchor="w", padx=px(20), pady=(px(14), px(6)))
    tk.Entry(c1, textvariable=user_var, font=(FONT, 12), bg=CARD2, fg=FG, insertbackground=FG,
             relief="flat", highlightthickness=1, highlightbackground=LINE,
             highlightcolor=ACCENT).pack(fill="x", padx=px(20), pady=(0, px(16)), ipady=px(7))

    c2 = card(sett)
    c2.pack(fill="x", padx=px(36), pady=(0, px(14)))
    small_title(c2, "TAMAÑO DE LA INTERFAZ").pack(anchor="w", padx=px(20), pady=(px(14), px(6)))
    srow = tk.Frame(c2, bg=CARD)
    srow.pack(fill="x", padx=px(20), pady=(0, px(16)))
    scale_box = ttk.Combobox(srow, textvariable=scale_var, values=SCALES, state="readonly",
                             font=(FONT, 11), width=14)
    scale_box.pack(side="left", ipady=px(3))
    tk.Label(srow, text="Se aplica al reiniciar el launcher. Subilo si se ve chico o borroso.",
             font=(FONT, 9), fg=MUTED, bg=CARD).pack(side="left", padx=px(14))

    cacc = card(sett)
    cacc.pack(fill="x", padx=px(36), pady=(0, px(14)))
    small_title(cacc, "MI CUENTA").pack(anchor="w", padx=px(20), pady=(px(14), px(6)))
    acc_lbl = tk.StringVar(value="")
    tk.Label(cacc, textvariable=acc_lbl, font=(FONT, 11), fg=FG, bg=CARD, anchor="w").pack(fill="x", padx=px(20))
    arow = tk.Frame(cacc, bg=CARD)
    arow.pack(fill="x", padx=px(20), pady=(px(10), px(16)))
    flat_btn(arow, "Cambiar contraseña", lambda: change_password(), size=10).pack(
        side="left", ipadx=px(14), ipady=px(7))
    flat_btn(arow, "Cerrar sesión", lambda: logout(), bg=CARD2, hover=RED, fg="#ff8a9b", size=10).pack(
        side="left", padx=px(8), ipadx=px(14), ipady=px(7))

    c3 = card(sett)
    c3.pack(fill="x", padx=px(36), pady=(0, px(14)))
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
    tk.Label(sett, text=f"Datos guardados en:  {ROOT}", font=(FONT, 9), fg=MUTED, bg=BG,
             anchor="w").pack(fill="x", padx=px(38), pady=(px(4), 0))

    # ================================================================== CONSOLA
    cons = tk.Frame(content, bg=BG)
    pages["console"] = cons
    head = tk.Frame(cons, bg=BG)
    head.pack(fill="x", padx=px(36), pady=(px(28), px(12)))
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

    for label, fn in (("✧ Pedir ayuda a la IA", lambda: ai_analyze_log()), ("Limpiar", clear_log),
                      ("Copiar todo", copy_log)):
        flat_btn(head, label, fn).pack(side="right", padx=(px(8), 0), ipadx=px(12), ipady=px(5))
    sb = tk.Scrollbar(cons, command=log_text.yview)
    log_text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y", padx=(0, px(36)), pady=(0, px(28)))
    log_text.pack(side="left", fill="both", expand=True, padx=(px(36), 0), pady=(0, px(28)))

    def append_log(text):
        log_text.config(state="normal")
        log_text.insert("end", text.rstrip("\n") + "\n")
        log_text.see("end")
        log_text.config(state="disabled")

    # ================================================================== navegación / refresco
    def show_page(name):
        gate = {"mods": ("store", "la tienda"), "ai": ("ai", "el asistente IA"), "console": ("console", "la consola")}
        if name in gate and not allowed(gate[name][0]):
            messagebox.showinfo(APP_NAME, f"Tu cuenta no tiene permiso para usar {gate[name][1]}. "
                                          "Pedile acceso al administrador.")
            return
        if name == "admin" and not is_admin():
            return
        for f in pages.values():
            f.pack_forget()
        state["page"] = name
        pages[name].pack(fill="both", expand=True)
        if name in ("play", "login"):
            back_btn.pack_forget()
        else:
            back_btn.pack(side="left", padx=(px(12), 0), before=brand)
        if name == "mods":
            mods_enter()
        elif name == "ai":
            ai_enter()
        elif name == "admin":
            admin_enter()

    def show_console_if_allowed():
        if allowed("console"):
            show_page("console")

    def refresh_play():
        names = [i["name"] for i in instances()]
        inst_box["values"] = names
        inst = cur()
        if inst:
            sel_var.set(inst["name"])
            ram_text.set(f"{inst.get('ram_mb', 4096) / 1024:.1f} GB")
            loader_text.set(LOADER_NAMES.get(inst["loader"], inst["loader"]))
            time_text.set(fmt_time(inst.get("playtime", 0)))
        else:
            time_text.set("—")
            sel_var.set("")
            ram_text.set("—")
            loader_text.set("—")
        draw_hero()

    def refresh_all():
        refresh_play()
        build_instances()
        refresh_home()

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
        a = state.get("acct")
        if a:
            a["mc_name"] = user_var.get().strip() or a["user"]
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
        flat_btn(footer, "Guardar" if editing else "Crear instancia", save, bg=ACCENT, hover="#7ba3ff",
                 fg="white", size=10).pack(side="right", ipadx=px(18), ipady=px(8))
        load_lv()

    # ================================================================== jugar
    def worker(inst, username, ram_cap=0):
        launcher = Launcher(
            log=lambda t: msgs.put(("log", t)),
            progress=lambda dn, t, l: msgs.put(("progress", dn, t, l)),
            on_change=lambda: msgs.put(("save",)),
            ram_cap=ram_cap,
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
        audit_event("jugar", inst["name"])
        acct = state.get("acct") or {}
        ram_cap = 0 if is_admin() else int((acct.get("perms") or {}).get("ram_mb", 0))
        state["busy"] = True
        play_btn.set_state("PREPARANDO...", False)
        status_var.set("Preparando...")
        threading.Thread(target=worker, args=(inst, name, ram_cap), daemon=True).start()

    play_btn.command = on_play

    def load_versions():
        try:
            state["manifest"] = load_manifest()
            msgs.put(("manifest",))
        except Exception as e:
            msgs.put(("net", "mojang", False))
            msgs.put(("log", f"No se pudo cargar la lista de versiones: {e}"))
            msgs.put(("status", "Sin conexión con Mojang"))

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
                    status_vals["mojang"] = True
                    if not instances():
                        latest = (state["manifest"] or {}).get("latest", {}).get("release")
                        if latest:
                            first = new_instance(f"Minecraft {latest}", latest, ram_mb=min(4096, max_ram))
                            instances().append(first)
                            cfg["last_instance"] = first["id"]
                            save_all()
                    refresh_all()
                    status_var.set("Listo para jugar")
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
                        if state.get("acct") is not None:
                            state["acct"]["playtime"] = int(state["acct"].get("playtime", 0)) + m[2]
                        audit_event("sesión de juego", f"{i['name']} · {fmt_time(m[2])}")
                        save_all()
                        refresh_all()
                elif kind == "crash":
                    status_var.set(f"El juego se cerró con error (código {m[1]}). Tocá “Pedir ayuda a la IA”.")
                    show_console_if_allowed()
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
                elif kind == "ai":
                    ai_done(m[1])
                elif kind == "sizes":
                    show_sizes(m[1], m[2])
                elif kind == "net":
                    status_vals[m[1]] = m[2]
                    refresh_home()
                elif kind == "started":
                    bar["value"] = 0
                    play_btn.set_state("JUGANDO", False)
                    status_var.set(f"{m[1]} en ejecución")
                    if con_var.get():
                        show_console_if_allowed()
                    if min_var.get():
                        root.iconify()
                elif kind == "error":
                    status_var.set("Error: revisá la consola")
                    show_console_if_allowed()
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
    refresh_all()
    pump()
    start_session()
    threading.Thread(target=load_versions, daemon=True).start()

    def ping_modrinth():
        try:
            fetch_json("https://api.modrinth.com/", timeout=10)
            msgs.put(("net", "modrinth", True))
        except Exception:
            msgs.put(("net", "modrinth", False))
    threading.Thread(target=ping_modrinth, daemon=True).start()
    append_log(f"Datos guardados en: {ROOT}")
    root.mainloop()


if __name__ == "__main__":
    run_gui()
