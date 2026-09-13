"""core/map_render.py — Rendu HTML de la grande carte d'ANO-GPT.

Une seule carte dans toute l'application : le conteneur plein cadre. Elle
affiche aussi bien un point unique (« montre-moi Kaloum ») qu'une liste de
lieux trouvés, avec pins numérotés et fiches flottantes de style cyberpunk.

Prend désormais en charge la NAVIGATION GUIDÉE PAS-À-PAS complète :
- Requête OSRM avec steps=true & annotations=true ;
- Traduction en français des manœuvres et synthèse vocale TTS intégrée (500m / 150m / maintenant) ;
- Suivi du GPS en direct (window.ANO_UPDATE_GPS), recentrage dynamique et réévaluation de distance ;
- Détection d'écart à l'itinéraire (> 60 m pendant 10 s) avec recalcul silencieux ;
- HUD de navigation cyberpunk avec pictogrammes néon et aperçu de la manœuvre suivante.

Le HTML est produit ici, hors de l'interface, pour être testable sans lancer
Qt : c'est la partie qui casse en silence quand un nom contient une
apostrophe.
"""

from __future__ import annotations

import json
from typing import Any

# Tuiles officielles OpenStreetMap : aucune clé API. CARTO a commencé en août
# 2026 à filigraner ses anciennes tuiles publiques « API KEY REQUIRED ».
# Le thème sombre est produit localement par CSS, sans compte tiers.
_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
_LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"

# Routage : serveur de démonstration public d'OSRM avec support des étapes.
_OSRM = "https://router.project-osrm.org/route/v1/driving/"

# Au-delà, l'écran devient illisible : les fiches se replient en pastilles et
# ne se déploient qu'au survol ou au clic.
_DENSE_THRESHOLD = 9

# En dessous de ce zoom, les fiches se marchent dessus quelle que soit leur
# quantité : même repli.
_DENSE_ZOOM = 13


_STYLE = """
  :root {
    --cyan:#00e5ff; --magenta:#ff2e97; --amber:#ffc861;
    --green:#00ff9c; --void:#04070d; --panel:rgba(6,15,24,.94);
    --panel-glow:rgba(0,229,255,.16);
  }
  html, body, #map { margin:0; padding:0; width:100%; height:100%;
                     background:var(--void); overflow:hidden;
                     font-family:'Inter','Segoe UI',sans-serif; }
  .leaflet-container { background:var(--void); }

  /* Transformer localement les tuiles OSM claires en fond sombre bleuté. */
  .leaflet-tile-pane { filter:invert(1) hue-rotate(180deg) brightness(.62)
                              contrast(1.28) saturate(.72); }
  .leaflet-control-attribution { color:#7f9eaa !important;
    background:rgba(2,7,12,.78) !important; font-size:9px !important; }
  .leaflet-control-attribution a { color:#00bcd4 !important; }

  /* ── habillage plein cadre ───────────────────────────────────────────── */
  .crt { position:absolute; inset:0; pointer-events:none; z-index:640; }
  .crt::before {                      /* lignes de balayage */
    content:''; position:absolute; inset:0; opacity:.16;
    background:repeating-linear-gradient(180deg,
      rgba(0,229,255,.10) 0 1px, transparent 1px 3px); }
  .crt::after {                       /* vignette */
    content:''; position:absolute; inset:0;
    background:radial-gradient(ellipse at center,
      transparent 52%, rgba(2,5,10,.72) 100%); }
  .grid { position:absolute; inset:-40%; pointer-events:none; z-index:395;
    opacity:.13; background-image:
      linear-gradient(rgba(0,229,255,.55) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,229,255,.55) 1px, transparent 1px);
    background-size:64px 64px;
    mask:radial-gradient(ellipse at center, #000 12%, transparent 62%);
    -webkit-mask:radial-gradient(ellipse at center, #000 12%, transparent 62%);
    animation:drift 26s linear infinite; }
  .sweep { position:absolute; left:0; right:0; height:180px; z-index:641;
    pointer-events:none; opacity:.5;
    background:linear-gradient(180deg, transparent,
      rgba(0,229,255,.13) 62%, rgba(0,229,255,.34) 88%, transparent);
    animation:sweepdown 6.5s cubic-bezier(.45,0,.2,1) infinite; }

  @keyframes drift { to { background-position:64px 64px, 64px 64px; } }
  @keyframes sweepdown { 0% { top:-190px; opacity:0; }
                         12% { opacity:.5; }
                         88% { opacity:.5; }
                         100% { top:100%; opacity:0; } }

  /* ── panneau d'état ──────────────────────────────────────────────────── */
  .hud { position:absolute; top:14px; left:14px; z-index:660;
    padding:9px 13px 10px; min-width:186px; max-width:min(46vw,330px);
    color:#cdf6ff; background:var(--panel);
    border:1px solid rgba(0,229,255,.34);
    box-shadow:0 0 26px var(--panel-glow), inset 0 0 26px rgba(0,229,255,.05);
    clip-path:polygon(0 9px, 9px 0, 100% 0, 100% calc(100% - 9px),
                      calc(100% - 9px) 100%, 0 100%);
    animation:bootin .5s cubic-bezier(.2,.9,.25,1) both; }
  .hud .k { font-size:9px; letter-spacing:.24em; color:rgba(0,229,255,.62);
            text-transform:uppercase; }
  .hud .q { font-size:14px; font-weight:800; color:#e8fdff; margin-top:2px;
            letter-spacing:.02em; text-shadow:0 0 12px rgba(0,229,255,.5);
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .hud .s { display:flex; align-items:center; gap:6px; margin-top:6px;
            font-size:10px; letter-spacing:.16em; color:var(--green); }
  .hud .s i { width:6px; height:6px; border-radius:50%; background:var(--green);
              box-shadow:0 0 9px var(--green); animation:blink 1.5s infinite; }
  .hud .bar { position:relative; height:2px; margin-top:8px;
              background:rgba(0,229,255,.16); overflow:hidden; }
  .hud .bar::after { content:''; position:absolute; inset:0;
              background:linear-gradient(90deg, transparent, var(--cyan), transparent);
              animation:scanx 2.6s linear infinite; }
  .hud .btn { margin-top:9px; display:inline-block; cursor:pointer;
              font-size:9.5px; letter-spacing:.18em; padding:4px 9px;
              color:var(--cyan); border:1px solid rgba(0,229,255,.4);
              background:rgba(0,229,255,.07); user-select:none;
              transition:background .18s, color .18s, box-shadow .18s; }
  .hud .btn:hover { background:rgba(0,229,255,.2); color:#eafeff;
                    box-shadow:0 0 14px rgba(0,229,255,.35); }
  body.nocards .hud .btn { color:var(--magenta); border-color:rgba(255,46,151,.45);
                    background:rgba(255,46,151,.09); }

  @keyframes blink { 0%,100% { opacity:1; } 50% { opacity:.25; } }
  @keyframes scanx { 0% { transform:translateX(-100%); }
                     100% { transform:translateX(100%); } }

  /* ── position de l'utilisateur ───────────────────────────────────────── */
  .me { position:absolute; left:0; top:0; width:0; height:0;
        transition:transform 0.45s ease-out; }
  .me .dot { position:absolute; left:-7px; top:-7px; width:14px; height:14px;
    border-radius:50%; background:var(--green);
    box-shadow:0 0 18px var(--green), 0 0 3px #fff inset; }
  .me .radar { position:absolute; left:-36px; top:-36px; width:72px; height:72px;
    border-radius:50%; animation:spin 3.4s linear infinite;
    background:conic-gradient(from 0deg, rgba(0,255,156,.38), transparent 32%);
    mask:radial-gradient(closest-side, #000 0 98%, transparent);
    -webkit-mask:radial-gradient(closest-side, #000 0 98%, transparent); }
  .me .ring { position:absolute; left:-10px; top:-10px; width:20px; height:20px;
    border-radius:50%; border:1px solid rgba(0,255,156,.75);
    animation:ping 3s cubic-bezier(.2,.6,.3,1) infinite; }
  .me .ring.b { animation-delay:1.5s; }
  .me .heading-arrow { position:absolute; left:-8px; top:-22px; width:16px; height:16px;
    display:none; filter:drop-shadow(0 0 6px var(--green)); }

  /* ── noeud : pin + fiche flottante ───────────────────────────────────── */
  .node { position:absolute; left:0; top:0; width:0; height:0;
          animation:bootin .55s cubic-bezier(.2,.9,.25,1) both;
          animation-delay:var(--d,0s); }

  .pin { position:absolute; left:-13px; top:-30px; width:26px; height:26px;
    display:flex; align-items:center; justify-content:center; cursor:pointer;
    color:#04121a; font-weight:900; font-size:12px; font-variant-numeric:tabular-nums;
    background:linear-gradient(150deg, #7ff4ff, var(--cyan) 55%, #00a6d0);
    clip-path:polygon(50% 0, 100% 27%, 100% 73%, 50% 100%, 0 73%, 0 27%);
    box-shadow:0 0 18px rgba(0,229,255,.55);
    transition:transform .22s cubic-bezier(.2,.9,.25,1), filter .22s;
    animation:hover-y var(--f,5s) ease-in-out infinite;
    animation-delay:var(--fd,0s); }
  .node:hover .pin, .node.on .pin { transform:scale(1.16); filter:brightness(1.15); }
  .node.on .pin { background:linear-gradient(150deg, #ffb0d8, var(--magenta) 55%, #c00d63);
                  box-shadow:0 0 22px rgba(255,46,151,.6); }
  .pin .tip { position:absolute; left:50%; top:100%; width:1px; height:5px;
    margin-left:-.5px; background:linear-gradient(180deg, var(--cyan), transparent); }

  .halo { position:absolute; left:-23px; top:-40px; width:46px; height:46px;
    border-radius:50%; pointer-events:none;
    background:conic-gradient(from 0deg, transparent 0 62%,
                              rgba(0,229,255,.85) 82%, transparent 100%);
    mask:radial-gradient(closest-side, transparent 0 70%, #000 72%);
    -webkit-mask:radial-gradient(closest-side, transparent 0 70%, #000 72%);
    animation:spin 4.2s linear infinite; opacity:.75; }
  .ping { position:absolute; left:-13px; top:-30px; width:26px; height:26px;
    border-radius:50%; border:1px solid rgba(0,229,255,.55); pointer-events:none;
    animation:ping 3.2s cubic-bezier(.2,.6,.3,1) infinite;
    animation-delay:var(--fd,0s); }

  .link { position:absolute; pointer-events:none; transition:opacity .25s; }
  .link.v { left:-.5px; bottom:30px; width:1px;
    height:calc(26px + var(--dy,0px));
    background:linear-gradient(180deg, rgba(0,229,255,.9), rgba(0,229,255,.1)); }
  .link.h { bottom:calc(56px + var(--dy,0px)); width:18px; height:1px;
    background:linear-gradient(90deg, rgba(0,229,255,.9), rgba(0,229,255,.15)); }
  .node.r .link.h { left:0; }
  .node.l .link.h { right:0;
    background:linear-gradient(270deg, rgba(0,229,255,.9), rgba(0,229,255,.15)); }
  .link .knot { position:absolute; top:-2px; width:5px; height:5px;
    background:var(--cyan); box-shadow:0 0 8px var(--cyan);
    transform:rotate(45deg); }
  .node.r .link.h .knot { right:-2px; }
  .node.l .link.h .knot { left:-2px; }

  .card { position:absolute; bottom:calc(48px + var(--dy,0px));
    width:214px; padding:1px;
    background:linear-gradient(155deg, rgba(0,229,255,.85), rgba(255,46,151,.55) 58%,
                                rgba(0,229,255,.28));
    clip-path:polygon(0 11px, 11px 0, 100% 0, 100% calc(100% - 11px),
                      calc(100% - 11px) 100%, 0 100%);
    box-shadow:0 10px 34px rgba(0,0,0,.72), 0 0 26px rgba(0,229,255,.16);
    animation:hover-y var(--f,5s) ease-in-out infinite;
    animation-delay:var(--fd,0s);
    transition:transform .26s cubic-bezier(.2,.9,.25,1), opacity .26s;
    will-change:transform; }
  .node.r .card { left:18px; }
  .node.l .card { right:18px; }
  .card .in { position:relative; overflow:hidden; padding:9px 11px 10px;
    background:linear-gradient(160deg, rgba(7,17,27,.96), rgba(4,10,17,.97));
    clip-path:polygon(0 10px, 10px 0, 100% 0, 100% calc(100% - 10px),
                      calc(100% - 10px) 100%, 0 100%); }
  .card .in::after { content:''; position:absolute; left:0; right:0; height:34%;
    background:linear-gradient(180deg, transparent, rgba(0,229,255,.16), transparent);
    animation:cardscan 4.6s linear infinite; animation-delay:var(--fd,0s);
    pointer-events:none; }

  .card .hd { display:flex; align-items:baseline; gap:6px; }
  .card .ix { font-size:9px; font-weight:800; letter-spacing:.12em;
              color:#04121a; background:var(--cyan); padding:1px 4px;
              font-variant-numeric:tabular-nums; }
  .card .nm { position:relative; font-size:12.5px; font-weight:800; color:#eafdff;
              line-height:1.25; letter-spacing:.01em;
              text-shadow:0 0 10px rgba(0,229,255,.35);
              display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
              overflow:hidden; }
  .node:hover .card .nm::before, .node:hover .card .nm::after {
    content:attr(data-t); position:absolute; left:0; top:0; width:100%;
    pointer-events:none; }
  .node:hover .card .nm::before { color:var(--magenta); mix-blend-mode:screen;
    animation:glitch-a .42s steps(2,end) 2; }
  .node:hover .card .nm::after { color:var(--cyan); mix-blend-mode:screen;
    animation:glitch-b .42s steps(2,end) 2; }

  .card .sep { height:1px; margin:7px 0 6px;
    background:linear-gradient(90deg, rgba(0,229,255,.5), transparent); }
  .card .rw { display:flex; align-items:center; gap:5px; font-size:10.5px;
    color:#9fe9f7; margin:2.5px 0; line-height:1.35; }
  .card .rw b { color:#dffaff; font-weight:700; }
  .card .rw.gold { color:var(--amber); }
  .card .rw .g { flex:0 0 auto; width:4px; height:4px; transform:rotate(45deg);
    background:rgba(0,229,255,.75); box-shadow:0 0 6px rgba(0,229,255,.6); }
  .card .rw.gold .g { background:var(--amber); box-shadow:0 0 6px var(--amber); }
  .card .go { display:block; margin-top:9px; text-align:center; cursor:pointer;
    font-size:9.5px; font-weight:800; letter-spacing:.2em; padding:5px 0;
    color:#04121a; text-decoration:none;
    background:linear-gradient(90deg, var(--cyan), #6ef2ff);
    clip-path:polygon(6px 0, 100% 0, calc(100% - 6px) 100%, 0 100%);
    transition:filter .18s, box-shadow .18s; }
  .card .go:hover { filter:brightness(1.12);
                    box-shadow:0 0 18px rgba(0,229,255,.55); }
  .card .more { max-height:0; opacity:0; overflow:hidden;
    transition:max-height .3s cubic-bezier(.2,.9,.25,1), opacity .25s; }
  .node:hover .card .more, .node.on .card .more { max-height:130px; opacity:1; }

  body.dense .node:not(.on) .card,
  body.dense .node:not(.on) .link,
  body.nocards .node:not(.on) .card,
  body.nocards .node:not(.on) .link {
    opacity:0; transform:scale(.82) translateY(8px); pointer-events:none; }
  body.dense .node:hover .card, body.dense .node:hover .link,
  body.nocards .node:hover .card, body.nocards .node:hover .link {
    opacity:1; transform:none; pointer-events:auto; }

  /* ── itinéraire & tracé ──────────────────────────────────────────────── */
  path.routeline { stroke-dasharray:14 11; animation:flow 1.3s linear infinite;
                   filter:drop-shadow(0 0 6px rgba(0,229,255,.85)); }
  @keyframes flow { to { stroke-dashoffset:-25; } }

  .node.dest .pin { background:linear-gradient(150deg, #ffe9a8, var(--amber) 55%, #c98a17);
                    box-shadow:0 0 24px rgba(255,200,97,.7); }

  .route { position:absolute; left:14px; bottom:14px; z-index:660;
    padding:9px 13px 10px; min-width:210px; max-width:min(52vw,350px);
    color:#cdf6ff; background:var(--panel);
    border:1px solid rgba(255,200,97,.38);
    box-shadow:0 0 26px rgba(255,200,97,.12), inset 0 0 26px rgba(0,229,255,.05);
    clip-path:polygon(0 9px, 9px 0, 100% 0, 100% calc(100% - 9px),
                      calc(100% - 9px) 100%, 0 100%);
    animation:bootin .45s cubic-bezier(.2,.9,.25,1) both; }
  .route[hidden] { display:none; }
  .route .k { font-size:9px; letter-spacing:.24em; color:rgba(255,200,97,.75);
              text-transform:uppercase; }
  .route .q { font-size:13px; font-weight:800; color:#e8fdff; margin-top:2px;
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .route .v { margin-top:5px; font-size:11px; letter-spacing:.1em;
              color:var(--green); }
  .route .v b { color:#eafeff; font-size:14px; font-weight:800;
                font-variant-numeric:tabular-nums; }
  .route .v.warn { color:var(--magenta); }
  .route .acts { display:flex; flex-wrap:wrap; gap:6px; margin-top:9px; }
  .route .btn { cursor:pointer; text-decoration:none; user-select:none;
    font-size:9.5px; letter-spacing:.16em; padding:4px 9px;
    color:var(--amber); border:1px solid rgba(255,200,97,.4);
    background:rgba(255,200,97,.08);
    transition:background .18s, color .18s, box-shadow .18s; }
  .route .btn:hover { background:rgba(255,200,97,.2); color:#fff6e2;
                      box-shadow:0 0 14px rgba(255,200,97,.35); }
  .route .btn.primary { color:#04121a; font-weight:800;
    background:linear-gradient(90deg, var(--green), #7dffca);
    border-color:var(--green); }
  .route .btn.primary:hover { filter:brightness(1.15); box-shadow:0 0 16px var(--green); }

  /* ── BANNIÈRE DE GUIDAGE PAS-À-PAS CYBERPUNK ─────────────────────────── */
  .nav-hud { position:absolute; top:14px; left:50%; transform:translateX(-50%);
    z-index:670; width:min(90vw, 460px); color:#eafdff;
    background:var(--panel); border:1px solid rgba(0,229,255,.5);
    box-shadow:0 0 32px rgba(0,229,255,.24), inset 0 0 20px rgba(0,229,255,.08);
    clip-path:polygon(0 12px, 12px 0, calc(100% - 12px) 0, 100% 12px,
                      100% calc(100% - 12px), calc(100% - 12px) 100%,
                      12px 100%, 0 calc(100% - 12px));
    animation:bootin .4s cubic-bezier(.2,.9,.25,1) both; }
  .nav-hud[hidden] { display:none; }

  .nav-main { display:flex; align-items:center; gap:14px; padding:12px 16px; }
  .nav-icon-box { flex:0 0 54px; height:54px; display:flex; align-items:center;
    justify-content:center; background:linear-gradient(135deg, rgba(0,229,255,.18), rgba(0,255,156,.1));
    border:1px solid var(--cyan); border-radius:8px;
    box-shadow:0 0 16px rgba(0,229,255,.35); }
  .nav-icon-box svg { width:36px; height:36px; fill:var(--cyan);
    filter:drop-shadow(0 0 8px var(--cyan)); }

  .nav-content { flex:1 1 auto; min-width:0; }
  .nav-dist { font-size:22px; font-weight:900; color:#fff; letter-spacing:.02em;
    font-variant-numeric:tabular-nums; text-shadow:0 0 14px rgba(0,229,255,.6);
    line-height:1.1; }
  .nav-dist small { font-size:12px; font-weight:700; color:var(--cyan);
    margin-left:4px; letter-spacing:.12em; }
  .nav-instr { font-size:13.5px; font-weight:700; color:#cbf5ff; margin-top:3px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

  .nav-sub { display:flex; align-items:center; justify-content:space-between;
    padding:6px 16px 8px; border-top:1px solid rgba(0,229,255,.15);
    background:rgba(2,8,16,.6); font-size:10.5px; }
  .nav-next { display:flex; align-items:center; gap:6px; color:rgba(200,245,255,.75);
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .nav-next b { color:var(--amber); }

  .nav-tools { display:flex; gap:6px; align-items:center; }
  .nav-btn { cursor:pointer; padding:3px 7px; font-size:9px; letter-spacing:.14em;
    font-weight:700; color:var(--cyan); border:1px solid rgba(0,229,255,.35);
    background:rgba(0,229,255,.08); border-radius:3px;
    transition:background .15s, color .15s; }
  .nav-btn:hover { background:rgba(0,229,255,.24); color:#fff; }
  .nav-btn.active { color:var(--green); border-color:var(--green); background:rgba(0,255,156,.12); }
  .nav-btn.danger { color:var(--magenta); border-color:var(--magenta); }

  .nav-reroute-toast { position:absolute; bottom:-34px; left:50%; transform:translateX(-50%);
    padding:4px 12px; background:rgba(255,46,151,.9); color:#fff; font-size:10px;
    font-weight:800; letter-spacing:.14em; clip-path:polygon(4px 0, 100% 0, calc(100% - 4px) 100%, 0 100%);
    box-shadow:0 0 16px rgba(255,46,151,.5); display:none; animation:blink 1s infinite; }

  @keyframes bootin {
    from { opacity:0; transform:translateY(16px) scale(.86); filter:blur(6px); }
    to   { opacity:1; transform:none; filter:none; } }
  @keyframes hover-y { 0%,100% { translate:0 0; } 50% { translate:0 -6px; } }
  @keyframes spin { to { transform:rotate(360deg); } }
  @keyframes ping { 0% { transform:scale(.55); opacity:.75; }
                    100% { transform:scale(2.4); opacity:0; } }
  @keyframes cardscan { 0% { top:-40%; } 100% { top:110%; } }
  @keyframes glitch-a { 0% { transform:translate(-2px,1px); clip-path:inset(0 0 62% 0); }
                        50% { transform:translate(2px,-1px); clip-path:inset(58% 0 0 0); }
                        100% { transform:none; clip-path:inset(0 0 0 0); } }
  @keyframes glitch-b { 0% { transform:translate(2px,-1px); clip-path:inset(52% 0 0 0); }
                        50% { transform:translate(-2px,1px); clip-path:inset(0 0 48% 0); }
                        100% { transform:none; clip-path:inset(0 0 0 0); } }

  @media (prefers-reduced-motion: reduce) {
    .node, .card, .pin, .halo, .ping, .sweep, .grid, .me .radar, .me .ring,
    .card .in::after, .hud, .hud .bar::after, .route, .nav-hud,
    path.routeline { animation:none !important; }
    .sweep { display:none; }
  }

  /* Machine à deux cœurs : une grille qui dérive, un balayage, des anneaux
     qui pulsent et des cartes qui flottent en boucle font repeindre toute la
     page à 60 images par seconde dans Chromium, et ce temps est pris au
     micro — carte ouverte, l'assistant n'entendait plus. Les animations en
     boucle sont donc coupées d'office (l'entrée « bootin », jouée une fois,
     reste). La vue garde son style, immobile. */
  .grid, .sweep, .hud .s i, .hud .bar::after, .me .radar, .me .ring, .pin,
  .halo, .ping, .card, .card .in::after, .route, path.routeline
    { animation:none !important; }
  .sweep { display:none; }
"""


_SCRIPT = """
  var center = @@CENTER@@;
  var userPos = [center[0], center[1]];
  // Sans animation de zoom ni fondu des tuiles, et tuiles chargées seulement
  // une fois le déplacement fini : moins de trames à composer pour Chromium.
  var map = L.map('map', { zoomControl: false, attributionControl: true,
                           zoomAnimation: false, fadeAnimation: false,
                           markerZoomAnimation: false, preferCanvas: true })
             .setView(center, @@ZOOM@@);
  L.control.zoom({ position: 'bottomright' }).addTo(map);
  L.tileLayer('@@TILES@@', {
    maxZoom: 19, updateWhenIdle: true,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);

  var bounds = [];
  var userMarker = null;
  if (@@MARK_CENTER@@) {
    var meIcon = L.divIcon({ className: '', iconSize: [0, 0], iconAnchor: [0, 0],
      html: '<div class="me" id="user-me-marker"><div class="radar"></div><div class="ring"></div>' +
            '<div class="ring b"></div><div class="dot"></div></div>' });
    userMarker = L.marker(center, { icon: meIcon, interactive: false }).addTo(map);
    L.circle(center, { color: '#00e5ff', weight: 1, dashArray: '6 8',
                       fillColor: '#00e5ff', fillOpacity: 0.05,
                       radius: @@RADIUS_M@@ }).addTo(map);
    bounds.push(center);
  }

  var places = @@PLACES@@;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null && text !== '') node.textContent = text;
    return node;
  }

  function row(text, gold) {
    var line = el('div', gold ? 'rw gold' : 'rw');
    line.appendChild(el('span', 'g'));
    line.appendChild(el('span', '', text));
    return line;
  }

  function buildNode(p, index) {
    var node = el('div', 'node ' + (index % 2 ? 'l' : 'r'));
    node.style.setProperty('--d', (index * 0.06).toFixed(2) + 's');
    node.style.setProperty('--f', (4.4 + (index % 5) * 0.55).toFixed(2) + 's');
    node.style.setProperty('--fd', (-(index % 7) * 0.73).toFixed(2) + 's');
    node.style.setProperty('--dy', ((index % 3) * 34) + 'px');
    node.dataset.dy = String((index % 3) * 34);

    node.appendChild(el('div', 'halo'));
    node.appendChild(el('div', 'ping'));

    var pin = el('div', 'pin', String(p.n));
    pin.appendChild(el('span', 'tip'));
    node.appendChild(pin);

    node.appendChild(el('div', 'link v'));
    var linkH = el('div', 'link h');
    linkH.appendChild(el('span', 'knot'));
    node.appendChild(linkH);

    var card = el('div', 'card');
    var inner = el('div', 'in');

    var head = el('div', 'hd');
    head.appendChild(el('span', 'ix', ('0' + p.n).slice(-2)));
    var name = el('span', 'nm', p.name);
    name.setAttribute('data-t', p.name);
    head.appendChild(name);
    inner.appendChild(head);

    if (p.rating) {
      inner.appendChild(row('\\u2605 ' + p.rating +
        (p.reviews ? '  \\u00b7  ' + p.reviews + ' avis' : ''), true));
    }
    var meta = (p.dist !== null && p.dist !== undefined && p.dist !== '')
      ? p.dist + ' km' : '';
    if (p.category) meta = meta ? meta + '  \\u00b7  ' + p.category : p.category;
    if (meta) inner.appendChild(row(meta));

    var more = el('div', 'more');
    if (p.address) more.appendChild(row(p.address));
    if (p.hours) more.appendChild(row(p.hours));
    if (p.phone) more.appendChild(row(p.phone));
    if (more.childNodes.length) {
      inner.appendChild(el('div', 'sep'));
      inner.appendChild(more);
    }

    if (p.url) {
      var go = el('a', 'go', 'ITIN\\u00c9RAIRE \\u25b8');
      go.href = p.url;
      go.target = '_blank';
      go.rel = 'noopener';
      inner.appendChild(go);
    }

    card.appendChild(inner);
    node.appendChild(card);
    return node;
  }

  /* ── MOTEUR DE NAVIGATION GUIDÉE PAS-À-PAS ────────────────────────────── */
  var HAS_ME = @@MARK_CENTER@@;
  var OSRM = '@@OSRM@@';
  var routeLine = null, routeHalo = null, routeSeq = 0;
  var activeRoute = null;
  var activeDest = null;
  var activeSteps = [];
  var currentStepIdx = 0;
  var voiceEnabled = true;
  var autoFollow = true;
  var offRouteSince = null;
  var lastRecalcAt = 0;
  var announcedThresholds = {}; // {stepIdx: {500: bool, 150: bool, now: bool}}

  var panel = document.getElementById('route');
  var panelName = document.getElementById('route-name');
  var panelInfo = document.getElementById('route-info');
  var panelExt = document.getElementById('route-ext');
  var navHud = document.getElementById('nav-hud');
  var navDist = document.getElementById('nav-dist-val');
  var navDistUnit = document.getElementById('nav-dist-unit');
  var navInstr = document.getElementById('nav-instr-txt');
  var navNextTxt = document.getElementById('nav-next-txt');
  var navIconBox = document.getElementById('nav-icon-box');
  var navRerouteToast = document.getElementById('nav-reroute-toast');
  var navVoiceBtn = document.getElementById('nav-voice-btn');
  var navFollowBtn = document.getElementById('nav-follow-btn');

  // Dictionnaire SVG des manœuvres
  var ICONS_SVG = {
    'turn-right': '<svg viewBox="0 0 24 24"><path d="M12 4l-1.41 1.41L16.17 11H4v2h12.17l-5.58 5.59L12 20l8-8z"/></svg>',
    'turn-left': '<svg viewBox="0 0 24 24"><path d="M12 4l1.41 1.41L7.83 11H20v2H7.83l5.58 5.59L12 20l-8-8z"/></svg>',
    'turn-slight-right': '<svg viewBox="0 0 24 24"><path d="M14 4h6v6l-2.5-2.5-5.5 5.5-1.41-1.41 5.5-5.5L14 4z M4 20l8-8 1.41 1.41-8 8H4z"/></svg>',
    'turn-slight-left': '<svg viewBox="0 0 24 24"><path d="M10 4H4v6l2.5-2.5 5.5 5.5 1.41-1.41-5.5-5.5L10 4z M20 20l-8-8-1.41 1.41 8 8h1.41z"/></svg>',
    'turn-sharp-right': '<svg viewBox="0 0 24 24"><path d="M6 4h8v8l-2.5-2.5-4 4-1.41-1.41 4-4L6 4z"/></svg>',
    'turn-sharp-left': '<svg viewBox="0 0 24 24"><path d="M18 4h-8v8l2.5-2.5 4 4 1.41-1.41-4-4L18 4z"/></svg>',
    'straight': '<svg viewBox="0 0 24 24"><path d="M12 4l-8 8h5v8h6v-8h5z"/></svg>',
    'u-turn': '<svg viewBox="0 0 24 24"><path d="M6 9v6c0 3.31 2.69 6 6 6s6-2.69 6-6V4l3 3-1.41 1.41L18 6.83V15c0 2.21-1.79 4-4 4s-4-1.79-4-4V9h3L10 4 5 9h1z"/></svg>',
    'roundabout': '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 13v-2l-4-4-4 4h2.12C3.04 11.64 3 12.31 3 13c0 4.97 4.03 9 9 9v-2.07z"/></svg>',
    'arrive': '<svg viewBox="0 0 24 24"><path d="M12 2c-4.42 0-8 3.58-8 8 0 5.25 8 12 8 12s8-6.75 8-12c0-4.42-3.58-8-8-8zm0 11c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3z"/></svg>',
    'fork-left': '<svg viewBox="0 0 24 24"><path d="M13.5 12.09L7.91 6.5 10 4.41H4v6l2.09-2.09 5.41 5.41V20h2v-7.91z"/></svg>',
    'fork-right': '<svg viewBox="0 0 24 24"><path d="M10.5 12.09l5.59-5.59L14 4.41h6v6l-2.09-2.09-5.41 5.41V20h-2v-7.91z"/></svg>',
    'merge': '<svg viewBox="0 0 24 24"><path d="M6 4l3 3H7v5.59l5 5V20h2v-3.41l-5-5V7H7l3-3H6z M18 4l-3 3h2v5.59l-2.5 2.5 1.41 1.41 3.09-3.09V7h2l-3-3z"/></svg>',
    'ramp-on': '<svg viewBox="0 0 24 24"><path d="M11 4h8v8l-2.5-2.5-6.5 6.5-1.41-1.41 6.5-6.5L11 4z"/></svg>',
    'ramp-off': '<svg viewBox="0 0 24 24"><path d="M13 20h-8v-8l2.5 2.5 6.5-6.5 1.41 1.41-6.5 6.5L13 20z"/></svg>'
  };

  // Traduction des étapes en français
  function translateStep(s, isLast) {
    var man = s.maneuver || {};
    var type = man.type || 'turn';
    var mod = man.modifier || 'straight';
    var name = (s.name || '').trim();
    var road = name ? ' sur ' + name : '';
    var roadV = name ? ' sur ' + name : '';
    var icon = 'straight';
    var instr = '', voice = '';

    if (mod === 'right') icon = 'turn-right';
    else if (mod === 'left') icon = 'turn-left';
    else if (mod === 'slight right') icon = 'turn-slight-right';
    else if (mod === 'slight left') icon = 'turn-slight-left';
    else if (mod === 'sharp right') icon = 'turn-sharp-right';
    else if (mod === 'sharp left') icon = 'turn-sharp-left';
    else if (mod === 'uturn') icon = 'u-turn';

    if (isLast || type === 'arrive') {
      instr = 'Arrivé à destination';
      voice = 'Vous êtes arrivé à destination.';
      icon = 'arrive';
    } else if (type === 'depart') {
      instr = road ? 'Départ' + road : 'Prenez la route';
      voice = 'Prenez la route' + roadV + '.';
      icon = 'straight';
    } else if (type === 'roundabout' || type === 'rotary' || type === 'roundabout turn') {
      icon = 'roundabout';
      var exit = man.exit || 1;
      var ord = (exit === 1) ? '1ère' : exit + 'e';
      var ordV = (exit === 1) ? 'première' : (exit === 2) ? 'deuxième' : exit + 'ième';
      instr = 'Au rond-point, ' + ord + ' sortie' + road;
      voice = 'Au rond-point, prenez la ' + ordV + ' sortie' + roadV + '.';
    } else if (type === 'fork') {
      if (mod.indexOf('left') !== -1) { instr = 'Restez à gauche' + road; voice = 'Restez sur la gauche' + roadV + '.'; icon = 'fork-left'; }
      else if (mod.indexOf('right') !== -1) { instr = 'Restez à droite' + road; voice = 'Restez sur la droite' + roadV + '.'; icon = 'fork-right'; }
      else { instr = 'Bifurcation' + road; voice = 'Prenez la bifurcation' + roadV + '.'; }
    } else if (type === 'merge') {
      instr = 'Rejoignez la voie' + road; voice = 'Rejoignez la voie' + roadV + '.'; icon = 'merge';
    } else if (type === 'on ramp') {
      instr = 'Prenez la bretelle' + road; voice = 'Prenez la bretelle d\\u2019accès' + roadV + '.'; icon = 'ramp-on';
    } else if (type === 'off ramp') {
      instr = 'Prenez la sortie' + road; voice = 'Prenez la sortie' + roadV + '.'; icon = 'ramp-off';
    } else if (type === 'end of road') {
      var dir = (mod.indexOf('left') !== -1) ? 'à gauche' : (mod.indexOf('right') !== -1) ? 'à droite' : 'tout droit';
      instr = 'En bout de route, ' + dir + road;
      voice = 'En bout de route, tournez ' + dir + roadV + '.';
      icon = (mod.indexOf('left') !== -1) ? 'turn-left' : 'turn-right';
    } else if (type === 'continue' || type === 'new name') {
      instr = 'Continuez' + (mod === 'straight' ? ' tout droit' : '') + road;
      voice = 'Continuez' + (mod === 'straight' ? ' tout droit' : '') + roadV + '.';
    } else {
      if (mod === 'uturn') { instr = 'Faites demi-tour' + road; voice = 'Faites demi-tour' + roadV + '.'; }
      else if (mod.indexOf('left') !== -1) {
        var qual = (mod === 'sharp left') ? 'franchement à gauche' : (mod === 'slight left') ? 'légèrement à gauche' : 'à gauche';
        instr = 'Tournez ' + qual + road; voice = 'Tournez ' + qual + roadV + '.';
      } else if (mod.indexOf('right') !== -1) {
        var qualR = (mod === 'sharp right') ? 'franchement à droite' : (mod === 'slight right') ? 'légèrement à droite' : 'à droite';
        instr = 'Tournez ' + qualR + road; voice = 'Tournez ' + qualR + roadV + '.';
      } else {
        instr = 'Continuez tout droit' + road; voice = 'Continuez tout droit' + roadV + '.';
      }
    }

    return {
      instruction: instr,
      voice: voice,
      icon: icon,
      distance: s.distance || 0,
      duration: s.duration || 0,
      lat: (man.location && man.location[1]) || 0,
      lon: (man.location && man.location[0]) || 0
    };
  }

  function haversineDistM(lat1, lon1, lat2, lon2) {
    var R = 6371000;
    var dLat = (lat2 - lat1) * Math.PI / 180;
    var dLon = (lon2 - lon1) * Math.PI / 180;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function distPointToSegment(px, py, ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay;
    var lenSq = dx * dx + dy * dy;
    if (lenSq === 0) return Math.hypot(px - ax, py - ay);
    var t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lenSq));
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }

  function distToPolylineM(lat, lon, polyline) {
    if (!polyline || polyline.length === 0) return Infinity;
    if (polyline.length === 1) return haversineDistM(lat, lon, polyline[0][0], polyline[0][1]);
    var minD = Infinity;
    for (var i = 0; i < polyline.length - 1; i++) {
      var p1 = polyline[i], p2 = polyline[i + 1];
      var d = distPointToSegment(lon, lat, p1[1], p1[0], p2[1], p2[0]);
      var dM = d * 111320 * Math.cos(lat * Math.PI / 180);
      if (dM < minD) minD = dM;
    }
    return minD;
  }

  function speakText(text) {
    if (!voiceEnabled || !text || !window.speechSynthesis) return;
    try {
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(text);
      u.lang = 'fr-FR';
      u.rate = 1.05;
      window.speechSynthesis.speak(u);
    } catch (e) {}
  }

  function clearRoute() {
    routeSeq++;
    if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
    if (routeHalo) { map.removeLayer(routeHalo); routeHalo = null; }
    document.querySelectorAll('.node.dest').forEach(function (node) {
      node.classList.remove('dest');
    });
    if (panel) panel.hidden = true;
    if (navHud) navHud.hidden = true;
    activeRoute = null;
    activeDest = null;
    activeSteps = [];
    currentStepIdx = 0;
  }

  function fmtDuration(seconds) {
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + ' min';
    return Math.floor(minutes / 60) + ' h ' + ('0' + (minutes % 60)).slice(-2);
  }

  function setInfo(text, warn) {
    if (!panelInfo) return;
    panelInfo.innerHTML = '';
    panelInfo.className = warn ? 'v warn' : 'v';
    panelInfo.appendChild(document.createTextNode(text));
  }

  function updateNavHud(step, nextStep, distM) {
    if (!navHud || !step) return;
    navHud.hidden = false;
    if (distM >= 1000) {
      navDist.textContent = (distM / 1000).toFixed(1);
      navDistUnit.textContent = 'KM';
    } else {
      navDist.textContent = Math.round(distM);
      navDistUnit.textContent = 'M';
    }
    navInstr.textContent = step.instruction;
    var iconSvg = ICONS_SVG[step.icon] || ICONS_SVG['straight'];
    navIconBox.innerHTML = iconSvg;

    if (nextStep) {
      var nextDist = Math.round(nextStep.distance);
      navNextTxt.textContent = 'Puis dans ' + nextDist + ' m : ' + nextStep.instruction;
    } else {
      navNextTxt.textContent = 'Dernière étape vers la destination';
    }
  }

  function checkVoiceThresholds(step, distM) {
    var sIdx = currentStepIdx;
    if (!announcedThresholds[sIdx]) announcedThresholds[sIdx] = {};
    var a = announcedThresholds[sIdx];

    // Seuil 500 m
    if (distM <= 550 && distM > 250 && !a['500']) {
      a['500'] = true;
      speakText('Dans 500 mètres, ' + step.voice);
    }
    // Seuil 150 m
    else if (distM <= 180 && distM > 40 && !a['150']) {
      a['150'] = true;
      speakText('Dans 150 mètres, ' + step.voice);
    }
    // Seuil « maintenant » (<= 35 m)
    else if (distM <= 35 && !a['now']) {
      a['now'] = true;
      if (step.icon === 'arrive') {
        speakText('Vous êtes arrivé à destination.');
      } else {
        speakText(step.voice.replace(/\\.$/, '') + ' maintenant.');
      }
    }
  }

  function processNavPosition(lat, lon) {
    if (!activeRoute || activeSteps.length === 0) return;

    // 1. Contrôle d'écart à l'itinéraire (> 60 m pendant 10 s)
    var distToPath = distToPolylineM(lat, lon, activeRoute.polyline);
    var now = Date.now();
    if (distToPath > 60) {
      if (!offRouteSince) offRouteSince = now;
      if (navRerouteToast) navRerouteToast.style.display = 'block';

      if ((now - offRouteSince >= 10000) && (now - lastRecalcAt >= 30000)) {
        lastRecalcAt = now;
        offRouteSince = null;
        if (activeDest) {
          speakText('Nouvel itinéraire en cours de calcul.');
          drawRoute(activeDest, null, true);
        }
      }
    } else {
      offRouteSince = null;
      if (navRerouteToast) navRerouteToast.style.display = 'none';
    }

    // 2. Étape courante et avancement
    var cur = activeSteps[currentStepIdx];
    if (!cur) return;

    var distToStep = haversineDistM(lat, lon, cur.lat, cur.lon);

    // Si on dépasse/atteint la manœuvre (rayon 35 m)
    if (distToStep <= 35 && currentStepIdx < activeSteps.length - 1) {
      currentStepIdx++;
      cur = activeSteps[currentStepIdx];
      distToStep = haversineDistM(lat, lon, cur.lat, cur.lon);
    }

    var nxt = (currentStepIdx + 1 < activeSteps.length) ? activeSteps[currentStepIdx + 1] : null;
    updateNavHud(cur, nxt, distToStep);
    checkVoiceThresholds(cur, distToStep);

    // 3. Suivi caméra si actif
    if (autoFollow && map) {
      map.panTo([lat, lon], { animate: true, duration: 0.6 });
    }
  }

  function drawRoute(p, node, isRecalc) {
    var seq = ++routeSeq;
    activeDest = p;
    if (routeLine) { map.removeLayer(routeLine); routeLine = null; }
    if (routeHalo) { map.removeLayer(routeHalo); routeHalo = null; }
    document.querySelectorAll('.node.dest').forEach(function (other) {
      other.classList.remove('dest');
    });
    if (node) node.classList.add('dest');
    if (panel) {
      panel.hidden = false;
      if (panelName) panelName.textContent = p.name;
      if (panelExt) panelExt.href = p.url || '#';
      setInfo('CALCUL DE L\\u2019ITIN\\u00c9RAIRE\\u2026', false);
    }

    var startLon = userPos[1], startLat = userPos[0];
    var url = OSRM + startLon + ',' + startLat + ';' + p.lon + ',' + p.lat +
              '?steps=true&annotations=true&overview=full&geometries=geojson';

    fetch(url).then(function (response) {
      return response.json();
    }).then(function (data) {
      if (seq !== routeSeq) return;
      var route = data && data.routes && data.routes[0];
      if (!route || !route.geometry) throw new Error('sans route');

      var line = route.geometry.coordinates.map(function (c) {
        return [c[1], c[0]];
      });

      routeHalo = L.polyline(line, { color: '#00e5ff', weight: 12, opacity: 0.16,
                                     lineJoin: 'round', interactive: false })
                   .addTo(map);
      routeLine = L.polyline(line, { color: '#7ff4ff', weight: 3.5, opacity: 0.95,
                                     lineJoin: 'round', interactive: false,
                                     className: 'routeline' }).addTo(map);

      setInfo((route.distance / 1000).toFixed(1) + ' km  \\u00b7  ' +
              fmtDuration(route.duration) + '  \\u00b7  EN VOITURE', false);

      // Décodage des étapes de navigation
      var rawSteps = (route.legs && route.legs[0] && route.legs[0].steps) || [];
      activeSteps = rawSteps.map(function(s, idx) {
        return translateStep(s, idx === rawSteps.length - 1);
      });
      currentStepIdx = 0;
      announcedThresholds = {};
      activeRoute = {
        distance: route.distance,
        duration: route.duration,
        polyline: line
      };

      if (!isRecalc) {
        map.fitBounds(routeLine.getBounds(), {
          paddingTopLeft: [150, 200], paddingBottomRight: [150, 150], maxZoom: 16
        });
        if (activeSteps.length > 0) {
          speakText('Itinéraire vers ' + p.name + '. ' + activeSteps[0].voice);
        }
      }

      if (activeSteps.length > 0) {
        var firstDist = haversineDistM(userPos[0], userPos[1], activeSteps[0].lat, activeSteps[0].lon);
        var nxt = (activeSteps.length > 1) ? activeSteps[1] : null;
        updateNavHud(activeSteps[0], nxt, firstDist);
      }

    }).catch(function () {
      if (seq !== routeSeq) return;
      setInfo('ROUTAGE INDISPONIBLE \\u2014 VOIR MAPS', true);
    });
  }

  // API Globale pour recevoir la position GPS en direct (depuis Qt ou le serveur)
  window.ANO_UPDATE_GPS = function(lat, lon, accuracy, heading, speed) {
    lat = parseFloat(lat); lon = parseFloat(lon);
    if (isNaN(lat) || isNaN(lon)) return;
    userPos = [lat, lon];
    if (userMarker) {
      userMarker.setLatLng([lat, lon]);
      var markerEl = document.getElementById('user-me-marker');
      if (markerEl && heading !== null && heading !== undefined) {
        markerEl.style.transform = 'rotate(' + heading + 'deg)';
      }
    }
    processNavPosition(lat, lon);
  };

  window.ANO_START_NAVIGATION = function(lat, lon, name) {
    drawRoute({ lat: parseFloat(lat), lon: parseFloat(lon), name: name || 'Destination' }, null);
  };

  window.ANO_STOP_NAVIGATION = function() {
    clearRoute();
  };

  var routeClose = document.getElementById('route-close');
  if (routeClose) {
    routeClose.addEventListener('click', function (event) {
      L.DomEvent.stopPropagation(event);
      clearRoute();
    });
  }
  var navQuitBtn = document.getElementById('nav-quit-btn');
  if (navQuitBtn) {
    navQuitBtn.addEventListener('click', function (event) {
      L.DomEvent.stopPropagation(event);
      clearRoute();
    });
  }
  if (navVoiceBtn) {
    navVoiceBtn.addEventListener('click', function (event) {
      L.DomEvent.stopPropagation(event);
      voiceEnabled = !voiceEnabled;
      navVoiceBtn.textContent = voiceEnabled ? '\\ud83d\\udd0a VOIX ON' : '\\ud83d\\udd07 VOIX OFF';
      navVoiceBtn.classList.toggle('active', voiceEnabled);
    });
  }
  if (navFollowBtn) {
    navFollowBtn.addEventListener('click', function (event) {
      L.DomEvent.stopPropagation(event);
      autoFollow = !autoFollow;
      navFollowBtn.textContent = autoFollow ? '\\u2316 SUIVI ON' : '\\u2316 SUIVI OFF';
      navFollowBtn.classList.toggle('active', autoFollow);
      if (autoFollow) map.panTo(userPos, { animate: true });
    });
  }

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') clearRoute();
  });

  var markers = [];
  places.forEach(function (p, index) {
    var marker = L.marker([p.lat, p.lon], {
      icon: L.divIcon({ className: '', iconSize: [0, 0], iconAnchor: [0, 0],
                        html: buildNode(p, index).outerHTML }),
      riseOnHover: true,
      zIndexOffset: (places.length - index) * 10
    });
    marker.on('add', function () {
      var element = marker.getElement();
      if (!element) return;
      var node = element.firstElementChild;
      if (!node) return;
      node.addEventListener('click', function (event) {
        if (event.target.closest('.go')) {
          if (!HAS_ME) return;
          event.preventDefault();
          L.DomEvent.stopPropagation(event);
          drawRoute(p, node);
          return;
        }
        L.DomEvent.stopPropagation(event);
        var wasOn = node.classList.contains('on');
        document.querySelectorAll('.node.on').forEach(function (other) {
          other.classList.remove('on');
        });
        if (!wasOn) {
          node.classList.add('on');
          marker.setZIndexOffset(10000);
        } else {
          marker.setZIndexOffset((places.length - index) * 10);
        }
      });
    });

    marker.addTo(map);
    bounds.push([p.lat, p.lon]);
    markers.push(marker);
  });

  if (bounds.length > 1) {
    map.fitBounds(bounds, {
      paddingTopLeft: [140, 210],
      paddingBottomRight: [140, 70],
      maxZoom: 16
    });
  }

  var DENSE_COUNT = @@DENSE_COUNT@@;
  var DENSE_ZOOM = @@DENSE_ZOOM@@;

  function applyDensity() {
    var dense = places.length > DENSE_COUNT || map.getZoom() < DENSE_ZOOM;
    document.body.classList.toggle('dense', dense);
  }

  function overlaps(a, b, margin) {
    var m = margin === undefined ? 10 : margin;
    return !(a.right + m < b.left || a.left - m > b.right ||
             a.bottom + m < b.top || a.top - m > b.bottom);
  }

  function declutter() {
    var body = document.body;
    if (body.classList.contains('dense') || body.classList.contains('nocards')) {
      return;
    }
    var nodes = [];
    markers.forEach(function (marker) {
      var element = marker.getElement();
      var node = element && element.firstElementChild;
      if (!node || !node.querySelector('.card')) return;
      node.style.setProperty('--dy', node.dataset.dy + 'px');
      nodes.push(node);
    });
    nodes.sort(function (a, b) {
      return b.getBoundingClientRect().top - a.getBoundingClientRect().top;
    });

    var pins = nodes.map(function (node) {
      return { node: node, box: node.querySelector('.pin').getBoundingClientRect() };
    });

    var placed = [];
    nodes.forEach(function (node) {
      var card = node.querySelector('.card');
      var offset = parseFloat(node.dataset.dy) || 0;
      var obstacles = pins
        .filter(function (pin) { return pin.node !== node; })
        .map(function (pin) { return pin.box; });
      for (var step = 0; step < 8; step++) {
        var rect = card.getBoundingClientRect();
        var clash =
          placed.some(function (other) { return overlaps(rect, other); }) ||
          obstacles.some(function (box) { return overlaps(rect, box, 4); });
        if (!clash) break;
        offset += 30;
        node.style.setProperty('--dy', offset + 'px');
      }
      placed.push(card.getBoundingClientRect());
    });
  }

  var declutterPending = false;
  function scheduleDeclutter() {
    if (declutterPending) return;
    declutterPending = true;
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        declutterPending = false;
        declutter();
      });
    });
  }

  function refresh() { applyDensity(); scheduleDeclutter(); }
  map.on('zoomend', refresh);
  map.on('moveend', refresh);
  refresh();

  var toggle = document.getElementById('cards-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var hidden = document.body.classList.toggle('nocards');
      toggle.textContent = hidden ? '\\u25a2 FICHES OFF' : '\\u25a3 FICHES ON';
      if (!hidden) scheduleDeclutter();
    });
  }

  map.on('click', function () {
    document.querySelectorAll('.node.on').forEach(function (node) {
      node.classList.remove('on');
    });
  });
"""


def render_map(
    title: str,
    center: tuple[float, float],
    *,
    places: list[dict[str, Any]] | None = None,
    radius_km: float = 3.0,
    center_label: str = "Votre position",
    mark_center: bool = True,
) -> str:
    """Construit la page Leaflet de la grande carte avec support du guidage pas-à-pas."""
    payload = [_marker_payload(index, place)
               for index, place in enumerate(places or [], 1)]

    data = _json_for_script(json.dumps(payload, ensure_ascii=False))

    script = _fill(_SCRIPT, {
        "@@CENTER@@": f"[{center[0]}, {center[1]}]",
        "@@ZOOM@@": str(_zoom_for(radius_km)),
        "@@TILES@@": _TILES,
        "@@MARK_CENTER@@": str(bool(mark_center)).lower(),
        "@@RADIUS_M@@": str(int(radius_km * 1000)),
        "@@PLACES@@": data,
        "@@DENSE_COUNT@@": str(_DENSE_THRESHOLD),
        "@@DENSE_ZOOM@@": str(_DENSE_ZOOM),
        "@@OSRM@@": _OSRM,
    })

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<link rel="stylesheet" href="{_LEAFLET_CSS}"/>
<script src="{_LEAFLET_JS}"></script>
<style>{_STYLE}</style></head>
<body>
<div id="map"></div>
<div class="grid"></div>
<div class="sweep"></div>
<div class="crt"></div>
{_hud(title, center_label, len(payload))}
{_nav_hud()}
{_route_panel()}
<script>
{script}
</script></body></html>"""


def _hud(title: str, center_label: str, count: int) -> str:
    """Bandeau d'état, en haut à gauche de la carte."""
    if count:
        label = "LIEUX DÉTECTÉS"
        state = f"{count} NOEUD{'S' if count > 1 else ''} · SCAN TERMINÉ"
    else:
        label = "POSITION"
        state = "VERROUILLÉE"
    heading = _html_escape(title or center_label or "CARTE")
    toggle = ('<div class="btn" id="cards-toggle">▣ FICHES ON</div>'
              if count else "")
    return (
        '<div class="hud">'
        f'<div class="k">{label}</div>'
        f'<div class="q">{heading}</div>'
        f'<div class="s"><i></i>{_html_escape(state)}</div>'
        '<div class="bar"></div>'
        f'{toggle}'
        '</div>'
        f"<script>window.ANO_CENTER_LABEL = '{_escape(center_label)}';</script>"
    )


def _nav_hud() -> str:
    """Bannière supérieure cyberpunk pour la navigation pas-à-pas."""
    return (
        '<div class="nav-hud" id="nav-hud" hidden>'
        '<div class="nav-main">'
        '<div class="nav-icon-box" id="nav-icon-box">'
        '<svg viewBox="0 0 24 24"><path d="M12 4l-8 8h5v8h6v-8h5z"/></svg>'
        '</div>'
        '<div class="nav-content">'
        '<div class="nav-dist"><span id="nav-dist-val">--</span><small id="nav-dist-unit">M</small></div>'
        '<div class="nav-instr" id="nav-instr-txt">En attente du trajet…</div>'
        '</div>'
        '</div>'
        '<div class="nav-sub">'
        '<div class="nav-next" id="nav-next-txt">Puis continuez tout droit</div>'
        '<div class="nav-tools">'
        '<span class="nav-btn active" id="nav-voice-btn">🔊 VOIX ON</span>'
        '<span class="nav-btn active" id="nav-follow-btn">⌖ SUIVI ON</span>'
        '<span class="nav-btn danger" id="nav-quit-btn">✕</span>'
        '</div>'
        '</div>'
        '<div class="nav-reroute-toast" id="nav-reroute-toast">⚠ ÉCART DÉTECTÉ · RECALCUL</div>'
        '</div>'
    )


def _route_panel() -> str:
    """Panneau d'itinéraire récapitulatif, en bas à gauche."""
    return (
        '<div class="route" id="route" hidden>'
        '<div class="k">Itinéraire</div>'
        '<div class="q" id="route-name"></div>'
        '<div class="v" id="route-info"></div>'
        '<div class="acts">'
        '<span class="btn" id="route-close">✕ EFFACER</span>'
        '<a class="btn" id="route-ext" target="_blank" rel="noopener">MAPS ↗</a>'
        '</div></div>'
    )


def _json_for_script(data: str) -> str:
    """Rend un JSON sûr à l'intérieur d'une balise `<script>`."""
    return (
        data
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _fill(template: str, values: dict[str, str]) -> str:
    for token, value in values.items():
        template = template.replace(token, value)
    return template


def _marker_payload(index: int, place: dict[str, Any]) -> dict[str, Any]:
    return {
        "n": index,
        "name": str(place.get("name") or "Sans nom"),
        "lat": float(place.get("lat") or 0.0),
        "lon": float(place.get("lon") or 0.0),
        "dist": place.get("dist_km"),
        "address": str(place.get("address") or ""),
        "category": str(place.get("category") or ""),
        "hours": str(place.get("opening_hours") or ""),
        "phone": str(place.get("phone") or ""),
        "rating": place.get("rating"),
        "reviews": place.get("reviews"),
        "url": str(place.get("directions_url") or ""),
    }


def _escape(text: str) -> str:
    """Échappe pour une chaîne JavaScript entre apostrophes simples."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", " ")
        .replace("<", "&lt;")
    )


def _html_escape(text: str) -> str:
    """Échappe pour du texte inséré directement dans le corps de la page."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _zoom_for(radius_km: float) -> int:
    if radius_km <= 0.5:
        return 16
    if radius_km <= 2:
        return 15
    if radius_km <= 5:
        return 14
    if radius_km <= 12:
        return 13
    if radius_km <= 30:
        return 11
    return 9
