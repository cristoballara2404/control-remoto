# Arquitectura

## Componentes

| Componente | Tecnología | Función |
|---|---|---|
| App Android | Kotlin, Jetpack Compose, OkHttp, AudioTrack, NsdManager | Pantalla remota, gestos, teclado, sonido |
| App de escritorio (Windows) | Python, asyncio/websockets, customtkinter, pystray | Servidor, ventana de control, bandeja |
| Relay | Python, websockets, Caddy, systemd | Acceso desde otras redes sin abrir puertos |
| Instalador | PyInstaller, Inno Setup | `.exe` con regla de firewall para redes privadas |

## Flujo: red local

```mermaid
sequenceDiagram
    participant C as Celular
    participant P as PC (app)
    P->>P: Anuncia el servicio por mDNS (_ctrlremoto._tcp)
    C->>P: Busca por mDNS / última IP
    C->>P: auth {device_id, token}
    P-->>C: ok (+ dirección del relay)
    C->>P: stream on, quality, fps
    loop cada frame
        P-->>C: cursor {x,y,w,h}
        P-->>C: binario 0x01 + JPEG
    end
    C->>P: move / click / key / scroll
```

## Flujo: vinculación (solo red local)

```mermaid
sequenceDiagram
    participant U as Usuario en el PC
    participant P as PC (app)
    participant C as Celular
    U->>P: "Vincular nuevo dispositivo" (PIN de 6 dígitos, 2 min)
    C->>P: pair {pin, nombre}
    P->>U: ¿Permitir este dispositivo?
    U->>P: Permitir
    P-->>C: device_id + token (solo el hash queda en el PC)
    Note over P: El PIN se invalida y la vinculación se cierra
```

## Flujo: por internet (relay)

```mermaid
sequenceDiagram
    participant C as Celular
    participant R as Relay (TLS)
    participant P as PC (app)
    P->>R: /agent register (conexión de salida, siempre abierta)
    C->>R: /app connect {pc_id}
    R->>P: session {sid, ip del celular}
    P->>R: /session attach {sid}
    C->>R: auth {device_id, token}
    R->>P: (reenvía tal cual)
    P-->>C: mismo protocolo que en red local
```

## Protocolo (JSON por WebSocket, más mensajes binarios)

**Celular → PC:** `pair`, `auth`, `move`, `click` (botón, clics), `down`/`up`, `scroll`, `text`, `key`,
`stream` (on), `quality` (preset), `fps` (valor), `audio` (on, preset).

**PC → celular:** respuestas `{ok, code, error}`, `cursor {x,y,w,h}`, `audio_format {rate,channels}`,
`audio_error {message}`.

**Binarios:** el primer byte es la etiqueta: `0x01` = imagen JPEG, `0x02` = audio PCM de 16 bits.

## Decisiones de diseño

- **El cursor se dibuja en el celular**, con la posición real enviada aparte: la captura de pantalla no lo incluye
  y así se puede agrandar y hacer zoom que sigue al cursor.
- **Relé sin estado de seguridad:** el relay solo reenvía; la autenticación ocurre en el PC.
  Un relay comprometido no puede suplantar a un celular vinculado.
- **Fallback de DNS sobre HTTPS** en Android: si el operador móvil no resuelve el dominio del relay, la app lo
  resuelve consultando a Cloudflare y Google por IP.
- **Captura de audio** por loopback WASAPI en un hilo propio con COM inicializado por hilo.
