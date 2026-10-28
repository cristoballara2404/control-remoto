"""Relay: une al PC (agente) con el celular a través de internet.

- El relay NO guarda credenciales de dispositivos: la verificación de tokens
  sigue ocurriendo en el PC. El relay solo reenvía mensajes.
- Se ejecuta detrás de Caddy/Nginx, que pone el TLS (wss://).

Variables de entorno:
  RELAY_KEY    clave compartida con tu PC (obligatoria)
  RELAY_HOST   por defecto 127.0.0.1
  RELAY_PORT   por defecto 8080

Instalar:  pip install websockets
Ejecutar:  RELAY_KEY=... python relay.py
"""
import asyncio
import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict, deque

from websockets.asyncio.server import serve

RELAY_KEY = os.environ.get("RELAY_KEY", "")
HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RELAY_PORT", "8080"))

SESSION_WAIT = 6          # segundos esperando que el PC abra la sesión
MAX_CONN_PER_MIN = 30     # conexiones nuevas por minuto por IP de celular
MAX_SESSIONS_PER_PC = 4   # sesiones simultáneas por PC
PC_ID_RE = re.compile(r"^[0-9a-f]{32}$")

agents: dict[str, object] = {}                 # pc_id -> websocket de control del PC
pending: dict[str, asyncio.Future] = {}        # sid -> futuro con el websocket del PC
active: dict[str, int] = defaultdict(int)      # pc_id -> sesiones activas
hits: dict[str, deque] = defaultdict(deque)    # ip -> instantes de conexión


def key_ok(key) -> bool:
    return bool(RELAY_KEY) and hmac.compare_digest(str(key).encode(), RELAY_KEY.encode())


def client_ip(ws) -> str:
    ip = ws.remote_address[0]
    if ip in ("127.0.0.1", "::1"):  # detrás de Caddy: usar la IP real que reenvía
        xff = ws.request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[-1].strip()
    return ip


def rate_limited(ip: str) -> bool:
    now = time.time()
    q = hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    q.append(now)
    return len(q) > MAX_CONN_PER_MIN


async def send_json(ws, obj: dict) -> None:
    await ws.send(json.dumps(obj))


async def pipe(src, dst) -> None:
    """Copia mensajes (texto o binario) de src a dst hasta que uno se cierre."""
    try:
        async for msg in src:
            await dst.send(msg)
    except Exception:
        pass
    finally:
        await dst.close()


async def first_message(ws) -> dict | None:
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    except Exception:
        return None


# ---------- /agent : conexión de control del PC ----------
async def handle_agent(ws) -> None:
    first = await first_message(ws)
    pc_id = str(first.get("pc_id", "")) if first else ""
    if not first or first.get("type") != "register" or not key_ok(first.get("key")) \
            or not PC_ID_RE.match(pc_id):
        await send_json(ws, {"ok": False})
        await ws.close()
        return
    old = agents.get(pc_id)
    agents[pc_id] = ws
    if old is not None:
        await old.close()  # una sola conexión de control por PC
    await send_json(ws, {"ok": True})
    print(f"[relay] PC conectado: {pc_id[:8]}")
    try:
        async for _ in ws:  # el PC no envía nada más; solo mantenemos la conexión
            pass
    except Exception:
        pass  # el PC se cayó sin cerrar limpiamente
    finally:
        if agents.get(pc_id) is ws:
            del agents[pc_id]
        print(f"[relay] PC desconectado: {pc_id[:8]}")


# ---------- /app : celular ----------
async def handle_app(ws) -> None:
    ip = client_ip(ws)
    if rate_limited(ip):
        await send_json(ws, {"ok": False, "code": "rate_limited", "error": "Demasiados intentos"})
        await ws.close()
        return

    first = await first_message(ws)
    pc_id = str(first.get("pc_id", "")) if first else ""
    agent = agents.get(pc_id) if first and first.get("type") == "connect" else None
    if agent is None or active[pc_id] >= MAX_SESSIONS_PER_PC:
        await send_json(ws, {"ok": False, "code": "pc_offline",
                             "error": "El PC no está conectado al relay"})
        await ws.close()
        return

    sid = secrets.token_urlsafe(16)
    fut = asyncio.get_running_loop().create_future()
    pending[sid] = fut
    try:
        await send_json(agent, {"type": "session", "sid": sid, "client_ip": ip})
        agent_ws = await asyncio.wait_for(fut, timeout=SESSION_WAIT)
    except Exception:
        await send_json(ws, {"ok": False, "code": "pc_offline",
                             "error": "El PC no respondió"})
        await ws.close()
        return
    finally:
        pending.pop(sid, None)

    active[pc_id] += 1
    print(f"[relay] sesión {sid[:6]} desde {ip} -> PC {pc_id[:8]}")
    t1 = asyncio.create_task(pipe(ws, agent_ws))
    t2 = asyncio.create_task(pipe(agent_ws, ws))
    try:
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t1, t2):
            t.cancel()
        await ws.close()
        await agent_ws.close()
        active[pc_id] -= 1
        print(f"[relay] sesión {sid[:6]} terminada")


# ---------- /session : el PC abre una conexión por cada celular ----------
async def handle_session(ws) -> None:
    first = await first_message(ws)
    fut = pending.get(first.get("sid")) if first else None
    if not first or first.get("type") != "attach" or not key_ok(first.get("key")) \
            or fut is None or fut.done():
        await ws.close()
        return
    fut.set_result(ws)
    await ws.wait_closed()  # handle_app se encarga de reenviar mientras tanto


async def router(ws) -> None:
    path = ws.request.path.split("?")[0]
    if path == "/agent":
        await handle_agent(ws)
    elif path == "/app":
        await handle_app(ws)
    elif path == "/session":
        await handle_session(ws)
    else:
        await ws.close()


async def main() -> None:
    if not RELAY_KEY:
        raise SystemExit("Falta la variable de entorno RELAY_KEY")
    print(f"Relay escuchando en {HOST}:{PORT}")
    async with serve(router, HOST, PORT, max_size=2**21):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
