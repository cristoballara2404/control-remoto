"""Prueba del relay SIN celular, todo en tu PC.

Antes: arranca el relay y la app de escritorio (o desktop/server.py). En la app pulsa
«Vincular nuevo dispositivo» y, cuando ejecutes esto, acepta el diálogo que aparece. Luego:
    python test_relay.py <PIN_QUE_MUESTRA_LA_APP> [ws://127.0.0.1:8080]

Hace 3 cosas: 1) se vincula por la red local, 2) se conecta por el relay con el
token, mueve el mouse 20 px y pide 3 frames de pantalla, 3) comprueba que el PIN
NO se acepta a través del relay.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from websockets.asyncio.client import connect


async def main(pin: str, relay: str):
    data_dir = Path(os.environ.get("CONTROLREMOTO_DIR") or Path(os.environ.get("APPDATA", Path.home())) / "ControlRemoto")
    pc_id = json.loads((data_dir / "data.json").read_text())["pc_id"]

    async with connect("ws://127.0.0.1:8765") as ws:
        await ws.send(json.dumps({"type": "pair", "pin": pin, "name": "test_relay"}))
        r = json.loads(await ws.recv())
    print("1) Vinculación local:", "OK" if r.get("ok") else r)
    if not r.get("ok"):
        return

    async with connect(f"{relay}/app") as ws:
        await ws.send(json.dumps({"type": "connect", "pc_id": pc_id}))
        await ws.send(json.dumps({"type": "auth", "device_id": r["device_id"], "token": r["token"]}))
        reply = json.loads(await ws.recv())
        print("2) Auth por relay:", reply)
        if reply.get("ok"):
            await ws.send(json.dumps({"type": "move", "dx": 20, "dy": 0}))
            await ws.send(json.dumps({"type": "stream", "on": True}))
            got = 0
            while got < 3:
                msg = await ws.recv()
                if isinstance(msg, bytes):  # los mensajes de texto son la posición del cursor
                    got += 1
                    print(f"   frame {got}: {len(msg) // 1024} KB")

    async with connect(f"{relay}/app") as ws:
        await ws.send(json.dumps({"type": "connect", "pc_id": pc_id}))
        await ws.send(json.dumps({"type": "pair", "pin": "000000", "name": "x"}))
        print("3) PIN por relay:", json.loads(await ws.recv()).get("code"))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "ws://127.0.0.1:8080"))
