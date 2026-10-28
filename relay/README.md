# Relay

Servidor intermedio que une al PC con el celular cuando no están en la misma red. **No guarda credenciales de
dispositivos**: solo reenvía mensajes; el PC sigue verificando cada token.

```
Celular --wss--> [Caddy :443 (TLS)] --ws--> relay.py :8080 <--wss-- PC (conexión de salida)
```

## Despliegue (VM Ubuntu con IP pública y un dominio)

1. Abre los puertos 80 y 443 (firewall del proveedor y `iptables`/`ufw`).
2. `sudo apt install -y python3-venv caddy`
3. Copia `relay.py` a `~/relay/` y crea el entorno: `python3 -m venv venv && ./venv/bin/pip install -r requirements.txt`
4. Crea `/etc/relay.env` a partir de `deploy/relay.env.example` (`chmod 600`).
5. Instala `deploy/relay.service` en `/etc/systemd/system/` y ejecuta `sudo systemctl enable --now relay`.
6. Pon tu dominio en `/etc/caddy/Caddyfile` (ver `deploy/Caddyfile.example`) y `sudo systemctl restart caddy`.
   Caddy obtiene el certificado de Let's Encrypt automáticamente.
7. En el PC, crea `%APPDATA%\ControlRemoto\config.json` con `relay_url` (`wss://tu-dominio`) y `relay_key`.

## Protocolo del relay

| Ruta | Quién | Mensaje inicial | Qué hace |
|---|---|---|---|
| `/agent` | PC | `{"type":"register","pc_id","key"}` | Conexión de control del PC |
| `/app` | Celular | `{"type":"connect","pc_id"}` | Pide una sesión; el relay avisa al PC |
| `/session` | PC | `{"type":"attach","sid","key"}` | El PC abre un canal por cada celular y el relay lo une con el celular |

Límites: 30 conexiones nuevas por minuto por IP y 4 sesiones simultáneas por PC.
