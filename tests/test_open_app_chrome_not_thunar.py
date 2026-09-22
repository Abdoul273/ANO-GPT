"""« Ouvre Chrome » doit ouvrir Chrome, pas le gestionnaire de fichiers."""
from types import SimpleNamespace

import actions.desktop_apps as da
import actions.open_app as oa


def _entry(id_, name, binary, keywords=(), generic=""):
    return SimpleNamespace(id=id_, name=name, generic_name=generic, binary=binary,
                           wm_class=name, keywords=list(keywords), exec_argv=[binary],
                           terminal=False)


def _index(monkeypatch):
    apps = [
        _entry("thunar", "Thunar File Manager", "thunar",
               keywords=["file manager", "browser", "home", "trash"], generic="File Manager"),
        _entry("thunar-bulk-rename", "Bulk Rename", "thunar"),
        _entry("google-chrome", "Google Chrome", "google-chrome-stable",
               keywords=["browser", "web"], generic="Web Browser"),
        _entry("jetbrains-pycharm", "PyCharm", "pycharm"),
    ]
    monkeypatch.setattr(da, "all_apps", lambda force=False: apps)


def test_chrome_ne_matche_plus_le_mot_cle_home_de_thunar(monkeypatch):
    _index(monkeypatch)
    assert da.find_app("Chrome").id == "google-chrome"
    assert da.find_app("chrome").id == "google-chrome"
    # Les mots-clés restent utiles en correspondance exacte.
    assert da.find_app("home").id == "thunar"
    # Le nom exact prime sur une variante qui partage le binaire.
    assert da.find_app("thunar").id == "thunar"
    # La tolérance aux fautes de transcription est conservée.
    assert da.find_app("pie charm").id == "jetbrains-pycharm"


def test_open_app_essaie_l_alias_binaire_avant_le_nom_brut(monkeypatch):
    launched = []

    def fake_launcher(candidate, instance_name=None):
        launched.append(candidate)
        return True

    monkeypatch.setattr(oa, "_SYSTEM", "Linux")
    monkeypatch.setattr(oa, "_OS_LAUNCHERS", {"Linux": fake_launcher})
    monkeypatch.setattr(oa, "_normalize", lambda raw: "google-chrome")
    monkeypatch.setattr(oa.kit, "which", lambda name: "/usr/bin/google-chrome" if "chrome" in name else None)
    monkeypatch.setattr(oa, "_is_process_running", lambda name: False)
    monkeypatch.setattr(oa, "_hyprctl_json", lambda *_a, **_k: [])
    monkeypatch.setattr(oa, "_HAS_TRACKER", False)

    result = oa.open_app({"app_name": "Chrome"})

    assert launched[0] == "google-chrome"
    assert "Chrome" in result
