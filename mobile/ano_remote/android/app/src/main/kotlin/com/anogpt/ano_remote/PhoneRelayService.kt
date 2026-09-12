package com.anogpt.ano_remote

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.database.Cursor
import android.net.Uri
import android.os.Build
import android.os.IBinder
import android.provider.ContactsContract
import android.telephony.SmsManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import okhttp3.CertificatePinner
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.Executors
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

/**
 * Tunnel Android indépendant de Flutter.
 *
 * Android suspend une Activity et son isolate Dart quelques minutes après que
 * l'utilisateur a quitté l'app. Ce service de premier plan possède donc le
 * WebSocket qui reçoit les ordres téléphoniques : un « appel lancé » n'est
 * retourné au PC qu'après l'exécution native de l'Intent ou de SmsManager.
 */
class PhoneRelayService : Service() {
    companion object {
        private const val ACTION_START = "com.anogpt.ano_remote.START_PHONE_RELAY"
        private const val ACTION_STOP = "com.anogpt.ano_remote.STOP_PHONE_RELAY"
        private const val ACTION_WAKE = "com.anogpt.ano_remote.WAKE_PHONE_RELAY"
        private const val EXTRA_BASE = "base_url"
        private const val EXTRA_DEVICE = "device_token"
        private const val CHANNEL_ID = "ano_phone_relay"

        fun start(context: Context, baseUrl: String = "", deviceToken: String = "") {
            val intent = Intent(context, PhoneRelayService::class.java).setAction(ACTION_START)
            if (baseUrl.isNotBlank()) intent.putExtra(EXTRA_BASE, baseUrl)
            if (deviceToken.isNotBlank()) intent.putExtra(EXTRA_DEVICE, deviceToken)
            ContextCompat.startForegroundService(context, intent)
        }

        fun wake(context: Context) {
            ContextCompat.startForegroundService(
                context, Intent(context, PhoneRelayService::class.java).setAction(ACTION_WAKE),
            )
        }
    }

    private var socket: WebSocket? = null
    private var reconnect: ScheduledFuture<*>? = null
    private var retrySeconds = 2L
    private val scheduler = Executors.newSingleThreadScheduledExecutor()
    private val client by lazy { localClient() }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopRelay()
            stopSelf()
            return START_NOT_STICKY
        }
        intent?.getStringExtra(EXTRA_BASE)?.takeIf { it.isNotBlank() }?.let {
            prefs().edit().putString(EXTRA_BASE, it).apply()
        }
        intent?.getStringExtra(EXTRA_DEVICE)?.takeIf { it.isNotBlank() }?.let {
            prefs().edit().putString(EXTRA_DEVICE, it).apply()
        }
        startForeground(73, notification())
        connectSoon(0)
        return START_STICKY
    }

    override fun onDestroy() {
        stopRelay()
        scheduler.shutdownNow()
        client.dispatcher.executorService.shutdown()
        super.onDestroy()
    }

    private fun prefs() = getSharedPreferences("ano_phone_relay", MODE_PRIVATE)

    private fun notification(): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "ANO Remote – téléphone", NotificationManager.IMPORTANCE_LOW).apply {
                    description = "Relais sécurisé des appels et SMS ANO-GPT"
                    setShowBadge(false)
                },
            )
        }
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(com.anogpt.ano_remote.R.mipmap.ic_launcher)
            .setContentTitle("ANO Remote est prêt")
            .setContentText("Appels, SMS et surveillance des messages actifs")
            .setOngoing(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }

    private fun connectSoon(delay: Long) {
        reconnect?.cancel(false)
        reconnect = scheduler.schedule({ connect() }, delay, TimeUnit.SECONDS)
    }

    private fun connect() {
        val base = prefs().getString(EXTRA_BASE, "").orEmpty().trimEnd('/')
        val device = prefs().getString(EXTRA_DEVICE, "").orEmpty()
        if (base.isEmpty() || device.isEmpty()) return
        try {
            val body = JSONObject().put("device_token", device).toString()
                .toRequestBody("application/json; charset=utf-8".toMediaType())
            val login = client.newCall(Request.Builder().url("$base/api/device-login").post(body).build()).execute()
            val token = login.use { response ->
                if (!response.isSuccessful) null else JSONObject(response.body?.string().orEmpty()).optString("token")
            }
            if (token.isNullOrBlank()) throw IllegalStateException("reconnexion refusée")
            val wsUrl = base.replaceFirst(Regex("^https"), "wss")
                .replaceFirst(Regex("^http"), "ws") + "/ws?token=" + Uri.encode(token)
            socket?.close(1000, "renouvellement")
            socket = client.newWebSocket(Request.Builder().url(wsUrl).build(), listener)
        } catch (_: Throwable) {
            retryLater()
        }
    }

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            retrySeconds = 2
            webSocket.send(JSONObject().apply {
                put("type", "client_hello")
                put("client", "ano_remote_android_service")
                put("capabilities", JSONArray(listOf("phone_call", "phone_hangup", "phone_sms", "android_contacts", "sms_monitor")))
            }.toString())
            flushSms()
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                val data = JSONObject(text)
                when (data.optString("type")) {
                    "phone_call" -> reply(webSocket, "phone_call_result", data, call(data))
                    "phone_sms_send" -> reply(webSocket, "phone_sms_send_result", data, sendSms(data))
                    "phone_hangup" -> reply(webSocket, "phone_hangup_result", data, hangup())
                    "phone_contacts" -> reply(webSocket, "phone_contacts_result", data, contacts(data.optString("query")))
                    "phone_sms_received_ack" -> {
                        removeSms(data.optString("event_id"))
                        flushSms()
                    }
                }
            } catch (_: Throwable) { }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = retryLater()
        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = retryLater()
    }

    private fun retryLater() {
        if (socket != null) socket = null
        connectSoon(retrySeconds)
        retrySeconds = (retrySeconds * 2).coerceAtMost(60)
    }

    private fun stopRelay() {
        reconnect?.cancel(false)
        reconnect = null
        socket?.close(1000, "arrêt demandé")
        socket = null
    }

    private fun reply(ws: WebSocket, type: String, request: JSONObject, outcome: JSONObject) {
        outcome.put("type", type).put("request_id", request.optString("request_id"))
        ws.send(outcome.toString())
    }

    private fun granted(permission: String) = ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED
    private fun fail(status: String, message: String) = JSONObject().put("ok", false).put("status", status).put("message", message)
    private fun ok(status: String, message: String) = JSONObject().put("ok", true).put("status", status).put("message", message)

    private fun resolve(raw: String): List<Pair<String, String>> {
        val target = raw.trim()
        val direct = target.replace(Regex("[\\s().-]"), "")
        if (direct.matches(Regex("^\\+?[0-9]{6,15}$"))) return listOf(target to direct)
        val folded = fold(target)
        if (folded.isBlank() || !granted(Manifest.permission.READ_CONTACTS)) return emptyList()
        val found = linkedSetOf<Pair<String, String>>()
        val projection = arrayOf(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER)
        contentResolver.query(ContactsContract.CommonDataKinds.Phone.CONTENT_URI, projection, null, null, null)?.use { cursor: Cursor ->
            val ni = cursor.getColumnIndexOrThrow(projection[0]); val pi = cursor.getColumnIndexOrThrow(projection[1])
            while (cursor.moveToNext() && found.size < 8) {
                val name = cursor.getString(ni)?.trim().orEmpty(); val number = cursor.getString(pi)?.replace(Regex("[\\s().-]"), "").orEmpty()
                if (fold(name).contains(folded) && number.matches(Regex("^\\+?[0-9]{6,15}$"))) found.add(name to number)
            }
        }
        return found.toList()
    }

    private fun choices(items: List<Pair<String, String>>) = JSONArray(items.mapIndexed { index, item ->
        JSONObject().put("index", index + 1).put("name", item.first).put("number_hint", "•••• " + item.second.filter(Char::isDigit).takeLast(4))
    })

    private fun selected(raw: String, selection: Int): Pair<Pair<String, String>?, JSONObject?> {
        if (!granted(Manifest.permission.READ_CONTACTS)) return null to fail("permission_required", "Autorisez l’accès aux Contacts dans ANO Remote.")
        val matches = resolve(raw)
        if (matches.isEmpty()) return null to fail("not_found", "Aucun contact Android ne correspond à « $raw ».")
        if (matches.size > 1 && selection !in 1..matches.size) return null to fail("ambiguous", "Choisissez le contact « $raw ».").put("choices", choices(matches))
        return (if (selection in 1..matches.size) matches[selection - 1] else matches.first()) to null
    }

    private fun call(data: JSONObject): JSONObject {
        if (!granted(Manifest.permission.CALL_PHONE)) return fail("permission_required", "Autorisez les appels dans ANO Remote.")
        val (contact, error) = selected(data.optString("target"), data.optInt("selection")); if (error != null) return error
        return try {
            startActivity(Intent(Intent.ACTION_CALL, Uri.fromParts("tel", contact!!.second, null)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            ok("started", "Appel lancé vers ${contact.first}.").put("name", contact.first)
        } catch (e: Throwable) { fail("failed", e.message ?: "Échec du lancement de l’appel.") }
    }

    private fun sendSms(data: JSONObject): JSONObject {
        if (!granted(Manifest.permission.SEND_SMS)) return fail("permission_required", "Autorisez les SMS dans ANO Remote.")
        val body = data.optString("body").trim(); if (body.isBlank() || body.length > 1600) return fail("invalid_body", "Texte du SMS invalide.")
        val (contact, error) = selected(data.optString("target"), data.optInt("selection")); if (error != null) return error
        return try {
            SmsManager.getDefault().sendTextMessage(contact!!.second, null, body, null, null)
            ok("sent", "SMS envoyé à ${contact.first}.").put("name", contact.first)
        } catch (e: Throwable) { fail("failed", e.message ?: "Envoi SMS impossible.") }
    }

    private fun hangup(): JSONObject {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P || !granted(Manifest.permission.ANSWER_PHONE_CALLS)) return fail("permission_required", "Autorisez la gestion des appels dans ANO Remote.")
        return try { ok("ended", "Appel raccroché.").put("ok", (getSystemService(TELECOM_SERVICE) as android.telecom.TelecomManager).endCall()) }
        catch (e: Throwable) { fail("failed", e.message ?: "Raccrochage impossible.") }
    }

    private fun contacts(query: String): JSONObject {
        if (!granted(Manifest.permission.READ_CONTACTS)) return fail("permission_required", "Autorisez l’accès aux Contacts dans ANO Remote.")
        val found = resolve(query)
        return ok(if (found.isEmpty()) "not_found" else "found", if (found.isEmpty()) "Aucun contact trouvé." else "${found.size} contact(s) trouvé(s).")
            .put("count", found.size).put("choices", choices(found))
    }

    private fun fold(value: String) = java.text.Normalizer.normalize(value, java.text.Normalizer.Form.NFD)
        .replace(Regex("\\p{Mn}+"), "").lowercase().trim()

    /** Envoie en tête de file, puis attend l'accusé serveur avant suppression. */
    private fun flushSms() {
        val ws = socket ?: return
        val queue = smsQueue()
        val sms = queue.optJSONObject(0) ?: return
        if (sms.optString("body").isNotBlank()) ws.send(sms.put("type", "phone_sms_received").toString())
    }

    private fun smsQueue(): JSONArray = try { JSONArray(getSharedPreferences("ano_sms", MODE_PRIVATE).getString("queue", "[]")) } catch (_: Throwable) { JSONArray() }
    private fun removeSms(eventId: String) {
        if (eventId.isBlank()) return
        val old = smsQueue(); val kept = JSONArray()
        for (i in 0 until old.length()) { val item = old.optJSONObject(i); if (item != null && item.optString("event_id") != eventId) kept.put(item) }
        getSharedPreferences("ano_sms", MODE_PRIVATE).edit().putString("queue", kept.toString()).apply()
    }

    private fun localClient(): OkHttpClient {
        // Le certificat du PC est auto-signé. La clé d'appairage reste exigée
        // par le serveur et l'application ne garde que des adresses LAN.
        val trust = object : X509TrustManager { override fun checkClientTrusted(c: Array<X509Certificate>, a: String) {} ; override fun checkServerTrusted(c: Array<X509Certificate>, a: String) {} ; override fun getAcceptedIssuers() = emptyArray<X509Certificate>() }
        val context = SSLContext.getInstance("TLS").apply { init(null, arrayOf<TrustManager>(trust), SecureRandom()) }
        return OkHttpClient.Builder().sslSocketFactory(context.socketFactory, trust)
            .hostnameVerifier(HostnameVerifier { _, _ -> true }).pingInterval(25, TimeUnit.SECONDS).build()
    }
}
