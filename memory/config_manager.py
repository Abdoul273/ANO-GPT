"""
Configuration manager for MARK XL — Ultra‑robust & future‑proof edition.

Features:
  • Typed configuration via dataclasses.
  • Multiple storage backends: JSON file (primary), environment variables, .env fallback.
  • Atomic writes to prevent file corruption.
  • Sensible defaults, inline validation.
  • Thread‑safe writes, optional caching.
  • Optional encrypted secrets storage (keyring / machine‑bound).
  • Async‑compatible load/save methods.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import copy
from dataclasses import dataclass, asdict, field
from pathlib import Path
from threading import Lock, RLock
from typing import ClassVar, Dict, Optional
from core.live_model_policy import BALANCED_MODEL

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("config.mark_xl")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(levelname)s] %(name)s - %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)

# ---------------------------------------------------------------------------
# Base directory resolution (frozen exe compatible)
# ---------------------------------------------------------------------------
def get_base_dir() -> Path:
    """Return the application root directory, works even when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # When imported as a module, go two parents up if needed
    return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

# ---------------------------------------------------------------------------
# Configuration Data Model (typed & validated)
# ---------------------------------------------------------------------------
@dataclass
class AssistantConfig:
    """Full MARK XL configuration with sensible defaults."""
    # ── Core keys ────────────────────────────────────────────────────────
    gemini_api_key: str = ""
    elevenlabs_api_key: str = ""
    deepseek_api_key: str = ""
    xai_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    # OpenRouter expose son catalogue multi-éditeurs au format OpenAI.
    openrouter_api_key: str = ""
    # Picovoice reste entièrement local après l'initialisation. Cette clé est
    # nécessaire pour créer son moteur de mot d'activation ; elle ne doit jamais
    # être inscrite dans les journaux ni dans une configuration suivie par git.
    picovoice_access_key: str = ""
    # Fichiers .ppn facultatifs créés dans la console Picovoice (par ex. « ANO »).
    # Sans eux, ANO-GPT utilise le mot-clé intégré « Jarvis ».
    picovoice_keyword_paths: list[str] = field(default_factory=list)

    # ── Cerveau externe optionnel ───────────────────────────────────────
    brain_provider: str = "auto"
    brain_provider_priority: list[str] = field(
        default_factory=lambda: [
            "azure_openai", "openrouter", "deepseek", "grok", "openai", "anthropic", "groq", "gemini",
        ]
    )
    deepseek_model: str = "deepseek-v4-flash"
    grok_model: str = "grok-4.3"
    openai_model: str = "gpt-5.6-terra"
    anthropic_model: str = "claude-sonnet-5"
    openrouter_model: str = "openai/gpt-4.1"
    groq_model: str = "llama-3.3-70b-versatile"
    azure_openai_model: str = "anogpt-brain"
    gemini_model: str = BALANCED_MODEL
    # Budget de réflexion du cerveau externe, en secondes (10 à 90).
    brain_timeout_s: float = 45.0

    # ── Personalisation ──────────────────────────────────────────────────
    assistant_name: str = "ANO-GPT"
    user_name: str = ""

    # ── Features toggles ─────────────────────────────────────────────────
    morning_brief_enabled: bool = True
    voice_commands_enabled: bool = True
    offline_mode: bool = False

    # ── TTS / STT preferences ────────────────────────────────────────────
    tts_engine: str = "edgetts"            # "edgetts", "kokoro", "elevenlabs"
    tts_voice: str = "en-US-GuyNeural"     # used by all engines
    live_voice: str = "Charon"              # Gemini Live native audio voice
    stt_engine: str = "whisper"            # "whisper", "vosk"
    stt_model: str = "large-v3-turbo"      # whisper model size (large-v3-turbo, medium, small, base)
    stt_beam_size: int = 5                 # Beam search width (1..5)
    stt_ai_correction: bool = True         # AI phonetic & contextual correction
    stt_noise_reduction: bool = True       # AGC dynamic gain & 80Hz high-pass filter

    # ── UI / behaviour ───────────────────────────────────────────────────
    language: str = "en"
    volume: float = 0.8

    # ── Advanced ─────────────────────────────────────────────────────────
    debug: bool = False
    cache_tts: bool = True
    proxy: Optional[str] = None

    # ── Validation ───────────────────────────────────────────────────────
    def validate(self) -> None:
        """Raise ValueError if critical fields are missing/invalid."""
        if self.gemini_api_key and len(self.gemini_api_key) < 15:
            raise ValueError("Gemini API key seems too short.")
        if self.tts_engine not in ("edgetts", "kokoro", "elevenlabs"):
            raise ValueError(f"Unknown TTS engine: {self.tts_engine}")
        if self.stt_engine not in ("whisper", "vosk"):
            raise ValueError(f"Unknown STT engine: {self.stt_engine}")
        if not 0 <= self.volume <= 1.0:
            raise ValueError("Volume must be between 0.0 and 1.0.")
        if not 1 <= self.stt_beam_size <= 10:
            raise ValueError("stt_beam_size must be between 1 and 10.")
        # Tout fournisseur du registre peut devenir le cerveau principal :
        # l'utilisateur choisit, ce n'est plus une liste réservée au repli.
        allowed_brains = {
            "auto", "azure_openai", "openrouter", "deepseek", "grok", "groq", "openai",
            "anthropic", "gemini", "ollama", "custom",
        }
        if self.brain_provider not in allowed_brains:
            raise ValueError(f"Unknown brain provider: {self.brain_provider}")
        if not 10 <= self.brain_timeout_s <= 90:
            raise ValueError("brain_timeout_s must be between 10 and 90 seconds.")

    @classmethod
    def from_dict(cls, data: dict) -> "AssistantConfig":
        """Create config from a dictionary, ignoring unknown keys."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

# ---------------------------------------------------------------------------
# Atomic JSON file I/O
# ---------------------------------------------------------------------------
class AtomicJSONFile:
    """Thread‑safe, atomic JSON file writer. Reads normal, writes via temp file + rename."""

    _locks: ClassVar[Dict[Path, Lock]] = {}
    _locks_lock: ClassVar[Lock] = Lock()

    def __init__(self, file_path: Path):
        self._path = Path(file_path).resolve()
        self._lock = AtomicJSONFile._get_lock(self._path)

    @classmethod
    def _get_lock(cls, path: Path) -> Lock:
        with cls._locks_lock:
            if path not in cls._locks:
                cls._locks[path] = RLock()
            return cls._locks[path]

    def read(self) -> dict:
        """Read JSON content; returns empty dict on missing or corrupt file."""
        if not self._path.exists():
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("Failed to read %s: %s", self._path, exc)
            return {}

    def update(self, changes: dict) -> None:
        """Merge under the same lock as writes, preserving concurrent settings.

        Refuse to overwrite corrupt configuration: recovery must be explicit.
        This serializes threads in this process, not independent processes.
        """
        with self._lock:
            if self._path.exists():
                with self._path.open(encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("Configuration JSON must contain an object")
            else:
                data = {}
            data.update(changes)
            self.write(data)

    def write(self, data: dict) -> None:
        """Atomically write dictionary as JSON."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            # Write to temporary file in the same directory
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=self._path.parent,
                prefix="." + self._path.name + ".",
                suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                # Atomic rename (works on same filesystem)
                os.replace(tmp_path, self._path)
                logger.debug("Config written atomically to %s", self._path)
            except Exception:
                # Clean up temp file on failure
                Path(tmp_path).unlink(missing_ok=True)
                raise

# ---------------------------------------------------------------------------
# Configuration Manager (core)
# ---------------------------------------------------------------------------
class ConfigManager:
    """
    Centralised configuration manager with:
      - JSON file storage (primary)
      - Environment variable fallback (MARK_XL_*)
      - Optional keyring encryption for secrets
      - Auto‑create defaults if missing
      - Async‑compatible load/save helpers
    """

    ENV_PREFIX: ClassVar[str] = "MARK_XL_"

    def __init__(
        self,
        file_path: Optional[Path] = None,
        use_env: bool = True,
        use_keyring: bool = False,
    ):
        self._file = AtomicJSONFile(file_path or CONFIG_FILE)
        self._use_env = use_env
        self._use_keyring = use_keyring
        self._cache: Optional[AssistantConfig] = None
        self._cache_ttl: float = 5.0  # seconds (simple cache)
        self._last_load: float = 0.0
        # Ensure config directory exists
        self._file._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Load & merge (file → env → defaults)
    # ------------------------------------------------------------------
    def load(self, force_reload: bool = False) -> AssistantConfig:
        """Return fully assembled configuration (with caching)."""
        now = __import__("time").monotonic()
        if (
            not force_reload
            and self._cache is not None
            and (now - self._last_load) < self._cache_ttl
        ):
            return copy.deepcopy(self._cache)

        # 1. Base from JSON file
        file_data = self._file.read()
        config = AssistantConfig.from_dict(file_data)

        # 2. Merge environment overrides
        if self._use_env:
            self._apply_env_overrides(config)

        # 3. Apply keyring secrets (if enabled)
        if self._use_keyring:
            for secret_name in (
                "gemini_api_key", "elevenlabs_api_key", "deepseek_api_key",
                "xai_api_key", "openai_api_key", "anthropic_api_key", "openrouter_api_key",
                "picovoice_access_key",
            ):
                if not getattr(config, secret_name):
                    setattr(config, secret_name, self._get_secret(secret_name) or "")

        # Validate
        config.validate()

        self._cache = copy.deepcopy(config)
        self._last_load = now
        return config

    def _apply_env_overrides(self, config: AssistantConfig) -> None:
        """Read MARK_XL_* env vars and override corresponding fields."""
        env_map = {
            "GEMINI_API_KEY": "gemini_api_key",
            "ELEVENLABS_API_KEY": "elevenlabs_api_key",
            "DEEPSEEK_API_KEY": "deepseek_api_key",
            "XAI_API_KEY": "xai_api_key",
            "OPENAI_API_KEY": "openai_api_key",
            "ANTHROPIC_API_KEY": "anthropic_api_key",
            "OPENROUTER_API_KEY": "openrouter_api_key",
            "PICOVOICE_ACCESS_KEY": "picovoice_access_key",
            "ASSISTANT_NAME": "assistant_name",
            "USER_NAME": "user_name",
            "TTS_ENGINE": "tts_engine",
            "TTS_VOICE": "tts_voice",
            "LIVE_VOICE": "live_voice",
            "STT_ENGINE": "stt_engine",
            "STT_MODEL": "stt_model",
            "LANGUAGE": "language",
            "VOLUME": "volume",
            "DEBUG": "debug",
            "OFFLINE_MODE": "offline_mode",
            "PROXY": "proxy",
        }
        for env_key, attr in env_map.items():
            full_key = self.ENV_PREFIX + env_key
            value = os.environ.get(full_key)
            if value is not None:
                # Type conversion
                field_type = type(getattr(config, attr))
                if field_type is bool:
                    value = value.lower() in ("1", "true", "yes")
                elif field_type is float:
                    value = float(value)
                setattr(config, attr, value)

    # ------------------------------------------------------------------
    # Save (atomic)
    # ------------------------------------------------------------------
    def save(self, config: AssistantConfig) -> None:
        """Persist the configuration to disk (atomic)."""
        config.validate()
        # Préserver les réglages avancés ajoutés par d'autres modules. L'ancien
        # save remplaçait tout le JSON par les seuls champs de la dataclass et
        # pouvait donc effacer silencieusement les clés multi-provider.
        data = asdict(config)
        # Remove sensitive keys if using keyring (they are stored encrypted)
        if self._use_keyring:
            for secret_name in (
                "gemini_api_key", "elevenlabs_api_key", "deepseek_api_key",
                "xai_api_key", "openai_api_key", "anthropic_api_key", "openrouter_api_key",
                "picovoice_access_key",
            ):
                secret_value = getattr(config, secret_name)
                if secret_value:
                    self._set_secret(secret_name, secret_value)
                    data[secret_name] = ""
        self._file.update(data)
        self._cache = copy.deepcopy(config)
        self._last_load = __import__("time").monotonic()

    # ------------------------------------------------------------------
    # Keyring integration (optional encryption of secrets)
    # ------------------------------------------------------------------
    def _get_secret(self, key: str) -> Optional[str]:
        """Retrieve secret from keyring / encrypted store."""
        try:
            import keyring
            service = "mark_xl"
            return keyring.get_password(service, key)
        except ImportError:
            logger.debug("keyring not available, secrets stored in JSON (plain).")
            return None
        except Exception as exc:
            logger.warning("keyring read failed: %s", exc)
            return None

    def _set_secret(self, key: str, value: str) -> None:
        """Store secret in keyring."""
        try:
            import keyring
            service = "mark_xl"
            keyring.set_password(service, key, value)
        except ImportError:
            raise RuntimeError("Keyring unavailable; configuration was not saved") from None
        except Exception as exc:
            raise RuntimeError("Keyring storage failed; configuration was not saved") from exc

    # ------------------------------------------------------------------
    # Async helpers (for use with asyncio)
    # ------------------------------------------------------------------
    async def load_async(self) -> AssistantConfig:
        """Async wrapper around load (runs in thread)."""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.load)

    async def save_async(self, config: AssistantConfig) -> None:
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.save, config)

# ---------------------------------------------------------------------------
# Singleton convenience instance (legacy compatibility)
# ---------------------------------------------------------------------------
_default_manager = ConfigManager()

def load_config() -> AssistantConfig:
    """Legacy – returns typed config object."""
    return _default_manager.load()

def save_config(config: AssistantConfig) -> None:
    """Persist typed config object."""
    _default_manager.save(config)

# ---------------------------------------------------------------------------
# Legacy API (backward-compatible with original module)
# ---------------------------------------------------------------------------
def ensure_config_dir() -> None:
    """No‑op, directory is created automatically on write."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str, elevenlabs_api_key: str = "") -> None:
    """Quick save for API keys (legacy)."""
    config = _default_manager.load()
    config.gemini_api_key = gemini_api_key.strip()
    if elevenlabs_api_key:
        config.elevenlabs_api_key = elevenlabs_api_key.strip()
    _default_manager.save(config)

def load_api_keys() -> dict:
    """Return legacy dict with 'gemini_api_key' etc."""
    config = _default_manager.load()
    return {
        "gemini_api_key": config.gemini_api_key,
        "elevenlabs_api_key": config.elevenlabs_api_key,
        "assistant_name": config.assistant_name,
        "user_name": config.user_name,
        "morning_brief_enabled": config.morning_brief_enabled,
    }

def get_gemini_key() -> Optional[str]:
    return _default_manager.load().gemini_api_key or None

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)

def get_assistant_name() -> str:
    return _default_manager.load().assistant_name or "ANO-GPT"

def get_user_name() -> str:
    return _default_manager.load().user_name or ""

def save_assistant_config(assistant_name: str, user_name: str) -> None:
    config = _default_manager.load()
    config.assistant_name = assistant_name.strip() or "ANO-GPT"
    config.user_name = user_name.strip()
    _default_manager.save(config)

def get_brief_enabled() -> bool:
    return _default_manager.load().morning_brief_enabled

def save_brief_enabled(enabled: bool) -> None:
    config = _default_manager.load()
    config.morning_brief_enabled = enabled
    _default_manager.save(config)


def save_voice_provider(provider: str) -> None:
    if provider not in ("gemini", "elevenlabs"):
        raise ValueError("Fournisseur vocal inconnu")
    _default_manager._file.update({"voice_provider": provider})
    _default_manager._cache = None


def save_stt_provider(provider: str) -> None:
    if provider != "gemini":
        raise ValueError("Moteur de reconnaissance inconnu")
    _default_manager._file.update({"stt_provider": "gemini"})
    _default_manager._cache = None


def save_elevenlabs_voice(voice_id: str, model_id: str) -> None:
    from core.elevenlabs_voice import MODEL_OPTIONS
    if not voice_id or not voice_id.isascii() or not voice_id.isalnum():
        raise ValueError("Identifiant de voix ElevenLabs invalide")
    if model_id not in dict(MODEL_OPTIONS):
        raise ValueError("Modèle ElevenLabs inconnu")
    _default_manager._file.update({
        "elevenlabs_voice_id": voice_id,
        "elevenlabs_model_id": model_id,
    })
    _default_manager._cache = None


def save_live_voice(voice_name: str) -> None:
    """Persist only the Gemini Live voice while preserving extension keys."""
    _default_manager._file.update({"live_voice": str(voice_name).strip()})
    _default_manager._cache = None


def save_conversation_language(language: str) -> None:
    """Persist the explicit conversation-language choice without touching secrets."""
    from core.conversation_language import normalise_conversation_language
    selected = normalise_conversation_language(language)
    _default_manager._file.update({"conversation_language": selected.code})
    _default_manager._cache = None

# ---------------------------------------------------------------------------
# Command-line tool (optional)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MARK XL Configuration Tool")
    parser.add_argument("--export", action="store_true", help="Export current config to stdout")
    parser.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"), help="Set a config field")
    args = parser.parse_args()

    mgr = ConfigManager()
    config = mgr.load()

    if args.export:
        print(json.dumps(asdict(config), indent=2))
    elif args.set:
        key, value = args.set
        if hasattr(AssistantConfig, key):
            setattr(config, key, type(getattr(config, key))(value))
            mgr.save(config)
            print(f"Set {key} = {value}")
        else:
            print(f"Unknown config key: {key}", file=sys.stderr)
    else:
        print("No action specified. Use --export or --set KEY VALUE.")
