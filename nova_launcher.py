#!/usr/bin/env python3
"""
nova_server.py - backend mínimo de NovaLauncher (solo librería estándar).

Guarda usuarios, anuncios y bans en un JSON. Valida los tokens de Auth0 preguntándole
a Auth0 (/userinfo) y solo deja usar las rutas /admin/* si el email es ADMIN_EMAIL
y está verificado.

Uso:
    AUTH0_DOMAIN=tu-tenant.us.auth0.com python nova_server.py

Variables de entorno opcionales: ADMIN_EMAIL, PORT, DB_FILE.
En producción ponelo detrás de HTTPS (los tokens viajan en el header Authorization).
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "dev-3nq3xfjokujc6awq.us.auth0.com")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "mateogey88@gmail.com").lower()
PORT = int(os.environ.get("PORT", "8080"))
DB_FILE = os.environ.get("DB_FILE", "nova_db.json")
LOCK = threading.Lock()
_cache = {}


def load_db():
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}, "news": []}


def save_db(db):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)
    os.replace(tmp, DB_FILE)


def whoami(token):
    """Valida el token preguntándole a Auth0 (/userinfo) y cachea 60 s."""
    hit = _cache.get(token)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        req = urllib.request.Request(f"https://{AUTH0_DOMAIN}/userinfo",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    _cache[token] = (time.time() + 60, info)
    return info


def is_admin(info):
    return bool(info.get("email_verified")) and (info.get("email") or "").lower() == ADMIN_EMAIL


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None):
        body = json.dumps(obj or {}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(min(n, 65536)) or b"{}")

    def user(self):
        auth = self.headers.get("Authorization", "")
        return whoami(auth[7:]) if auth.startswith("Bearer ") else None

    def admin(self):
        info = self.user()
        if not info:
            self.send(401, {"error": "No autenticado, volvé a iniciar sesión"})
            return None
        if not is_admin(info):
            self.send(403, {"error": "Solo el administrador puede hacer esto"})
            return None
        return info

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/news":
            with LOCK:
                db = load_db()
            return self.send(200, {"news": db["news"][:20]})
        if path.startswith("/status/"):
            sub = urllib.parse.unquote(path[8:])
            with LOCK:
                db = load_db()
            return self.send(200, {"banned": bool(db["users"].get(sub, {}).get("banned"))})
        if path == "/admin/users":
            if not self.admin():
                return
            with LOCK:
                db = load_db()
            users = sorted(db["users"].values(), key=lambda u: u.get("last_seen", 0), reverse=True)
            return self.send(200, {"users": users})
        self.send(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self.body()
        except Exception:
            return self.send(400, {"error": "JSON inválido"})
        now = int(time.time())
        if path == "/ping":
            info = self.user()
            if not info:
                return self.send(401, {"error": "No autenticado"})
            with LOCK:
                db = load_db()
                u = db["users"].setdefault(info["sub"], {"sub": info["sub"], "first_seen": now,
                                                          "logins": 0, "banned": False})
                u.update(name=str(data.get("name", ""))[:16], email=info.get("email", ""),
                         last_seen=now, version=str(data.get("version", ""))[:10])
                u["logins"] += 1
                save_db(db)
            return self.send(200, {"banned": u["banned"]})
        if path == "/admin/news":
            if not self.admin():
                return
            title = str(data.get("title", "")).strip()[:80]
            text = str(data.get("body", "")).strip()[:1000]
            if not title or not text:
                return self.send(400, {"error": "Falta el título o el texto"})
            item = {"id": uuid.uuid4().hex[:8], "title": title, "body": text,
                    "author": "Nova Staff", "created": now}
            with LOCK:
                db = load_db()
                db["news"].insert(0, item)
                save_db(db)
            return self.send(200, item)
        if path == "/admin/ban":
            if not self.admin():
                return
            with LOCK:
                db = load_db()
                u = db["users"].get(str(data.get("sub", "")))
                if not u:
                    return self.send(404, {"error": "Usuario no encontrado"})
                if (u.get("email") or "").lower() == ADMIN_EMAIL:
                    return self.send(400, {"error": "No podés banearte a vos mismo"})
                u["banned"] = bool(data.get("banned"))
                save_db(db)
            return self.send(200, {"banned": u["banned"]})
        self.send(404)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/admin/news/"):
            if not self.admin():
                return
            nid = path.rsplit("/", 1)[1]
            with LOCK:
                db = load_db()
                db["news"] = [n for n in db["news"] if n["id"] != nid]
                save_db(db)
            return self.send(200)
        self.send(404)


if __name__ == "__main__":
    print(f"NovaServer en el puerto {PORT} (admin: {ADMIN_EMAIL})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
