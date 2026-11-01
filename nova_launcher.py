#!/usr/bin/env python3
"""
parche_nova.py - Agrega a NovaLauncher 0.8 (cuentas online / Supabase):
  * Banear / desbanear con motivo
  * Modo mantenimiento
  * Mensaje global
  * Novedades editables (aparecen arriba en "Últimas novedades")

Uso:  poné este archivo junto a nova_launcher.py y ejecutá:   python parche_nova.py
Hace una copia de seguridad (nova_launcher.py.bak) y NO toca nada si algo no coincide.
"""
import os
import py_compile
import shutil
import sys
import tempfile

PATCHES = []


def patch(name, anchor, replacement):
    PATCHES.append((name, anchor, replacement))


# ---------------------------------------------------------------- versión
patch("versión", r'''APP_VERSION = "0.8"''', r'''APP_VERSION = "0.9"''')

# ---------------------------------------------------------------- Cloud: métodos nuevos
_ANCHOR = r'''    def audit(self, n=150):
        return self.get(f"audit_log?select=*&order=at.desc&limit={int(n)}")
'''
patch("Cloud: métodos nuevos", _ANCHOR, _ANCHOR + r'''
    def send(self, method, path, payload=None, prefer=None):
        self._fresh()
        h = self._hdr()
        if prefer:
            h["Prefer"] = prefer
        return http_json(method, f"{self.url}/rest/v1/{path}", payload, h)

    def settings(self):
        rows = self.get("app_settings?select=key,value") or []
        return {r["key"]: r["value"] for r in rows}

    def announcements(self, n=8):
        return self.get(f"announcements?select=*&order=created_at.desc&limit={int(n)}") or []
''')

# ---------------------------------------------------------------- login: baneo con motivo, mantenimiento, mensaje global
patch("login: baneo/mantenimiento/mensaje", r'''        if not acct["enabled"]:
            raise CloudError("Tu cuenta está desactivada. Pedile al administrador que la active.")
        _safe(lambda: cloud.rpc("touch_login"))
        return acct
''', r'''        if not acct["enabled"]:
            why = str(prof.get("ban_reason") or "").strip()
            raise CloudError("Tu cuenta fue baneada." + (f"\nMotivo: {why}" if why else "")
                             + "\nPedile al administrador que te desbanee.")
        srv = {}
        try:
            srv = cloud.settings()
        except Exception:
            pass
        mnt = srv.get("maintenance") or {}
        if mnt.get("enabled") and acct["role"] not in ("owner", "admin"):
            raise CloudError(mnt.get("message") or "NovaLauncher está en mantenimiento. Volvé más tarde.")
        gmsg = srv.get("global_message") or {}
        if gmsg.get("enabled") and gmsg.get("text"):
            acct["_global_msg"] = gmsg["text"]
        _safe(lambda: cloud.rpc("touch_login"))
        return acct
''')

# ---------------------------------------------------------------- apply_session: cargar novedades y mostrar mensaje global
patch("apply_session", r'''        audit_event("inicio de sesión")
        save_all()
        refresh_all()
        show_page("play")
''', r'''        audit_event("inicio de sesión")
        save_all()
        refresh_all()
        show_page("play")
        if CLOUD and acct.get("online") and not acct.get("offline"):
            bg(load_news)
        gmsg_txt = acct.pop("_global_msg", "")
        if gmsg_txt:
            root.after(400, lambda: messagebox.showinfo("Aviso de NovaLauncher", gmsg_txt))
''')

# ---------------------------------------------------------------- inicio: novedades del owner arriba
patch("inicio: novedades", r'''        for w_ in news_body.winfo_children():
            w_.destroy()
        m = state["manifest"]
''', r'''        for w_ in news_body.winfo_children():
            w_.destroy()
        ann = (state.get("news") or [])[:2]
        for a_ in ann:
            nrow = tk.Frame(news_body, bg=HC)
            nrow.pack(fill="x", pady=px(3))
            tk.Label(nrow, text="NOVEDAD", font=(FONT, 8, "bold"), fg="white", bg=ACCENT, width=10).pack(
                side="left", ipady=px(3), anchor="n")
            ncol = tk.Frame(nrow, bg=HC)
            ncol.pack(side="left", fill="x", expand=True, padx=px(12))
            tk.Label(ncol, text=a_.get("title") or "", font=(FONT, 11, "bold"), fg=FG, bg=HC,
                     anchor="w").pack(fill="x")
            nbody = (a_.get("body") or "").strip().replace("\n", " ")
            if nbody:
                tk.Label(ncol, text=nbody[:140] + ("…" if len(nbody) > 140 else ""), font=(FONT, 9),
                         fg=MUTED, bg=HC, anchor="w", justify="left", wraplength=px(520)).pack(fill="x")
        m = state["manifest"]
''')

patch("inicio: cantidad de versiones", r'''            for v in m["versions"][:4]:''',
      r'''            for v in m["versions"][:max(1, 4 - 2 * len(ann))]:''')

# ---------------------------------------------------------------- admin: botón Banear / Desbanear
patch("admin: insignia", r'''            if not a["enabled"]:
                badges.append(("DESACTIVADA", "#c0455a"))
''', r'''            if not a["enabled"]:
                badges.append(("BANEADA", "#c0455a"))
''')

patch("admin: botón banear", r'''            if can:
                acts.append(("Activar" if not a["enabled"] else "Desactivar",
                             lambda x=a: remote_set_enabled(x, not x["enabled"]), CARD2, LINE, FG))
''', r'''            if can:
                acts.append(("Desbanear" if not a["enabled"] else "Banear",
                             lambda x=a: remote_ban(x), CARD2, RED if a["enabled"] else LINE,
                             "#ff8a9b" if a["enabled"] else FG))
''')

_ANCHOR = r'''    def remote_set_role(a, role):
        remote_action("Cambiando rol", lambda: cloud.rpc("owner_set_role", {"p_target": a["id"], "p_role": role}))
'''
patch("admin: remote_ban", _ANCHOR, _ANCHOR + r'''
    def remote_ban(a):
        qid = urllib.parse.quote(str(a["id"]))
        if a["enabled"]:
            why = simpledialog.askstring(APP_NAME, f"Motivo del baneo para {a['user']}:", parent=root)
            if why is None:
                return
            why = why.strip()
            audit_event("usuario baneado", a["user"] + (f" · {why}" if why else ""))

            def fn_ban():
                cloud.rpc("admin_set_enabled", {"p_target": a["id"], "p_enabled": False})
                if why:
                    _safe(lambda: cloud.send("PATCH", f"profiles?id=eq.{qid}", {"ban_reason": why}, "return=minimal"))
            remote_action("Usuario baneado", fn_ban)
        else:
            audit_event("usuario desbaneado", a["user"])

            def fn_unban():
                cloud.rpc("admin_set_enabled", {"p_target": a["id"], "p_enabled": True})
                _safe(lambda: cloud.send("PATCH", f"profiles?id=eq.{qid}", {"ban_reason": None}, "return=minimal"))
            remote_action("Usuario desbaneado", fn_unban)
''')

# ---------------------------------------------------------------- pump: mensajes nuevos
patch("pump", r'''                elif kind == "auth":
                    handle_auth(m)
''', r'''                elif kind == "news":
                    state["news"] = m[1]
                    refresh_home()
                    if state.get("ann_render"):
                        state["ann_render"]()
                elif kind == "call":
                    m[1](*m[2:])
                elif kind == "auth":
                    handle_auth(m)
''')

# ---------------------------------------------------------------- admin > Global (mantenimiento, mensaje global, novedades)
_ANCHOR = r'''bg=CARD, wraplength=px(700), justify="left").pack(anchor="w", padx=px(20), pady=(0, px(16)))'''
patch("admin: pestaña Global", _ANCHOR, _ANCHOR + r'''

        glob_status = tk.StringVar(value="")
        tk.Label(gv, textvariable=glob_status, font=(FONT, 9), fg=MUTED, bg=BG, anchor="w").pack(
            fill="x", pady=(px(8), px(4)))
        gsf = ScrollFrame(gv, BG)
        gsf.pack(fill="both", expand=True)
        gmain = gsf.inner

        def srv_card(title):
            c_ = card(gmain)
            c_.pack(fill="x", pady=(0, px(10)), padx=(0, px(8)))
            small_title(c_, title).pack(anchor="w", padx=px(20), pady=(px(14), px(4)))
            return c_

        def srv_text(parent, lines):
            t_ = tk.Text(parent, height=lines, wrap="word", font=(FONT, 10), bg=CARD2, fg=FG,
                         insertbackground=FG, relief="flat", highlightthickness=1,
                         highlightbackground=LINE, highlightcolor=ACCENT)
            t_.pack(fill="x", padx=px(20), pady=(px(2), px(8)))
            return t_

        def srv_check(parent, text, var):
            tk.Checkbutton(parent, text=text, variable=var, fg=FG, bg=CARD, selectcolor=CARD2,
                           activebackground=CARD, activeforeground=FG, font=(FONT, 10), bd=0,
                           highlightthickness=0, anchor="w").pack(fill="x", padx=px(16), pady=px(2))

        def gjob(work, done):
            def run():
                try:
                    res = work()
                except Exception as e:
                    res = e
                msgs.put(("call", done, res))
            bg(run)

        def save_setting(key, value, label):
            glob_status.set(f"Guardando {label.lower()}...")
            audit_event(f"{label.lower()} actualizado")

            def work():
                cloud.send("POST", "app_settings", {"key": key, "value": value},
                           "resolution=merge-duplicates,return=minimal")

            def done(res):
                if isinstance(res, Exception):
                    glob_status.set(f"Error: {res}")
                    messagebox.showwarning(APP_NAME, str(res))
                else:
                    glob_status.set(f"{label} guardado.")
            gjob(work, done)

        # --- mantenimiento
        mcard = srv_card("MODO MANTENIMIENTO")
        maint_v = tk.BooleanVar()
        srv_check(mcard, "Bloquear el launcher a los usuarios (los admins siguen entrando)", maint_v)
        maint_t = srv_text(mcard, 3)
        flat_btn(mcard, "Guardar", lambda: save_setting(
            "maintenance", {"enabled": bool(maint_v.get()), "message": maint_t.get("1.0", "end").strip()},
            "Mantenimiento"), bg=ACCENT, hover="#7ba3ff", fg="white", size=10).pack(
            anchor="w", padx=px(20), pady=(0, px(14)), ipadx=px(16), ipady=px(6))

        # --- mensaje global
        gmcard = srv_card("MENSAJE GLOBAL")
        gm_v = tk.BooleanVar()
        srv_check(gmcard, "Mostrar este aviso a todos al iniciar sesión", gm_v)
        gm_t = srv_text(gmcard, 3)
        flat_btn(gmcard, "Guardar", lambda: save_setting(
            "global_message", {"enabled": bool(gm_v.get()), "text": gm_t.get("1.0", "end").strip()},
            "Mensaje global"), bg=ACCENT, hover="#7ba3ff", fg="white", size=10).pack(
            anchor="w", padx=px(20), pady=(0, px(14)), ipadx=px(16), ipady=px(6))

        # --- novedades
        def load_news():
            try:
                rows = cloud.announcements()
            except Exception:
                rows = []
            rows = sorted(rows, key=lambda r_: not r_.get("pinned"))
            msgs.put(("news", rows))

        ncard = srv_card("NOVEDADES (se ven arriba en la pantalla de inicio)")
        tk.Label(ncard, text="Título", font=(FONT, 9), fg=MUTED, bg=CARD).pack(anchor="w", padx=px(20))
        nt_v = tk.StringVar()
        tk.Entry(ncard, textvariable=nt_v, font=(FONT, 11), bg=CARD2, fg=FG, insertbackground=FG,
                 relief="flat", highlightthickness=1, highlightbackground=LINE,
                 highlightcolor=ACCENT).pack(fill="x", padx=px(20), pady=(px(2), px(8)), ipady=px(6))
        tk.Label(ncard, text="Texto", font=(FONT, 9), fg=MUTED, bg=CARD).pack(anchor="w", padx=px(20))
        nb_t = srv_text(ncard, 4)

        def publish_news():
            title = nt_v.get().strip()
            body = nb_t.get("1.0", "end").strip()
            if not title:
                messagebox.showinfo(APP_NAME, "Escribí un título para la novedad.")
                return
            glob_status.set("Publicando novedad...")
            audit_event("novedad publicada", title[:80])

            def work():
                cloud.send("POST", "announcements", {"title": title, "body": body}, "return=minimal")

            def done(res):
                if isinstance(res, Exception):
                    glob_status.set(f"Error: {res}")
                    messagebox.showwarning(APP_NAME, str(res))
                    return
                nt_v.set("")
                nb_t.delete("1.0", "end")
                glob_status.set("Novedad publicada.")
                bg(load_news)
            gjob(work, done)

        flat_btn(ncard, "Publicar novedad", publish_news, bg=GREEN, hover=GREEN_H, fg="white", size=10).pack(
            anchor="w", padx=px(20), pady=(0, px(8)), ipadx=px(16), ipady=px(6))
        nlist = tk.Frame(ncard, bg=CARD)
        nlist.pack(fill="x", padx=px(20), pady=(px(4), px(14)))

        def delete_news(aid):
            if not messagebox.askokcancel(APP_NAME, "¿Borrar esta novedad?"):
                return
            audit_event("novedad borrada", str(aid))

            def work():
                rows = cloud.send("DELETE", f"announcements?id=eq.{urllib.parse.quote(str(aid))}", None,
                                  "return=representation")
                if not rows:
                    raise CloudError("No se pudo borrar (solo el dueño puede).")

            def done(res):
                if isinstance(res, Exception):
                    glob_status.set(f"Error: {res}")
                else:
                    glob_status.set("Novedad borrada.")
                    bg(load_news)
            gjob(work, done)

        def render_ann_list():
            for w_ in nlist.winfo_children():
                w_.destroy()
            items = state.get("news") or []
            if not items:
                tk.Label(nlist, text="Todavía no publicaste novedades.", font=(FONT, 9), fg=MUTED,
                         bg=CARD).pack(anchor="w")
            for r_ in items:
                nr = tk.Frame(nlist, bg=CARD2)
                nr.pack(fill="x", pady=px(3))
                tk.Label(nr, text=(r_.get("title") or "")[:60], font=(FONT, 10), fg=FG, bg=CARD2,
                         anchor="w").pack(side="left", padx=px(10), pady=px(6))
                flat_btn(nr, "Borrar", lambda i=r_.get("id"): delete_news(i), bg=CARD2, hover=RED,
                         fg="#ff8a9b").pack(side="right", padx=px(6), ipadx=px(8), ipady=px(3))
        state["ann_render"] = render_ann_list

        def refresh_global():
            glob_status.set("Cargando ajustes...")

            def done(res):
                if isinstance(res, Exception):
                    glob_status.set(f"No se pudo cargar: {res}")
                    return
                glob_status.set("")
                mnt = res.get("maintenance") or {}
                maint_v.set(bool(mnt.get("enabled")))
                maint_t.delete("1.0", "end")
                maint_t.insert("1.0", mnt.get("message") or "")
                gmm = res.get("global_message") or {}
                gm_v.set(bool(gmm.get("enabled")))
                gm_t.delete("1.0", "end")
                gm_t.insert("1.0", gmm.get("text") or "")
            gjob(cloud.settings, done)
            bg(load_news)
''')


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "nova_launcher.py")
    if not os.path.isfile(path):
        print(f"No encuentro {path}. Poné este script junto a nova_launcher.py.")
        return 1
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if "def remote_ban(" in src:
        print("Este launcher ya tiene el parche aplicado. No hice cambios.")
        return 0
    errors = []
    for name, anchor, repl in PATCHES:
        n = src.count(anchor)
        if n != 1:
            errors.append(f"  - {name}: encontré {n} coincidencias (tiene que ser 1)")
            continue
        src = src.replace(anchor, repl, 1)
    if errors:
        print("No apliqué nada porque algunas partes no coinciden con tu archivo:")
        print("\n".join(errors))
        print("Seguramente tu nova_launcher.py no es la versión 0.8 original. Pasame este mensaje y lo ajusto.")
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as t:
        t.write(src)
        tmp = t.name
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        os.remove(tmp)
        print("El resultado tendría un error de sintaxis, así que no toqué tu archivo:")
        print(e)
        return 1
    os.remove(tmp)
    shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("Listo. Parche aplicado a", path)
    print("Copia de seguridad en", path + ".bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
