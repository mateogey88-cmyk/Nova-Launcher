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
                    update_mod_versions()
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
                elif kind == "mod_results":
                    hits = m[1]
                    mod_state["hits"] = hits
                    mod_results.delete(0, "end")
                    for hit in hits:
                        title = hit.get("title") or hit.get("slug") or "Mod sin nombre"
                        downloads = hit.get("downloads", 0)
                        mod_results.insert("end", f"{title}   ·   {downloads:,} descargas")
                    mod_status.set(f"{len(hits)} resultados. Elegí uno para ver su descripción.")
                    if not hits:
                        mod_status.set("No encontré mods para esa búsqueda y versión.")
                elif kind == "mod_error":
                    mod_status.set(m[1])
                    messagebox.showerror("Tienda de mods", m[1])
                elif kind == "mod_installed":
                    mod_status.set(f"Instalado: {m[1]}  ·  carpeta: {m[2]}")
                    messagebox.showinfo(
                        "Tienda de mods",
                        f"Se descargó {m[1]} en la carpeta mods.\n\n"
                        "Importante: el perfil del juego debe tener instalado el loader "
                        "seleccionado (Fabric/Forge/NeoForge/Quilt). Este launcher todavía "
                        "no instala ni inicia automáticamente esos loaders."
                    )
                elif kind == "mod_search_done":
                    mod_state["busy"] = False
                    mod_search_btn.config(state="normal")
                    on_mod_select()
                elif kind == "mod_install_done":
                    mod_state["busy"] = False
                    mod_search_btn.config(state="normal")
                    sel = mod_results.curselection()
                    mod_install_btn.config(state="normal" if sel else "disabled")
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
