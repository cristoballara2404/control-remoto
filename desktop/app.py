"""Control Remoto: app de escritorio (Windows).

Muestra el PIN para vincular celulares, los dispositivos vinculados, quién está conectado
y las opciones de seguridad. Se queda en la bandeja del sistema al cerrar la ventana.
"""
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from tkinter import TclError, messagebox

import customtkinter as ctk

import core
from icons import make_icon

try:
    import pystray
except Exception:  # sin bandeja (otro sistema o falta de librería): la app funciona igual
    pystray = None

GREEN, ORANGE, RED, GRAY = "#44bd50", "#f0a030", "#d9534f", "#8a8f98"
CARD = "#1c2127"

SWITCHES = [
    ("require_approval", "Pedir mi aprobación al vincular un celular nuevo"),
    ("allow_audio", "Permitir compartir el sonido del PC"),
    ("view_only", "Solo ver (el celular no controla mouse ni teclado)"),
    ("autostart", "Iniciar con Windows"),
    ("start_minimized", "Iniciar minimizado en la bandeja"),
]


# ---------- Windows: inicio automático e instancia única ----------
def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}" --minimized'


def set_autostart(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE)
    try:
        if enabled:
            winreg.SetValueEx(key, "ControlRemoto", 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, "ControlRemoto")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


_mutex = None  # se guarda para que Windows no lo libere mientras la app vive


def already_running(wait_seconds: float = 0) -> bool:
    """¿Ya hay otra copia abierta? Con wait_seconds espera a que la copia anterior termine (se usa al reiniciar)."""
    global _mutex
    if sys.platform != "win32":
        return False
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    deadline = time.time() + wait_seconds
    while True:
        handle = kernel32.CreateMutexW(None, False, "ControlRemoto_SingleInstance")
        if ctypes.get_last_error() != 183:  # 183 = ERROR_ALREADY_EXISTS
            _mutex = handle
            return False
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        if time.time() >= deadline:
            return True
        time.sleep(0.3)


# ---------- Interfaz ----------
class App(ctk.CTk):
    def __init__(self, start_hidden: bool = False):
        super().__init__()
        self.title("Control Remoto")
        self.geometry("600x820")
        self.minsize(560, 640)

        self.ctrl = core.Controller()
        self.tray = None
        self.tray_color = None
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.last_error = ""
        self.devices_sig = None
        self.pair_mode = None
        self.approval = None  # (id, ventana)
        self.hint_shown = False

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if core.SETTINGS["autostart"]:
            try:
                set_autostart(True)  # por si cambió la ruta del programa
            except Exception:
                pass
        self.ctrl.start()
        self._setup_tray()
        self.refresh_all()
        self.after(200, self._poll)
        if start_hidden and self.tray is not None:
            self.after(100, self.withdraw)

    # ----- construcción -----
    def _card(self, title: str | None = None) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.body, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", padx=4, pady=6)
        if title:
            ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=15, weight="bold"),
                         anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        return card

    def _build(self) -> None:
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        # Estado
        card = self._card()
        ctk.CTkLabel(card, text="Control Remoto", font=ctk.CTkFont(size=24, weight="bold"),
                     anchor="w").pack(fill="x", padx=16, pady=(14, 0))
        self.lbl_status = ctk.CTkLabel(card, text="● Iniciando...", anchor="w",
                                       font=ctk.CTkFont(size=14, weight="bold"), text_color=GRAY)
        self.lbl_status.pack(fill="x", padx=16, pady=(2, 0))
        self.lbl_addr = ctk.CTkLabel(card, text="", anchor="w", text_color=GRAY)
        self.lbl_addr.pack(fill="x", padx=16, pady=(0, 2))
        self.lbl_relay = ctk.CTkLabel(card, text="Acceso remoto: iniciando...", anchor="w", justify="left",
                                      text_color=GRAY, wraplength=500)
        self.lbl_relay.pack(fill="x", padx=16, pady=(0, 8))
        self.banner = ctk.CTkFrame(card, fg_color="#5a3d12", corner_radius=10)
        self.lbl_banner = ctk.CTkLabel(self.banner, text="", anchor="w", justify="left", wraplength=360)
        self.lbl_banner.pack(side="left", fill="x", expand=True, padx=12, pady=10)
        ctk.CTkButton(self.banner, text="Desconectar", width=110, fg_color=RED, hover_color="#b3423f",
                      command=self.disconnect_all).pack(side="right", padx=10)
        self.actions_row = ctk.CTkFrame(card, fg_color="transparent")
        self.actions_row.pack(fill="x", padx=16, pady=(4, 14))
        self.btn_block = ctk.CTkButton(self.actions_row, text="Bloquear acceso", fg_color="#7a2b2b",
                                       hover_color="#963535", command=self.toggle_block)
        self.btn_block.pack(side="left")
        ctk.CTkButton(self.actions_row, text="Salir", width=80, fg_color="#3a4048", hover_color="#4a525c",
                      command=self.quit_app).pack(side="right")
        ctk.CTkButton(self.actions_row, text="Reiniciar", width=100, fg_color="#2f5f86", hover_color="#3a79ac",
                      command=self.restart_app).pack(side="right", padx=(0, 8))

        # Vincular
        card = self._card("Vincular un celular")
        ctk.CTkLabel(card, anchor="w", justify="left", wraplength=500, text_color=GRAY,
                     text="1) Pulsa «Vincular nuevo dispositivo».\n"
                          "2) En la app del celular toca «Vincular» y escribe este PIN "
                          "(deja la IP vacía para que busque el PC).\n"
                          "3) Acepta aquí cuando te pregunte."
                     ).pack(fill="x", padx=16)
        self.lbl_pin = ctk.CTkLabel(card, text="••• •••", font=ctk.CTkFont(family="Consolas", size=54, weight="bold"),
                                    text_color=GRAY)
        self.lbl_pin.pack(pady=(10, 0))
        self.lbl_pin_time = ctk.CTkLabel(card, text="", text_color=GRAY)
        self.lbl_pin_time.pack()
        self.btn_pair = ctk.CTkButton(card, text="Vincular nuevo dispositivo", height=40)
        self.btn_pair.pack(pady=(8, 16))
        self.pair_default_color = self.btn_pair.cget("fg_color")

        # Dispositivos
        card = self._card("Dispositivos vinculados")
        self.devices_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.devices_frame.pack(fill="x", padx=12, pady=(0, 12))

        # Opciones
        card = self._card("Seguridad y opciones")
        self.switches = {}
        for key, text in SWITCHES:
            sw = ctk.CTkSwitch(card, text=text, command=lambda k=key: self.on_switch(k))
            sw.pack(anchor="w", padx=16, pady=5)
            self.switches[key] = sw
        ctk.CTkLabel(card, text="", height=6).pack()

        # Pie
        self.lbl_stats = ctk.CTkLabel(self, text="", text_color=GRAY, anchor="w")
        self.lbl_stats.pack(fill="x", padx=18)
        self.lbl_msg = ctk.CTkLabel(self, text="", text_color=ORANGE, anchor="w", justify="left", wraplength=540)
        self.lbl_msg.pack(fill="x", padx=18, pady=(0, 8))

    # ----- refrescos -----
    def refresh_all(self) -> None:
        self.refresh_status()
        self.refresh_pairing()
        self.refresh_devices(force=True)
        self.refresh_switches()

    def refresh_status(self) -> None:
        sessions = core.list_sessions()
        if self.last_error:
            self.lbl_status.configure(text="● " + self.last_error, text_color=RED)
        elif not core.STATE["accepting"]:
            self.lbl_status.configure(text="● Acceso bloqueado", text_color=RED)
        elif self.ctrl.running:
            self.lbl_status.configure(text="● Listo para conectar", text_color=GREEN)
        else:
            self.lbl_status.configure(text="● Iniciando...", text_color=GRAY)
        self.lbl_addr.configure(text=f"{socket.gethostname()}  ·  {self.ctrl.ip}:{core.PORT}")

        if sessions:
            def where(s):
                return "por internet · " + s["ip"][6:] if s["ip"].startswith("relay:") else s["ip"]
            names = "\n".join(f"●  {s['name']}  ({where(s)})" for s in sessions)
            self.lbl_banner.configure(text="Alguien está usando este PC ahora:\n" + names)
            self.banner.pack(fill="x", padx=16, pady=(0, 8), before=self.actions_row)
        else:
            self.banner.pack_forget()

        if core.STATE["accepting"]:
            self.btn_block.configure(text="Bloquear acceso", fg_color="#7a2b2b", hover_color="#963535")
        else:
            self.btn_block.configure(text="Desbloquear acceso", fg_color="#2f6b3a", hover_color="#3a8248")

        color = RED if (not core.STATE["accepting"] or self.last_error) else ORANGE if sessions else GREEN
        if self.tray is not None and color != self.tray_color:
            self.tray_color = color
            try:
                self.tray.icon = make_icon(color)
            except Exception:
                pass

    def refresh_pairing(self) -> None:
        remaining = core.STATE["pairing_until"] - time.time()
        if remaining > 0:
            pin = core.STATE["pin"]
            self.lbl_pin.configure(text=f"{pin[:3]} {pin[3:]}", text_color="white")
            self.lbl_pin_time.configure(text=f"Expira en {int(remaining) // 60}:{int(remaining) % 60:02d}")
            if self.pair_mode != "on":
                self.pair_mode = "on"
                self.btn_pair.configure(text="Cancelar vinculación", fg_color=GRAY, command=core.stop_pairing)
        else:
            self.lbl_pin.configure(text="••• •••", text_color=GRAY)
            self.lbl_pin_time.configure(text="")
            if self.pair_mode != "off":
                self.pair_mode = "off"
                self.btn_pair.configure(text="Vincular nuevo dispositivo", fg_color=self.pair_default_color,
                                        command=core.start_pairing)

    def refresh_devices(self, force: bool = False) -> None:
        devices = core.list_devices()
        sig = tuple((d["id"], d["name"], d["last_seen"], d["connected"]) for d in devices)
        if sig == self.devices_sig and not force:
            return
        self.devices_sig = sig
        for child in self.devices_frame.winfo_children():
            child.destroy()
        if not devices:
            ctk.CTkLabel(self.devices_frame, text="Aún no hay dispositivos vinculados.",
                         text_color=GRAY).pack(anchor="w", padx=8, pady=6)
        for d in devices:
            row = ctk.CTkFrame(self.devices_frame, fg_color="#252b32", corner_radius=10)
            row.pack(fill="x", pady=4)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True, padx=12, pady=8)
            ctk.CTkLabel(info, text=d["name"], font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
            sub = "● conectado ahora" if d["connected"] else f"Último uso: {d['last_seen'] or 'nunca'}"
            ctk.CTkLabel(info, text=sub, anchor="w", text_color=GREEN if d["connected"] else GRAY).pack(fill="x")
            ctk.CTkButton(row, text="Revocar", width=80, fg_color="#7a2b2b", hover_color="#963535",
                          command=lambda dev=d: self.revoke(dev)).pack(side="right", padx=(4, 10))
            if d["connected"]:
                ctk.CTkButton(row, text="Desconectar", width=96, fg_color=RED, hover_color="#b3423f",
                              command=lambda dev=d: self.disconnect_device(dev["id"])).pack(side="right", padx=4)

    def refresh_switches(self) -> None:
        for key, sw in self.switches.items():
            if core.SETTINGS.get(key):
                sw.select()
            else:
                sw.deselect()

    # ----- acciones -----
    def on_switch(self, key: str) -> None:
        core.SETTINGS[key] = bool(self.switches[key].get())
        core.save_settings()
        if key == "autostart":
            try:
                set_autostart(core.SETTINGS[key])
            except Exception as e:
                messagebox.showerror("Control Remoto", f"No se pudo cambiar el inicio automático: {e}")

    def toggle_block(self) -> None:
        core.set_accepting(not core.STATE["accepting"])

    def disconnect_all(self) -> None:
        for s in core.list_sessions():
            core.disconnect_session(s["id"])

    def disconnect_device(self, device_id: str) -> None:
        for s in core.list_sessions():
            if s["device_id"] == device_id:
                core.disconnect_session(s["id"])

    def revoke(self, device: dict) -> None:
        if messagebox.askyesno("Revocar dispositivo",
                               f"¿Revocar «{device['name']}»?\nTendrá que vincularse de nuevo para volver a entrar."):
            core.revoke_device(device["id"])

    # ----- eventos del servidor -----
    def _poll(self) -> None:
        self.after(200, self._poll)
        try:
            while True:
                kind, data = core.EVENTS.get_nowait()
                try:
                    self._on_event(kind, data)
                except Exception as e:  # una falla visual no debe detener el resto
                    print("Error en evento", kind, e)
        except queue.Empty:
            pass
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.refresh_pairing()

    def _on_event(self, kind: str, data: dict) -> None:
        if kind == "server_started":
            self.last_error = ""
            self.refresh_status()
        elif kind == "server_error":
            self.last_error = "Error del servidor"
            self.lbl_msg.configure(text=data["message"])
            self.refresh_status()
        elif kind in ("ip_changed", "accepting_changed", "server_stopped"):
            self.refresh_status()
        elif kind in ("pin_changed", "pairing_changed"):
            self.refresh_pairing()
        elif kind == "devices_changed":
            self.refresh_devices(force=True)
        elif kind == "session_started":
            self.refresh_status()
            self.refresh_devices(force=True)
            self._notify(f"«{data['name']}» se conectó a este PC")
        elif kind == "session_ended":
            self.refresh_status()
            self.refresh_devices(force=True)
        elif kind == "pair_request":
            self._ask_approval(data)
        elif kind == "pair_request_closed":
            if self.approval and self.approval[0] == data["id"]:
                self.approval[1].destroy()
                self.approval = None
        elif kind == "relay_status":
            color = {"connected": GREEN, "connecting": ORANGE, "error": RED}.get(data["state"], GRAY)
            self.lbl_relay.configure(text="Acceso remoto: " + data["message"], text_color=color)
        elif kind == "stats":
            self.lbl_stats.configure(text="Transmisión: " + data["text"])
        elif kind == "security":
            self.lbl_msg.configure(text=data["message"])
            self._notify(data["message"])
        elif kind == "log":
            self.lbl_msg.configure(text=data["message"])

    def _ask_approval(self, req: dict) -> None:
        self.show_window()
        dlg = ctk.CTkToplevel(self)
        dlg.title("Nuevo dispositivo")
        dlg.geometry("460x290")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        self.approval = (req["id"], dlg)
        state = {"left": int(req["timeout"]), "done": False}

        ctk.CTkLabel(dlg, text="¿Permitir este dispositivo?",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(22, 6))
        ctk.CTkLabel(dlg, justify="center",
                     text=f"«{req['name']}»\nIP: {req['ip']}\n\n"
                          "Podrá ver la pantalla y controlar este PC.").pack(pady=4)
        lbl = ctk.CTkLabel(dlg, text="", text_color=GRAY)
        lbl.pack(pady=4)

        def answer(allow: bool) -> None:
            if state["done"]:
                return
            state["done"] = True
            if self.approval and self.approval[0] == req["id"]:
                self.approval = None
            core.resolve_approval(req["id"], allow)
            try:
                dlg.destroy()
            except TclError:
                pass

        buttons = ctk.CTkFrame(dlg, fg_color="transparent")
        buttons.pack(pady=10)
        ctk.CTkButton(buttons, text="Permitir", width=140, fg_color="#2f6b3a", hover_color="#3a8248",
                      command=lambda: answer(True)).pack(side="left", padx=8)
        ctk.CTkButton(buttons, text="Rechazar", width=140, fg_color=RED, hover_color="#b3423f",
                      command=lambda: answer(False)).pack(side="left", padx=8)
        dlg.protocol("WM_DELETE_WINDOW", lambda: answer(False))
        dlg.after(150, lambda: (dlg.lift(), dlg.focus_force()))

        def tick() -> None:
            if state["done"]:
                return
            try:
                lbl.configure(text=f"Se rechaza solo en {state['left']} s")
            except TclError:
                return
            state["left"] -= 1
            if state["left"] < 0:
                answer(False)
            else:
                dlg.after(1000, tick)

        tick()

    # ----- bandeja del sistema -----
    def _setup_tray(self) -> None:
        if pystray is None:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Abrir Control Remoto", lambda: self.ui_queue.put(self.show_window), default=True),
                pystray.MenuItem(
                    lambda item: "Desbloquear acceso" if not core.STATE["accepting"] else "Bloquear acceso",
                    lambda: self.ui_queue.put(self.toggle_block)),
                pystray.MenuItem("Reiniciar", lambda: self.ui_queue.put(self.restart_app)),
                pystray.MenuItem("Salir", lambda: self.ui_queue.put(self.quit_app)),
            )
            self.tray = pystray.Icon("ControlRemoto", make_icon(GREEN), "Control Remoto", menu)
            self.tray_color = GREEN
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            self.tray = None

    def _notify(self, message: str) -> None:
        if self.tray is not None:
            try:
                self.tray.notify(message, "Control Remoto")
            except Exception:
                pass

    def show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_close(self) -> None:
        if self.tray is not None:
            self.withdraw()
            if not self.hint_shown:
                self.hint_shown = True
                self._notify("Sigue activo en la bandeja del sistema. Para cerrarlo del todo: clic derecho → Salir.")
        else:
            self.quit_app()

    def _restart_command(self) -> list:
        """Comando para abrir una copia nueva de la app (el .exe instalado o 'python app.py')."""
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, os.path.abspath(__file__)]
        cmd.append("--restarted")
        if str(self.state()) == "withdrawn":  # si estaba en la bandeja, vuelve a la bandeja
            cmd.append("--minimized")
        return cmd

    def restart_app(self) -> None:
        if core.list_sessions() and not messagebox.askyesno(
                "Reiniciar", "Hay un dispositivo conectado y se desconectará. ¿Reiniciar de todos modos?"):
            return
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # La copia nueva espera a que esta termine (libera el puerto y el candado de instancia única)
            subprocess.Popen(self._restart_command(), creationflags=flags, close_fds=True)
        except Exception as e:
            messagebox.showerror("Control Remoto", f"No se pudo reiniciar: {e}")
            return
        self.quit_app(confirm=False)

    def quit_app(self, confirm: bool = True) -> None:
        if confirm and core.list_sessions() and not messagebox.askyesno(
                "Salir", "Hay un dispositivo conectado. ¿Cerrar Control Remoto de todos modos?"):
            return
        try:
            self.ctrl.stop()
        finally:
            if self.tray is not None:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            self.destroy()


def main() -> None:
    if already_running(wait_seconds=20 if "--restarted" in sys.argv else 0):
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "Control Remoto ya se está ejecutando. Búscalo en la bandeja del sistema.",
                "Control Remoto", 0x40)
        return
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    start_hidden = "--minimized" in sys.argv or core.SETTINGS["start_minimized"]
    App(start_hidden).mainloop()


if __name__ == "__main__":
    main()
