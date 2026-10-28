"""Núcleo de Control Remoto (sin interfaz gráfica).

Lo usan la app de escritorio (app.py) y el modo consola (server.py).
Seguridad incluida:
  - Vincular un celular nuevo solo es posible con el "modo vinculación" activo (PIN de un solo uso, 2 min).
  - Aprobación en el PC de cada dispositivo nuevo.
  - Tokens por dispositivo (en el PC solo se guarda su hash); se pueden revocar.
  - Bloqueo por intentos fallidos (por IP y global) y cierre del modo vinculación.
  - Las conexiones directas solo se aceptan desde redes locales/privadas.
  - Sesiones visibles, con botón para desconectar, "bloquear acceso" y modo "solo ver".
"""
import asyncio
import hashlib
import hmac
import io
import ipaddress
import itertools
import json
import os
import queue
import random
import secrets
import shutil
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mss
import pyautogui
from PIL import Image
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

APP_NAME = "ControlRemoto"
PORT = 8765
SERVICE_TYPE = "_ctrlremoto._tcp.local."

PAIRING_SECONDS = 120     # cuánto dura el modo vinculación
APPROVAL_TIMEOUT = 30     # segundos para aprobar un dispositivo nuevo
MAX_FAILS = 5             # intentos fallidos por IP antes de bloquear
LOCK_SECONDS = 60
MAX_PIN_FAILS = 5         # PIN incorrectos (de cualquier IP) antes de cerrar la vinculación
MAX_PENDING_AUTH = 20     # conexiones simultáneas sin autenticar

# Presets de calidad del video: (ancho en px, calidad JPEG, FPS objetivo)
PRESETS = {
    "normal": (800, 45, 20),
    "alta": (1280, 55, 20),
    "maxima": (1600, 65, 15),
}
DEFAULT_PRESET = "normal"

HEADLESS = False  # True en server.py: sin interfaz (vinculación siempre abierta y sin aprobación)

pyautogui.PAUSE = 0
# pyautogui trae una "parada de emergencia": si el cursor toca CUALQUIERA de las 4 esquinas de la pantalla,
# rechaza todas las órdenes y el cursor queda pegado. Aquí no hace falta: la ventana del PC tiene
# «Desconectar» y «Bloquear acceso», y el celular debe poder llegar a las esquinas (botón de cerrar, etc.).
pyautogui.FAILSAFE = False


# ---------------------------------------------------------------------
#  Carpeta de datos (%APPDATA%\ControlRemoto) y migración
# ---------------------------------------------------------------------
def _data_dir() -> Path:
    base = os.environ.get("CONTROLREMOTO_DIR")
    path = Path(base) if base else Path(os.environ.get("APPDATA") or Path.home() / ".config") / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


DATA_DIR = _data_dir()
DATA_FILE = DATA_DIR / "data.json"          # id del PC y dispositivos vinculados (hash de tokens)
SETTINGS_FILE = DATA_DIR / "settings.json"  # opciones de la app
CONFIG_FILE = DATA_DIR / "config.json"      # relay_url y relay_key (opcional)

for _name in ("data.json", "config.json"):  # trae lo que tenías junto al server.py antiguo
    _legacy = Path(__file__).with_name(_name)
    if _legacy.exists() and not (DATA_DIR / _name).exists():
        shutil.copy2(_legacy, DATA_DIR / _name)

LOCK = threading.RLock()

DEFAULT_SETTINGS = {
    "require_approval": True,   # pedir aprobación en el PC al vincular
    "allow_audio": True,        # permitir compartir el sonido del PC
    "view_only": False,         # solo ver: ignora mouse y teclado
    "autostart": False,         # iniciar con Windows
    "start_minimized": False,   # iniciar en la bandeja
}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return settings


SETTINGS = load_settings()


def save_settings() -> None:
    with LOCK:
        SETTINGS_FILE.write_text(json.dumps(SETTINGS, indent=2), encoding="utf-8")


def load_data() -> dict:
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault("pc_id", uuid.uuid4().hex)
    data.setdefault("devices", {})
    DATA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


DATA = load_data()


def save_data() -> None:
    with LOCK:
        DATA_FILE.write_text(json.dumps(DATA, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------
#  Eventos hacia la interfaz (cola segura entre hilos)
# ---------------------------------------------------------------------
EVENTS: "queue.Queue[tuple[str, dict]]" = queue.Queue()


def emit(kind: str, **data) -> None:
    if HEADLESS:
        _console_event(kind, data)
    else:
        EVENTS.put((kind, data))


def _console_event(kind: str, data: dict) -> None:
    if kind == "server_started":
        print(f"IP del PC: {data['ip']}  |  Puerto: {data['port']}")
    elif kind == "pin_changed":
        print(f"PIN de emparejamiento: {STATE['pin']}")
    elif kind == "session_started":
        print(f"Dispositivo conectado: {data['name']} ({data['ip']})")
    elif kind == "session_ended":
        print(f"Dispositivo desconectado: {data['name']}")
    elif kind == "relay_status":
        print(f"Relay: {data['message']}")
    elif kind in ("log", "stats", "security", "server_error"):
        print(data.get("message") or data.get("text", ""))


# ---------------------------------------------------------------------
#  Estado de seguridad
# ---------------------------------------------------------------------
STATE = {
    "accepting": True,       # False = acceso bloqueado
    "pin": "",
    "pairing_until": 0.0,
    "pin_fails": 0,
    "approving": False,
    "pending_auth": 0,
}
FAILED: dict[str, list] = {}  # ip -> [intentos, bloqueado_hasta]
PENDING: dict[str, asyncio.Future] = {}
SESSIONS: dict[int, dict] = {}
_SESSION_IDS = itertools.count(1)
LOOP: asyncio.AbstractEventLoop | None = None


def new_pin() -> None:
    STATE["pin"] = f"{random.SystemRandom().randint(0, 999999):06d}"
    STATE["pin_fails"] = 0
    emit("pin_changed")


new_pin()


def start_pairing(seconds: int = PAIRING_SECONDS) -> None:
    new_pin()
    STATE["pairing_until"] = time.time() + seconds
    emit("pairing_changed")


def stop_pairing() -> None:
    STATE["pairing_until"] = 0.0
    emit("pairing_changed")


def pairing_active() -> bool:
    return HEADLESS or time.time() < STATE["pairing_until"]


def register_pin_failure() -> None:
    """Cuenta PIN incorrectos de cualquier IP; al llegar al límite cierra la vinculación."""
    STATE["pin_fails"] += 1
    if STATE["pin_fails"] >= MAX_PIN_FAILS:
        emit("security", message="Demasiados PIN incorrectos: se cerró la vinculación y se renovó el PIN")
        if not HEADLESS:
            stop_pairing()
        new_pin()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def is_local_ip(ip: str) -> bool:
    """Redes privadas, loopback, link-local y la de Tailscale (100.64.0.0/10)."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    return addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10")


def is_locked(ip: str) -> bool:
    entry = FAILED.get(ip)
    return bool(entry) and entry[1] > time.time()


def register_fail(ip: str) -> None:
    entry = FAILED.setdefault(ip, [0, 0.0])
    entry[0] += 1
    if entry[0] >= MAX_FAILS:
        entry[0] = 0
        entry[1] = time.time() + LOCK_SECONDS
        emit("security", message=f"Bloqueado por 1 minuto: demasiados intentos fallidos desde {ip}")


# ---------------------------------------------------------------------
#  Dispositivos vinculados y sesiones
# ---------------------------------------------------------------------
def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _clean_name(name) -> str:
    cleaned = "".join(c for c in str(name) if c.isprintable()).strip()[:40]
    return cleaned or "Dispositivo"


def list_devices() -> list[dict]:
    with LOCK:
        connected = {s["device_id"] for s in SESSIONS.values()}
        return [
            {"id": i, "name": d["name"], "created": d.get("created", ""),
             "last_seen": d.get("last_seen", ""), "connected": i in connected}
            for i, d in DATA["devices"].items()
        ]


def list_sessions() -> list[dict]:
    with LOCK:
        return [{k: v for k, v in s.items() if k != "ws"} for s in SESSIONS.values()]


def disconnect_session(sid: int) -> None:
    session = SESSIONS.get(sid)
    if session and LOOP:
        asyncio.run_coroutine_threadsafe(session["ws"].close(1000, "desconectado desde el PC"), LOOP)


def revoke_device(device_id: str) -> None:
    """Borra el vínculo: el celular tendrá que vincularse de nuevo."""
    with LOCK:
        DATA["devices"].pop(device_id, None)
        save_data()
        victims = [s["id"] for s in SESSIONS.values() if s["device_id"] == device_id]
    for sid in victims:
        disconnect_session(sid)
    emit("devices_changed")


def set_accepting(on: bool) -> None:
    """Botón de pánico: bloquea conexiones nuevas y corta las actuales."""
    STATE["accepting"] = on
    if not on:
        for sid in list(SESSIONS):
            disconnect_session(sid)
    emit("accepting_changed")


def resolve_approval(req_id: str, allow: bool) -> None:
    fut = PENDING.get(req_id)
    if fut and LOOP:
        LOOP.call_soon_threadsafe(lambda: fut.done() or fut.set_result(allow))


async def ask_approval(name: str, ip: str) -> bool:
    if HEADLESS or not SETTINGS["require_approval"]:
        return True
    if STATE["approving"]:
        return False
    STATE["approving"] = True
    req_id = uuid.uuid4().hex
    fut = asyncio.get_running_loop().create_future()
    PENDING[req_id] = fut
    emit("pair_request", id=req_id, name=name, ip=ip, timeout=APPROVAL_TIMEOUT)
    try:
        return bool(await asyncio.wait_for(fut, APPROVAL_TIMEOUT))
    except asyncio.TimeoutError:
        return False
    finally:
        PENDING.pop(req_id, None)
        STATE["approving"] = False
        emit("pair_request_closed", id=req_id)


# ---------------------------------------------------------------------
#  Autenticación
# ---------------------------------------------------------------------
async def _reject(websocket, code: str, message: str, ip: str | None = None, fail: bool = False) -> None:
    if fail and ip:
        register_fail(ip)
    try:
        await websocket.send(json.dumps({"ok": False, "code": code, "error": message}))
        await websocket.close()
    except Exception:
        pass


def relay_url_for_phone() -> str | None:
    cfg, _ = read_relay_config()
    return cfg["relay_url"] if cfg else None


async def authenticate(websocket, ip: str, via_relay: bool = False):
    """Devuelve (device_id, nombre) si la conexión queda autorizada; si no, None."""
    if not STATE["accepting"]:
        await _reject(websocket, "blocked", "El acceso a este PC está bloqueado")
        return None
    if not via_relay and not is_local_ip(ip):
        emit("security", message=f"Conexión rechazada desde una IP fuera de la red local: {ip}")
        await websocket.close()
        return None
    if is_locked(ip):
        await _reject(websocket, "locked", "Demasiados intentos, espera un minuto")
        return None

    try:
        msg = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
    except Exception:
        return None
    kind = msg.get("type")

    if kind == "pair":  # primera vez: PIN + aprobación -> se entrega un token
        if via_relay:
            await _reject(websocket, "pair_local_only", "Vincula el celular estando en la misma red que el PC")
            return None
        if not pairing_active():
            await _reject(websocket, "pairing_off", "En el PC, abre «Vincular nuevo dispositivo» y vuelve a intentar")
            return None
        if not safe_equal(str(msg.get("pin", "")), STATE["pin"]):
            register_pin_failure()
            await _reject(websocket, "bad_pin", "PIN incorrecto", ip, fail=True)
            return None
        name = _clean_name(msg.get("name", "Dispositivo"))
        if not await ask_approval(name, ip):
            await _reject(websocket, "denied", "El PC no aprobó la vinculación")
            return None
        if not pairing_active():  # el usuario canceló mientras decidía
            await _reject(websocket, "pairing_off", "La vinculación se canceló en el PC")
            return None
        device_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        with LOCK:
            DATA["devices"][device_id] = {
                "name": name,
                "token_hash": hash_token(token),  # nunca guardamos el token en claro
                "created": _now(),
                "last_seen": _now(),
            }
            save_data()
        FAILED.pop(ip, None)
        if not HEADLESS:
            stop_pairing()  # el PIN sirve una sola vez
        new_pin()
        reply = {"ok": True, "device_id": device_id, "token": token,
                 "pc_id": DATA["pc_id"], "pc_name": socket.gethostname()}
        if relay_url_for_phone():
            reply["relay_url"] = relay_url_for_phone()  # solo la dirección; la clave del relay nunca sale del PC
        try:
            await websocket.send(json.dumps(reply))
        except Exception:
            return None
        emit("devices_changed")
        return device_id, name

    if kind == "auth":  # siguientes veces: device_id + token
        device_id = str(msg.get("device_id", ""))
        dev = DATA["devices"].get(device_id)
        if dev and safe_equal(dev["token_hash"], hash_token(str(msg.get("token", "")))):
            FAILED.pop(ip, None)
            with LOCK:
                dev["last_seen"] = _now()
                save_data()
            reply = {"ok": True}
            if relay_url_for_phone():
                reply["relay_url"] = relay_url_for_phone()
            await websocket.send(json.dumps(reply))
            return device_id, dev["name"]
        await _reject(websocket, "unknown_device", "El PC no reconoce este vínculo", ip, fail=True)
        return None

    await _reject(websocket, "bad_request", "Solicitud inválida", ip, fail=True)
    return None


# ---------------------------------------------------------------------
#  Captura de pantalla y streaming
# ---------------------------------------------------------------------
CAPTURE_POOL = ThreadPoolExecutor(max_workers=1)
_local = threading.local()


def _sct():
    if not hasattr(_local, "sct"):
        _local.sct = mss.mss()
    return _local.sct


def grab_frame(width: int, quality: int) -> tuple[bytes, float, dict]:
    """Captura el monitor principal. Devuelve (JPEG etiquetado, ms, posición del cursor)."""
    t0 = time.perf_counter()
    sct = _sct()
    monitor = sct.monitors[1]
    shot = sct.grab(monitor)
    cx, cy = pyautogui.position()  # mss no dibuja el cursor: lo enviamos aparte
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    width = min(width, img.width)  # nunca ampliar
    height = int(img.height * width / img.width)
    img = img.resize((width, height), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    buf.write(b"\x01")  # etiqueta: imagen
    img.save(buf, "JPEG", quality=quality)
    cursor = {
        "x": cx - monitor["left"], "y": cy - monitor["top"],
        "w": shot.size[0], "h": shot.size[1],
    }
    return buf.getvalue(), (time.perf_counter() - t0) * 1000, cursor


async def stream_frames(websocket, state: dict) -> None:
    loop = asyncio.get_running_loop()
    frames, total_bytes, total_ms = 0, 0, 0.0
    window_start = time.perf_counter()
    try:
        while True:
            if not state["streaming"]:
                frames, total_bytes, total_ms = 0, 0, 0.0
                window_start = time.perf_counter()
                await asyncio.sleep(0.05)
                continue

            width, quality, preset_fps = PRESETS[state["preset"]]
            fps = state["fps"] or preset_fps
            start = time.perf_counter()
            frame, ms, cursor = await loop.run_in_executor(CAPTURE_POOL, grab_frame, width, quality)
            await websocket.send(json.dumps({"type": "cursor", **cursor}))
            await websocket.send(frame)

            frames += 1
            total_bytes += len(frame)
            total_ms += ms

            now = time.perf_counter()
            if now - window_start >= 2:
                secs = now - window_start
                emit("stats", text=(
                    f"{frames / secs:.0f} FPS · {total_bytes * 8 / secs / 1e6:.1f} Mbps · "
                    f"{total_bytes / frames / 1024:.0f} KB/frame · captura {total_ms / frames:.0f} ms"))
                frames, total_bytes, total_ms = 0, 0, 0.0
                window_start = now

            await asyncio.sleep(max(0.0, 1 / fps - (time.perf_counter() - start)))
    except Exception:
        return


# ---------------------------------------------------------------------
#  Audio (lo que suena en el PC)
# ---------------------------------------------------------------------
AUDIO_RATE = 48000
AUDIO_BLOCK = AUDIO_RATE // 50  # 20 ms por bloque
AUDIO_FORMATS = {"normal": (24000, 1), "alta": (48000, 2)}  # preset -> (frecuencia enviada, canales)


def _init_com_in_this_thread() -> None:
    """soundcard solo inicia COM en el hilo que la importa por primera vez. Cada activación del sonido crea un
    hilo nuevo, así que hay que iniciarlo aquí o aparece el error 0x800401f0 (COM no inicializado).
    IMPORTANTE: llamar DESPUÉS de importar soundcard. Si COM ya estaba iniciado cuando soundcard se importa,
    su propio arranque recibe un aviso (S_FALSE) que toma por error (0x100000001)."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED; si ya estaba iniciado no pasa nada


def audio_capture(stop: threading.Event, preset: str, put) -> None:
    """Hilo: captura lo que suena en el PC (loopback WASAPI) y lo entrega como PCM de 16 bits."""
    import numpy as np
    import soundcard as sc

    _init_com_in_this_thread()

    speaker = sc.default_speaker()
    mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    # Siempre se graba en estéreo: en Windows la grabación mono entrega basura
    with mic.recorder(samplerate=AUDIO_RATE, channels=2, blocksize=AUDIO_BLOCK) as rec:
        while not stop.is_set():
            data = rec.record(numframes=AUDIO_BLOCK)
            if len(data) == 0:
                time.sleep(0.005)
                continue
            if preset == "normal":  # mono y a la mitad de frecuencia: 4 veces menos datos
                mono = data.mean(axis=1)
                n = len(mono) // 2 * 2
                data = mono[:n].reshape(-1, 2).mean(axis=1)
            pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
            put(b"\x02" + pcm.tobytes())  # etiqueta: audio


async def audio_sender(websocket, preset: str) -> None:
    loop = asyncio.get_running_loop()
    queue_: asyncio.Queue = asyncio.Queue(maxsize=25)  # ~0,5 s; si el celular se atrasa, se descarta lo viejo
    stop = threading.Event()

    def put(item) -> None:  # lo llama el hilo de captura
        def _put():
            if queue_.full():
                queue_.get_nowait()
            queue_.put_nowait(item)
        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # el servidor se está cerrando

    def run() -> None:
        try:
            audio_capture(stop, preset, put)
        except ImportError:
            put("Falta instalar en el PC: pip install soundcard numpy")
        except Exception as e:
            put(f"{type(e).__name__}: {e}")

    rate, channels = AUDIO_FORMATS[preset]
    try:
        await websocket.send(json.dumps({"type": "audio_format", "rate": rate, "channels": channels}))
        threading.Thread(target=run, daemon=True).start()
        while True:
            item = await queue_.get()
            if isinstance(item, str):  # error de captura
                await websocket.send(json.dumps({"type": "audio_error", "message": item}))
                return
            await websocket.send(item)
    except Exception:
        return
    finally:
        stop.set()


def stop_audio(state: dict) -> None:
    task = state.get("audio")
    if task:
        task.cancel()
        state["audio"] = None


async def set_audio(websocket, state: dict, on: bool, preset) -> None:
    stop_audio(state)
    if not on:
        return
    if not SETTINGS["allow_audio"]:
        await websocket.send(json.dumps({"type": "audio_error", "message": "El PC no permite compartir el sonido"}))
        return
    preset = preset if preset in AUDIO_FORMATS else "normal"
    state["audio"] = asyncio.create_task(audio_sender(websocket, preset))


# ---------------------------------------------------------------------
#  Comandos de mouse y teclado
# ---------------------------------------------------------------------
try:  # opcional: permite escribir tildes, ñ y símbolos
    from pynput.keyboard import Controller as _Keyboard
    _keyboard = _Keyboard()
except Exception:
    _keyboard = None


def type_text(text: str) -> None:
    if _keyboard is not None:
        _keyboard.type(text)
    else:
        pyautogui.write(text, interval=0.01)  # solo caracteres ASCII


def wheel(delta: int) -> None:
    """Rueda vertical. delta en unidades de rueda: 120 = una muesca (positivo = hacia arriba).
    Ojo: pyautogui.scroll() en Windows manda el número crudo, así que scroll(1) es 1/120 de muesca (casi nada)."""
    if delta == 0:
        return
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta, 0)  # MOUSEEVENTF_WHEEL
    else:
        pyautogui.scroll(round(delta / 120))


INPUT_KINDS = {"move", "click", "down", "up", "scroll", "text", "key"}


def handle_command(msg: dict, state: dict) -> None:
    kind = msg.get("type")
    if kind in INPUT_KINDS and SETTINGS["view_only"]:
        return  # modo "solo ver"
    if kind == "move":
        pyautogui.moveRel(int(msg["dx"]), int(msg["dy"]))
    elif kind == "click":
        clicks = max(1, min(3, int(msg.get("clicks", 1))))
        pyautogui.click(button=msg.get("button", "left"), clicks=clicks)
    elif kind == "down":  # mantener el clic (arrastrar, seleccionar)
        pyautogui.mouseDown(button=msg.get("button", "left"))
        state["dragging"] = True
    elif kind == "up":
        pyautogui.mouseUp(button=msg.get("button", "left"))
        state["dragging"] = False
    elif kind == "scroll":
        # "delta" va en unidades de rueda de Windows (120 = una muesca); "amount" (muescas) se mantiene por compatibilidad
        delta = int(msg["delta"]) if "delta" in msg else int(msg.get("amount", 0)) * 120
        wheel(max(-2400, min(2400, delta)))
    elif kind == "text":
        type_text(str(msg["text"]))
    elif kind == "key":
        pyautogui.hotkey(*msg["keys"])
    elif kind == "stream":
        state["streaming"] = bool(msg.get("on"))
    elif kind == "quality":
        if msg.get("preset") in PRESETS:
            state["preset"] = msg["preset"]
    elif kind == "fps":  # 0 = automático
        state["fps"] = max(0, min(60, int(msg.get("value", 0))))


# ---------------------------------------------------------------------
#  Atención de cada celular
# ---------------------------------------------------------------------
def _register_session(ws, device_id: str, name: str, ip: str, via_relay: bool) -> int:
    sid = next(_SESSION_IDS)
    with LOCK:
        SESSIONS[sid] = {"id": sid, "ws": ws, "device_id": device_id, "name": name,
                         "ip": ip, "via_relay": via_relay, "since": time.time()}
    emit("session_started", id=sid, name=name, ip=ip, via_relay=via_relay)
    return sid


def _unregister_session(sid: int) -> None:
    with LOCK:
        session = SESSIONS.pop(sid, None)
    if session:
        emit("session_ended", id=sid, name=session["name"])


async def handler(websocket, client_ip: str | None = None, via_relay: bool = False):
    ip = client_ip or websocket.remote_address[0]
    if STATE["pending_auth"] >= MAX_PENDING_AUTH:
        await websocket.close()
        return
    STATE["pending_auth"] += 1
    try:
        ident = await authenticate(websocket, ip, via_relay)
    finally:
        STATE["pending_auth"] -= 1
    if ident is None:
        return
    device_id, name = ident
    sid = _register_session(websocket, device_id, name, ip, via_relay)
    if device_id not in DATA["devices"]:  # lo revocaron justo ahora
        _unregister_session(sid)
        await websocket.close()
        return

    state = {"streaming": False, "preset": DEFAULT_PRESET, "fps": 0, "dragging": False, "audio": None}
    streamer = asyncio.create_task(stream_frames(websocket, state))
    try:
        async for raw in websocket:
            if isinstance(raw, bytes):
                continue
            try:
                msg = json.loads(raw)
                if msg.get("type") == "audio":
                    await set_audio(websocket, state, bool(msg.get("on")), msg.get("preset"))
                else:
                    handle_command(msg, state)
            except Exception as e:
                emit("log", message=f"Comando inválido: {e}")
    finally:
        streamer.cancel()
        stop_audio(state)
        if state["dragging"]:  # no dejar el botón del mouse apretado
            try:
                pyautogui.mouseUp()
            except Exception:
                pass
        _unregister_session(sid)


# ---------------------------------------------------------------------
#  Relay (acceso desde otras redes, opcional)
# ---------------------------------------------------------------------
def read_relay_config() -> tuple[dict | None, str]:
    """Lee config.json. Devuelve (configuración, problema)."""
    if not CONFIG_FILE.exists():
        return None, "sin configurar (falta config.json en %APPDATA%\\ControlRemoto)"
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))  # utf-8-sig: el Bloc de notas agrega un BOM
    except Exception as e:
        return None, f"config.json no es un JSON válido ({e})"
    if not isinstance(cfg, dict) or not cfg.get("relay_url") or not cfg.get("relay_key"):
        return None, "config.json debe tener «relay_url» y «relay_key»"
    url = str(cfg["relay_url"]).strip().rstrip("/")
    if not url.startswith(("ws://", "wss://")):
        return None, "«relay_url» debe empezar con wss://"
    return {"relay_url": url, "relay_key": str(cfg["relay_key"]).strip()}, ""


RELAY_TASKS: set = set()


async def run_relay_session(cfg: dict, msg: dict) -> None:
    """Un celular entra por el relay: abrimos una conexión de salida y lo atendemos con el mismo handler."""
    try:
        async with ws_connect(cfg["relay_url"] + "/session", max_size=2**21) as ws:
            await ws.send(json.dumps({"type": "attach", "sid": msg["sid"], "key": cfg["relay_key"]}))
            await handler(ws, client_ip="relay:" + str(msg.get("client_ip", "?")), via_relay=True)
    except Exception as e:
        emit("log", message=f"Sesión por relay terminó: {e}")


async def relay_agent() -> None:
    """Mantiene la conexión de salida con el relay. Relee config.json solo: no hace falta reiniciar la app."""
    delay = 1
    while True:
        cfg, problem = read_relay_config()
        if cfg is None:
            emit("relay_status", state="off", message=problem)
            await asyncio.sleep(5)
            continue
        try:
            emit("relay_status", state="connecting", message="conectando...")
            async with ws_connect(cfg["relay_url"] + "/agent", max_size=2**21) as ws:
                await ws.send(json.dumps({"type": "register", "pc_id": DATA["pc_id"], "key": cfg["relay_key"]}))
                reply = json.loads(await ws.recv())
                if not reply.get("ok"):
                    emit("relay_status", state="error",
                         message="el relay rechazó la clave: revisa que relay_key sea igual a la de la VM")
                    delay = 10
                else:
                    emit("relay_status", state="connected", message="conectado, acceso remoto activo")
                    delay = 1
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "session":
                            task = asyncio.create_task(run_relay_session(cfg, msg))
                            RELAY_TASKS.add(task)
                            task.add_done_callback(RELAY_TASKS.discard)
                    emit("relay_status", state="error", message="se cortó la conexión con el relay, reintentando...")
        except Exception as e:
            emit("relay_status", state="error", message=f"{type(e).__name__}: {e}")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


# ---------------------------------------------------------------------
#  Servidor (se ejecuta en un hilo propio)
# ---------------------------------------------------------------------
def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Controller:
    """Arranca y detiene el servidor sin bloquear la interfaz."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.running = False
        self.ip = local_ip()
        self._stop: asyncio.Event | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if LOOP and self._stop:
            LOOP.call_soon_threadsafe(self._stop.set)
        if self.thread:
            self.thread.join(timeout=3)

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as e:
            emit("server_error", message=str(e))

    async def _main(self) -> None:
        global LOOP
        LOOP = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            async with serve(handler, "0.0.0.0", PORT):
                self.running = True
                self.ip = local_ip()
                emit("server_started", ip=self.ip, port=PORT)
                tasks = [asyncio.create_task(self._mdns()), asyncio.create_task(relay_agent())]
                await self._stop.wait()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except OSError as e:
            emit("server_error", message=f"No se pudo abrir el puerto {PORT} (¿ya hay otra copia abierta?): {e}")
        finally:
            self.running = False
            emit("server_stopped")

    async def _mdns(self) -> None:
        """Anuncia el PC en la red local y se actualiza si cambia la IP."""
        azc = None
        info = None
        current_ip = None
        try:
            azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            while True:
                ip = local_ip()
                if ip != current_ip and not ip.startswith("127."):
                    if info is not None:
                        await azc.async_unregister_service(info)
                    info = ServiceInfo(
                        SERVICE_TYPE,
                        f"ControlRemoto-{DATA['pc_id'][:8]}.{SERVICE_TYPE}",
                        addresses=[socket.inet_aton(ip)],
                        port=PORT,
                        properties={"pc_id": DATA["pc_id"], "name": socket.gethostname()},
                    )
                    await azc.async_register_service(info)
                    current_ip = ip
                    self.ip = ip
                    emit("ip_changed", ip=ip)
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            emit("log", message=f"Búsqueda automática (mDNS) no disponible: {e}")
        finally:
            if azc is not None:
                try:
                    if info is not None:
                        await azc.async_unregister_service(info)
                    await azc.async_close()
                except Exception:
                    pass
