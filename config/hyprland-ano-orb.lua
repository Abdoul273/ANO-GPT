-- Bulle compagnon d'ANO-GPT — règles de fenêtre
--
-- Chargé depuis rules.lua (dernière ligne). À supprimer proprement : retirer
-- le `require("hyprland.ano-orb")` de rules.lua et effacer ce fichier.
--
-- Pourquoi le titre et non la classe : toutes les fenêtres Qt d'ANO-GPT
-- partagent le même app_id (`jarvis-dashboard`). Une règle sur la classe
-- attraperait aussi la fenêtre principale. Le titre « ANO Orb » est fixé en
-- dur dans le code (CompanionOrb.WINDOW_TITLE).
--
-- `pin` est ce qui fait suivre le bureau : une fenêtre flottante épinglée
-- reste affichée quel que soit le workspace regardé. C'est exactement le
-- comportement voulu — l'orbe apparaît là où tu es.
--
-- La fenêtre fait 348 × 120 et ne change JAMAIS de taille : c'est un masque,
-- côté application, qui rend visible et cliquable la seule partie utile.
-- D'où une position fixe, calculée depuis le coin bas-droit avec 24 px de
-- marge.

hl.window_rule({
    match            = { title = "^(ANO Orb)$" },
    float            = true,
    pin              = true,
    move             = "(monitor_w-372) (monitor_h-144)",
    size             = "348 120",
    -- Pas de `no_border` dans les règles Lua d'Hyprland (c'est une clé de la
    -- syntaxe .conf) : la bordure se retire par sa taille.
    border_size      = 0,
    no_shadow        = true,
    no_blur          = true,
    no_anim          = true,
    -- La bulle ne doit jamais prendre le clavier à ce que tu es en train de
    -- faire : elle apparaît pendant que tu travailles ailleurs.
    no_focus         = true,
    no_initial_focus = true,
    -- La règle globale du haut de rules.lua rend toutes les fenêtres
    -- translucides ; ici le fond est déjà transparent et le contenu doit
    -- rester net.
    opacity          = "1.0 1.0 override",
    rounding         = 0,
})
