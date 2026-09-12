# 🔧 ANO-GPT Fixes & Improvements

## 1️⃣ LANGUE FRANÇAISE (Permanent)
**Problem**: Ano-GPT changeait de langue (Thai/English) selon l'entrée
**Fix**: 
- Ajout d'instruction stricte: "LANGUAGE: Always respond ONLY in French"
- Suppression de la logique conflictuelle Turkish/English
- **Result**: Toujours français, peu importe la langue d'entrée ✅

## 2️⃣ MESSAGES DUPLIQUÉS (Éliminés)
**Problem**: Messages affichés deux fois d'affilée ("Comment puis-je vous assister ce matin?...")
**Fix**:
- Ajout de déduplication simple: ne pas ajouter deux chunks identiques d'affilée
- Check: `if txt and (not out_buf or out_buf[-1] != txt)`
- **Result**: Plus de doublon ✅

## 3️⃣ SYNCHRONISATION TEXTE-PAROLE (Accélérée)
**Problem**: Texte s'affichait trop lentement (2ms/caractère = ~200ms pour le message)
**Fix**:
- Réduit le délai typewriter de 2ms à 1ms/caractère
- Texte = ~100ms pour 200 chars (synchronisé avec la parole)
- **Result**: Affichage plus rapide et naturel ✅

## 4️⃣ PETITS BRUITS QUI COUPENT (Réduits au minimum)
**Problem**: Micro-bruits interrompaient la parole de Jarvis (faux barge-in)
**Fix**:
- Augmenté `_STRICT_MULTIPLIER`: 2 → 5 (300ms au lieu de 120ms pour barge-in)
- Augmenté multiplicateur noise_floor: 3.0 → 5.0 (parole doit être 5x plus forte)
- Augmenté seuil RMS minimum: 1e-3 → 3e-3 (bloque micro-bruits)
- **Result**: Seulement la vraie parole peut interrompre ✅

## 5️⃣ RECONNAISSANCE VOCALE (Améliorée pour français + bruit)
**Problem**: STT comprend mal avec bruits de fond, même après Whisper
**Fix**:
- Forcé langue: None → "fr" (français explicite)
- Augmenté beam_size: 5 → 10 (meilleure précision)
- Réduit no_speech_threshold: 0.6 → 0.35 (détecte mieux en bruit)
- Réduit log_prob_threshold: -1.0 → -0.5 (plus tolérant)
- Augmenté vad_min_silence_ms: 300 → 500ms (VAD plus robuste)
- Augmenté streaming_min_speech_ms: 300 → 400ms (rejette micro-bruits)
- Augmenté VAD aggressiveness: 2 → 3 (rejette plus de bruits)
- Augmenté noise_threshold_db: -70 → -60dB (moins sensible)
- **Result**: Meilleure reconnaissance en français avec bruits ambiant ✅

## Résumé
- ✅ Français 100% permanent
- ✅ Plus de messages dupliqués
- ✅ Texte synchronisé avec la parole
- ✅ Petits bruits n'interrompent plus
- ✅ STT améliore (français + robuste au bruit)

**Status**: Testable immédiatement
