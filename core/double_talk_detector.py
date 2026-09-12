"""Détection de double parole (DTD) pour l'AEC full-duplex.

Quand Jarvis parle, le micro contient l'écho du haut-parleur. Un filtre
adaptatif (NLMS / Speex) l'annule tant que l'utilisateur se tait. Dès que
les deux parlent à la fois, l'erreur d'annulation n'est plus de l'écho
résiduel : c'est de la parole proche. Continuer d'adapter fait diverger
les coefficients et déforme la voix envoyée à Gemini Live.

Ce module tranche en moins de 10 ms entre « écho seul » et « double
parole » en combinant :

* le **ratio de Geigel** — pic micro / pic far-end sur la queue du filtre
* la **corrélation croisée normalisée (NCC)** — alignée sur l'estimée
  d'écho du NLMS, ou par recherche de retard tant que le filtre n'a pas
  verrouillé

Décisions
---------
* **Gel** : dès la double parole, les coefficients NLMS sont figés.
  L'annulation continue (convolution figée) ; seule l'adaptation s'arrête.
* **Canal micro** : jamais coupé. Un gain de confort lissé atténue le
  résidu d'écho, et passe à 1.0 dès que l'utilisateur parle.
* **Barge-in** : uniquement si le VAD neural confirme la voix sur
  **2 trames consécutives**, pendant un état double-parole ou near-end.
"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass

import numpy as np

# ═══════════════════════════════════════════════════════════════════════════════
# Constantes DSP
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_RATE = 16_000
FRAME_MS = 5                          # Décision à 5 ms → marge sous les 10 ms
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 80

# Queue Geigel = queue AEC (200 ms) : couvre les réflexions d'un salon.
GEIGEL_TAIL_MS = 200
GEIGEL_TAIL_SAMPLES = SAMPLE_RATE * GEIGEL_TAIL_MS // 1000  # 3200

# Recherche de retard NCC tant que le NLMS n'a pas verrouillé (~64 ms).
NCC_MAX_LAG_SAMPLES = SAMPLE_RATE * 64 // 1000  # 1024

# NLMS : 32 ms de taps — chemin direct + premières réflexions, budget CPU
# compatible avec le callback audio (GIL partagé avec Qt).
NLMS_TAPS = 512
NLMS_MU = 0.45
NLMS_EPS = 1e-8

# Seuils de décision
GEIGEL_THRESHOLD = 0.50       # ratio d'énergie / pic au-dessus → parole proche
NCC_LOW = 0.58                # NCC sous ce seuil pendant far-end → double parole
NCC_HIGH = 0.84               # NCC au-dessus → écho seul, veto du Geigel
FAR_RMS_FLOOR = 0.012         # Far-end considéré actif
MIC_RMS_FLOOR = 0.010         # Micro considéré actif
LOCK_ECHO_RATIO = 0.35        # ŷ / |d| au-delà → le NLMS a accroché l'écho

# Hangover : garder le gel pendant les micro-trous (plosives, liaisons).
HOLD_MS = 40.0

# Gain de confort — le canal reste ouvert (plancher > 0).
GAIN_OPEN = 1.0
GAIN_ECHO = 0.55
GAIN_SILENCE = 0.40
GAIN_FLOOR = 0.28
ATTACK_MS = 4.0               # Ouverture rapide vers la voix utilisateur
RELEASE_MS = 18.0             # Fermeture douce, sans clic

# Barge-in : 2 trames neurales consécutives, jamais le DTD seul.
VAD_THRESHOLD = 0.55
BARGE_CONFIRM_FRAMES = 2

_EPS = 1e-12


class TalkState(enum.Enum):
    SILENCE = "silence"
    ECHO_ONLY = "echo_only"
    NEAR_END = "near_end"
    DOUBLE_TALK = "double_talk"


# ═══════════════════════════════════════════════════════════════════════════════
# Primitives
# ═══════════════════════════════════════════════════════════════════════════════

def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def float_to_int16(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return np.empty(0, dtype=np.int16)
    scaled = np.clip(np.rint(x.astype(np.float64) * 32767.0), -32768, 32767)
    return scaled.astype(np.int16)


def to_float32(pcm: np.ndarray | bytes) -> np.ndarray:
    if isinstance(pcm, (bytes, bytearray)):
        pcm = np.frombuffer(pcm, dtype=np.int16)
    if pcm.dtype == np.int16:
        return pcm.astype(np.float32) * (1.0 / 32768.0)
    return np.ascontiguousarray(pcm, dtype=np.float32)


def normalized_cross_correlation(a: np.ndarray, b: np.ndarray) -> float:
    """NCC |<a,b>| / (||a|| ||b||) sur deux vecteurs de même longueur."""
    n = min(a.size, b.size)
    if n == 0:
        return 0.0
    a = a[:n].astype(np.float64, copy=False)
    b = b[:n].astype(np.float64, copy=False)
    denom = float(np.sqrt(np.dot(a, a) * np.dot(b, b))) + _EPS
    return abs(float(np.dot(a, b))) / denom


def max_lag_ncc(mic: np.ndarray, far_hist: np.ndarray, max_lag: int) -> float:
    """NCC maximale entre le micro et le far-end sur ``max_lag`` retards."""
    n = mic.size
    if n == 0 or far_hist.size < n:
        return 0.0
    lag = int(min(max(0, max_lag), far_hist.size - n))
    ref = far_hist[-(n + lag):].astype(np.float64, copy=False)
    mic64 = mic.astype(np.float64, copy=False)
    corr = np.correlate(ref, mic64, mode="valid")
    if corr.size == 0:
        return 0.0
    mic_n = float(np.sqrt(np.dot(mic64, mic64))) + _EPS
    csum = np.concatenate(([0.0], np.cumsum(ref * ref)))
    energies = csum[n:] - csum[:-n]
    ncc_lags = np.abs(corr) / (mic_n * np.sqrt(energies + _EPS))
    return float(np.max(ncc_lags))


def geigel_ratio(
    mic: np.ndarray,
    far_tail: np.ndarray,
    far_frame: np.ndarray | None = None,
) -> float:
    """Ratio de Geigel combiné pic + énergie.

    Le critère historique est ``max|d| / max|x|`` sur la queue. Sur de la
    parole (pics rarement alignés) il sous-détecte : on prend aussi le
    ratio d'énergie RMS de la trame courante, et on conserve le max.
    L'écho seul reste borné par le gain de retour (typiquement < 0,4).
    """
    if mic.size == 0:
        return 0.0
    far_peak = float(np.max(np.abs(far_tail))) if far_tail.size else 0.0
    mic_peak = float(np.max(np.abs(mic)))
    peak_ratio = mic_peak / (far_peak + _EPS)
    energy_ref = far_frame if far_frame is not None and far_frame.size else far_tail
    energy_ratio = rms(mic) / (rms(energy_ref) + _EPS)
    return float(max(peak_ratio, energy_ratio))


# ═══════════════════════════════════════════════════════════════════════════════
# Filtre NLMS à gel explicite
# ═══════════════════════════════════════════════════════════════════════════════

class AdaptiveEchoFilter:
    """NLMS bloc dont les coefficients se gèlent à la demande.

    ``w[0]`` s'applique au plus ancien échantillon de la fenêtre (FIR
    causale). Une mise à jour par trame suffit : le DTD n'a besoin que
    d'une estimée d'écho cohérente, pas d'une annulation 60 dB.
    """

    def __init__(
        self,
        n_taps: int = NLMS_TAPS,
        mu: float = NLMS_MU,
        eps: float = NLMS_EPS,
    ) -> None:
        if n_taps < 2:
            raise ValueError("n_taps must be >= 2")
        self.n_taps = int(n_taps)
        self.mu = float(mu)
        self.eps = float(eps)
        self.w = np.zeros(self.n_taps, dtype=np.float32)
        self._z = np.zeros(self.n_taps - 1, dtype=np.float32)
        self._last_x = np.zeros(self.n_taps, dtype=np.float32)
        self.frozen = False
        self.updates = 0

    @property
    def coefficients(self) -> np.ndarray:
        return self.w.copy()

    def reset(self) -> None:
        self.w.fill(0.0)
        self._z.fill(0.0)
        self._last_x.fill(0.0)
        self.frozen = False
        self.updates = 0

    def snapshot(self) -> np.ndarray:
        return self.w.copy()

    def apply(self, mic: np.ndarray, far: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Convolution figée ou non : n'adapte jamais ``w``."""
        mic = np.ascontiguousarray(mic, dtype=np.float32)
        far = np.ascontiguousarray(far, dtype=np.float32)
        n = min(mic.size, far.size)
        if n == 0:
            empty = np.empty(0, dtype=np.float32)
            return empty, empty
        mic = mic[:n]
        far = far[:n]
        buf = np.concatenate((self._z, far))
        echo = np.convolve(buf, self.w, mode="valid")[:n].astype(np.float32)
        residual = mic - echo
        self._z = buf[-self._z.size:] if self._z.size else self._z
        self._last_x = buf[-self.n_taps:].astype(np.float32, copy=True)
        return residual, echo

    def adapt(self, residual: np.ndarray) -> None:
        """Une itération NLMS sur la dernière trame, ignorée si gelé."""
        if self.frozen or residual.size == 0:
            return
        power = float(np.dot(self._last_x, self._last_x)) + self.eps
        self.w = (
            self.w + (self.mu * float(residual[-1]) / power) * self._last_x
        ).astype(np.float32, copy=False)
        np.clip(self.w, -8.0, 8.0, out=self.w)
        self.updates += 1

    def process(
        self,
        mic: np.ndarray,
        far: np.ndarray,
        freeze: bool | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Retourne ``(résidu, estimée d'écho)`` en float32.

        Si ``freeze`` est vrai, ``w`` n'est pas touché. L'annulation
        continue avec les coefficients figés.
        """
        if freeze is not None:
            self.frozen = bool(freeze)
        residual, echo = self.apply(mic, far)
        self.adapt(residual)
        return residual, echo


# ═══════════════════════════════════════════════════════════════════════════════
# Résultat d'une trame
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DTDResult:
    state: TalkState
    ncc: float
    geigel: float
    frozen: bool
    comfort_gain: float
    residual: np.ndarray
    echo_estimate: np.ndarray
    should_barge_in: bool = False
    vad_probability: float = 0.0
    filter_locked: bool = False
    far_active: bool = False
    mic_active: bool = False

    @property
    def residual_int16(self) -> np.ndarray:
        return float_to_int16(self.residual)

    @property
    def echo_estimate_int16(self) -> np.ndarray:
        return float_to_int16(self.echo_estimate)


# ═══════════════════════════════════════════════════════════════════════════════
# Détecteur
# ═══════════════════════════════════════════════════════════════════════════════

class DoubleTalkDetector:
    """DTD temps réel : Geigel + NCC, gel NLMS, gain de confort, barge-in.

    Thread-safety : ``process`` / ``update_barge_in`` sont protégés par
    ``_lock`` — appelables depuis le callback PortAudio.
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        frame_samples: int = FRAME_SAMPLES,
        n_taps: int = NLMS_TAPS,
        geigel_threshold: float = GEIGEL_THRESHOLD,
        ncc_low: float = NCC_LOW,
        ncc_high: float = NCC_HIGH,
        vad=None,
        vad_threshold: float = VAD_THRESHOLD,
        barge_confirm_frames: int = BARGE_CONFIRM_FRAMES,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.frame_samples = int(frame_samples)
        self.geigel_threshold = float(geigel_threshold)
        self.ncc_low = float(ncc_low)
        self.ncc_high = float(ncc_high)
        self.vad_threshold = float(vad_threshold)
        self.barge_confirm_frames = int(barge_confirm_frames)

        self.filter = AdaptiveEchoFilter(n_taps=n_taps)
        self._vad = vad
        self._lock = threading.Lock()

        self._far_hist = np.zeros(GEIGEL_TAIL_SAMPLES, dtype=np.float32)
        self._state = TalkState.SILENCE
        self._ncc = 0.0
        self._ncc_frames = 0
        self._geigel = 0.0
        self._gain = GAIN_SILENCE
        self._hold_remaining_ms = 0.0
        self._filter_locked = False
        self._vad_streak = 0
        self._last_result: DTDResult | None = None
        self._frames = 0
        self._dt_frames = 0
        self._barge_events = 0
        self._freeze_events = 0

    # ── API lecture ───────────────────────────────────────────────────────

    @property
    def state(self) -> TalkState:
        return self._state

    @property
    def frozen(self) -> bool:
        return self.filter.frozen

    @property
    def last_result(self) -> DTDResult | None:
        return self._last_result

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "frozen": self.filter.frozen,
                "ncc": round(self._ncc, 4),
                "geigel": round(self._geigel, 4),
                "comfort_gain": round(self._gain, 4),
                "filter_locked": self._filter_locked,
                "frames": self._frames,
                "dt_frames": self._dt_frames,
                "freeze_events": self._freeze_events,
                "barge_events": self._barge_events,
                "vad_streak": self._vad_streak,
                "nlms_updates": self.filter.updates,
            }

    def reset(self) -> None:
        with self._lock:
            self.filter.reset()
            self._far_hist.fill(0.0)
            self._state = TalkState.SILENCE
            self._ncc = 0.0
            self._ncc_frames = 0
            self._geigel = 0.0
            self._gain = GAIN_SILENCE
            self._hold_remaining_ms = 0.0
            self._filter_locked = False
            self._vad_streak = 0
            self._last_result = None
            self._frames = 0
            self._dt_frames = 0
            self._barge_events = 0
            self._freeze_events = 0
            if self._vad is not None:
                reset = getattr(self._vad, "reset", None)
                if callable(reset):
                    try:
                        reset()
                    except Exception:
                        pass

    # ── Traitement d'une trame ────────────────────────────────────────────

    def process(
        self,
        mic: np.ndarray | bytes,
        far: np.ndarray | bytes,
        vad_probability: float | None = None,
        jarvis_speaking: bool = False,
    ) -> DTDResult:
        """Analyse une trame micro + far-end et met à jour gel / gain / barge-in.

        ``mic`` et ``far`` : float32 [-1, 1] ou int16. Longueurs différentes
        → troncature au plus court. Une trame de 5 ms (80 échantillons à
        16 kHz) garantit une décision en moins de 10 ms.
        """
        mic_f = to_float32(mic)
        far_f = to_float32(far)
        n = min(mic_f.size, far_f.size)
        if n == 0:
            empty = np.empty(0, dtype=np.float32)
            result = DTDResult(
                state=self._state,
                ncc=self._ncc,
                geigel=self._geigel,
                frozen=self.filter.frozen,
                comfort_gain=self._gain,
                residual=empty,
                echo_estimate=empty,
            )
            return result
        mic_f = np.clip(mic_f[:n], -1.0, 1.0)
        far_f = np.clip(far_f[:n], -1.0, 1.0)
        frame_ms = 1000.0 * n / self.sample_rate

        with self._lock:
            self._push_far(far_f)

            far_rms = rms(far_f)
            mic_rms = rms(mic_f)
            far_active = far_rms >= FAR_RMS_FLOOR
            mic_active = mic_rms >= MIC_RMS_FLOOR

            # 1. Convolution (jamais d'adaptation ici) puis critères DTD.
            residual, echo_est = self.filter.apply(mic_f, far_f)

            echo_rms = rms(echo_est)
            ncc_echo = normalized_cross_correlation(mic_f, echo_est)
            # Un ŷ énergétique mais mal aligné n'est pas un verrou : il
            # fausserait la NCC et gèlerait le filtre sur de l'écho seul.
            self._filter_locked = (
                echo_rms >= LOCK_ECHO_RATIO * max(mic_rms, _EPS)
                and ncc_echo >= self.ncc_high
            )

            # 2. NCC : ŷ alignée si le filtre a accroché, sinon max sur retards.
            if self._filter_locked:
                ncc_inst = ncc_echo
            else:
                ncc_inst = max_lag_ncc(mic_f, self._far_hist, NCC_MAX_LAG_SAMPLES)
            if self._ncc_frames == 0:
                self._ncc = ncc_inst
            else:
                self._ncc = 0.55 * self._ncc + 0.45 * ncc_inst
            self._ncc_frames += 1

            # 3. Geigel pic (queue) + énergie (trame courante).
            self._geigel = geigel_ratio(mic_f, self._far_hist, far_f)

            detected_dt = self._decide_double_talk(
                far_active=far_active,
                mic_active=mic_active,
            )

            if detected_dt:
                new_state = TalkState.DOUBLE_TALK
                self._hold_remaining_ms = HOLD_MS
            elif self._hold_remaining_ms > 0.0:
                new_state = TalkState.DOUBLE_TALK
                self._hold_remaining_ms = max(0.0, self._hold_remaining_ms - frame_ms)
            elif far_active:
                new_state = TalkState.ECHO_ONLY
            elif mic_active:
                new_state = TalkState.NEAR_END
            else:
                new_state = TalkState.SILENCE

            freeze_now = new_state is TalkState.DOUBLE_TALK
            if freeze_now and not self.filter.frozen:
                self._freeze_events += 1
            self.filter.frozen = freeze_now
            # Gel immédiat : cette trame de double parole n'adapte pas.
            if not freeze_now:
                self.filter.adapt(residual)
            self._state = new_state

            target_gain = self._target_gain(new_state)
            self._gain = self._smooth_gain(target_gain, frame_ms)
            gained = np.clip(residual * np.float32(self._gain), -1.0, 1.0)

            self._frames += 1
            if new_state is TalkState.DOUBLE_TALK:
                self._dt_frames += 1

            vad_prob = 0.0
            should_barge = False
            # Le barge-in n'est mis à jour que si l'appelant fournit une
            # proba VAD, un VAD interne, ou jarvis_speaking. Sinon on
            # laisse le streak intact pour ``update_barge_in``.
            if vad_probability is not None or self._vad is not None or jarvis_speaking:
                vad_ready = False
                if vad_probability is not None:
                    vad_prob = float(vad_probability)
                    vad_ready = True
                elif self._vad is not None:
                    try:
                        vad_prob = float(self._vad.probability(gained))
                        vad_ready = gained.size >= 512 or vad_prob > 0.0
                    except Exception:
                        vad_prob = 0.0
                        vad_ready = False
                should_barge = self._update_barge_locked(
                    vad_prob, jarvis_speaking, vad_ready
                )

            result = DTDResult(
                state=new_state,
                ncc=self._ncc,
                geigel=self._geigel,
                frozen=self.filter.frozen,
                comfort_gain=self._gain,
                residual=gained,
                echo_estimate=echo_est,
                should_barge_in=should_barge,
                vad_probability=vad_prob,
                filter_locked=self._filter_locked,
                far_active=far_active,
                mic_active=mic_active,
            )
            self._last_result = result
            return result

    def process_int16(
        self,
        mic: np.ndarray | bytes,
        far: np.ndarray | bytes,
        vad_probability: float | None = None,
        jarvis_speaking: bool = False,
    ) -> DTDResult:
        return self.process(
            mic, far,
            vad_probability=vad_probability,
            jarvis_speaking=jarvis_speaking,
        )

    def update_barge_in(
        self,
        vad_probability: float,
        jarvis_speaking: bool,
        vad_ready: bool = True,
    ) -> bool:
        """Confirme le barge-in : DTD (DT/near-end) + 2 trames VAD neurales."""
        with self._lock:
            return self._update_barge_locked(
                float(vad_probability), bool(jarvis_speaking), bool(vad_ready)
            )

    # ── Internes ──────────────────────────────────────────────────────────

    def _push_far(self, far: np.ndarray) -> None:
        n = far.size
        if n >= self._far_hist.size:
            self._far_hist = far[-self._far_hist.size:].copy()
            return
        self._far_hist = np.concatenate((self._far_hist[n:], far))

    def _decide_double_talk(self, far_active: bool, mic_active: bool) -> bool:
        """Combine Geigel et NCC. Le gel est « cheap » : on préfère un faux
        positif (pause d'adaptation) à une divergence du filtre.

        Le barge-in, lui, exige le VAD — c'est l'action coûteuse.
        """
        if not far_active or not mic_active:
            return False

        ncc = self._ncc
        geigel = self._geigel

        # Filtre verrouillé + NCC haute : l'écho explique le micro.
        if self._filter_locked and ncc >= self.ncc_high:
            return False

        geigel_dt = geigel >= self.geigel_threshold
        ncc_ready = self._ncc_frames >= 3
        ncc_dt = ncc_ready and ncc <= self.ncc_low

        if self._filter_locked:
            # Après convergence, la NCC attrape ce que le Geigel rate.
            return bool(ncc_dt or geigel_dt)
        # Avant verrouillage, la NCC n'est pas encore alignée : Geigel seul.
        # Un OR trop tôt (NCC à 0 au démarrage) gèlerait le filtre en
        # permanence sur de l'écho retardé.
        return bool(geigel_dt)

    def _target_gain(self, state: TalkState) -> float:
        if state is TalkState.DOUBLE_TALK or state is TalkState.NEAR_END:
            return GAIN_OPEN
        if state is TalkState.ECHO_ONLY:
            return GAIN_ECHO
        return GAIN_SILENCE

    def _smooth_gain(self, target: float, frame_ms: float) -> float:
        tau = ATTACK_MS if target > self._gain else RELEASE_MS
        alpha = 1.0 - float(np.exp(-frame_ms / max(tau, 0.5)))
        gain = self._gain + (target - self._gain) * alpha
        return float(max(GAIN_FLOOR, min(GAIN_OPEN, gain)))

    def _update_barge_locked(
        self,
        vad_prob: float,
        jarvis_speaking: bool,
        vad_ready: bool,
    ) -> bool:
        if not jarvis_speaking:
            self._vad_streak = 0
            return False
        if self._state not in (TalkState.DOUBLE_TALK, TalkState.NEAR_END):
            self._vad_streak = 0
            return False
        if not vad_ready:
            return False
        if vad_prob >= self.vad_threshold:
            self._vad_streak += 1
        else:
            self._vad_streak = 0
            return False
        if self._vad_streak >= self.barge_confirm_frames:
            self._vad_streak = 0
            self._barge_events += 1
            return True
        return False
