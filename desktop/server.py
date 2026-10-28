"""Modo consola (sin interfaz): python server.py

Para uso diario, usa la app de escritorio (app.py). Este modo deja la vinculación siempre abierta
y sin aprobación en el PC, igual que las versiones anteriores.
"""
import time

import core

core.HEADLESS = True

if __name__ == "__main__":
    controller = core.Controller()
    controller.start()
    print(f"Dispositivos vinculados: {len(core.DATA['devices'])}")
    print(f"PIN de emparejamiento: {core.STATE['pin']}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        controller.stop()
