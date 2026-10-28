package com.example.myapplication
// IMPORTANTE: deja tu propia línea "package ..." original de Android Studio
// (por ejemplo: package com.example.myapplication) como primera línea del archivo.

import android.content.Context
import android.content.pm.ActivityInfo
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.positionChange
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.Dns
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONArray
import org.json.JSONObject
import java.net.InetAddress
import java.net.URLEncoder
import java.net.UnknownHostException
import java.security.KeyStore
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlin.math.abs

// =====================================================================
//  Almacenamiento seguro: AES-GCM con una clave del Android Keystore
// =====================================================================
class SecureStore(context: Context) {
    private val prefs = context.getSharedPreferences("vinculo_pc", Context.MODE_PRIVATE)
    private val alias = "ctrlremoto_key"

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(alias, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(
                alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build()
        )
        return gen.generateKey()
    }

    fun put(name: String, value: String) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val encrypted = cipher.doFinal(value.toByteArray())
        val packed = Base64.encodeToString(cipher.iv + encrypted, Base64.NO_WRAP)
        prefs.edit().putString(name, packed).apply()
    }

    fun get(name: String): String? {
        val raw = prefs.getString(name, null) ?: return null
        return try {
            val bytes = Base64.decode(raw, Base64.NO_WRAP)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes.copyOfRange(0, 12)))
            String(cipher.doFinal(bytes, 12, bytes.size - 12))
        } catch (e: Exception) {
            null
        }
    }

    fun clear() {
        prefs.edit().clear().apply()
    }
}

// =====================================================================
//  Descubrimiento del PC en la red local (mDNS / NSD)
// =====================================================================
object Discovery {
    /** Busca el PC anunciado por el servidor. Si pcId != null, solo acepta ese PC. */
    fun find(context: Context, pcId: String?, timeoutMs: Long, onResult: (String?) -> Unit) {
        val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
        val handler = Handler(Looper.getMainLooper())
        val done = AtomicBoolean(false)
        var discovery: NsdManager.DiscoveryListener? = null

        fun finish(ip: String?) {
            if (!done.compareAndSet(false, true)) return
            handler.removeCallbacksAndMessages(null)
            try {
                discovery?.let { nsd.stopServiceDiscovery(it) }
            } catch (_: Exception) {
            }
            onResult(ip)
        }

        discovery = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onServiceLost(serviceInfo: NsdServiceInfo) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) = finish(null)
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                nsd.resolveService(serviceInfo, object : NsdManager.ResolveListener {
                    override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {}
                    override fun onServiceResolved(info: NsdServiceInfo) {
                        val id = info.attributes["pc_id"]?.let { String(it) }
                        if (pcId == null || id == pcId) finish(info.host?.hostAddress)
                    }
                })
            }
        }
        nsd.discoverServices("_ctrlremoto._tcp", NsdManager.PROTOCOL_DNS_SD, discovery)
        handler.postDelayed({ finish(null) }, timeoutMs)
    }
}

// =====================================================================
//  Cliente WebSocket
// =====================================================================
interface ClientEvents {
    fun onReply(json: JSONObject)      // mensajes de texto del servidor (respuestas, cursor)
    fun onFrame(bitmap: Bitmap)        // frames de pantalla (JPEG)
    fun onAudio(data: ByteArray)       // audio del PC (PCM de 16 bits)
    fun onFailure(msg: String)         // no se pudo conectar / se cayó la conexión
    fun onClosed()                     // cierre normal
}

/**
 * DNS del sistema y, si no puede resolver el nombre, DNS sobre HTTPS (Cloudflare y Google, por IP).
 * Algunos operadores móviles no resuelven dominios como duckdns.org; así la app funciona igual.
 */
class FallbackDns : Dns {
    private val doh = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.SECONDS)
        .build()

    // Se consultan por IP, así no dependen del DNS que está fallando
    private val endpoints = listOf(
        "https://1.1.1.1/dns-query?type=A&name=",
        "https://8.8.8.8/resolve?type=A&name="
    )

    override fun lookup(hostname: String): List<InetAddress> {
        try {
            return Dns.SYSTEM.lookup(hostname)
        } catch (e: UnknownHostException) {
            val name = URLEncoder.encode(hostname, "UTF-8")
            for (base in endpoints) {
                val found = try {
                    resolveWithDoh(base + name)
                } catch (_: Exception) {
                    emptyList()
                }
                if (found.isNotEmpty()) return found
            }
            throw e
        }
    }

    private fun resolveWithDoh(url: String): List<InetAddress> {
        val req = Request.Builder().url(url).header("accept", "application/dns-json").build()
        return doh.newCall(req).execute().use { resp ->
            val text = if (resp.isSuccessful) resp.body?.string() else null
            val answers = text?.let { JSONObject(it).optJSONArray("Answer") }
            val out = ArrayList<InetAddress>()
            if (answers != null) {
                for (i in 0 until answers.length()) {
                    val a = answers.getJSONObject(i)
                    if (a.optInt("type") == 1) out.add(InetAddress.getByName(a.getString("data"))) // registros A
                }
            }
            out
        }
    }
}

class RemoteClient {
    private val http = OkHttpClient.Builder()
        .dns(FallbackDns())
        .connectTimeout(3, TimeUnit.SECONDS)
        .pingInterval(15, TimeUnit.SECONDS) // detecta rápido una conexión caída
        .build()
    private val httpRelay = http.newBuilder()
        .connectTimeout(10, TimeUnit.SECONDS) // por internet tarda más que en la red local
        .build()
    private var ws: WebSocket? = null

    @Volatile
    private var gen = 0 // descarta callbacks de conexiones viejas

    /** preamble: mensaje que se envía antes del hello (el relay lo usa para saber a qué PC conectar). */
    fun connect(
        url: String,
        hello: JSONObject,
        events: ClientEvents,
        preamble: JSONObject? = null,
        viaRelay: Boolean = false
    ) {
        ws?.cancel()
        gen += 1
        val myGen = gen
        val req = Request.Builder().url(url).build()
        ws = (if (viaRelay) httpRelay else http).newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (myGen != gen) return
                if (preamble != null) webSocket.send(preamble.toString())
                webSocket.send(hello.toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (myGen == gen) events.onReply(JSONObject(text))
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (myGen != gen) return
                val data = bytes.toByteArray()
                if (data.size < 2) return
                when (data[0].toInt()) {
                    1 -> BitmapFactory.decodeByteArray(data, 1, data.size - 1)?.let(events::onFrame)
                    2 -> events.onAudio(data.copyOfRange(1, data.size))
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (myGen == gen) events.onFailure(t.message ?: "error de red")
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (myGen == gen) events.onClosed()
            }
        })
    }

    fun send(json: JSONObject) {
        ws?.send(json.toString())
    }

    fun close() {
        gen += 1
        ws?.close(1000, null)
        ws = null
    }
}

// =====================================================================
//  Audio: reproduce el sonido del PC (PCM de 16 bits)
// =====================================================================
class AudioPlayer {
    private val queue = LinkedBlockingQueue<ByteArray>()
    @Volatile
    private var running = false
    @Volatile
    private var track: AudioTrack? = null

    @Synchronized
    fun start(rate: Int, channels: Int, volume: Float) {
        stop()
        val mask = if (channels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val minBuf = AudioTrack.getMinBufferSize(rate, mask, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) return
        val t = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(rate)
                    .setChannelMask(mask)
                    .build()
            )
            .setBufferSizeInBytes(minBuf * 2)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        t.setVolume(volume)
        t.play()
        track = t
        queue.clear()
        running = true
        Thread {
            // Esperar un poco de audio acumulado para evitar cortes al empezar
            val deadline = System.currentTimeMillis() + 400
            while (running && queue.size < 3 && System.currentTimeMillis() < deadline) Thread.sleep(10)
            while (running && track === t) {
                val chunk = queue.poll(100, TimeUnit.MILLISECONDS) ?: continue
                try {
                    t.write(chunk, 0, chunk.size)
                } catch (_: Exception) {
                    break
                }
            }
        }.apply {
            isDaemon = true
            start()
        }
    }

    @Synchronized
    fun stop() {
        running = false
        val t = track
        track = null
        queue.clear()
        try {
            t?.pause()
            t?.flush()
            t?.release()
        } catch (_: Exception) {
        }
    }

    fun push(data: ByteArray) {
        if (!running) return
        while (queue.size > 12) queue.poll() // limita el retraso a ~250 ms
        queue.offer(data)
    }

    fun setVolume(value: Float) {
        track?.setVolume(value)
    }
}

// =====================================================================
//  Sesión: conexión, vínculo guardado, entrada y ajustes
// =====================================================================
class Session(private val context: Context) {
    private val store = SecureStore(context)
    private val client = RemoteClient()
    private val player = AudioPlayer()

    // --- Conexión ---
    var status by mutableStateOf("Sin conexión")
    var connected by mutableStateOf(false)
    var paired by mutableStateOf(store.get("token") != null)
    var pcName by mutableStateOf(store.get("pc_name") ?: "")
    var relayUrl by mutableStateOf(store.get("relay_url") ?: "")
    var viaRelay by mutableStateOf(false)

    // --- Pantalla remota ---
    var viewing by mutableStateOf(false)
    var frame by mutableStateOf<Bitmap?>(null)
    var fps by mutableStateOf(0)
    var screenW by mutableStateOf(0f)   // tamaño real de la pantalla del PC
    var screenH by mutableStateOf(0f)
    var cursorX by mutableStateOf(0f)   // posición real del cursor en el PC
    var cursorY by mutableStateOf(0f)
    var zoom by mutableStateOf(1f)
    var fullscreen by mutableStateOf(false)
    var dragging by mutableStateOf(false)
    var audioOn by mutableStateOf(false)

    // --- Ajustes guardados ---
    var quality by mutableStateOf(store.get("quality") ?: "normal")
    var cursorScale by mutableStateOf(store.get("cursor_scale")?.toFloatOrNull() ?: 1.6f)
    var sensitivity by mutableStateOf(store.get("sensitivity")?.toFloatOrNull() ?: 1.5f)
    var scrollSpeed by mutableStateOf(store.get("scroll_speed")?.toFloatOrNull() ?: 1.0f)
    var fpsTarget by mutableStateOf(store.get("fps_target")?.toIntOrNull() ?: 0) // 0 = automático
    var audioQuality by mutableStateOf(store.get("audio_quality") ?: "normal")
    var audioVolume by mutableStateOf(store.get("audio_volume")?.toFloatOrNull() ?: 1f)

    private val mainHandler = Handler(Looper.getMainLooper())
    private var userDisconnected = false
    private var lastError = ""
    private var rejected = false
    private var moveRemX = 0f
    private var moveRemY = 0f
    private var scrollRem = 0f

    private fun reset() {
        stopFling()
        connected = false
        viaRelay = false
        rejected = false
        viewing = false
        frame = null
        fps = 0
        dragging = false
        audioOn = false
        player.stop()
    }

    private fun lost(message: String) {
        reset()
        status = message
    }

    // ---------- Vincular y conectar ----------
    fun pair(ipInput: String, pin: String) {
        reset()
        val hello = JSONObject()
            .put("type", "pair")
            .put("pin", pin)
            .put("name", Build.MODEL)
        if (ipInput.isNotBlank()) {
            status = "Vinculando..."
            attempt(ipInput, hello) { status = "No se pudo conectar a $ipInput" }
        } else {
            status = "Buscando el PC en la red..."
            Discovery.find(context, null, 6000) { found ->
                if (found == null) {
                    status = "No encontré el PC. Escribe su IP manualmente"
                } else {
                    attempt(found, hello) { status = "No se pudo conectar a $found" }
                }
            }
        }
    }

    /** ¿Estamos en Wi-Fi o cable? Si no (datos móviles, VPN), se va directo al relay. */
    private fun onLocalNetwork(): Boolean {
        return try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
        } catch (e: Exception) {
            true // sin permiso de red o sin datos: probar primero la red local
        }
    }

    fun connectSaved() {
        val deviceId = store.get("device_id")
        val token = store.get("token")
        if (deviceId == null || token == null) {
            status = "Primero vincula el PC"
            return
        }
        reset()
        userDisconnected = false
        val hello = JSONObject()
            .put("type", "auth")
            .put("device_id", deviceId)
            .put("token", token)

        if (!onLocalNetwork()) {
            connectViaRelay(hello) { status = "Sin Wi-Fi y sin acceso por internet configurado" }
            return
        }
        val lastIp = store.get("last_ip")
        if (!lastIp.isNullOrBlank()) {
            status = "Probando $lastIp..."
            attempt(lastIp, hello) { searchAndConnect(hello) }
        } else {
            searchAndConnect(hello)
        }
    }

    private fun searchAndConnect(hello: JSONObject) {
        status = "Buscando el PC en la red..."
        Discovery.find(context, store.get("pc_id"), 4000) { found ->
            if (found == null) {
                connectViaRelay(hello) { status = "No encontré el PC en esta red" }
            } else {
                attempt(found, hello) {
                    connectViaRelay(hello) { status = "No se pudo conectar a $found" }
                }
            }
        }
    }

    /** Último recurso: entrar por el relay (internet). Solo funciona si ya se conectó una vez en la red local. */
    private fun connectViaRelay(hello: JSONObject, onUnavailable: () -> Unit) {
        val url = store.get("relay_url")
        val pcId = store.get("pc_id")
        if (url.isNullOrBlank() || pcId.isNullOrBlank()) {
            onUnavailable()
            return
        }
        status = "Conectando por internet..."
        val preamble = JSONObject().put("type", "connect").put("pc_id", pcId)
        attemptUrl("$url/app", "internet", hello, preamble, true) {
            status = "No se pudo conectar por internet (${lastError.take(90)}). ¿El PC está encendido y con la app abierta?"
        }
    }

    private fun attempt(ip: String, hello: JSONObject, onFail: () -> Unit) =
        attemptUrl("ws://$ip:8765", ip, hello, null, false, onFail)

    private fun attemptUrl(
        url: String,
        label: String,
        hello: JSONObject,
        preamble: JSONObject?,
        relay: Boolean,
        onFail: () -> Unit
    ) {
        rejected = false
        var count = 0
        var windowStart = System.currentTimeMillis()
        client.connect(url, hello, object : ClientEvents {
            override fun onReply(json: JSONObject) = handleReply(json, label, relay)

            override fun onFrame(bitmap: Bitmap) {
                if (!viewing) return // frames que venían en camino al apagar la vista: se descartan
                frame = bitmap
                count++
                val now = System.currentTimeMillis()
                if (now - windowStart >= 1000) {
                    fps = count
                    count = 0
                    windowStart = now
                }
            }

            override fun onAudio(data: ByteArray) = player.push(data)

            override fun onFailure(msg: String) {
                lastError = msg
                if (connected) {
                    lost("Conexión perdida")
                    scheduleReconnect()
                } else if (!rejected) {
                    onFail()
                }
            }

            override fun onClosed() {
                if (connected) lost("Desconectado")
            }
        }, preamble, relay)
    }

    /** Si se cae la conexión (por ejemplo al cambiar de Wi-Fi a datos), reintenta sola una vez. */
    private fun scheduleReconnect() {
        if (!paired || userDisconnected) return
        status = "Conexión perdida, reconectando..."
        mainHandler.postDelayed({
            if (!connected && paired && !userDisconnected) connectSaved()
        }, 2500)
    }

    private fun handleReply(json: JSONObject, label: String, relay: Boolean) {
        if (json.optString("type") == "cursor") { // posición del cursor del PC
            cursorX = json.optDouble("x", 0.0).toFloat()
            cursorY = json.optDouble("y", 0.0).toFloat()
            screenW = json.optDouble("w", 0.0).toFloat()
            screenH = json.optDouble("h", 0.0).toFloat()
            return
        }
        when (json.optString("type")) {
            "audio_format" -> {
                player.start(json.optInt("rate", 48000), json.optInt("channels", 2), audioVolume)
                audioOn = true
                return
            }
            "audio_error" -> {
                player.stop()
                audioOn = false
                status = "Sonido: " + json.optString("message")
                return
            }
        }
        if (!json.optBoolean("ok")) {
            rejected = true
            status = when (json.optString("code")) {
                "unknown_device" -> "El PC ya no reconoce este vínculo. Olvídalo y vincula de nuevo"
                else -> json.optString("error", "Rechazado")
            }
            return
        }
        if (json.has("token")) { // respuesta de un emparejamiento nuevo
            store.put("device_id", json.getString("device_id"))
            store.put("token", json.getString("token"))
            store.put("pc_id", json.optString("pc_id"))
            store.put("pc_name", json.optString("pc_name"))
            pcName = json.optString("pc_name")
            paired = true
        }
        json.optString("relay_url").takeIf { it.isNotBlank() }?.let { // el PC avisa si hay acceso por internet
            store.put("relay_url", it)
            relayUrl = it
        }
        if (!relay) store.put("last_ip", label)
        connected = true
        viaRelay = relay
        status = if (relay) "Conectado a $pcName por internet" else "Conectado a $pcName ($label)"
        setStreaming(true) // al conectar, mostrar la pantalla
    }

    fun disconnect() {
        userDisconnected = true
        client.close()
        lost("Desconectado")
    }

    fun forget() {
        client.close()
        store.clear()
        paired = false
        pcName = ""
        relayUrl = ""
        userDisconnected = true
        quality = "normal"
        cursorScale = 1.6f
        sensitivity = 1.5f
        scrollSpeed = 1f
        fpsTarget = 0
        audioQuality = "normal"
        audioVolume = 1f
        fullscreen = false
        reset()
        status = "Vínculo eliminado de este celular"
    }

    // ---------- Pantalla ----------
    private fun qualityMessage() = JSONObject().put("type", "quality").put("preset", quality)

    private fun fpsMessage() = JSONObject().put("type", "fps").put("value", fpsTarget)

    fun setStreaming(on: Boolean) {
        viewing = on
        if (on) {
            send(qualityMessage())
            send(fpsMessage())
        } else {
            frame = null
            fps = 0
        }
        send(JSONObject().put("type", "stream").put("on", on))
    }

    fun updateQuality(preset: String) {
        quality = preset
        store.put("quality", preset)
        if (connected) send(qualityMessage())
    }

    fun updateCursorScale(value: Float) {
        cursorScale = value
        store.put("cursor_scale", value.toString())
    }

    fun updateSensitivity(value: Float) {
        sensitivity = value
        store.put("sensitivity", value.toString())
    }

    fun updateScrollSpeed(value: Float) {
        scrollSpeed = value
        store.put("scroll_speed", value.toString())
    }

    fun updateZoom(value: Float) {
        zoom = value.coerceIn(1f, 4f)
    }

    fun cycleZoom() {
        zoom = if (zoom < 1.5f) 2f else if (zoom < 2.5f) 3f else 1f
    }

    /** FPS máximos del video. 0 = automático (según la calidad). */
    fun updateFps(value: Int) {
        fpsTarget = value
        store.put("fps_target", value.toString())
        if (connected) send(fpsMessage())
    }

    // ---------- Sonido ----------
    fun setAudio(on: Boolean) {
        audioOn = on
        if (on) {
            send(JSONObject().put("type", "audio").put("on", true).put("preset", audioQuality))
        } else {
            send(JSONObject().put("type", "audio").put("on", false))
            player.stop()
        }
    }

    fun updateAudioQuality(preset: String) {
        audioQuality = preset
        store.put("audio_quality", preset)
        if (audioOn) setAudio(true) // reinicia con la nueva calidad
    }

    fun updateVolume(value: Float) {
        audioVolume = value
        player.setVolume(value)
    }

    fun saveVolume() = store.put("audio_volume", audioVolume.toString())

    // ---------- Entrada ----------
    fun send(json: JSONObject) = client.send(json)

    /** Mueve el mouse. Acumula fracciones para no perder movimientos pequeños (sobre todo con zoom). */
    fun moveMouse(dx: Float, dy: Float) {
        val k = sensitivity / zoom
        moveRemX += dx * k
        moveRemY += dy * k
        val ix = moveRemX.toInt()
        val iy = moveRemY.toInt()
        if (ix != 0 || iy != 0) {
            moveRemX -= ix
            moveRemY -= iy
            send(JSONObject().put("type", "move").put("dx", ix).put("dy", iy))
        }
    }

    fun click(button: String = "left", clicks: Int = 1) {
        send(JSONObject().put("type", "click").put("button", button).put("clicks", clicks))
    }

    fun toggleDrag() {
        dragging = !dragging
        send(JSONObject().put("type", if (dragging) "down" else "up").put("button", "left"))
    }

    /**
     * Scroll de la rueda. dyDp = movimiento vertical de los dedos en dp (positivo = hacia abajo).
     * Se envía en unidades de rueda de Windows (120 = una muesca): con velocidad Normal, unos 1,5 cm de dedo
     * equivalen a unas 3 muescas.
     */
    fun scrollBy(dyDp: Float) {
        scrollRem += dyDp * 5.7f * scrollSpeed
        val delta = scrollRem.toInt()
        if (abs(delta) >= 8) { // junta movimientos muy pequeños para no saturar la conexión
            scrollRem -= delta
            send(JSONObject().put("type", "scroll").put("delta", delta))
        }
    }

    // ----- Impulso: el scroll sigue un momento después de soltar, como en una pantalla táctil -----
    private var flingVel = 0f // dp por segundo
    private val flingStep = object : Runnable {
        override fun run() {
            if (abs(flingVel) < 40f) {
                flingVel = 0f
                return
            }
            scrollBy(flingVel * 0.016f)
            flingVel *= 0.95f // fricción
            mainHandler.postDelayed(this, 16)
        }
    }

    fun fling(velocityDpPerSec: Float) {
        stopFling()
        flingVel = velocityDpPerSec
        mainHandler.post(flingStep)
    }

    fun stopFling() {
        flingVel = 0f
        mainHandler.removeCallbacks(flingStep)
    }

    fun typeText(text: String) {
        if (text.isNotEmpty()) send(JSONObject().put("type", "text").put("text", text))
    }

    fun pressKeys(vararg keys: String) {
        send(JSONObject().put("type", "key").put("keys", JSONArray(keys.toList())))
    }

    fun close() {
        client.close()
        player.stop()
    }
}

// =====================================================================
//  Actividad
// =====================================================================
class MainActivity : ComponentActivity() {
    private lateinit var session: Session

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, window.decorView).isAppearanceLightStatusBars = false
        session = Session(applicationContext)
        setContent {
            MaterialTheme(
                colorScheme = darkColorScheme(
                    primary = Color(0xFF4FC3F7),
                    onPrimary = Color(0xFF00344A),
                    background = Color(0xFF101418),
                    surface = Color(0xFF181C20),
                    surfaceVariant = Color(0xFF23282D)
                )
            ) {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    LaunchedEffect(session.fullscreen) { applyFullscreen(session.fullscreen) }
                    RemoteApp(session)
                }
            }
        }
    }

    /** Pantalla completa: horizontal, sin barras del sistema y sin apagar la pantalla. */
    private fun applyFullscreen(on: Boolean) {
        val controller = WindowInsetsControllerCompat(window, window.decorView)
        window.decorView.keepScreenOn = on
        if (on) {
            requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
            if (Build.VERSION.SDK_INT >= 28) {
                window.attributes = window.attributes.apply {
                    layoutInDisplayCutoutMode =
                        WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
                }
            }
            controller.systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            controller.hide(WindowInsetsCompat.Type.systemBars())
        } else {
            requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
            controller.show(WindowInsetsCompat.Type.systemBars())
        }
    }

    override fun onDestroy() {
        session.close()
        super.onDestroy()
    }
}

// =====================================================================
//  Interfaz
// =====================================================================
@Composable
fun RemoteApp(s: Session) {
    var showKeyboard by remember { mutableStateOf(false) }
    var showSettings by remember { mutableStateOf(false) }

    // Al abrir la app, si ya hay un PC vinculado, conecta solo
    LaunchedEffect(Unit) {
        if (s.paired) s.connectSaved()
    }
    BackHandler(enabled = s.fullscreen) { s.fullscreen = false }

    if (s.fullscreen) {
        FullscreenLayout(s, onKeyboard = { showKeyboard = true }, onSettings = { showSettings = true })
    } else {
        NormalLayout(s, onKeyboard = { showKeyboard = true }, onSettings = { showSettings = true })
    }
    if (showKeyboard) KeyboardDialog(s) { showKeyboard = false }
    if (showSettings) SettingsDialog(s) { showSettings = false }
}

@Composable
fun NormalLayout(s: Session, onKeyboard: () -> Unit, onSettings: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().safeDrawingPadding().padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        ConnectionCard(s)

        RemoteView(s, Modifier.fillMaxWidth().weight(1f).clip(RoundedCornerShape(12.dp)))

        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
            ToolButton("Clic izq.", Modifier.weight(1f)) { s.click() }
            ToolButton("Doble clic", Modifier.weight(1f)) { s.click(clicks = 2) }
            ToolButton("Clic der.", Modifier.weight(1f)) { s.click("right") }
            ToolButton("Arrastrar", Modifier.weight(1f), selected = s.dragging) { s.toggleDrag() }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
            ToolButton("Teclado", Modifier.weight(1f)) { onKeyboard() }
            ToolButton("Sonido", Modifier.weight(1f), selected = s.audioOn) { s.setAudio(!s.audioOn) }
            ToolButton("Ajustes", Modifier.weight(1f)) { onSettings() }
            ToolButton("Pantalla completa", Modifier.weight(1.8f)) { s.fullscreen = true }
        }
    }
}

@Composable
fun FullscreenLayout(s: Session, onKeyboard: () -> Unit, onSettings: () -> Unit) {
    var barOpen by remember { mutableStateOf(false) }
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        RemoteView(s, Modifier.fillMaxSize())

        Row(
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .padding(8.dp)
                .background(Color(0xCC000000), RoundedCornerShape(16.dp))
                .padding(6.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            if (barOpen) {
                ToolButton("Clic der.") { s.click("right") }
                ToolButton("Doble clic") { s.click(clicks = 2) }
                ToolButton("Arrastrar", selected = s.dragging) { s.toggleDrag() }
                ToolButton("Teclado") { onKeyboard() }
                ToolButton("Sonido", selected = s.audioOn) { s.setAudio(!s.audioOn) }
                ToolButton("Ajustes") { onSettings() }
                ToolButton("Salir") { s.fullscreen = false }
            }
            ToolButton(if (barOpen) "Ocultar" else "Menú", selected = barOpen) { barOpen = !barOpen }
        }
    }
}

@Composable
fun ConnectionCard(s: Session) {
    var ip by remember { mutableStateOf("") }
    var pin by remember { mutableStateOf("") }

    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            if (!s.paired) {
                OutlinedTextField(
                    value = ip, onValueChange = { ip = it },
                    label = { Text("IP del PC (vacío = buscar automático)") }, singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                OutlinedTextField(
                    value = pin, onValueChange = { pin = it },
                    label = { Text("PIN que muestra el PC") }, singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                Button(onClick = { s.pair(ip.trim(), pin.trim()) }) { Text("Vincular") }
                Text(s.status, fontSize = 13.sp)
            } else {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    Box(
                        Modifier.size(10.dp).background(
                            if (s.connected) Color(0xFF44BD50) else Color(0xFFB3261E), CircleShape
                        )
                    )
                    Column(Modifier.weight(1f)) {
                        Text(s.pcName.ifBlank { "PC vinculado" }, fontWeight = FontWeight.Bold)
                        Text(s.status, fontSize = 12.sp, maxLines = 2)
                    }
                    if (s.connected) {
                        OutlinedButton(onClick = { s.disconnect() }) { Text("Desconectar") }
                    } else {
                        Button(onClick = { s.connectSaved() }) { Text("Conectar") }
                    }
                }
                if (s.connected) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Switch(checked = s.viewing, onCheckedChange = { s.setStreaming(it) })
                        Text("Ver pantalla")
                        Switch(checked = s.audioOn, onCheckedChange = { s.setAudio(it) })
                        Text("Sonido")
                    }
                }
            }
        }
    }
}

/**
 * La pantalla del PC, con cursor propio bien visible, zoom que sigue al cursor y gestos:
 *  - 1 dedo: mover el mouse (como un touchpad); tocar = clic izquierdo
 *  - 2 dedos: tocar = clic derecho, deslizar = scroll, pellizcar = zoom
 */
@Composable
fun RemoteView(s: Session, modifier: Modifier = Modifier) {
    val bitmap = s.frame
    val image = remember(bitmap) { bitmap?.asImageBitmap() }

    Box(
        modifier = modifier
            .background(Color.Black)
            .clipToBounds()
            .pointerInput(Unit) {
                val slop = viewConfiguration.touchSlop
                awaitEachGesture {
                    val down = awaitFirstDown()
                    val startTime = down.uptimeMillis
                    var lastTime = startTime
                    var travelled = 0f
                    var started = false
                    var pending = Offset.Zero
                    var multi = false
                    var multiStart = 0L
                    var multiTravel = 0f
                    var prevDist = 0f
                    var prevCy = 0f
                    var scrolled = false
                    var scrollVel = 0f // velocidad del scroll en dp/s, para el impulso al soltar
                    var lastScrollTime = 0L
                    s.stopFling() // tocar la pantalla detiene el impulso anterior

                    do {
                        val event = awaitPointerEvent()
                        val pressed = event.changes.filter { it.pressed }
                        lastTime = event.changes.first().uptimeMillis
                        when {
                            pressed.size >= 2 -> {
                                val a = pressed[0].position
                                val b = pressed[1].position
                                val dist = (a - b).getDistance()
                                val cy = (a.y + b.y) / 2f
                                if (!multi) {
                                    multi = true
                                    multiStart = pressed[0].uptimeMillis
                                    prevDist = dist
                                    prevCy = cy
                                } else if (prevDist == 0f) {
                                    prevDist = dist
                                    prevCy = cy
                                } else {
                                    val dDist = dist - prevDist
                                    val dCy = cy - prevCy
                                    multiTravel += abs(dDist) + abs(dCy)
                                    if (multiTravel > slop * 2) {
                                        if (abs(dDist) > abs(dCy)) {
                                            s.updateZoom(s.zoom * dist / prevDist)
                                        } else {
                                            val now = event.changes.first().uptimeMillis
                                            val dt = (now - (if (scrolled) lastScrollTime else multiStart)).coerceAtLeast(1L)
                                            val inst = (dCy / density) / dt * 1000f
                                            scrollVel = if (scrolled) 0.7f * scrollVel + 0.3f * inst else inst
                                            lastScrollTime = now
                                            scrolled = true
                                            s.scrollBy(dCy / density)
                                        }
                                    }
                                    prevDist = dist
                                    prevCy = cy
                                }
                            }

                            pressed.size == 1 && !multi -> {
                                val d = pressed[0].positionChange()
                                if (d != Offset.Zero) {
                                    travelled += d.getDistance()
                                    if (!started) {
                                        pending += d
                                        if (travelled > slop) {
                                            started = true
                                            s.moveMouse(pending.x, pending.y)
                                        }
                                    } else {
                                        s.moveMouse(d.x, d.y)
                                    }
                                }
                            }

                            else -> prevDist = 0f // quedó un dedo tras usar dos: ignorarlo
                        }
                        event.changes.forEach { it.consume() }
                    } while (event.changes.any { it.pressed })

                    // Al soltar: ¿venía rápido? Entonces el scroll sigue un momento
                    if (multi && scrolled && abs(scrollVel) > 150f && lastTime - lastScrollTime < 80) {
                        s.fling(scrollVel)
                    }
                    // ¿fue un toque?
                    if (!multi && !started && lastTime - startTime < 400) {
                        s.click()
                    } else if (multi && multiTravel <= slop * 2 && lastTime - multiStart < 400) {
                        s.click("right")
                    }
                }
            }
    ) {
        Canvas(Modifier.fillMaxSize()) {
            val img = (if (s.viewing) image else null) ?: return@Canvas
            val bw = img.width.toFloat()
            val bh = img.height.toFloat()
            val z = s.zoom.coerceIn(1f, 4f)
            val viewW = bw / z
            val viewH = bh / z
            val hasCursor = s.screenW > 0f && s.screenH > 0f

            // Posición del cursor en coordenadas de la imagen recibida
            val cbx = if (hasCursor) s.cursorX / s.screenW * bw else bw / 2f
            val cby = if (hasCursor) s.cursorY / s.screenH * bh else bh / 2f

            // Con zoom, la vista se centra en el cursor
            val left = (cbx - viewW / 2f).coerceIn(0f, bw - viewW)
            val top = (cby - viewH / 2f).coerceIn(0f, bh - viewH)

            val scale = minOf(size.width / viewW, size.height / viewH)
            val dw = viewW * scale
            val dh = viewH * scale
            val ox = (size.width - dw) / 2f
            val oy = (size.height - dh) / 2f

            drawImage(
                image = img,
                srcOffset = IntOffset(left.toInt(), top.toInt()),
                srcSize = IntSize(viewW.toInt(), viewH.toInt()),
                dstOffset = IntOffset(ox.toInt(), oy.toInt()),
                dstSize = IntSize(dw.toInt(), dh.toInt())
            )
            if (hasCursor) {
                drawCursor(
                    x = ox + (cbx - left) * scale,
                    y = oy + (cby - top) * scale,
                    sizePx = 22.dp.toPx() * s.cursorScale
                )
            }
        }

        when {
            s.viewing && image != null -> Row(
                modifier = Modifier.align(Alignment.TopStart).padding(8.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                Chip("${s.fps} FPS")
                Chip("Zoom ${"%.1f".format(s.zoom)}×") { s.cycleZoom() }
            }

            !s.connected -> Text(
                text = "Conéctate al PC para empezar",
                color = Color.White.copy(alpha = 0.8f),
                textAlign = TextAlign.Center,
                fontSize = 14.sp,
                modifier = Modifier.align(Alignment.Center).padding(24.dp)
            )

            s.viewing -> Text(
                text = "Cargando pantalla...",
                color = Color.White.copy(alpha = 0.8f),
                textAlign = TextAlign.Center,
                fontSize = 14.sp,
                modifier = Modifier.align(Alignment.Center).padding(24.dp)
            )

            else -> TouchRing(s, Modifier.align(Alignment.Center)) // pantalla apagada: modo táctil
        }
    }
}

/** Modo táctil (pantalla apagada): anillo con flechas del teclado y un touchpad al centro. */
@Composable
fun TouchRing(s: Session, modifier: Modifier = Modifier) {
    BoxWithConstraints(modifier.fillMaxSize().padding(12.dp), contentAlignment = Alignment.Center) {
        val d = minOf(maxWidth, maxHeight * 0.8f) // diámetro del anillo
        val key = d * 0.26f
        Box(
            modifier = Modifier.size(d).clip(CircleShape).background(Color(0xFF24272C)),
            contentAlignment = Alignment.Center
        ) {
            Box(
                modifier = Modifier.size(d * 0.5f).clip(CircleShape).background(Color(0xFF3B4047)),
                contentAlignment = Alignment.Center
            ) {
                Text("Touchpad", color = Color(0xFF9AA0A6), fontSize = 13.sp)
            }
            ArrowKey("▲", "up", s, key, Modifier.align(Alignment.TopCenter))
            ArrowKey("▼", "down", s, key, Modifier.align(Alignment.BottomCenter))
            ArrowKey("◄", "left", s, key, Modifier.align(Alignment.CenterStart))
            ArrowKey("►", "right", s, key, Modifier.align(Alignment.CenterEnd))
        }
        Text(
            text = "Arrastra para mover · tocar = clic\n2 dedos: clic derecho · deslizar = scroll",
            color = Color.White.copy(alpha = 0.6f),
            fontSize = 12.sp,
            textAlign = TextAlign.Center,
            modifier = Modifier.align(Alignment.BottomCenter)
        )
    }
}

/** Flecha del anillo: un toque pulsa la tecla; mantenerla presionada la repite. */
@Composable
private fun ArrowKey(symbol: String, key: String, s: Session, size: Dp, modifier: Modifier = Modifier) {
    val scope = rememberCoroutineScope()
    Box(
        modifier = modifier
            .size(size)
            .pointerInput(key) {
                detectTapGestures(onPress = {
                    s.pressKeys(key)
                    val repeater = scope.launch {
                        delay(400)
                        while (true) {
                            s.pressKeys(key)
                            delay(90)
                        }
                    }
                    tryAwaitRelease()
                    repeater.cancel()
                })
            },
        contentAlignment = Alignment.Center
    ) {
        Text(symbol, color = Color(0xFFD0D4D9), fontSize = 26.sp)
    }
}

/** Flecha de cursor blanca con borde negro; la punta queda exactamente en (x, y). */
private fun DrawScope.drawCursor(x: Float, y: Float, sizePx: Float) {
    val path = Path().apply {
        moveTo(x, y)
        lineTo(x, y + sizePx)
        lineTo(x + sizePx * 0.28f, y + sizePx * 0.78f)
        lineTo(x + sizePx * 0.50f, y + sizePx * 1.15f)
        lineTo(x + sizePx * 0.66f, y + sizePx * 1.07f)
        lineTo(x + sizePx * 0.45f, y + sizePx * 0.72f)
        lineTo(x + sizePx * 0.78f, y + sizePx * 0.72f)
        close()
    }
    drawPath(path, Color.Black, style = Stroke(width = sizePx * 0.14f, join = StrokeJoin.Round))
    drawPath(path, Color.White)
}

@Composable
fun Chip(text: String, onClick: (() -> Unit)? = null) {
    val base = Modifier.background(Color(0x99000000), RoundedCornerShape(8.dp))
    Text(
        text = text,
        color = Color.White,
        fontSize = 12.sp,
        modifier = (if (onClick != null) base.clickable(onClick = onClick) else base)
            .padding(horizontal = 8.dp, vertical = 4.dp)
    )
}

@Composable
fun ToolButton(
    label: String,
    modifier: Modifier = Modifier,
    selected: Boolean = false,
    onClick: () -> Unit
) {
    val content: @Composable RowScope.() -> Unit = { Text(label, fontSize = 12.sp, maxLines = 1) }
    if (selected) {
        Button(
            onClick = onClick, modifier = modifier,
            contentPadding = PaddingValues(horizontal = 6.dp), content = content
        )
    } else {
        OutlinedButton(
            onClick = onClick, modifier = modifier,
            contentPadding = PaddingValues(horizontal = 6.dp), content = content
        )
    }
}

// ---------- Diálogos ----------
@Composable
fun KeyRow(s: Session, keys: List<Pair<String, List<String>>>) {
    Row(
        modifier = Modifier.horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(6.dp)
    ) {
        keys.forEach { (label, combo) ->
            OutlinedButton(
                onClick = { s.pressKeys(*combo.toTypedArray()) },
                contentPadding = PaddingValues(horizontal = 10.dp)
            ) { Text(label, fontSize = 12.sp) }
        }
    }
}

@Composable
fun KeyboardDialog(s: Session, onClose: () -> Unit) {
    var text by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onClose,
        title = { Text("Teclado") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = text, onValueChange = { text = it },
                    label = { Text("Escribir en el PC") }, singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { s.typeText(text); text = "" }) { Text("Enviar") }
                    OutlinedButton(onClick = {
                        s.typeText(text)
                        s.pressKeys("enter")
                        text = ""
                    }) { Text("Enviar + Enter") }
                }
                Text("Teclas especiales", fontSize = 12.sp)
                KeyRow(
                    s, listOf(
                        "Esc" to listOf("esc"), "Tab" to listOf("tab"), "Enter" to listOf("enter"),
                        "⌫" to listOf("backspace"), "Supr" to listOf("delete")
                    )
                )
                KeyRow(
                    s, listOf(
                        "←" to listOf("left"), "↑" to listOf("up"),
                        "↓" to listOf("down"), "→" to listOf("right")
                    )
                )
                KeyRow(
                    s, listOf(
                        "Win" to listOf("win"), "Alt+Tab" to listOf("alt", "tab"),
                        "Ctrl+C" to listOf("ctrl", "c"), "Ctrl+V" to listOf("ctrl", "v"),
                        "Ctrl+Z" to listOf("ctrl", "z"), "Ctrl+A" to listOf("ctrl", "a")
                    )
                )
            }
        },
        confirmButton = { TextButton(onClick = onClose) { Text("Cerrar") } }
    )
}

@Composable
fun <T> Choice(title: String, options: List<Pair<String, T>>, selected: T, onSelect: (T) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(title, fontSize = 13.sp)
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            options.forEach { (label, value) ->
                ToolButton(label, Modifier.weight(1f), selected = value == selected) { onSelect(value) }
            }
        }
    }
}

@Composable
fun FpsSetting(s: Session) {
    var value by remember { mutableStateOf(if (s.fpsTarget == 0) 20f else s.fpsTarget.toFloat()) }
    Column {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Switch(
                checked = s.fpsTarget == 0,
                onCheckedChange = { auto -> s.updateFps(if (auto) 0 else value.toInt()) }
            )
            Text(
                if (s.fpsTarget == 0) "FPS: automático (según la calidad)" else "FPS máximos: ${value.toInt()}",
                fontSize = 13.sp
            )
        }
        Slider(
            value = value,
            onValueChange = { value = it },
            onValueChangeFinished = { s.updateFps(value.toInt()) },
            valueRange = 5f..60f,
            enabled = s.fpsTarget != 0
        )
        Text("Es un tope: los FPS reales dependen de tu PC y de tu Wi-Fi.", fontSize = 11.sp)
    }
}

@Composable
fun SettingsDialog(s: Session, onClose: () -> Unit) {
    AlertDialog(
        onDismissRequest = onClose,
        title = { Text("Ajustes") },
        text = {
            Column(
                modifier = Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(14.dp)
            ) {
                Choice(
                    "Calidad de imagen (más calidad = más datos)",
                    listOf("Normal" to "normal", "Alta" to "alta", "Máxima" to "maxima"),
                    s.quality
                ) { s.updateQuality(it) }
                FpsSetting(s)
                Choice(
                    "Tamaño del cursor",
                    listOf("Normal" to 1.0f, "Grande" to 1.6f, "Enorme" to 2.4f),
                    s.cursorScale
                ) { s.updateCursorScale(it) }
                Choice(
                    "Velocidad del mouse",
                    listOf("Lenta" to 1.0f, "Media" to 1.5f, "Rápida" to 2.5f),
                    s.sensitivity
                ) { s.updateSensitivity(it) }
                Choice(
                    "Velocidad del scroll (subir y bajar)",
                    listOf("Lenta" to 0.6f, "Normal" to 1.0f, "Rápida" to 1.8f, "Muy rápida" to 3.0f),
                    s.scrollSpeed
                ) { s.updateScrollSpeed(it) }
                Choice(
                    "Zoom (sigue al cursor)",
                    listOf("1×" to 1f, "2×" to 2f, "3×" to 3f, "4×" to 4f),
                    s.zoom
                ) { s.updateZoom(it) }
                Choice(
                    "Calidad del sonido",
                    listOf("Normal (mono)" to "normal", "Alta (estéreo)" to "alta"),
                    s.audioQuality
                ) { s.updateAudioQuality(it) }
                Column {
                    Text("Volumen del sonido: ${(s.audioVolume * 100).toInt()}%", fontSize = 13.sp)
                    Slider(
                        value = s.audioVolume,
                        onValueChange = { s.updateVolume(it) },
                        onValueChangeFinished = { s.saveVolume() }
                    )
                }
                Text(
                    "Gestos: 1 dedo mueve el mouse y tocar hace clic. Con 2 dedos: tocar = clic derecho, " +
                            "deslizar = scroll, pellizcar = zoom.",
                    fontSize = 12.sp
                )
                Text(
                    if (s.relayUrl.isBlank()) {
                        "Acceso por internet: aún no disponible. Conéctate una vez en la misma Wi-Fi que el PC, " +
                                "con el relay configurado en su app."
                    } else {
                        "Acceso por internet: listo (${s.relayUrl.removePrefix("wss://")})"
                    },
                    fontSize = 12.sp
                )
                if (s.paired) {
                    TextButton(onClick = { s.forget(); onClose() }) { Text("Olvidar vínculo con el PC") }
                }
            }
        },
        confirmButton = { TextButton(onClick = onClose) { Text("Cerrar") } }
    )
}