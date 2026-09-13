import json

from actions import tiktok_tracker as tt


def _snap(followers=41, likes=217, items=None, ts=1000.0):
    return {
        "handle": "anogpt", "nickname": "Ano-GPT", "verified": False,
        "followers": followers, "following": 3, "likes": likes, "videos": 15,
        "items": items if items is not None else [
            {"id": "1", "desc": "vidéo A", "created": 10, "plays": 100, "likes": 12, "comments": 1, "shares": 0},
            {"id": "2", "desc": "vidéo B", "created": 5, "plays": 50, "likes": 4, "comments": 0, "shares": 0},
        ],
        "items_available": True, "ts": ts,
    }


def test_normalize_handle_accepts_at_and_urls():
    assert tt.normalize_handle("@AnoGPT") == "anogpt"
    assert tt.normalize_handle("https://www.tiktok.com/@anogpt?lang=fr") == "anogpt"
    assert tt.normalize_handle("https://www.tiktok.com/@anogpt/video/123") == "anogpt"
    assert tt.normalize_handle(" ano gpt ") == "anogpt"


def test_parse_profile_json_reads_counts_and_detects_missing_account():
    raw = json.dumps({"__DEFAULT_SCOPE__": {"webapp.user-detail": {
        "statusCode": 0,
        "userInfo": {"user": {"uniqueId": "anogpt", "nickname": "Ano-GPT"},
                     "stats": {"followerCount": 41, "followingCount": 3, "heartCount": 217, "videoCount": 15}},
    }}})
    acc = tt.parse_profile_json(raw)
    assert (acc["followers"], acc["likes"], acc["videos"], acc["nickname"]) == (41, 217, 15, "Ano-GPT")

    missing = json.dumps({"__DEFAULT_SCOPE__": {"webapp.user-detail": {"statusCode": 10202, "statusMsg": "user not exist"}}})
    try:
        tt.parse_profile_json(missing)
    except LookupError:
        pass
    else:
        raise AssertionError("un compte inexistant doit lever LookupError")


def test_parse_item_list_sorts_newest_first():
    payload = {"itemList": [
        {"id": "old", "desc": "x", "createTime": 1, "stats": {"playCount": 1}},
        {"id": "new", "desc": "y", "createTime": 9, "stats": {"playCount": 2, "diggCount": 3}},
    ]}
    items = tt.parse_item_list(payload)
    assert [v["id"] for v in items] == ["new", "old"]
    assert items[0]["likes"] == 3


def test_notable_events_new_follower_milestone_and_surge():
    prev = _snap(followers=41)
    cur = _snap(followers=42, ts=1100.0)
    events = tt.notable_events(prev, cur)
    assert any("Nouvel abonné" in m and "42" in m for _, m, _ in events)

    cur = _snap(followers=50, ts=1100.0)
    events = tt.notable_events(prev, cur)
    assert any(k == "tiktok:milestone:50" for k, _, _ in events)
    assert not any("nouveaux abonnés" in m for _, m, _ in events)  # le palier remplace le simple gain

    items = [dict(v) for v in prev["items"]]
    items[0]["plays"] = 260   # +160 vues d'un coup
    cur = _snap(followers=41, items=items, ts=1100.0)
    events = tt.notable_events(prev, cur)
    assert any("décolle" in m and "+160" in m for _, m, _ in events)

    items = [dict(v) for v in prev["items"]] + [
        {"id": "3", "desc": "toute neuve", "created": 99, "plays": 7, "likes": 0, "comments": 0, "shares": 0}]
    cur = _snap(items=items, ts=1100.0)
    events = tt.notable_events(prev, cur)
    assert any("nouvelle vidéo" in m and "toute neuve" in m for _, m, _ in events)


def test_no_events_without_previous_snapshot_or_change():
    assert tt.notable_events(None, _snap()) == []
    assert tt.notable_events(_snap(), _snap(ts=2000.0)) == []


def test_day_baseline_rolls_over_at_midnight():
    state = tt._default_state()
    first = _snap(followers=40, ts=1_700_000_000.0)          # un jour
    tt.record_snapshot(state, first)
    assert state["day_baseline"]["followers"] == 40
    same_day = _snap(followers=45, ts=1_700_000_000.0 + 3600)
    tt.record_snapshot(state, same_day)
    assert state["day_baseline"]["followers"] == 40
    next_day = _snap(followers=50, ts=1_700_000_000.0 + 86400 * 2)
    tt.record_snapshot(state, next_day)
    assert state["day_baseline"]["followers"] == 50


def test_card_and_speech_mention_deltas():
    prev = _snap(followers=41, likes=217)
    cur = _snap(followers=43, likes=230, ts=1100.0)
    card = tt.format_card(cur, prev, prev)
    assert "**43 abonnés**" in card and "+2" in card and "+13" in card
    spoken = tt.format_spoken(cur, prev, prev)
    assert "43 abonnés (+2 aujourd'hui)" in spoken and "230 j'aime" in spoken


def test_tool_status_uses_fresh_snapshot_without_opening_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    state = tt._default_state()
    state["handle"] = "anogpt"
    state["snapshots"] = [_snap(ts=tt.time.time())]
    tt.save_state(state)
    monkeypatch.setattr(tt, "fetch_snapshot", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Chrome ouvert")))
    text = tt.tiktok_tracker({"action": "status"})
    assert "41 abonnés" in text and "suis mon TikTok" in text


def test_tool_start_polls_and_enables(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(tt, "fetch_snapshot", lambda handle, **k: _snap(ts=tt.time.time()))
    shown = []

    class Player:
        def show_card(self, kind, title, body):
            shown.append((kind, title))

    text = tt.tiktok_tracker({"action": "start", "handle": "@anogpt"}, player=Player())
    assert "activé" in text and "41 abonnés" in text
    assert shown == [("tiktok", "TikTok @anogpt")]
    assert tt.load_state()["enabled"] is True
    assert "arrêté" in tt.tiktok_tracker({"action": "stop"})
    assert tt.load_state()["enabled"] is False


def test_tool_is_declared_to_the_model():
    from core.tool_dispatcher import TOOL_DECLARATIONS
    names = {d["name"] for d in TOOL_DECLARATIONS}
    assert "tiktok_tracker" in names
