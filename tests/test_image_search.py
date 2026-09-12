"""Recherche visuelle native : pertinence, sécurité et affichage sans navigateur."""

from __future__ import annotations

from actions import image_search as search


def _candidate(title: str, url: str, *, source: str = "example", width=1200, height=800):
    return {
        "title": title,
        "image_url": url,
        "thumbnail_url": "",
        "source_url": "https://example.com/page",
        "source": source,
        "width": width,
        "height": height,
    }


def test_le_titre_directement_lie_a_la_demande_passe_en_premier():
    exact = _candidate("Chat roux assis dans un jardin", "https://img.example/chat-roux.jpg")
    vague = _candidate("Collection de fonds d'écran", "https://img.example/wallpaper.jpg")

    assert search._candidate_score(exact, "chat roux", 4) > search._candidate_score(
        vague, "chat roux", 0
    )


def test_les_adresses_locales_ne_peuvent_jamais_etre_telechargees():
    for url in (
        "http://127.0.0.1/photo.jpg",
        "http://localhost/photo.jpg",
        "http://[::1]/photo.jpg",
        "file:///etc/passwd",
        "ftp://example.com/photo.jpg",
    ):
        assert search._public_http_url(url) is False


def test_la_recherche_classe_dedoublonne_et_borne_les_resultats(monkeypatch):
    candidates = [
        _candidate("Fond abstrait", "https://img.example/abstrait.jpg"),
        _candidate("Chat noir portrait", "https://img.example/chat-noir.jpg"),
        _candidate("Chat noir portrait copie", "https://img.example/chat-noir-2.jpg"),
    ]
    monkeypatch.setattr(search, "_search_serpapi", lambda query, count: candidates)
    monkeypatch.setattr(search, "_search_ddg", lambda query, count: [])

    def download(item):
        result = dict(item)
        result.update({
            "bytes": b"image", "mime": "image/jpeg",
            "digest": "same" if "chat-noir" in item["image_url"] else "abstract",
        })
        return result

    monkeypatch.setattr(search, "_download_image", download)
    results = search.search_images("chat noir", limit=2)

    assert len(results) == 2
    assert results[0]["title"].startswith("Chat noir")
    assert len({item["digest"] for item in results}) == 2


def test_loutil_affiche_la_galerie_et_pas_un_navigateur(monkeypatch):
    images = [{"title": "Chat", "bytes": b"ok", "source": "Images"}]
    monkeypatch.setattr(search, "search_images", lambda query, limit: images)

    class UI:
        def __init__(self):
            self.gallery = None

        def show_image_gallery(self, query, values):
            self.gallery = (query, values)

    ui = UI()
    result = search.image_search({"query": "chat", "limit": 4}, player=ui)

    assert ui.gallery == ("chat", images)
    assert "1 image" in result

