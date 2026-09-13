#!/usr/bin/env python3
"""
visual_recognition.py — « C'est qui ? », « C'est quoi ça ? »

L'utilisateur montre quelqu'un ou quelque chose à la caméra (ou à l'écran) :

* **Une personne** : la mémoire des visages (`core/face_memory.py`, locale)
  dit si ANO-GPT la connaît. Connue ⇒ nom, lien, dernière rencontre. Inconnue
  ⇒ l'empreinte reste en attente, ANO-GPT demande « c'est qui ? », et la
  réponse (« Karim, mon frère ») l'inscrit pour toujours — sans reprendre de
  photo. « C'est moi » inscrit l'utilisateur lui-même.
* **Un objet** : Gemini l'identifie (nom, marque, modèle, texte lu), puis une
  recherche web en temps réel apporte ce qu'on ne peut pas voir (prix, à quoi
  ça sert, fiche). La mémoire personnelle est consultée pour relier l'objet à
  ce que l'utilisateur en a déjà dit (« c'est mon Arduino du projet X »).
* **Veille** : pendant que la caméra est ouverte, ANO-GPT annonce qui entre
  dans le champ.

Le résultat est un compte-rendu que le modèle Live lit tel quel ; il ne voit
jamais l'image lui-même.
"""
from __future__ import annotations

import time
from typing import Any
from collections.abc import Callable

from core import action_kit as kit
from core import face_memory as fm

CARD_TYPE = "info"
LAST_OBJECT_KEY = "last_identified_object"
LAST_FACES_KEY = "last_identified_faces"
_watcher: fm.FaceWatcher | None = None

_OBJECT_TIMEOUT_S = 25.0
_SEARCH_BUDGET_S = 10.0


# ════════════════════════════════════════════════════════════════════════════
# 1. Capture
# ════════════════════════════════════════════════════════════════════════════

def _grab(source: str, grab_frame: Callable[[], Any] | None) -> tuple[bytes, str]:
    """Une image de la source demandée. La caméra passe par le studio déjà ouvert."""
    if source == "screen":
        from core import screen_capture
        img, mime, _meta = screen_capture.capture_window_or_screen(
            target="screen", compress=True, max_dim=(1920, 1080), quality=88,
        )
        return img, mime
    if grab_frame is None:
        from actions.screen_processor import _capture_camera
        return _capture_camera()
    got = grab_frame()
    if isinstance(got, tuple):
        return got[0], got[1] if len(got) > 1 else "image/jpeg"
    return got, "image/jpeg"


def _grab_best(source: str, grab_frame, *, frames: int = 3, spacing: float = 0.25,
               detect_faces: bool = True) -> tuple[bytes, str, list[fm.DetectedFace]]:
    """Plusieurs images rapprochées ; on garde celle où le visage est le plus net.

    Une seule image attrape souvent un clignement ou un flou de mouvement ;
    trois images en moins d'une seconde, c'est presque toujours une bonne.
    """
    mem = fm.get_face_memory()
    best: tuple[bytes, str, list[fm.DetectedFace]] | None = None
    best_score = -1.0
    for i in range(max(1, frames)):
        img, mime = _grab(source, grab_frame)
        faces = mem.engine.detect(img) if detect_faces and fm.models_present() else []
        score = max((f.enroll_quality() for f in faces), default=0.0)
        if best is None or score > best_score:
            best, best_score = (img, mime, faces), score
        if not faces or source == "screen":
            break
        if i < frames - 1:
            time.sleep(spacing)
    return best  # type: ignore[return-value]


# ════════════════════════════════════════════════════════════════════════════
# 2. Objets : Gemini + recherche web + mémoire personnelle
# ════════════════════════════════════════════════════════════════════════════

_OBJECT_PROMPT = """Tu es l'œil d'ANO-GPT. L'utilisateur te montre quelque chose (objet, produit, plante, animal, lieu, texte, écran, logo…) et demande ce que c'est.
RÈGLES : fonde-toi sur les pixels. Lis tout texte, logo, référence, numéro de modèle visible : c'est ce qui permet d'identifier précisément. Si c'est un produit, donne marque + modèle si lisibles, sinon dis « probablement ». Si plusieurs objets, décris le principal (au centre / tenu en main) et liste les autres.
Réponds en JSON strict :
{
  "name": "nom court et précis de l'objet principal (ex : 'Arduino Uno R3', 'Monstera deliciosa', 'clé USB SanDisk Ultra 64 Go')",
  "category": "produit électronique | plante | animal | aliment | vêtement | document | véhicule | outil | lieu | autre",
  "brand": "marque si visible, sinon ''",
  "model": "modèle / référence si visible, sinon ''",
  "visible_text": "texte lu sur l'objet, exact, sinon ''",
  "description": "2 phrases : ce que c'est et à quoi ça sert, en français",
  "distinguishing": ["détail 1 qui l'identifie", "détail 2"],
  "others": ["autre objet visible 1", "autre 2"],
  "confidence": 0.0,
  "search_query": "meilleure requête web pour en savoir plus (marque modèle, ou nom précis), en français ou anglais selon ce qui donnera les meilleurs résultats",
  "spoken": "1 à 2 phrases prêtes à dire : c'est quoi, en tutoyant, sans formule d'introduction"
}
Rends UNIQUEMENT le JSON."""


def identify_object(image_bytes: bytes, mime: str, question: str = "") -> dict:
    """Ce que montre l'image, d'après Gemini. Dict vide si la vision est indisponible."""
    from core import multimodal_vision as mv

    api_key = mv._get_api_key()
    if not api_key:
        return {"error": "clé API Gemini absente"}
    try:
        from google import genai
        from google.genai import types as gtypes
        client = genai.Client(api_key=api_key)
        prompt = _OBJECT_PROMPT
        if question:
            prompt += f"\n\nQuestion de l'utilisateur : « {question[:200]} »"
        contents = [gtypes.Part.from_bytes(data=image_bytes, mime_type=mime), prompt]
        resp, model = mv._call_gemini_vision(client, gtypes, contents, mv.vision_model_cascade())
        data = mv._parse_vision_json(resp.text)
        data["model_used"] = model
        return data
    except Exception as exc:
        return {"error": str(exc)[:200]}


def _search_object(query: str, budget_s: float = _SEARCH_BUDGET_S) -> str:
    """Recherche web en temps réel sur l'objet identifié. Vide si rien."""
    if not query:
        return ""
    try:
        from actions.web_search import web_search
        text = web_search({"query": query, "mode": "research", "_budget_s": budget_s, "count": 4})
        text = (text or "").strip()
        if not text or text.lower().startswith(("veuillez", "aucun résultat", "erreur")):
            return ""
        return text[:1800]
    except Exception as exc:
        print(f"[Reconnaissance] recherche web impossible : {exc}")
        return ""


def _personal_notes(*queries: str) -> str:
    """Ce que l'utilisateur a déjà dit à ANO-GPT sur cet objet."""
    try:
        from core import memory_store
        seen: set[int] = set()
        lines: list[str] = []
        for q in queries:
            if not q:
                continue
            for row in memory_store.search(q, limit=3):
                rid = int(row.get("id") or hash(row.get("value", "")))
                if rid in seen:
                    continue
                seen.add(rid)
                lines.append(f"- {row.get('value', '')}")
        return "\n".join(lines[:4])
    except Exception:
        return ""


def _object_report(data: dict, search: str, notes: str) -> str:
    name = str(data.get("name") or "objet non identifié")
    brand = str(data.get("brand") or "")
    model = str(data.get("model") or "")
    conf = data.get("confidence")
    try:
        conf_txt = f"{float(conf):.0%}" if conf is not None else "?"
    except (TypeError, ValueError):
        conf_txt = "?"
    lines = [f"[OBJET IDENTIFIÉ — {data.get('model_used', 'Gemini')}] {name} (certitude {conf_txt})"]
    if brand or model:
        lines.append(f"Marque / modèle : {brand} {model}".strip())
    if data.get("category"):
        lines.append(f"Catégorie : {data['category']}")
    if data.get("visible_text"):
        lines.append(f"Texte lu sur l'objet : {str(data['visible_text'])[:300]}")
    if data.get("description"):
        lines.append(f"Description : {data['description']}")
    dist = data.get("distinguishing") or []
    if isinstance(dist, list) and dist:
        lines.append("Détails identifiants : " + " ; ".join(str(d) for d in dist[:4]))
    others = data.get("others") or []
    if isinstance(others, list) and others:
        lines.append("Autres éléments visibles : " + ", ".join(str(o) for o in others[:5]))
    if notes:
        lines.append("\nCe que l'utilisateur t'a déjà dit à ce sujet (mémoire personnelle) :\n" + notes)
    if search:
        lines.append("\nRecherche web en temps réel :\n" + search)
    lines.append(
        "\nRéponds à partir de ce compte-rendu : dis ce que c'est, le détail qui te permet de "
        "l'affirmer, puis l'info utile trouvée en ligne (prix, usage, particularité) en une phrase. "
        "Si l'utilisateur veut que tu retiennes cet objet (« c'est mon… », « retiens que… »), "
        "appelle visual_recognition action=remember_object."
    )
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# 3. Cartes HUD
# ════════════════════════════════════════════════════════════════════════════

def _show_faces_card(player: Any, matches: list[fm.Match]) -> None:
    show = getattr(player, "show_card", None)
    if not callable(show):
        return
    try:
        parts: list[str] = []
        for m in matches:
            thumb = fm.thumb_markdown(m.face.thumb_jpeg)
            if m.status == "known" and m.person is not None:
                p = m.person
                seen = f" · vu {p.seen_count}×" if p.seen_count > 1 else ""
                parts.append(f"{thumb}\n\n**{p.label()}**{seen}  \n_certitude {m.similarity:.0%}_")
            elif m.status == "maybe" and m.person is not None:
                parts.append(f"{thumb}\n\n**{m.person.name} ?**  \n_ressemblance {m.similarity:.0%} — à confirmer_")
            else:
                parts.append(f"{thumb}\n\n**Inconnu** ({m.pending_id})  \n_dis-moi qui c'est_")
        show(CARD_TYPE, "👤 Reconnaissance", "\n\n---\n\n".join(parts))
    except Exception:
        pass


def _show_object_card(player: Any, data: dict, image_bytes: bytes) -> None:
    show = getattr(player, "show_card", None)
    if not callable(show):
        return
    body: list[str] = []
    try:
        import base64
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        im.thumbnail((360, 240))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=72)
        body.append(f"![objet](data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('ascii')})")
    except Exception:
        pass  # la carte vaut aussi sans vignette
    try:
        body.append(f"**{data.get('name', 'Objet')}**")
        bm = " ".join(x for x in (data.get("brand"), data.get("model")) if x)
        if bm:
            body.append(f"_{bm}_")
        if data.get("description"):
            body.append(str(data["description"]))
        show(CARD_TYPE, "🔎 Objet identifié", "\n\n".join(body))
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
# 4. Actions
# ════════════════════════════════════════════════════════════════════════════

def _pick_pending(mem: fm.FaceMemory, pending_id: str, which: str) -> fm.PendingFace | None:
    pending = mem.pending()
    if pending_id:
        for p in pending:
            if p.id.casefold() == pending_id.strip().casefold():
                return p
    if not pending:
        return None
    if len(pending) == 1 or not which:
        return pending[0]
    # « celui de gauche / droite » — les dossiers sont triés du plus récent au
    # plus ancien, pas par position ; on ne devine pas, on prend le plus récent.
    return pending[0]


def _identify(p: dict, player, session_memory, grab_frame, progress) -> str:
    source = "screen" if str(p.get("source") or "camera").lower().startswith(("screen", "ecran", "écran")) else "camera"
    question = str(p.get("question") or p.get("text") or "").strip()
    want = str(p.get("expect") or "auto").lower()
    mem = fm.get_face_memory()

    if want != "object" and not fm.models_present():
        progress("installation des modèles de visage (37 Mo, une seule fois)")
        fm.ensure_models(progress)

    try:
        img, mime, faces = _grab_best(source, grab_frame, detect_faces=want != "object")
    except Exception as exc:
        return f"Je n'ai pas pu capturer {'l’écran' if source == 'screen' else 'la caméra'} : {exc}"

    if want != "object" and faces:
        matches = mem.identify(img, source=source, faces=faces)
        if matches:
            if session_memory is not None:
                session_memory[LAST_FACES_KEY] = [
                    {"status": m.status, "name": m.person.name if m.person else "", "pending_id": m.pending_id}
                    for m in matches
                ]
            _show_faces_card(player, matches)
            block = "[RECONNAISSANCE DE VISAGES — mémoire locale]\n" + mem.describe(matches)
            if question and want == "auto" and any(w in question.casefold() for w in ("quoi", "objet", "porte", "tient", "fait")):
                # La question porte aussi sur la scène : on ajoute l'analyse Gemini.
                data = identify_object(img, mime, question)
                if data and not data.get("error"):
                    block += "\n\n" + _object_report(data, "", "")
            return block
    if want == "person":
        return ("Aucun visage net dans l'image : demande à l'utilisateur de se rapprocher ou de mieux "
                "éclairer la personne, puis rappelle visual_recognition.")

    # Objet / scène
    progress("identification de l'objet")
    data = identify_object(img, mime, question)
    if not data or data.get("error"):
        return f"Je n'ai pas pu identifier ce que tu me montres : {data.get('error', 'vision indisponible')}."
    name = str(data.get("name") or "")
    query = str(data.get("search_query") or " ".join(x for x in (data.get("brand"), data.get("model")) if x) or name)
    progress(f"recherche en ligne : {query}")
    search = _search_object(query) if not p.get("no_search") else ""
    notes = _personal_notes(name, " ".join(x for x in (data.get("brand"), data.get("model")) if x))
    if session_memory is not None:
        session_memory[LAST_OBJECT_KEY] = {
            "name": name, "brand": data.get("brand", ""), "model": data.get("model", ""),
            "description": data.get("description", ""), "visible_text": data.get("visible_text", ""),
            "category": data.get("category", ""), "at": time.strftime("%Y-%m-%d %H:%M"),
        }
    _show_object_card(player, data, img)
    return _object_report(data, search, notes)


def _remember_person(p: dict, player, session_memory, grab_frame, progress) -> str:
    name = str(p.get("name") or "").strip()
    if not name:
        return "Il me faut un prénom ou un nom pour retenir ce visage."
    relation = str(p.get("relation") or "").strip()
    notes = str(p.get("notes") or "").strip()
    mem = fm.get_face_memory()
    if not fm.models_present():
        progress("installation des modèles de visage")
        if not fm.ensure_models(progress):
            return "Les modèles de reconnaissance de visage n'ont pas pu être installés (réseau ?)."

    pend = _pick_pending(mem, str(p.get("pending_id") or ""), str(p.get("which") or ""))
    faces: list[fm.DetectedFace] = []
    if pend is None or bool(p.get("capture_now")):
        # Rien en attente (ou demande explicite) : on capture maintenant,
        # plusieurs images pour couvrir plusieurs angles.
        try:
            for _ in range(2):
                _img, _mime, got = _grab_best("camera", grab_frame, frames=3, spacing=0.3)
                faces.extend(got[:1])
                time.sleep(0.4)
        except Exception as exc:
            if pend is None:
                return f"Je ne vois personne pour l'instant : la capture a échoué ({exc})."
        if pend is None and not faces:
            return ("Je ne vois aucun visage net devant la caméra. Demande à la personne de se placer "
                    "face à la caméra, bien éclairée, puis rappelle remember_person.")
    try:
        person, added, new = mem.enroll(
            name, relation=relation, notes=notes, faces=faces,
            pending_id=pend.id if pend else "", is_owner=bool(p.get("is_owner")),
        )
    except ValueError as exc:
        return f"Je n'ai pas pu retenir ce visage : {exc}."
    if session_memory is not None:
        session_memory[LAST_FACES_KEY] = [{"status": "known", "name": person.name, "pending_id": ""}]
    show = getattr(player, "show_card", None)
    if callable(show):
        try:
            thumb = fm.thumb_markdown(mem.store.thumb(person.id))
            show(CARD_TYPE, "👤 Visage mémorisé", f"{thumb}\n\n**{person.label()}**  \n_{person.vectors} empreinte(s)_")
        except Exception:
            pass
    if person.is_owner:
        return f"C'est noté : ce visage, c'est toi{', ' + person.name if person.name and person.name != 'Toi' else ''}. Je te reconnaîtrai désormais."
    if new:
        return (f"C'est retenu : {person.label()} — {added} empreinte(s) enregistrée(s). "
                f"Je reconnaîtrai {person.name} la prochaine fois.")
    return (f"{person.name} était déjà dans ma mémoire : j'ai ajouté {added} empreinte(s), "
            f"je le reconnaîtrai encore mieux ({person.vectors} au total).")


def _remember_object(p: dict, session_memory) -> str:
    name = str(p.get("name") or "").strip()
    notes = str(p.get("notes") or "").strip()
    last = dict((session_memory or {}).get(LAST_OBJECT_KEY) or {}) if session_memory is not None else {}
    if not name and not last:
        return "Montre-moi d'abord l'objet (action=identify), puis dis-moi comment tu l'appelles."
    label = name or str(last.get("name") or "objet")
    bits = [f"Objet de l'utilisateur : {label}."]
    if last:
        ident = " ".join(x for x in (last.get("brand"), last.get("model")) if x) or last.get("name", "")
        if ident and ident != label:
            bits.append(f"Identifié comme : {ident}.")
        if last.get("visible_text"):
            bits.append(f"Texte visible : {str(last['visible_text'])[:80]}.")
    if notes:
        bits.append(notes)
    try:
        from core import memory_store
        memory_store.save(" ".join(bits), kind=memory_store.KIND_FACT,
                          key=f"objet_{fm.slug(label)}", category="notes")
    except Exception as exc:
        return f"Je n'ai pas pu l'enregistrer : {exc}"
    return f"C'est noté : « {label} »{' — ' + notes if notes else ''}. Je m'en souviendrai quand tu me le remontreras."


def _forget(p: dict) -> str:
    name = str(p.get("name") or "").strip()
    if not name:
        return "Quel visage veux-tu que j'oublie ?"
    person = fm.get_face_memory().forget(name)
    if person is None:
        return f"Je ne connais aucun visage nommé {name}."
    return f"J'ai oublié le visage de {person.name}."


def _update(p: dict) -> str:
    mem = fm.get_face_memory()
    name = str(p.get("name") or "").strip()
    person = mem.store.find(name) if name else None
    if person is None:
        return f"Je ne connais aucun visage nommé {name or '?'}."
    updated = mem.store.update_person(
        person.id, name=str(p.get("new_name") or "").strip(),
        relation=str(p.get("relation") or "").strip(), notes=str(p.get("notes") or "").strip(),
        alias=str(p.get("alias") or "").strip(),
    )
    if updated is None:
        return "Mise à jour impossible."
    mem._write_long_term(updated, first=False)
    return f"Fiche mise à jour : {updated.label()}{' — ' + updated.notes if updated.notes else ''}."


def _list() -> str:
    people = fm.get_face_memory().store.list_people()
    if not people:
        return "Je ne connais encore aucun visage. Montre-moi quelqu'un et dis-moi qui c'est."
    rows = []
    for p in people[:30]:
        seen = f", vu {p.seen_count}×" if p.seen_count else ""
        last = fm.FaceMemory._ago(p.last_seen)
        rows.append(f"{p.label()}{seen}{' (' + last + ')' if last else ''}")
    return f"Je connais {len(people)} visage(s) : " + " ; ".join(rows) + "."


def _watch(p: dict, player, grab_frame, speak: Callable[[str], None] | None) -> str:
    global _watcher
    action = str(p.get("action") or "watch").lower()
    if action in ("stop_watch", "unwatch"):
        if _watcher is None or not _watcher.running:
            return "La veille des visages n'était pas active."
        _watcher.stop()
        _watcher = None
        return "Veille des visages arrêtée."
    if _watcher is not None and _watcher.running:
        return "La veille des visages tourne déjà."
    if grab_frame is None:
        return "La veille a besoin du flux caméra d'ANO-GPT."
    if not fm.ensure_models():
        return "Les modèles de visage ne sont pas installés."
    mem = fm.get_face_memory()

    def _frame() -> bytes | None:
        try:
            got = grab_frame()
            return got[0] if isinstance(got, tuple) else got
        except Exception:
            return None

    def _on_event(kind: str, matches: list[fm.Match]) -> None:
        _show_faces_card(player, matches)
        phrases = []
        for m in matches:
            if m.status == "known" and m.person is not None:
                phrases.append(f"{m.person.name} vient d'apparaître à la caméra." if not m.person.is_owner
                               else "Te revoilà devant la caméra.")
            elif m.status == "maybe" and m.person is not None:
                phrases.append(f"Quelqu'un qui ressemble à {m.person.name} est devant la caméra.")
            else:
                phrases.append("Il y a quelqu'un que je ne connais pas devant la caméra. Tu me dis qui c'est ?")
        text = " ".join(phrases)
        log = getattr(player, "write_log", None)
        if callable(log):
            try:
                log(f"SYS : veille visages — {text}")
            except Exception:
                pass
        if callable(speak) and text:
            try:
                speak(text)
            except Exception as exc:
                print(f"[Reconnaissance] annonce impossible : {exc}")

    _watcher = fm.FaceWatcher(mem, _frame, _on_event)
    _watcher.start()
    return "Veille des visages activée : je te dirai qui apparaît à la caméra."


def watcher_running() -> bool:
    return _watcher is not None and _watcher.running


def stop_watcher() -> None:
    global _watcher
    if _watcher is not None:
        _watcher.stop()
        _watcher = None


# ════════════════════════════════════════════════════════════════════════════
# 5. Outil
# ════════════════════════════════════════════════════════════════════════════

@kit.action("visual_recognition")
def visual_recognition(parameters: dict | None = None, player: Any = None,
                       session_memory: Any = None, speak: Callable[[str], None] | None = None,
                       grab_frame: Callable[[], Any] | None = None, **_kw) -> str:
    p = parameters or {}
    action = str(p.get("action") or "identify").strip().lower()

    def _progress(msg: str) -> None:
        try:
            log = getattr(player, "write_log", None)
            if callable(log):
                log(f"SYS : reconnaissance visuelle — {msg}.")
        except Exception:
            pass

    if action in ("identify", "who", "what", "look", "regarde"):
        return _identify(p, player, session_memory, grab_frame, _progress)
    if action in ("remember_person", "remember", "enroll", "learn_face"):
        return _remember_person(p, player, session_memory, grab_frame, _progress)
    if action in ("remember_object", "note_object"):
        return _remember_object(p, session_memory)
    if action in ("forget_person", "forget"):
        return _forget(p)
    if action in ("update_person", "rename", "update"):
        return _update(p)
    if action in ("list_people", "list", "who_do_you_know"):
        return _list()
    if action in ("watch", "stop_watch", "unwatch"):
        return _watch(p, player, grab_frame, speak)
    if action == "status":
        st = fm.get_face_memory().status()
        return (f"Modèles : {'prêts' if st['models'] else 'absents'} ; {st['people']} visage(s) connu(s) ; "
                f"{st['pending']} en attente d'un nom ; veille {'active' if watcher_running() else 'inactive'}.")
    return f"Action inconnue : {action}."
