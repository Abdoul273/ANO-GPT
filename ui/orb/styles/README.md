# Styles d'orbe — guide pour écrire un nouveau style

Comme les fonds de `background/`, l'orbe se choisit dans **Personnaliser →
Style de l'orbe**. Un seul orbe existe à la fois : l'ancien est détruit au
changement, les styles non choisis ne sont jamais importés.

## Pièces du système

| Fichier | Rôle |
|---|---|
| `ui/orb/contract.py` | Contrat que l'hôte appelle, `OrbSnapshot`, états visuels |
| `ui/orb/registry.py` | Catalogue `OrbSpec`, import paresseux, choix au démarrage |
| `ui/orb/host.py` | `OrbHost` = `window.hud`, un seul orbe vivant, rejoue l'état, repli |
| `ui/orb/base.py` | `BaseOrb` : tout le commun (états, volume, palette, cadence, sommeil) |
| `ui/orb/styles/pulse.py` | Style PULSE : membrane sonore et échos concentriques |
| `ui/orb/styles/mark_core.py` | MARK CORE : réacteur circulaire de Mark-LIV |

Config : clé `orb_style` de la configuration (`arc` par défaut).

## Écrire un style

1. Créer `ui/orb/styles/<id>.py` avec une classe qui hérite de `BaseOrb`.
2. Implémenter uniquement :
   - `advance(self, dt, t)` — simulation (dt en secondes) ; pas de dessin ;
   - `paint_orb(self, p, cx, cy, radius, t)` — dessin centré, `radius` =
     22 % du petit côté (`RADIUS_RATIO` pour changer).
3. Dans `ui/orb/registry.py`, passer `ready=True` (ou ajouter un `register`).

Ce que `BaseOrb` fournit :

- `self.visual_state` : `idle | listening | thinking | speaking | acting | error`
- `self.volume` (0–1, lissé), `self.energy` (≈0,15 repos → 1,2 action)
- `self.bands` : 8 bandes FFT lissées (réelles ou synthétisées)
- `self.palette_live` : `core`, `halo`, `wire`, `hot` (QColor interpolées,
  suivent l'accent de l'interface) ; `self.spin`, `self.pulse_speed`
- `with_alpha`, `mix`, `ease` dans `ui.orb.base`
- `on_palette_changed()` à surcharger pour reconstruire un cache de sprites
- badges communs (geste, vision continue) déjà dessinés par-dessus

## Règles de performance (machine : 2 cœurs, voix sur le même GIL)

- **Budget : < 6 ms par image** (simulation + peinture). La cadence s'adapte
  au coût mesuré, mais un style lent = une voix qui hache.
- Rien d'alloué dans la boucle : tableaux et sprites créés dans `__init__`,
  `resizeEvent` ou `on_palette_changed`.
- Pas de `QRadialGradient` par particule : un sprite `QPixmap` pré-rendu +
  `drawPixmap`. Grouper les traits : `drawLines`, `drawPoints`, `QPainterPath`.
- Pas de `QTimer` propre, pas de thread : tout passe par `advance`.
- Transparent : ne jamais peindre de fond (la photo d'arrière-plan doit rester visible).
- Style GPU : `engine="gpu"` dans l'`OrbSpec` ; s'inspirer de `glsl_orb.py`
  (shader léger, iGPU Intel HD 520).

## Vérifier

```fish
python -m ui.orb.preview <id>      # fenêtre seule, touches 1-6 pour les états
ruff check ui/orb
```
