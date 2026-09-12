package com.anogpt.ano_remote

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.ContactsContract
import android.provider.Telephony
import org.json.JSONArray
import org.json.JSONObject

/**
 * File durable des SMS entrants. Le BroadcastReceiver peut être appelé alors
 * que Flutter se reconnecte ; il ne doit donc jamais dépendre de l'Activity.
 */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val messages = Telephony.Sms.Intents.getMessagesFromIntent(intent)
        if (messages.isEmpty()) return
        val sender = messages.first().originatingAddress ?: "Expéditeur inconnu"
        val body = messages.joinToString(separator = "") { it.messageBody ?: "" }.take(4000)
        if (body.isBlank()) return
        val prefs = context.getSharedPreferences("ano_sms", Context.MODE_PRIVATE)
        val queue = try { JSONArray(prefs.getString("queue", "[]")) } catch (_: Throwable) { JSONArray() }
        val now = System.currentTimeMillis()
        // Un même PDU peut être diffusé plus d'une fois selon le constructeur.
        // Éviter une annonce doublée, sans supprimer deux vrais SMS identiques.
        if (queue.length() > 0) {
            val last = queue.optJSONObject(queue.length() - 1)
            if (last != null && last.optString("sender") == sender && last.optString("body") == body &&
                now - last.optLong("received_at") < 5_000) return
        }
        queue.put(JSONObject().apply {
            put("event_id", java.util.UUID.randomUUID().toString())
            put("sender", sender)
            put("sender_name", contactName(context, sender))
            put("body", body)
            put("received_at", now)
        })
        // Borne le stockage et la charge de synchronisation après une longue
        // indisponibilité du PC.
        while (queue.length() > 50) queue.remove(0)
        prefs.edit().putString("queue", queue.toString()).apply()
        // Réveille le tunnel natif, sans démarrer l'interface Flutter ni
        // attendre que l'utilisateur ouvre ANO Remote.
        PhoneRelayService.wake(context)
    }

    private fun contactName(context: Context, number: String): String {
        try {
            val uri = android.net.Uri.withAppendedPath(
                ContactsContract.PhoneLookup.CONTENT_FILTER_URI,
                android.net.Uri.encode(number),
            )
            context.contentResolver.query(uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME),
                null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) return cursor.getString(0).orEmpty()
            }
        } catch (_: Throwable) { }
        return ""
    }
}
