"""PCM fidèle et mesures de capture, sans débruitage ni réécriture."""
from __future__ import annotations

import numpy as np


def pcm16_bytes(audio: np.ndarray) -> bytes:
    """Convertit un mono float normalisé sans perdre un bit du PCM16 d'origine."""
    values = np.asarray(audio)
    if values.ndim != 1:
        raise ValueError("PCM mono requis")
    if values.dtype == np.int16:
        return values.astype("<i2", copy=False).tobytes()
    if not np.isfinite(values).all():
        raise ValueError("Échantillons audio non finis")
    return np.clip(np.rint(values * 32768.0), -32768, 32767).astype("<i2").tobytes()


def signal_metrics(pcm: bytes) -> dict[str, float]:
    """Mesures factuelles ; le niveau seul n'établit pas l'intelligibilité."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    if not samples.size:
        return {"duration_s": 0.0, "rms_dbfs": -100.0,
                "peak_dbfs": -100.0, "clipped_percent": 0.0}
    rms = float(np.sqrt(np.mean(samples ** 2)))
    peak = float(np.max(np.abs(samples)))
    return {
        "duration_s": round(samples.size / 16000.0, 3),
        "rms_dbfs": round(20 * np.log10(max(rms, 1e-5)), 1),
        "peak_dbfs": round(20 * np.log10(max(peak, 1e-5)), 1),
        "clipped_percent": round(float(np.mean(np.abs(samples) >= 0.999)) * 100, 2),
    }
