"""Sous-processus sans réseau : PCM local en entrée, mot-clé d'arrêt en sortie."""
import json
import struct
import sys

from core.barge_in import INTERRUPT_GRAMMAR, InterruptPhraseDetector


def read_exact(stream, size):
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.read(size - len(parts))
        if not chunk:
            return None
        parts.extend(chunk)
    return bytes(parts)


def _confident(result, text):
    if "[unk]" in text:
        return False
    words = result.get("result") or []
    if words:
        return all(float(word.get("conf", 0) or 0) >= 0.80 for word in words)
    # Phrase courte contrainte par la grammaire : « stop », « écoute ».
    return bool(InterruptPhraseDetector.classify(text))


def _emit(generation, text, kind=None):
    if not kind:
        kind = InterruptPhraseDetector.classify(text)
    if not kind:
        return False
    print(json.dumps({"generation": generation, "kind": kind, "text": text}), flush=True)
    return True


def main():
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    model = Model(sys.argv[1])
    # Grammaire fermée : hors de ces phrases, Vosk rend [unk] au lieu de
    # forcer un ordre. Si le modèle ne supporte pas la grammaire dynamique
    # (ex: grand modèle Kaldi), repli sur le recognizer complet.
    try:
        recognizer = KaldiRecognizer(
            model, int(sys.argv[2]), json.dumps(list(INTERRUPT_GRAMMAR)),
        )
    except Exception:
        recognizer = KaldiRecognizer(model, int(sys.argv[2]))
    recognizer.SetWords(True)
    generation = -1
    last_partial = ""
    print(json.dumps({"ready": True}), flush=True)
    while True:
        header = read_exact(sys.stdin.buffer, 8)
        if header is None:
            return
        current, size = struct.unpack("<II", header)
        if size > 16000 * 2 * 2:
            return
        pcm = read_exact(sys.stdin.buffer, size)
        if pcm is None:
            return
        if current != generation or not pcm:
            recognizer.Reset()
            generation = current
            last_partial = ""
        if not pcm:
            continue
        if recognizer.AcceptWaveform(pcm):
            last_partial = ""
            result = json.loads(recognizer.Result())
            text = result.get("text", "")
            if text:
                kind = InterruptPhraseDetector.classify(text)
                if kind and _confident(result, text):
                    _emit(generation, text, kind)
                    recognizer.Reset()
                elif _confident(result, text):
                    _emit(generation, text, "speech")
        else:
            partial = json.loads(recognizer.PartialResult()).get("partial", "")
            if partial and partial != last_partial:
                last_partial = partial
                if "[unk]" not in partial and _emit(generation, partial):
                    recognizer.Reset()
                    last_partial = ""


if __name__ == "__main__":
    main()
