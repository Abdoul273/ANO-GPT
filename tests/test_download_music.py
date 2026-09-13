"""Téléchargement musical : meilleur match YouTube, pas n'importe quoi."""

import threading

import pytest

from actions import download_music as dl


def _track(**kwargs):
    item = {
        "id": "aaaaaaaaaaa",
        "title": "Kavinsky - Nightcall (Official Audio)",
        "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "duration": "4:18",
        "uploader": "Kavinsky - Topic",
        "views": 120_000_000,
    }
    item.update(kwargs)
    return item


def test_clean_query_retire_le_verbe_telecharge():
    assert dl.clean_query("télécharge Nightcall de Kavinsky") == "Nightcall Kavinsky"
    assert dl.clean_query("download blinding lights mp3") == "blinding lights"


def test_youtube_url_normalise_les_formes_usuelles():
    assert dl.youtube_url("dQw4w9wgGcQ") == "https://www.youtube.com/watch?v=dQw4w9wgGcQ"
    assert "watch?v=" in dl.youtube_url("https://youtu.be/dQw4w9wgGcQ")
    assert dl.is_youtube_url("https://music.youtube.com/watch?v=dQw4w9wgGcQ")


def test_score_prefere_audio_officiel_a_un_cover_et_un_mix():
    query = "nightcall kavinsky"
    official = _track()
    cover = _track(
        id="covercover1",
        title="Nightcall (Kavinsky COVER)",
        uploader="RandomCovers",
        duration="4:20",
        views=800_000,
    )
    hour_mix = _track(
        id="mixmixmixmi",
        title="Kavinsky - Nightcall (1 hour mix)",
        uploader="MixChannel",
        duration="1:00:12",
        views=5_000_000,
    )
    live = _track(
        id="liveliveliv",
        title="Kavinsky - Nightcall Live at Coachella",
        uploader="FestivalHD",
        duration="5:02",
        views=2_000_000,
    )
    best = dl.pick_best_track(query, [cover, hour_mix, live, official])
    assert best is official


def test_un_cover_est_autorise_si_la_requete_le_demande():
    query = "nightcall kavinsky cover"
    official = _track()
    cover = _track(
        id="covercover1",
        title="Nightcall Kavinsky (cover)",
        uploader="PianoGuy",
        duration="4:10",
        views=50_000,
    )
    best = dl.pick_best_track(query, [official, cover])
    assert best is cover


def test_refuse_un_paquet_hors_sujet():
    query = "nightcall kavinsky"
    junk = [
        _track(title="Funny cats compilation", uploader="Memes", duration="12:00", views=9),
        _track(title="How to cook pasta", uploader="Chef", duration="8:00", views=12),
    ]
    assert dl.pick_best_track(query, junk) is None


def test_file_stem_nettoie_le_bruit_youtube():
    stem = dl.file_stem_for(_track())
    assert "Official" not in stem
    assert "Kavinsky" in stem
    assert "Nightcall" in stem


def test_file_stem_ignore_un_pseudo_youtube():
    stem = dl.file_stem_for(_track(
        title="Ninho - La vie qu'on mène (Official Audio)",
        uploader="Quentin-le_BG",
    ))
    assert "Quentin" not in stem
    assert "Ninho" in stem
    assert "vie" in stem.lower()


def test_parse_progress_extrait_les_champs_yt_dlp():
    parsed = dl.parse_progress_line(
        "[download]  45.2% of  8.40MiB at  2.10MiB/s ETA 00:08"
    )
    assert parsed["percent"] == 45.2
    assert parsed["speed"] == "2.10MiB/s"
    assert parsed["eta"] == "00:08"
    convert = dl.parse_progress_line("[ExtractAudio] Destination: /tmp/Nightcall.m4a")
    assert convert["status"] == "converting"
    assert convert["path"].endswith("Nightcall.m4a")


def test_commande_yt_dlp_extrait_le_meilleur_audio_m4a(tmp_path):
    cmd = dl.build_ydl_cmd(
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        tmp_path,
        "Kavinsky - Nightcall",
        has_ffmpeg=True,
    )
    assert cmd[0] == "yt-dlp"
    assert "-x" in cmd
    assert cmd[cmd.index("--audio-format") + 1] == "m4a"
    assert "--audio-quality" in cmd
    assert "--no-playlist" in cmd
    assert "--embed-thumbnail" in cmd
    joined = " ".join(cmd)
    assert "bestaudio" in joined
    assert str(tmp_path / "Kavinsky - Nightcall.%(ext)s") in cmd


def test_download_music_exige_un_titre():
    assert "titre" in dl.download_music({}).lower()


def test_download_music_choisit_lofficiel_et_met_a_jour_la_carte(monkeypatch, tmp_path):
    shown = []
    dest = tmp_path / "Musique"
    dest.mkdir()
    audio = dest / "Kavinsky - Nightcall.m4a"
    audio.write_bytes(b"fake")

    class _UI:
        def show_music_download(self, payload):
            shown.append(dict(payload))

    monkeypatch.setattr(dl.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dl, "music_dir", lambda: dest)
    monkeypatch.setattr(dl, "_already_have", lambda _query: None)
    monkeypatch.setattr(dl, "_search", lambda _query: [
        _track(title="Nightcall COVER by a fan", uploader="Fan", duration="4:00", views=10),
        _track(),
    ])

    def fake_run(cmd, rec, on_line, timeout=180):
        on_line("[download]  12.0% of  5.00MiB at  1.00MiB/s ETA 00:20")
        on_line("[download] 100% of  5.00MiB in 00:04")
        on_line(f"[ExtractAudio] Destination: {audio}")
        return 0, "ok"

    monkeypatch.setattr(dl, "_run_yt_dlp", fake_run)

    spoken: list[str] = []
    result = dl.download_music(
        {"query": "télécharge Nightcall Kavinsky"},
        player=_UI(),
        speak=spoken.append,
    )

    assert "Nightcall" in result
    assert any(word in result.lower() for word in ("patienter", "patientez", "préviens"))
    dl.wait_active(5)
    assert shown[-1]["status"] == "done"
    assert shown[-1]["percent"] == 100.0
    assert "COVER" not in shown[-1]["title"]
    assert spoken
    assert "Musique" in spoken[-1]


def test_download_music_ne_retélécharge_pas_un_fichier_deja_la(monkeypatch, tmp_path):
    shown = []

    class _UI:
        def show_music_download(self, payload):
            shown.append(dict(payload))

    local = {
        "title": "Nightcall",
        "artist": "Kavinsky",
        "path": str(tmp_path / "Nightcall.m4a"),
        "score": 0.96,
    }
    monkeypatch.setattr(dl.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dl, "music_dir", lambda: tmp_path)
    monkeypatch.setattr(dl, "_already_have", lambda _query: local)
    monkeypatch.setattr(
        dl, "_search",
        lambda _query: pytest.fail("ne doit pas chercher YouTube si le fichier est déjà là"),
    )

    result = dl.download_music({"query": "Nightcall"}, player=_UI())

    assert "déjà" in result.lower()
    assert shown[-1]["status"] == "exists"


def test_phrase_suit_le_mode_astro(monkeypatch):
    from core.personality_modes import PersonalityMode

    monkeypatch.setattr("core.personality_modes.active_mode", lambda: PersonalityMode.ASTRO)
    start = dl.phrase_for("start", "La vie qu'on mène")
    done = dl.phrase_for("done", "La vie qu'on mène")
    assert "patienter" in start.lower()
    assert "choppe" in start.lower() or "parti" in start.lower()
    assert "plié" in done.lower()
    assert "Musique" in done


def test_un_second_appel_pendant_le_transfert_ne_relance_pas(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()

    class _UI:
        def show_music_download(self, payload):
            return None

    monkeypatch.setattr(dl.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(dl, "music_dir", lambda: tmp_path)
    monkeypatch.setattr(dl, "_already_have", lambda _query: None)
    monkeypatch.setattr(dl, "_search", lambda _query: [_track()])

    def fake_run(cmd, rec, on_line, timeout=600):
        started.set()
        release.wait(2)
        on_line("[download] 100% of  1.00MiB")
        return 0, "ok"

    monkeypatch.setattr(dl, "_run_yt_dlp", fake_run)

    first = dl.download_music({"query": "Nightcall"}, player=_UI())
    assert started.wait(1)
    second = dl.download_music({"query": "Nightcall Kavinsky"}, player=_UI())
    assert "encore" in second.lower() or "cours" in second.lower()
    assert "patienter" in second.lower() or "patiente" in second.lower() or "préviens" in second.lower()
    release.set()
    dl.wait_active(5)
    assert "Nightcall" in first


def test_delai_outil_est_largement_au_dessus_de_la_recherche():
    from core.action_runtime import ActionRuntime

    policy = ActionRuntime([]).policy_for("download_music")
    assert policy.timeout_s >= 90.0


def test_declaration_outil_et_prompt_routent_telecharge():
    from core.tool_dispatcher import TOOL_DECLARATIONS
    from pathlib import Path as _P

    decl = next(item for item in TOOL_DECLARATIONS if item["name"] == "download_music")
    assert "query" in decl["parameters"]["properties"]
    prompt = (_P(__file__).resolve().parents[1] / "core" / "prompt.txt").read_text(
        encoding="utf-8"
    )
    assert "download_music" in prompt
    assert "télécharge [titre]" in prompt
