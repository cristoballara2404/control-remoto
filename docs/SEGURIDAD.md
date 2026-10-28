# Modelo de seguridad

Este software permite **ver y controlar un PC**. Se diseñó pensando en ese riesgo.

## Qué se protege
- Que nadie vea o controle el PC sin que su dueño lo autorice.
- Que robar el archivo de datos del PC o interceptar el relay no permita suplantar a un celular.

## Controles implementados
| Amenaza | Control |
|---|---|
| Vincular un dispositivo sin permiso | Modo vinculación explícito (PIN de un solo uso, 2 min) **y** aprobación en el PC |
| Fuerza bruta del PIN | 5 fallos por IP → bloqueo de 1 min; 5 PIN incorrectos de cualquier IP → se cierra la vinculación y se renueva el PIN |
| Robo del archivo de datos del PC | Solo se guarda el **hash SHA-256** de cada token; los tokens son aleatorios de 256 bits |
| Robo de credenciales del celular | Token cifrado con AES-GCM, con clave en el **Android Keystore** |
| Acceso desde internet a un puerto abierto | No se abren puertos: el PC se conecta *hacia afuera* al relay; las conexiones directas solo se aceptan desde IP locales/privadas |
| Relay malicioso o comprometido | No almacena credenciales; el PC verifica cada token. Registro del PC en el relay con clave compartida |
| Control no deseado | Aviso visible de quién está conectado, **Desconectar**, **Bloquear acceso**, **Revocar**, modo **Solo ver**, sonido opcional |
| Abuso del relay | Límite de conexiones por IP y de sesiones por PC |
| Nombres engañosos al vincular | El nombre del dispositivo se limpia (caracteres imprimibles, 40 caracteres) |
| Parada de emergencia de pyautogui | Desactivada a propósito (bloqueaba el cursor en las esquinas); el control de emergencia es la interfaz del PC |

## Limitaciones conocidas
- En la **red local el tráfico no va cifrado** (`ws://`). En una red compartida o pública, otros podrían observarlo.
  Pendiente: TLS con certificado propio y *certificate pinning*.
- Por internet, el cifrado llega hasta el relay (TLS de Caddy); el relay ve el contenido. Pendiente: cifrado de extremo a extremo.
- Las ventanas con permisos de administrador y las pantallas UAC no se pueden controlar (aislamiento de Windows).
- El instalador no está firmado: SmartScreen y algunos antivirus pueden advertir.

## Reportar una vulnerabilidad
Abre un *issue* privado o escribe al autor antes de publicar los detalles.
