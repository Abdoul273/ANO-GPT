"""Tests de core/audio_router.py — politique micro (§D) : jamais de casque
Bluetooth en mains-libres (HFP narrowband) choisi automatiquement."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audio_router import (
    InputDevice, OutputDevice, choose_best_device, _bt_mic_quality, _classify_bus, _rank,
    choose_system_device, portaudio_input_device,
)
from core import audio_router


def _dev(name, kind, bt_quality="n/a"):
    return InputDevice(name=name, description=name, card_index=1,
                       sample_rate=48000, kind=kind, bt_quality=bt_quality)


def test_bt_narrowband_never_chosen_over_internal():
    devices = [
        _dev("bt_mic", "bluetooth", "narrowband"),
        _dev("internal_mic", "internal"),
    ]
    chosen = choose_best_device(devices)
    assert chosen.device.name == "internal_mic"


def test_bt_wideband_beats_everything():
    devices = [
        _dev("internal_mic", "internal"),
        _dev("usb_mic", "usb"),
        _dev("bt_mic_wideband", "bluetooth", "wideband"),
    ]
    chosen = choose_best_device(devices)
    assert chosen.device.name == "bt_mic_wideband"


def test_usb_beats_internal():
    devices = [_dev("internal_mic", "internal"), _dev("usb_mic", "usb")]
    chosen = choose_best_device(devices)
    assert chosen.device.name == "usb_mic"


def test_narrowband_is_last_resort_when_nothing_else_exists():
    devices = [_dev("bt_mic", "bluetooth", "narrowband")]
    chosen = choose_best_device(devices)
    assert chosen.device.name == "bt_mic"
    assert "mains-libres" in chosen.reason


def test_system_default_micro_is_preferred_even_if_bluetooth_is_narrowband(monkeypatch):
    """ANO suit le choix PipeWire de l'utilisateur, pas sa propre hiérarchie."""
    internal = _dev("internal_mic", "internal")
    airpods = _dev("airpods_mic", "bluetooth", "narrowband")
    monkeypatch.setattr(audio_router, "_manual_override", None)
    monkeypatch.setattr(audio_router, "_system_default_source_name", lambda: "airpods_mic")

    chosen = choose_system_device([internal, airpods])

    assert chosen.device is airpods
    assert "par défaut du système" in chosen.reason


def test_no_devices_at_all():
    chosen = choose_best_device([])
    assert chosen.device is None


def test_rank_ordering_wideband_usb_other_internal_narrowband():
    wideband = _dev("a", "bluetooth", "wideband")
    usb = _dev("b", "usb")
    internal = _dev("c", "internal")
    narrowband = _dev("d", "bluetooth", "narrowband")
    assert _rank(wideband) < _rank(usb) < _rank(internal) < _rank(narrowband)


def test_bt_mic_quality_detects_msbc_profile():
    card = {"profiles": {
        "headset-head-unit-msbc": "x", "off": "y", "a2dp-sink": "z",
    }}
    assert _bt_mic_quality(card) == "wideband"


def test_bt_mic_quality_falls_back_to_narrowband_cvsd_only():
    card = {"profiles": {"headset-head-unit": "x", "a2dp-sink": "y", "off": "z"}}
    assert _bt_mic_quality(card) == "narrowband"


def test_bt_mic_quality_no_headset_profile_at_all():
    card = {"profiles": {"a2dp-sink": "x", "off": "y"}}
    assert _bt_mic_quality(card) == "n/a"


def test_classify_bus_bluetooth():
    assert _classify_bus({"device.bus": "bluetooth"}) == "bluetooth"
    assert _classify_bus({"device.api": "bluez5"}) == "bluetooth"


def test_classify_bus_usb():
    assert _classify_bus({"device.bus": "usb"}) == "usb"


def test_classify_bus_internal_pci():
    assert _classify_bus({"device.bus": "pci"}) == "internal"
    assert _classify_bus({"device.form_factor": "internal"}) == "internal"


def test_easyeffects_virtual_source_ne_devient_pas_un_micro_candidat(monkeypatch):
    monkeypatch.setattr(audio_router, "list_cards", lambda: [])
    monkeypatch.setattr(audio_router, "_pactl_json", lambda *args: [
        {
            "name": "alsa_input.internal",
            "description": "Micro interne",
            "sample_specification": "float32le 2ch 48000Hz",
            "properties": {"device.bus": "pci"},
        },
        {
            "name": "easyeffects_source",
            "description": "Easy Effects Source",
            "sample_specification": "float32le 2ch 48000Hz",
            "properties": {},
        },
    ])

    assert [device.name for device in audio_router.list_input_devices()] == [
        "alsa_input.internal"
    ]


def test_easyeffects_default_source_emits_an_explicit_warning(monkeypatch):
    monkeypatch.setattr(audio_router, "_system_default_source_name", lambda: "easyeffects_source")
    assert "easyeffects_source" in audio_router.default_source_warning()


def test_refresh_ne_force_jamais_un_profil_bluetooth(monkeypatch):
    internal = _dev("internal_mic", "internal")
    monkeypatch.setattr(audio_router, "unload_all_echo_cancel", lambda: 0)
    monkeypatch.setattr(audio_router, "list_input_devices", lambda: [internal])
    monkeypatch.setattr(
        audio_router,
        "_ensure_bluetooth_mic_profile",
        lambda _devices: (_ for _ in ()).throw(
            AssertionError("refresh_and_apply ne doit pas toucher aux profils")
        ),
    )
    applied = []
    monkeypatch.setattr(audio_router, "apply_choice", lambda chosen: applied.append(chosen.device.name) or True)
    monkeypatch.setattr(audio_router, "prepare_source", lambda name: None)

    chosen = audio_router.refresh_and_apply()

    assert chosen.device.name == "internal_mic"
    assert applied == ["internal_mic"]


def test_prepare_source_corrige_seulement_un_gain_materiel_pathologique(monkeypatch):
    calls = []
    monkeypatch.setattr(audio_router, "_pactl", lambda *args, **kwargs: calls.append(args) or True)
    monkeypatch.setattr(audio_router, "_pactl_json", lambda *args, **kwargs: [{
        "name": "internal_mic",
        "base_volume": {"value": 6554},       # 10 %
        "volume": {"front-left": {"value": 65536}},  # 100 %
    }])

    audio_router.prepare_source("internal_mic")

    assert ("set-source-mute", "internal_mic", "0") in calls
    assert ("set-source-volume", "internal_mic", "30%") in calls


def test_prepare_source_preserve_un_micro_au_gain_normal(monkeypatch):
    calls = []
    monkeypatch.setattr(audio_router, "_pactl", lambda *args, **kwargs: calls.append(args) or True)
    monkeypatch.setattr(audio_router, "_pactl_json", lambda *args, **kwargs: [{
        "name": "usb_mic",
        "base_volume": {"value": 65536},
        "volume": {"front-left": {"value": 65536}},
    }])

    audio_router.prepare_source("usb_mic")

    assert not any(call[0] == "set-source-volume" for call in calls)


def test_nettoyage_aec_ne_supprime_pas_les_modules_du_systeme(monkeypatch):
    class Result:
        returncode = 0
        stdout = (
            "10\tmodule-echo-cancel\tsource_name=system_aec\n"
            "11\tmodule-echo-cancel\tsource_name=anogpt_mic_aec\n"
        )

    monkeypatch.setattr(audio_router.subprocess, "run", lambda *a, **k: Result())
    assert audio_router._list_echo_cancel_module_ids() == ["11"]


def test_portaudio_utilise_le_pont_pulse_qui_suit_la_source_choisie():
    class SoundDevice:
        @staticmethod
        def query_devices():
            return [
                {"name": "default", "max_input_channels": 128},
                {"name": "pipewire", "max_input_channels": 128},
                {"name": "pulse", "max_input_channels": 32},
            ]

    assert portaudio_input_device(SoundDevice) == "pulse"


def test_portaudio_se_replie_proprement_sans_pont_linux():
    class SoundDevice:
        @staticmethod
        def query_devices():
            return [{"name": "Microphone USB", "max_input_channels": 1}]

    assert portaudio_input_device(SoundDevice) is None


def test_selection_sortie_deplace_les_flux_deja_ouverts(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(audio_router, "_SELECTION_PATH", tmp_path / "audio.json")
    monkeypatch.setattr(
        audio_router, "list_output_devices",
        lambda: [OutputDevice("sink.usb", "USB", "usb")],
    )
    monkeypatch.setattr(
        audio_router, "_pactl_json",
        lambda *args: [{"index": 42}] if args == ("list", "sink-inputs") else [],
    )
    monkeypatch.setattr(
        audio_router, "_pactl",
        lambda *args, **kwargs: calls.append(args) or True,
    )

    assert audio_router.set_output_override("sink.usb")
    assert ("set-default-sink", "sink.usb") in calls
    assert ("move-sink-input", "42", "sink.usb") in calls


def test_sortie_inconnue_est_refusee(monkeypatch):
    monkeypatch.setattr(audio_router, "list_output_devices", lambda: [])
    assert not audio_router.set_output_override("sink.absent")
