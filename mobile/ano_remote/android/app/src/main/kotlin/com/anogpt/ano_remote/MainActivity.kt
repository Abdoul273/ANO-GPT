package com.anogpt.ano_remote

import android.Manifest
import android.content.pm.PackageManager
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Matrix
import android.graphics.Rect
import android.graphics.YuvImage
import android.os.Build
import android.net.Uri
import android.provider.ContactsContract
import android.telephony.SmsManager
import android.telecom.TelecomManager
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.ByteArrayOutputStream
import java.text.Normalizer

/**
 * Conversion NV21 → JPEG côté natif.
 *
 * La caméra livre des images YUV brutes. Les compresser en Dart coûterait plus
 * de 100 ms par image et le flux tomberait à quelques images par seconde ;
 * YuvImage le fait ici en quelques millisecondes, ce qui rend la diffusion
 * réellement fluide.
 */
class MainActivity : FlutterActivity() {

    private val channel = "ano.remote/frames"
    private val systemChannel = "ano.remote/system"
    private val notificationRequestCode = 4201
    private val phonePermissionRequestCode = 4202
    private var pendingPhonePermission: MethodChannel.Result? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "nv21ToJpeg" -> encode(call.argument("bytes"),
                                           call.argument("width"),
                                           call.argument("height"),
                                           call.argument("rotation"),
                                           call.argument("mirror") ?: false,
                                           call.argument("quality"),
                                           result)
                    else -> result.notImplemented()
                }
            }

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, systemChannel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "requestNotificationPermission" -> {
                        requestNotificationPermission()
                        result.success(true)
                    }
                    "phonePermissionStatus" -> result.success(phonePermissionStatus())
                    "requestPhonePermissions" -> requestPhonePermissions(result)
                    "startPhoneRelay" -> {
                        PhoneRelayService.start(
                            this,
                            call.argument<String>("base_url") ?: "",
                            call.argument<String>("device_token") ?: "",
                        )
                        result.success(true)
                    }
                    "stopPhoneRelay" -> {
                        stopService(Intent(this, PhoneRelayService::class.java).setAction("com.anogpt.ano_remote.STOP_PHONE_RELAY"))
                        result.success(true)
                    }
                    "resolveAndCall" -> resolveAndCall(
                        call.argument<String>("target") ?: "",
                        call.argument<Int>("selection") ?: 0,
                        result,
                    )
                    "takePendingSms" -> result.success(takePendingSms())
                    "sendSms" -> sendSms(call.argument<String>("number") ?: "", call.argument<String>("body") ?: "", result)
                    "resolveAndSendSms" -> resolveAndSendSms(
                        call.argument<String>("target") ?: "",
                        call.argument<Int>("selection") ?: 0,
                        call.argument<String>("body") ?: "",
                        result,
                    )
                    "searchContacts" -> searchContacts(call.argument<String>("query") ?: "", result)
                    "endCall" -> endCall(result)
                    else -> result.notImplemented()
                }
            }
    }

    /**
     * Renvoie toute la file, puis l'efface atomiquement.  L'ancienne version
     * ne gardait qu'un SMS : deux messages reçus pendant une reconnexion
     * écrasaient silencieusement le premier.
     */
    private fun takePendingSms(): List<Map<String, Any>> {
        val prefs = getSharedPreferences("ano_sms", MODE_PRIVATE)
        val raw = prefs.getString("queue", "[]").orEmpty()
        val result = mutableListOf<Map<String, Any>>()
        try {
            val queue = org.json.JSONArray(raw)
            for (index in 0 until queue.length()) {
                val sms = queue.optJSONObject(index) ?: continue
                val body = sms.optString("body").trim()
                if (body.isNotEmpty()) {
                    result.add(mapOf(
                        "sender" to sms.optString("sender", "Inconnu"),
                        "sender_name" to sms.optString("sender_name", ""),
                        "body" to body,
                        "received_at" to sms.optLong("received_at", 0L),
                    ))
                }
            }
        } catch (_: Throwable) {
            // Migration des versions qui stockaient un seul SMS.
            val body = prefs.getString("body", "").orEmpty().trim()
            if (body.isNotEmpty()) result.add(mapOf(
                "sender" to prefs.getString("sender", "Inconnu").orEmpty(),
                "sender_name" to "", "body" to body,
                "received_at" to prefs.getLong("received_at", 0L),
            ))
        }
        prefs.edit().clear().apply()
        return result
    }

    private fun sendSms(number: String, body: String, result: MethodChannel.Result) {
        if (!hasPermission(Manifest.permission.SEND_SMS)) { result.success(mapOf("ok" to false, "message" to "Permission SMS refusée.")); return }
        val target = number.replace(Regex("[\\s().-]"), "")
        if (!target.matches(Regex("^\\+?[0-9]{6,15}$")) || body.isBlank() || body.length > 1600) { result.success(mapOf("ok" to false, "message" to "Numéro ou message SMS invalide.")); return }
        try { SmsManager.getDefault().sendTextMessage(target, null, body, null, null); result.success(mapOf("ok" to true, "message" to "SMS envoyé.")) }
        catch (e: Throwable) { result.success(mapOf("ok" to false, "message" to (e.message ?: "Envoi SMS impossible."))) }
    }

    private fun endCall(result: MethodChannel.Result) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P || !hasPermission(Manifest.permission.ANSWER_PHONE_CALLS)) { result.success(mapOf("ok" to false, "message" to "Permission de raccrochage refusée.")); return }
        try { result.success(mapOf("ok" to (getSystemService(TELECOM_SERVICE) as TelecomManager).endCall(), "message" to "Appel raccroché.")) }
        catch (e: Throwable) { result.success(mapOf("ok" to false, "message" to (e.message ?: "Raccrochage impossible."))) }
    }

    /**
     * Sans POST_NOTIFICATIONS sur Android 13+, la notification du service de
     * localisation reste invisible — et Android finit par arrêter un service
     * de premier plan que l'utilisateur ne voit pas.
     */
    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (granted) return
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.POST_NOTIFICATIONS),
            notificationRequestCode,
        )
    }

    private fun phonePermissionStatus(): Map<String, Any> = mapOf(
        "contacts" to hasPermission(Manifest.permission.READ_CONTACTS),
        "calls" to hasPermission(Manifest.permission.CALL_PHONE),
        "sms" to hasPermission(Manifest.permission.SEND_SMS),
        "receive_sms" to hasPermission(Manifest.permission.RECEIVE_SMS),
        "hangup" to (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P &&
            hasPermission(Manifest.permission.ANSWER_PHONE_CALLS)),
        "telephony" to packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY),
    )

    private fun hasPermission(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    private fun requestPhonePermissions(result: MethodChannel.Result) {
        if (!packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY)) {
            result.error("no_telephony", "Ce téléphone ne peut pas passer d'appel cellulaire.", null)
            return
        }
        val wanted = mutableListOf(
            Manifest.permission.READ_CONTACTS,
            Manifest.permission.CALL_PHONE,
            Manifest.permission.RECEIVE_SMS,
            Manifest.permission.SEND_SMS,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            wanted.add(Manifest.permission.ANSWER_PHONE_CALLS)
        }
        val missing = wanted.filterNot(::hasPermission)
        if (missing.isEmpty()) {
            result.success(phonePermissionStatus())
            return
        }
        if (pendingPhonePermission != null) {
            result.error("permission_busy", "Une demande de permission est déjà ouverte.", null)
            return
        }
        pendingPhonePermission = result
        ActivityCompat.requestPermissions(this, missing.toTypedArray(), phonePermissionRequestCode)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == phonePermissionRequestCode) {
            pendingPhonePermission?.success(phonePermissionStatus())
            pendingPhonePermission = null
        }
    }

    /**
     * Résout un nom, un numéro complet ou une fin de numéro dans le carnet
     * Android. Renvoie soit la liste retenue, soit une réponse d'erreur prête
     * à repartir vers le PC : appel et SMS partagent exactement ces règles.
     */
    private fun resolveTargets(rawTarget: String): Pair<List<Pair<String, String>>, Map<String, Any>?> {
        val target = normalizeSpokenSuffix(rawTarget)
        if (target.isEmpty() || target.length > 120) {
            return emptyList<Pair<String, String>>() to mapOf(
                "ok" to false, "status" to "invalid_target",
                "message" to "Nom ou numéro invalide.")
        }
        val direct = target.replace(Regex("[\\s().-]"), "")
        val suffixLookup = direct.matches(Regex("^[0-9]{2,5}$"))
        val resolved = if (direct.matches(Regex("^\\+?[0-9]{6,15}$"))) {
            listOf(target to direct)
        } else if (suffixLookup) {
            findPhoneContactsBySuffix(direct)
        } else {
            findPhoneContacts(target)
        }
        if (resolved.isEmpty()) {
            val guidance = if (suffixLookup) {
                "Aucun contact Android ne finit par « $direct ». Donnez le numéro complet ou deux autres chiffres."
            } else {
                "Aucun contact Android ne correspond à « $target ». Donnez le numéro complet ou les deux derniers chiffres du numéro."
            }
            return emptyList<Pair<String, String>>() to mapOf(
                "ok" to false, "status" to "not_found", "message" to guidance)
        }
        return resolved to null
    }

    /** Le PC ne voit jamais un numéro entier : seulement un nom et 4 chiffres. */
    private fun choicesOf(resolved: List<Pair<String, String>>): List<Map<String, Any>> =
        resolved.take(8).mapIndexed { index, (name, number) ->
            mapOf("index" to index + 1, "name" to name, "number_hint" to maskNumber(number))
        }

    private fun isSuffixLookup(rawTarget: String): Boolean =
        rawTarget.trim().replace(Regex("[\\s().-]"), "").matches(Regex("^[0-9]{2,5}$"))

    /**
     * Recherche seule, sans effet : « ai-je un contact nommé X ? » doit avoir
     * une réponse honnête. Sans elle, l'assistant répondait « aucun contact »
     * en consultant le carnet du PC, vide, pendant que le téléphone en avait.
     */
    private fun searchContacts(query: String, result: MethodChannel.Result) {
        if (!hasPermission(Manifest.permission.READ_CONTACTS)) {
            result.success(mapOf("ok" to false, "status" to "permission_required",
                "message" to "Autorisez l'accès aux Contacts dans ANO Remote."))
            return
        }
        try {
            val trimmed = normalizeSpokenSuffix(query)
            val found: List<Pair<String, String>> = when {
                trimmed.isEmpty() -> emptyList()
                isSuffixLookup(trimmed) -> findPhoneContactsBySuffix(
                    trimmed.replace(Regex("[\\s().-]"), ""))
                else -> findPhoneContacts(trimmed)
            }
            result.success(mapOf(
                "ok" to true,
                "status" to if (found.isEmpty()) "not_found" else "found",
                "count" to found.size,
                "choices" to choicesOf(found),
                "message" to if (found.isEmpty())
                    "Aucun contact Android ne correspond à « $trimmed »."
                else "${found.size} contact(s) trouvé(s) pour « $trimmed »."))
        } catch (error: SecurityException) {
            result.success(mapOf("ok" to false, "status" to "permission_required",
                "message" to "Android a refusé l'accès aux contacts."))
        } catch (error: Throwable) {
            result.success(mapOf("ok" to false, "status" to "failed",
                "message" to (error.message ?: "Recherche de contacts impossible.")))
        }
    }

    /**
     * SMS envoyé par la SIM du téléphone. Le PC n'a ni carnet ni réseau
     * cellulaire : y taper un message dans une fenêtre était le vrai bug.
     */
    private fun resolveAndSendSms(rawTarget: String, selection: Int, body: String,
                                  result: MethodChannel.Result) {
        if (!hasPermission(Manifest.permission.SEND_SMS) ||
            !hasPermission(Manifest.permission.READ_CONTACTS)) {
            result.success(mapOf("ok" to false, "status" to "permission_required",
                "message" to "Autorisez Contacts et SMS dans ANO Remote."))
            return
        }
        if (body.isBlank() || body.length > 1600) {
            result.success(mapOf("ok" to false, "status" to "invalid_body",
                "message" to "Texte du SMS invalide."))
            return
        }
        try {
            val (resolved, failure) = resolveTargets(rawTarget)
            if (failure != null) { result.success(failure); return }
            // Comme pour l'appel, une fin de numéro ou plusieurs homonymes
            // n'envoient jamais d'eux-mêmes : l'utilisateur tranche d'abord.
            if ((isSuffixLookup(rawTarget) || resolved.size > 1) && selection !in 1..resolved.size) {
                result.success(mapOf("ok" to false, "status" to "ambiguous",
                    "message" to "Choisissez le destinataire du SMS pour « ${rawTarget.trim()} ».",
                    "choices" to choicesOf(resolved)))
                return
            }
            val (name, number) = if (selection in 1..resolved.size) resolved[selection - 1] else resolved.first()
            SmsManager.getDefault().sendTextMessage(number, null, body, null, null)
            result.success(mapOf("ok" to true, "status" to "sent", "name" to name,
                "number_hint" to maskNumber(number), "message" to "SMS envoyé à $name."))
        } catch (error: SecurityException) {
            result.success(mapOf("ok" to false, "status" to "permission_required",
                "message" to "Android a refusé l'envoi du SMS."))
        } catch (error: Throwable) {
            result.success(mapOf("ok" to false, "status" to "failed",
                "message" to (error.message ?: "Envoi SMS impossible.")))
        }
    }

    /** Résout le nom dans Contacts Android, jamais sur le PC, puis appelle. */
    private fun resolveAndCall(rawTarget: String, selection: Int, result: MethodChannel.Result) {
        if (!packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY)) {
            result.success(mapOf("ok" to false, "status" to "no_telephony",
                "message" to "Téléphonie cellulaire indisponible."))
            return
        }
        if (!hasPermission(Manifest.permission.READ_CONTACTS) ||
            !hasPermission(Manifest.permission.CALL_PHONE)) {
            result.success(mapOf("ok" to false, "status" to "permission_required",
                "message" to "Autorisez Contacts et Appels dans ANO Remote."))
            return
        }
        val target = rawTarget.trim()
        try {
            val (resolved, failure) = resolveTargets(target)
            if (failure != null) { result.success(failure); return }
            val suffixLookup = isSuffixLookup(target)
            // Une recherche par fin de numéro n'appelle jamais d'elle-même :
            // même un seul résultat doit être confirmé par l'utilisateur.
            if ((suffixLookup || resolved.size > 1) && selection !in 1..resolved.size) {
                result.success(mapOf("ok" to false, "status" to "ambiguous",
                    "message" to "Choisissez le contact à appeler pour « $target ».",
                    "choices" to choicesOf(resolved)))
                return
            }
            val (name, number) = if (selection in 1..resolved.size) {
                resolved[selection - 1]
            } else {
                resolved.first()
            }
            startActivity(Intent(Intent.ACTION_CALL, Uri.fromParts("tel", number, null)))
            result.success(mapOf("ok" to true, "status" to "started", "name" to name,
                "number_hint" to maskNumber(number), "message" to "Appel lancé vers $name."))
        } catch (error: SecurityException) {
            result.success(mapOf("ok" to false, "status" to "permission_required",
                "message" to "Android a refusé la permission d'appel."))
        } catch (error: Throwable) {
            result.success(mapOf("ok" to false, "status" to "failed",
                "message" to (error.message ?: "Échec du lancement de l'appel.")))
        }
    }

    private fun findPhoneContacts(query: String): List<Pair<String, String>> {
        val found = linkedSetOf<Pair<String, String>>()
        val projection = arrayOf(
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER,
        )
        // « appelle maman » doit aussi trouver un contact enregistré comme
        // Mum/Mummy/Mère. Les variantes restent sur Android : le PC ne voit
        // jamais le carnet d'adresses ni les numéros.
        val variants = contactNameVariants(query)
        for (variant in variants) contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI, projection,
            "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?",
            arrayOf("%${variant.replace("%", "").replace("_", "")}%"),
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " COLLATE NOCASE ASC",
        )?.use { cursor ->
            val nameIndex = cursor.getColumnIndexOrThrow(projection[0])
            val numberIndex = cursor.getColumnIndexOrThrow(projection[1])
            while (cursor.moveToNext() && found.size < 20) {
                val name = cursor.getString(nameIndex)?.trim().orEmpty()
                val number = cursor.getString(numberIndex)?.replace(Regex("[\\s().-]"), "").orEmpty()
                if (name.isNotEmpty() && number.matches(Regex("^\\+?[0-9]{6,15}$"))) {
                    found.add(name to number)
                }
            }
        }
        // LIKE/COLLATE NOCASE ne sait pas que « è » et « e » sont la même
        // lettre. En repli, on plie les accents en mémoire et on exige tous
        // les mots demandés : la recherche reste précise sans rater Frère.
        if (found.isEmpty()) contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI, projection,
            null, null, ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " COLLATE NOCASE ASC",
        )?.use { cursor ->
            val nameIndex = cursor.getColumnIndexOrThrow(projection[0])
            val numberIndex = cursor.getColumnIndexOrThrow(projection[1])
            while (cursor.moveToNext() && found.size < 20) {
                val name = cursor.getString(nameIndex)?.trim().orEmpty()
                val number = cursor.getString(numberIndex)?.replace(Regex("[\\s().-]"), "").orEmpty()
                if (name.isNotEmpty() && number.matches(Regex("^\\+?[0-9]{6,15}$")) &&
                    variants.any { matchesContactName(name, it) }) {
                    found.add(name to number)
                }
            }
        }
        val exact = found.filter { foldContactText(it.first) == foldContactText(query) }
        return (if (exact.isNotEmpty()) exact else found.toList()).distinctBy { it.second }
    }

    private fun findPhoneContactsBySuffix(suffix: String): List<Pair<String, String>> {
        val found = linkedSetOf<Pair<String, String>>()
        val projection = arrayOf(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER)
        // Android stocke les numéros sous des formats variés ; on normalise en
        // mémoire et ne renvoie jamais le numéro complet au PC.
        contentResolver.query(ContactsContract.CommonDataKinds.Phone.CONTENT_URI, projection,
            null, null, ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " COLLATE NOCASE ASC")?.use { cursor ->
            val nameIndex = cursor.getColumnIndexOrThrow(projection[0])
            val numberIndex = cursor.getColumnIndexOrThrow(projection[1])
            while (cursor.moveToNext() && found.size < 20) {
                val name = cursor.getString(nameIndex)?.trim().orEmpty()
                val number = cursor.getString(numberIndex)?.replace(Regex("[\\s().-]"), "").orEmpty()
                if (name.isNotEmpty() && number.filter(Char::isDigit).endsWith(suffix)) found.add(name to number)
            }
        }
        return found.toList().distinctBy { it.second }
    }

    private fun contactNameVariants(query: String): List<String> {
        val normalized = foldContactText(query)
        val family = mapOf(
            "maman" to listOf("maman", "mama", "mum", "mummy", "maman", "mere", "mère"),
            "mama" to listOf("maman", "mama", "mum", "mummy", "mere", "mère"),
            "mum" to listOf("maman", "mama", "mum", "mummy", "mere", "mère"),
            "mummy" to listOf("maman", "mama", "mum", "mummy", "mere", "mère"),
            "papa" to listOf("papa", "dad", "daddy", "pere", "père"),
            "dad" to listOf("papa", "dad", "daddy", "pere", "père"),
        )
        return family[normalized] ?: listOf(query)
    }

    /**
     * Android SQLite COLLATE NOCASE ne retire pas les accents. Comparer les
     * deux formes pliées ici rend « frere aladji » équivalent à « Frère
     * Aladji », y compris sur les carnets dont la base n'est pas en UTF-8.
     */
    private fun foldContactText(value: String): String =
        Normalizer.normalize(value, Normalizer.Form.NFD)
            .replace(Regex("\\p{Mn}+"), "")
            .lowercase()
            .trim()

    private fun normalizedContactTokens(value: String): List<String> =
        foldContactText(value).split(Regex("[^\\p{L}\\p{N}]+"))
            .filter { it.isNotBlank() }

    /** Accepte aussi les requêtes sans accents, sans dépendre de LIKE SQLite. */
    private fun matchesContactName(name: String, query: String): Boolean {
        val wanted = normalizedContactTokens(query)
        val actual = foldContactText(name)
        return wanted.isNotEmpty() && wanted.all { actual.contains(it) }
    }

    /**
     * Les mots-nombres les plus courants arrivent du STT sous forme de texte.
     * Cette méthode ne convertit qu'une expression entièrement numérique, pour
     * ne jamais altérer un nom de contact tel que « Les Deux Frères ».
     */
    private fun normalizeSpokenSuffix(raw: String): String {
        val original = raw.trim()
        if (original.any(Char::isDigit)) return original
        val text = foldContactText(original).replace('-', ' ')
            .replace(Regex("\\s+(a|au|aux) la fin$"), "")
        val tokens = text.split(Regex("\\s+")).filter { it.isNotBlank() && it != "et" }
        val units = mapOf("zero" to 0, "un" to 1, "une" to 1, "deux" to 2,
            "trois" to 3, "quatre" to 4, "cinq" to 5, "six" to 6, "sept" to 7,
            "huit" to 8, "neuf" to 9, "dix" to 10, "onze" to 11, "douze" to 12,
            "treize" to 13, "quatorze" to 14, "quinze" to 15, "seize" to 16)
        val tens = mapOf("vingt" to 20, "trente" to 30, "quarante" to 40,
            "cinquante" to 50, "soixante" to 60)
        if (tokens.isEmpty() || tokens.any { it !in units && it !in tens && it !in setOf("cent", "cents", "mille") }) return original
        // « sept zéro » est une dictée chiffre par chiffre : 70, et non 7.
        if (tokens.size in 2..5 && tokens.all { it in units && units.getValue(it) < 10 }) {
            return tokens.joinToString("") { units.getValue(it).toString() }
        }
        var total = 0
        var current = 0
        var index = 0
        while (index < tokens.size) {
            val token = tokens[index]
            if (token == "quatre" && index + 1 < tokens.size && tokens[index + 1] == "vingt") {
                current += 80; index += 2; continue
            }
            when {
                token in units -> current += units.getValue(token)
                token in tens -> current += tens.getValue(token)
                token == "cent" || token == "cents" -> current = maxOf(1, current) * 100
                token == "mille" -> { total += maxOf(1, current) * 1000; current = 0 }
            }
            index++
        }
        val number = total + current
        return if (number in 10..99999) number.toString() else original
    }

    private fun maskNumber(number: String): String {
        val digits = number.filter(Char::isDigit)
        return if (digits.length <= 4) "••••" else "•••• ${digits.takeLast(4)}"
    }

    private fun encode(
        bytes: ByteArray?,
        width: Int?,
        height: Int?,
        rotation: Int?,
        mirror: Boolean,
        quality: Int?,
        result: MethodChannel.Result,
    ) {
        if (bytes == null || width == null || height == null) {
            result.error("bad_args", "Image incomplète", null)
            return
        }
        try {
            val stream = ByteArrayOutputStream()
            YuvImage(bytes, ImageFormat.NV21, width, height, null)
                .compressToJpeg(Rect(0, 0, width, height), quality ?: 70, stream)
            val jpeg = stream.toByteArray()

            val degrees = ((rotation ?: 0) % 360 + 360) % 360
            if (degrees == 0 && !mirror) {
                result.success(jpeg)
                return
            }

            // Le capteur est monté de travers sur la plupart des téléphones :
            // sans cette rotation, la photo arriverait couchée sur le PC. Le
            // miroir, lui, ne concerne que la caméra frontale : se filmer en
            // image non retournée donne l'impression de regarder quelqu'un
            // d'autre.
            val source = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)
            if (source == null) {
                result.success(jpeg)
                return
            }
            val matrix = Matrix().apply {
                if (mirror) postScale(-1f, 1f, source.width / 2f, source.height / 2f)
                if (degrees != 0) postRotate(degrees.toFloat())
            }
            val rotated = Bitmap.createBitmap(
                source, 0, 0, source.width, source.height, matrix, true
            )
            val out = ByteArrayOutputStream()
            rotated.compress(Bitmap.CompressFormat.JPEG, quality ?: 70, out)
            source.recycle()
            rotated.recycle()
            result.success(out.toByteArray())
        } catch (error: Throwable) {
            result.error("encode_failed", error.message, null)
        }
    }
}
