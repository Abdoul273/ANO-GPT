import time

from actions import tiktok_coach as tc


def _v(i, plays, likes=0, comments=0, shares=0, saves=0, hours_ago=48, duration=20, tags=None, desc=""):
    return {
        "id": str(i), "desc": desc or f"vidéo {i}", "created": int(time.time() - hours_ago * 3600),
        "plays": plays, "likes": likes, "comments": comments, "shares": shares, "saves": saves,
        "duration": duration, "hashtags": tags if tags is not None else ["ia", "devlog"],
        "music": "", "original_sound": True,
    }


def _items():
    return [tc.enrich(v) for v in (
        _v(1, 404, likes=14, hours_ago=30, duration=84, desc="Nouvelle fonctionnalité génération d'image"),
        _v(2, 100, likes=12, hours_ago=60, desc="Moi j'étais déjà un fruit"),
        _v(3, 54, likes=2, hours_ago=90, tags=["fyp", "viral"], desc="🫠"),
    )]


def test_pick_video_by_words_last_best_and_worst():
    items = _items()
    assert tc.pick_video(items, "pourquoi ma vidéo sur la génération d'image plafonne")["id"] == "1"
    assert tc.pick_video(items, "la dernière")["id"] == "1"
    assert tc.pick_video(items, "l'avant-dernière")["id"] == "2"
    assert tc.pick_video(items, "celle qui a le moins marché")["id"] == "3"
    assert tc.pick_video(items, "la plus vue")["id"] == "1"
    assert tc.pick_video(items, "https://www.tiktok.com/@anogpt/video/2")["id"] == "2"
    assert tc.pick_video([], "x") is None


def test_heuristics_explain_low_views_and_generic_hashtags():
    items = _items()
    summary = tc.account_summary(items)
    worst = items[2]
    findings = " ".join(tc.heuristic_findings(worst, summary))
    assert "premier lot de test" in findings
    assert "génériques" in findings
    assert "Zéro commentaire" in findings
    best = items[0]
    findings = " ".join(tc.heuristic_findings(best, summary))
    assert "84 s" in findings and "mieux que ta médiane" in findings


def test_account_summary_cadence_and_best_hours():
    summary = tc.account_summary(_items())
    assert summary["count"] == 3 and summary["max_plays"] == 404 and summary["min_plays"] == 54
    assert 0 < summary["posts_per_week"] < 20
    assert summary["best"]["id"] == "1"
    assert len(summary["best_hours"]) <= 3


def test_file_findings_flag_horizontal_and_silent():
    findings = " ".join(tc.file_findings({"width": 1280, "height": 720, "duration": 4.0, "audio": False, "size_mb": 3}))
    assert "vertical 9:16" in findings and "Pas de piste audio" in findings and "très court" in findings
    assert tc.file_findings({"width": 1080, "height": 1920, "duration": 22.0, "audio": True, "size_mb": 30}) == []


def test_find_video_file_prefers_name_match_then_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(tc.Path, "home", staticmethod(lambda: tmp_path))
    vids = tmp_path / "Vidéos"
    vids.mkdir()
    old = vids / "chat_roux.mp4"
    old.write_bytes(b"0")
    time.sleep(0.01)
    new = vids / "demo_anogpt.mp4"
    new.write_bytes(b"0")
    assert tc.find_video_file("") == new
    assert tc.find_video_file("chat roux") == old
    assert tc.find_video_file(str(old)) == old
    assert tc.find_video_file("inexistant") == new


def test_diagnose_returns_first_verdict_and_announces_in_background(monkeypatch):
    items = _items()
    cur = {"handle": "anogpt", "nickname": "Ano-GPT", "ts": time.time(), "items": items}
    monkeypatch.setattr(tc, "dataset", lambda state=None: (cur, items))
    monkeypatch.setattr(tc, "analyze_posted_video", lambda v, s, h: {
        "spoken": "Verdict : accroche molle.", "why": ["a"], "improvements": ["b"],
        "hook_score": 3, "retention_score": 2,
    })
    done = []
    monkeypatch.setattr(tc, "_run_background", lambda key, work: (work(), done.append(key)) and True)
    said, cards = [], []

    class P:
        def show_card(self, k, t, b):
            cards.append((t, b))

    text = tc.diagnose("la dernière", P(), lambda x: said.append(x))
    assert "404 vues" in text and "verdict complet" in text
    assert done == ["diag:1"]
    assert said and "accroche molle" in said[0] and "[COACH TIKTOK]" in said[0]
    assert "Accroche **3/10**" in cards[-1][1]


def test_tools_are_declared_to_the_model():
    from core.tool_dispatcher import TOOL_DECLARATIONS
    names = {d["name"] for d in TOOL_DECLARATIONS}
    assert {"tiktok_tracker", "tiktok_coach"} <= names
