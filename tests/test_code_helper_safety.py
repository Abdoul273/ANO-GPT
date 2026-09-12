from types import SimpleNamespace

from actions import code_helper


def _diagnostic(code_diff: str, path):
    return SimpleNamespace(
        code_diff=code_diff,
        parsed_error=SimpleNamespace(file_path=str(path)),
    )


def test_screen_debug_est_lecture_seule_par_defaut(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_text("print('original')\n", encoding="utf-8")
    monkeypatch.setattr(
        "core.auto_debug.auto_debug_live",
        lambda **kwargs: ("Diagnostic", _diagnostic("print('remplace')\n" * 4, target)),
    )
    result = code_helper._screen_debug_action("debug", str(target), None)
    assert result == "Diagnostic"
    assert target.read_text(encoding="utf-8") == "print('original')\n"


def test_screen_debug_refuse_un_diff_comme_fichier_complet(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_text("print('original')\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-print('a')\n+print('b')"
    monkeypatch.setattr(
        "core.auto_debug.auto_debug_live",
        lambda **kwargs: ("Diagnostic", _diagnostic(diff, target)),
    )
    result = code_helper._screen_debug_action(
        "debug", str(target), None, apply_fix=True
    )
    assert "non appliqué" in result
    assert target.read_text(encoding="utf-8") == "print('original')\n"


def test_screen_debug_valide_python_et_cree_une_sauvegarde(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_text("print('original')\n", encoding="utf-8")
    replacement = "def main():\n    print('corrigé')\n\nmain()\n"
    monkeypatch.setattr(
        "core.auto_debug.auto_debug_live",
        lambda **kwargs: ("Diagnostic", _diagnostic(replacement, target)),
    )
    result = code_helper._screen_debug_action(
        "debug", str(target), None, apply_fix=True
    )
    assert "Correctif appliqué" in result
    assert target.read_text(encoding="utf-8") == replacement.strip()
    assert target.with_suffix(".py.bak").read_text(encoding="utf-8") == "print('original')\n"
