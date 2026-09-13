from __future__ import annotations



from PyQt6.QtCore import (
    Qt,
    QTimer, QThread, pyqtSignal,
)
from PyQt6.QtGui import (
    QFont, QFontMetrics,
)
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ui.core.fade_widget import FadeInWidget
from ui.paths import _patch_cached_config, _read_full_config
from ui.styles.cyber import CyberHeader, cyber_section, micro_label
from ui.styles.theme import C

# Déploiements Azure proposés dans les listes. Ce sont les noms de modèle
# usuels : Azure nomme presque toujours un déploiement d'après son modèle.
AZURE_DEEP_MODELS = (
    "gpt-6-astra", "gpt-5.6-terra", "gpt-5.1", "gpt-4.1", "o4-mini", "anogpt-brain",
)
AZURE_CODE_MODELS = ("gpt-5.3-codex", "gpt-5.1-codex", "gpt-4.1", "o4-mini")
AZURE_TEXT_MODELS = ("gpt-5.1", "gpt-4.1", "gpt-4.1-mini", "gpt-4o")
AZURE_IMAGE_MODELS = ("gpt-image-2", "gpt-image-1", "dall-e-3")
AZURE_VIDEO_MODELS = ("sora-2", "sora")
AZURE_SPEECH_REGIONS = (
    "francecentral", "westeurope", "northeurope", "swedencentral", "uksouth",
    "eastus", "eastus2", "westus2", "centralus", "canadacentral",
    "japaneast", "australiaeast", "southeastasia",
)


class _KeyTestWorker(QThread):
    """Exécute test_provider_key() en arrière-plan pour ne jamais geler l'UI
    pendant un appel réseau (Gemini/Anthropic/DeepSeek/Groq/Grok)."""
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, provider: str, api_key: str, model: str, url: str, parent=None):
        super().__init__(parent)
        self._provider, self._api_key, self._model, self._url = provider, api_key, model, url

    def run(self):
        try:
            from core.llm_client import test_provider_key
            ok, msg = test_provider_key(self._provider, self._api_key, self._model, self._url)
        except Exception as e:
            ok, msg = False, f"Erreur : {e}"
        self.finished_ok.emit(ok, msg)


class _OpenRouterCatalogWorker(QThread):
    """Charge le vaste catalogue OpenRouter hors du thread Qt/audio."""
    finished_ok = pyqtSignal(list, str)

    def __init__(self, api_key: str, parent=None):
        super().__init__(parent)
        self._api_key = api_key

    def run(self):
        try:
            from core.llm_client import openrouter_models
            models, note = openrouter_models(self._api_key), ""
        except Exception as exc:
            models, note = [], str(exc)
        self.finished_ok.emit(models, note)


def _shrinkable(combo: QComboBox) -> None:
    """Empêche une liste d'imposer au panneau la largeur de son plus long nom.

    Par défaut, Qt dimensionne une QComboBox sur son entrée la plus longue. Avec
    le catalogue Azure — une centaine de noms, certains très longs — le contenu
    devenait plus large que le panneau et débordait hors du cadre.
    """
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(12)
    combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)


class _AzureCatalogWorker(QThread):
    """Va chercher le catalogue Azure hors du thread Qt : l'appel prend
    plusieurs secondes et gèlerait l'interface — donc la voix avec elle."""
    finished_ok = pyqtSignal(dict, str)

    def __init__(self, endpoint: str, api_key: str, parent=None):
        super().__init__(parent)
        self._endpoint, self._api_key = endpoint, api_key

    def run(self):
        try:
            from core.llm_client import azure_catalog
            roles = azure_catalog(self._endpoint, self._api_key, force=True)
            note = ""
        except Exception as exc:
            roles, note = {}, str(exc)
        self.finished_ok.emit(roles, note)


class _AzureProbeWorker(QThread):
    """Vérifie qu'un déploiement répond vraiment avant d'annoncer un succès."""
    finished_ok = pyqtSignal(bool, str)

    def __init__(self, endpoint: str, api_key: str, deployment: str, parent=None):
        super().__init__(parent)
        self._endpoint, self._api_key, self._deployment = endpoint, api_key, deployment

    def run(self):
        try:
            from core.llm_client import probe_azure_deployment
            ok, msg = probe_azure_deployment(self._endpoint, self._api_key, self._deployment)
        except Exception as exc:
            ok, msg = False, f"Erreur : {exc}"
        self.finished_ok.emit(ok, msg)


class AIConfigOverlay(FadeInWidget):
    """Panneau premium de configuration multi-provider IA : Ollama (local),
    DeepSeek, Gemini, Grok, Groq, Anthropic, OpenAI-compatible. Chaque clé
    peut être testée puis appliquée immédiatement, sans redémarrage."""
    provider_changed = pyqtSignal(str)
    voice_key_changed = pyqtSignal()
    _OW, _OH = 500, 680

    def __init__(self, parent=None):
        super().__init__(parent, duration=300)
        self._worker: _KeyTestWorker | None = None
        self._openrouter_worker: _OpenRouterCatalogWorker | None = None
        self._openrouter_models: list[str] = []
        self._selected_provider = "gemini"
        self._provider_btns: dict[str, QPushButton] = {}

        shell = QVBoxLayout(self)
        shell.setContentsMargins(20, 16, 20, 16)
        shell.setSpacing(8)
        # L'en-tête est posé hors de la zone défilante : un contenu plus large
        # que le panneau poussait la croix hors du cadre, et le réglage ne se
        # fermait plus. Épinglé ici, il reste atteignable quoi qu'il arrive.
        header = CyberHeader("Configurer l'IA", "CERVEAU", parent=self)
        header.close_clicked.connect(self.hide)
        shell.addWidget(header)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body = QWidget(self)
        body.setStyleSheet("background: transparent;")
        scroll.setWidget(body)
        shell.addWidget(scroll)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(0, 0, 6, 0)
        outer.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignLeft):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Inter", fs, QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        outer.addWidget(micro_label("Fournisseur"))

        # ── Grille de providers (chips) ─────────────────────────────────────
        grid = QGridLayout(); grid.setSpacing(6)
        from core.llm_client import PROVIDERS
        self._PROVIDERS = PROVIDERS
        for i, (pid, info) in enumerate(PROVIDERS.items()):
            btn = QPushButton(info["label"])
            btn.setCheckable(True)
            btn.setFixedHeight(30)
            btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, p=pid: self._select_provider(p))
            grid.addWidget(btn, i // 2, i % 2)
            self._provider_btns[pid] = btn
        outer.addLayout(grid)
        outer.addSpacing(6)

        # ── Panneau dynamique (dépend du provider sélectionné) ──────────────
        self._panel = cyber_section()
        self._panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        pl = QVBoxLayout(self._panel)
        pl.setContentsMargins(12, 10, 12, 10)
        pl.setSpacing(6)

        self._key_label = micro_label("Clé API")
        pl.addWidget(self._key_label)
        key_row = QHBoxLayout(); key_row.setSpacing(4)
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("Collez votre clé API ici…")
        self._key_input.setFont(QFont("Inter", 10))
        self._key_input.setFixedHeight(30)
        key_row.addWidget(self._key_input)
        self._show_key_btn = QPushButton("👁")
        self._show_key_btn.setFixedSize(30, 30)
        self._show_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._show_key_btn.clicked.connect(self._toggle_key_visibility)
        key_row.addWidget(self._show_key_btn)
        pl.addLayout(key_row)

        # Gemini seulement : une seconde clé, réservée à la voix (Gemini Live).
        # Un second projet Google gratuit y suffit : la voix reste gratuite,
        # pendant que la clé principale (payante) sert à la vision, au coach
        # TikTok et aux résumés.
        self._voice_widgets: list[QWidget] = []
        self._voice_key_label = micro_label("Clé Gemini — voix (Gemini Live), second compte gratuit · optionnel")
        pl.addWidget(self._voice_key_label)
        self._voice_widgets.append(self._voice_key_label)
        voice_row = QHBoxLayout(); voice_row.setSpacing(4)
        self._voice_key_input = QLineEdit()
        self._voice_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._voice_key_input.setPlaceholderText("Vide = la voix utilise la clé principale")
        self._voice_key_input.setFont(QFont("Inter", 10))
        self._voice_key_input.setFixedHeight(30)
        self._voice_key_input.setToolTip(
            "Clé d'un second projet Google AI Studio (jamais rechargé) : la session vocale "
            "Gemini Live l'utilise seule. Tout le reste (vision, coach, résumés) garde la clé "
            "principale ci-dessus."
        )
        voice_row.addWidget(self._voice_key_input)
        self._show_voice_key_btn = QPushButton("👁")
        self._show_voice_key_btn.setFixedSize(30, 30)
        self._show_voice_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._show_voice_key_btn.clicked.connect(self._toggle_voice_key_visibility)
        voice_row.addWidget(self._show_voice_key_btn)
        self._test_voice_btn = QPushButton("◎ VOIX")
        self._test_voice_btn.setFixedSize(64, 30)
        self._test_voice_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._test_voice_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._test_voice_btn.setToolTip("Vérifie cette clé auprès de Google avant de l'enregistrer.")
        self._test_voice_btn.clicked.connect(self._test_voice_key)
        voice_row.addWidget(self._test_voice_btn)
        voice_holder = QWidget(); voice_holder.setLayout(voice_row)
        voice_holder.setStyleSheet("background: transparent;")
        pl.addWidget(voice_holder)
        self._voice_widgets.append(voice_holder)
        self._voice_hint = QLabel(
            "Recharge la clé principale (vision, coach TikTok, modèles Pro) ; laisse ce second "
            "compte gratuit : la voix ne te coûtera rien."
        )
        self._voice_hint.setWordWrap(True)
        self._voice_hint.setFont(QFont("Inter", 8))
        self._voice_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        pl.addWidget(self._voice_hint)
        self._voice_widgets.append(self._voice_hint)

        self._url_label = micro_label("URL du serveur")
        pl.addWidget(self._url_label)
        self._url_input = QLineEdit()
        self._url_input.setFont(QFont("Inter", 10))
        self._url_input.setFixedHeight(30)
        pl.addWidget(self._url_input)

        # Ne pas appeler ce widget ``_model_label`` : ce nom appartient aussi
        # à la méthode statique qui formate les entrées de la liste. La
        # collision masquait la méthode et empêchait l'ouverture complète du
        # panneau (« QLabel object is not callable »).
        self._model_title = micro_label("Modèle")
        pl.addWidget(self._model_title)
        # On choisit dans une liste. La saisie libre n'est rouverte que pour les
        # fournisseurs sans catalogue (serveur perso, Ollama non répertorié) :
        # ailleurs, taper un identifiant à la main ne produisait que des fautes.
        self._model_input = QComboBox()
        self._model_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._model_input.setFont(QFont("Inter", 10))
        self._model_input.setFixedHeight(30)
        self._model_input.setMaxVisibleItems(14)
        _shrinkable(self._model_input)
        pl.addWidget(self._model_input)

        self._openrouter_refresh_btn = QPushButton("↻  CHARGER TOUS LES MODÈLES OPENROUTER")
        self._openrouter_refresh_btn.setFixedHeight(28)
        self._openrouter_refresh_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._openrouter_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._openrouter_refresh_btn.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._openrouter_refresh_btn.setToolTip(
            "Lit le catalogue OpenRouter à la demande ; aucun appel réseau à l'ouverture.")
        self._openrouter_refresh_btn.clicked.connect(self._load_openrouter_catalog)
        pl.addWidget(self._openrouter_refresh_btn)

        # Champs Azure séparés : Azure OpenAI et Azure Speech n'utilisent pas
        # la même ressource ni la même clé. Ils n'apparaissent que pour Azure.
        self._azure_widgets: list[QWidget] = []
        def _azure_label(text: str):
            label = micro_label(text)
            pl.addWidget(label)
            self._azure_widgets.append(label)
            return label
        def _azure_input(placeholder: str, *, secret: bool = False):
            field = QLineEdit()
            field.setFixedHeight(30)
            field.setFont(QFont("Inter", 9))
            field.setPlaceholderText(placeholder)
            if secret:
                field.setEchoMode(QLineEdit.EchoMode.Password)
            pl.addWidget(field)
            self._azure_widgets.append(field)
            return field
        # Rôle du modèle -> clé du catalogue Azure, et repli si le réseau tombe.
        self._azure_role_combos: dict[QComboBox, tuple[str, tuple, bool]] = {}
        def _azure_model_combo(role: str, fallback, *, optional: bool):
            """Un déploiement se choisit dans la liste, il ne se tape plus.

            Les noms de déploiement Azure sont longs et sans indulgence : une
            lettre de travers et l'appel part en 404 sans que rien ne le dise.
            La liste vient de la ressource elle-même, pas d'un catalogue figé.
            """
            combo = QComboBox()
            combo.setFixedHeight(30)
            combo.setFont(QFont("Inter", 9))
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.setMaxVisibleItems(14)
            _shrinkable(combo)
            pl.addWidget(combo)
            self._azure_widgets.append(combo)
            self._azure_role_combos[combo] = (role, tuple(fallback), optional)
            return combo

        _azure_label("Azure Speech — clé API (F0 ou S0)")
        self._azure_speech_key = _azure_input("Clé de la ressource Speech…", secret=True)
        _azure_label("Région Speech")
        self._azure_speech_region = QComboBox()
        self._azure_speech_region.setFixedHeight(30)
        self._azure_speech_region.setFont(QFont("Inter", 9))
        _shrinkable(self._azure_speech_region)
        for _region in AZURE_SPEECH_REGIONS:
            self._azure_speech_region.addItem(_region, _region)
        pl.addWidget(self._azure_speech_region)
        self._azure_widgets.append(self._azure_speech_region)
        _azure_label("Profil Speech")
        self._azure_speech_tier = QComboBox()
        self._azure_speech_tier.setFixedHeight(30)
        _shrinkable(self._azure_speech_tier)
        self._azure_speech_tier.addItem("F0 — gratuit, validation ponctuelle", "f0")
        self._azure_speech_tier.addItem("S0 — standard, capacité supérieure", "s0")
        pl.addWidget(self._azure_speech_tier)
        self._azure_widgets.append(self._azure_speech_tier)
        self._azure_speech_verify = QCheckBox("Second avis STT par Azure Speech")
        pl.addWidget(self._azure_speech_verify)
        self._azure_widgets.append(self._azure_speech_verify)

        _azure_label("Déploiement raisonnement profond")
        self._azure_deep_model = _azure_model_combo("chat", AZURE_DEEP_MODELS, optional=False)
        _azure_label("Déploiement code")
        self._azure_code_model = _azure_model_combo("chat", AZURE_CODE_MODELS, optional=True)
        _azure_label("Déploiement documents")
        self._azure_document_model = _azure_model_combo("chat", AZURE_TEXT_MODELS, optional=True)
        _azure_label("Déploiement images")
        self._azure_image_model = _azure_model_combo("image", AZURE_IMAGE_MODELS, optional=True)
        _azure_label("Déploiement vidéo")
        self._azure_video_model = _azure_model_combo("video", AZURE_VIDEO_MODELS, optional=True)

        self._azure_refresh_btn = QPushButton("↻  ACTUALISER LE CATALOGUE")
        self._azure_refresh_btn.setFixedHeight(28)
        self._azure_refresh_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._azure_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._azure_refresh_btn.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._azure_refresh_btn.setToolTip(
            "Demande à ta ressource Azure la liste des modèles qu'elle peut servir.")
        self._azure_refresh_btn.clicked.connect(lambda: self._load_azure_catalog(force=True))
        pl.addWidget(self._azure_refresh_btn)
        self._azure_widgets.append(self._azure_refresh_btn)

        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setFont(QFont("Inter", 8))
        self._status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        pl.addWidget(self._status_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        self._test_btn = QPushButton("◎  TESTER")
        self._test_btn.setFixedHeight(32)
        self._test_btn.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        self._test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._test_btn.clicked.connect(self._test_key)
        btn_row.addWidget(self._test_btn)

        self._save_key_btn = QPushButton("◇  ENREGISTRER")
        self._save_key_btn.setFixedHeight(32)
        self._save_key_btn.setFont(QFont("Inter", 8, QFont.Weight.Bold))
        self._save_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_key_btn.clicked.connect(self._save_provider_credentials)
        btn_row.addWidget(self._save_key_btn)

        self._apply_btn = QPushButton("▸  APPLIQUER CE PROVIDER")
        self._apply_btn.setFixedHeight(32)
        self._apply_btn.setFont(QFont("Inter", 9, QFont.Weight.Bold))
        self._apply_btn.setObjectName("CyberPrimary")
        self._apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_btn.clicked.connect(self._apply_provider)
        btn_row.addWidget(self._apply_btn)
        pl.addLayout(btn_row)

        outer.addWidget(self._panel)
        outer.addStretch()


        self._refresh_provider_list()
        self._select_provider(self._active_provider_id())

    # ── Helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _combo_value(combo: QComboBox) -> str:
        """Valeur d'une liste, qu'elle soit à choix fermé ou éditable."""
        data = combo.currentData()
        if data is not None and not combo.isEditable():
            return str(data).strip()
        return combo.currentText().strip()

    @staticmethod
    def _select_combo(combo: QComboBox, value: str) -> None:
        """Positionne une liste sur `value`, en l'ajoutant si elle l'ignore.

        Un déploiement enregistré autrefois doit rester sélectionné même si le
        catalogue livré ne le contient plus : sinon, rouvrir le panneau
        réécrirait silencieusement la configuration de l'utilisateur.
        """
        value = (value or "").strip()
        combo.blockSignals(True)
        index = combo.findData(value)
        if index < 0:
            index = combo.findText(value)
        if index < 0 and value:
            combo.addItem(value, value)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def _model_text(self) -> str:
        return self._combo_value(self._model_input)

    def _set_model_choices(self, pid: str, current: str) -> None:
        """Remplit la liste des modèles connus et s'y positionne."""
        try:
            if pid == "azure_openai":
                # Azure sert bien plus que les modèles OpenAI : Grok, Claude,
                # DeepSeek, Llama… Le catalogue figé n'en montrait qu'une part.
                from core.llm_client import azure_catalog
                catalog = azure_catalog(
                    self._url_input.text().strip(), self._key_input.text().strip(),
                )
                choices = list(catalog.get("chat") or [])
                deployed = set(catalog.get("deployed") or ())
                served = catalog.get("served") or {}
                no_quota = set(catalog.get("no_quota") or ())
            else:
                from core.llm_client import models_for
                choices = models_for(pid)
                deployed, served, no_quota = set(choices), {}, set()
        except Exception:
            choices, deployed, served, no_quota = [], set(), {}, set()
        self._model_input.blockSignals(True)
        # OpenRouter est volontairement fermé à la saisie : son catalogue
        # complet est téléchargé pour que l'utilisateur n'ait aucun identifiant
        # technique à connaître. Le serveur personnel reste le seul cas libre.
        free_text = pid == "custom" or (pid != "openrouter" and len(choices) <= 1)
        self._model_input.setEditable(free_text)
        self._model_input.clear()
        for name in choices:
            self._model_input.addItem(
                self._model_label(name, deployed, served, no_quota), name)
        self._model_input.blockSignals(False)
        if free_text:
            self._model_input.setEditText(current)
        else:
            self._select_combo(self._model_input, current)

    def _load_openrouter_catalog(self) -> None:
        """Actualise explicitement la liste complète sans bloquer Qt ni la voix."""
        if self._selected_provider != "openrouter":
            return
        if not self._key_input.text().strip():
            self._set_status("Ajoutez d'abord votre clé OpenRouter.", C.ACC2)
            return
        self._openrouter_refresh_btn.setEnabled(False)
        self._model_input.setEnabled(False)
        self._set_status("◌ Lecture du catalogue OpenRouter…", C.TEXT_DIM)
        self._openrouter_worker = _OpenRouterCatalogWorker(
            self._key_input.text().strip(), parent=self)
        self._openrouter_worker.finished_ok.connect(self._on_openrouter_catalog_loaded)
        self._openrouter_worker.start()

    def _on_openrouter_catalog_loaded(self, models: list, note: str) -> None:
        self._openrouter_refresh_btn.setEnabled(True)
        self._model_input.setEnabled(True)
        if not models:
            self._set_status(f"✗ Catalogue OpenRouter illisible : {note or 'aucun modèle'}", C.RED)
            return
        previous = self._model_text()
        self._openrouter_models = list(models)
        self._model_input.blockSignals(True)
        self._model_input.clear()
        for model in models:
            self._model_input.addItem(model, model)
        self._model_input.blockSignals(False)
        self._select_combo(self._model_input, previous)
        self._set_status(f"✓ {len(models)} modèles OpenRouter chargés.", C.GREEN)

    def _active_provider_id(self) -> str:
        try:
            from core.llm_client import get_llm_provider
            return get_llm_provider()
        except Exception:
            return "gemini"

    def _chip_style(self, active: bool, has_key: bool) -> str:
        """Le liseré gauche dit d'un coup d'œil si une clé est déjà en place."""
        if active:
            return (
                "QPushButton { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
                " stop:0 rgba(0, 212, 255, 0.30), stop:1 rgba(255, 43, 214, 0.14));"
                f" color: {C.PRI}; border: 1px solid {C.PRI};"
                f" border-left: 2px solid {C.PRI}; border-radius: 4px; }}"
            )
        dot_color = C.GREEN if has_key else C.TEXT_DIM
        return (f"QPushButton {{ background: rgba(14, 23, 36, 0.92); color: {C.TEXT_MED}; "
                f"border: 1px solid rgba(0, 212, 255, 0.30); "
                f"border-left: 2px solid {dot_color}; border-radius: 4px; }}"
                f"QPushButton:hover {{ border-color: {C.PRI}; color: {C.WHITE}; "
                f"border-left: 2px solid {dot_color}; }}")

    def _refresh_provider_list(self):
        try:
            from core.llm_client import list_providers
            states = {p["id"]: p for p in list_providers()}
        except Exception:
            states = {}
        for pid, btn in self._provider_btns.items():
            st = states.get(pid, {})
            active = st.get("active", pid == self._selected_provider)
            has_key = st.get("has_key", False)
            btn.setChecked(pid == self._selected_provider)
            btn.setStyleSheet(self._chip_style(pid == self._selected_provider, has_key))
            suffix = "  ✓ actif" if active else ("  •" if has_key else "")
            self._set_chip_text(btn, f"{self._PROVIDERS[pid]['label']}{suffix}")

    @staticmethod
    def _set_chip_text(btn: QPushButton, text: str) -> None:
        """Tronque proprement un intitulé plus long que sa pastille.

        Deux colonnes de pastilles dans un panneau étroit : « Serveur compatible
        OpenAI » ne tenait pas et se faisait couper en pleine lettre. Une ellipse
        vaut mieux qu'un mot amputé.
        """
        btn.setProperty("fullText", text)
        # 14 px de marge intérieure de chaque côté (feuille de style commune),
        # plus une marge de sécurité : sans elle, l'ellipse tombait pile à la
        # limite et le texte se faisait quand même rogner.
        available = btn.width() - 34
        if available <= 40:
            btn.setText(text)
            return
        btn.setText(QFontMetrics(btn.font()).elidedText(
            text, Qt.TextElideMode.ElideRight, available))

    def resizeEvent(self, event):
        # La largeur des pastilles ne se connaît qu'une fois le panneau posé.
        super().resizeEvent(event)
        for btn in self._provider_btns.values():
            full = btn.property("fullText")
            if full:
                self._set_chip_text(btn, full)

    def _select_provider(self, pid: str):
        self._selected_provider = pid
        info = self._PROVIDERS[pid]
        for p, btn in self._provider_btns.items():
            btn.setChecked(p == pid)
        self._refresh_provider_list()

        cfg = _read_full_config()
        if pid == "auto":
            for widget in (
                self._key_label, self._key_input, self._show_key_btn,
                self._save_key_btn, self._url_label, self._url_input,
                self._model_title, self._model_input, self._openrouter_refresh_btn,
            ):
                widget.setVisible(False)
            for widget in self._azure_widgets:
                widget.setVisible(False)
            for widget in self._voice_widgets:
                widget.setVisible(False)
            try:
                from core.llm_client import PROVIDERS, configured_brain_providers
                order = configured_brain_providers()
            except Exception:
                order = []
            if order:
                labels = " → ".join(PROVIDERS[item]["label"] for item in order)
                self._status_lbl.setText(f"Ordre retenu : {labels}.")
            else:
                self._status_lbl.setText(
                    "Aucune clé enregistrée : ajoutez-en une pour que le mode "
                    "automatique ait de quoi choisir."
                )
            self._status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            return
        self._model_title.setVisible(True)
        self._model_input.setVisible(True)
        self._openrouter_refresh_btn.setVisible(pid == "openrouter")
        self._key_label.setVisible(info["needs_key"] or info["key_field"] is not None)
        self._key_input.setVisible(info["needs_key"] or info["key_field"] is not None)
        self._show_key_btn.setVisible(info["needs_key"] or info["key_field"] is not None)
        self._save_key_btn.setVisible(info.get("key_field") is not None)
        if info.get("key_field"):
            self._key_input.setText(cfg.get(info["key_field"], ""))
        else:
            self._key_input.clear()
        self._key_input.setPlaceholderText(
            "Collez votre clé API ici…" if info["needs_key"] else "Optionnelle selon votre serveur"
        )

        is_gemini = pid == "gemini"
        for widget in self._voice_widgets:
            widget.setVisible(is_gemini)
        if is_gemini:
            self._voice_key_input.setText(cfg.get("gemini_live_api_key", "") or "")

        self._url_label.setVisible(info["url_editable"])
        self._url_input.setVisible(info["url_editable"])
        if info["url_editable"]:
            field = "azure_openai_endpoint" if pid == "azure_openai" else "llm_url"
            self._url_input.setText(cfg.get(field, info["default_url"]))

        active_model = cfg.get(f"{pid}_model", "") or (
            cfg.get("llm_model", "") if pid == cfg.get("llm_provider", "gemini") else ""
        )
        self._set_model_choices(pid, active_model or info["default_model"])
        if pid == "openrouter":
            if self._openrouter_models:
                # Le catalogue reste disponible pendant toute l'ouverture du
                # panneau ; pas de second appel réseau à chaque clic.
                self._on_openrouter_catalog_loaded(self._openrouter_models, "")
            elif self._key_input.text().strip():
                # Chargement différé : Qt finit d'abord de poser le panneau.
                QTimer.singleShot(0, self._load_openrouter_catalog)
            else:
                self._model_input.setEnabled(False)
        else:
            self._model_input.setEnabled(True)
        is_azure = pid == "azure_openai"
        for widget in self._azure_widgets:
            widget.setVisible(is_azure)
        if is_azure:
            self._load_azure_catalog()
            self._azure_speech_key.setText(cfg.get("azure_speech_key", ""))
            self._select_combo(self._azure_speech_region,
                               str(cfg.get("azure_speech_region", "")).lower())
            tier = str(cfg.get("azure_speech_tier", "f0")).lower()
            self._azure_speech_tier.setCurrentIndex(max(0, self._azure_speech_tier.findData(tier)))
            self._azure_speech_verify.setChecked(bool(cfg.get("azure_speech_verify", False)))
            self._select_combo(self._azure_deep_model, cfg.get("azure_deep_model", ""))
            self._select_combo(self._azure_code_model, cfg.get("azure_code_model", ""))
            self._select_combo(self._azure_document_model, cfg.get("azure_document_model", ""))
            self._select_combo(self._azure_image_model, cfg.get("azure_image_model", ""))
            self._select_combo(self._azure_video_model, cfg.get("azure_video_model", ""))
        if pid != "openrouter":
            self._status_lbl.setText("")
            self._status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

    @staticmethod
    def _model_label(name: str, deployed: set, served: dict | None = None,
                     no_quota: set | None = None) -> str:
        """Un modèle non déployé répond ``DeploymentNotFound`` : ça se dit ici.

        Le libellé change, jamais la donnée : la configuration enregistrée reste
        le nom exact du déploiement. Un déploiement au nom libre affiche en plus
        le modèle qu'il sert, sinon « anogpt-brain » ne dit rien à personne.
        """
        served = served or {}
        if name not in deployed:
            # Un modèle peut tourner sous un déploiement au nom libre : le dire,
            # sinon « gpt-5.6-terra · à déployer » contredit « anogpt-brain ».
            host = next((dep for dep, mdl in served.items() if mdl == name), "")
            if host:
                return f"{name}  ·  servi par {host}"
            # Sans quota, aucun déploiement n'est possible : « à déployer »
            # enverrait l'utilisateur vers une commande vouée à l'échec.
            if name in (no_quota or ()):
                return f"{name}  ·  quota nul, indisponible"
            return f"{name}  ·  à déployer"
        model = served.get(name)
        return f"{name}  ({model})" if model else name

    def _fill_azure_combos(self, roles: dict) -> None:
        """Remplit chaque liste Azure pour son rôle, sélection conservée."""
        deployed = set(roles.get("deployed") or ())
        served = roles.get("served") or {}
        no_quota = set(roles.get("no_quota") or ())
        for combo, (role, fallback, optional) in self._azure_role_combos.items():
            choices = list(roles.get(role) or fallback)
            previous = self._combo_value(combo)
            combo.blockSignals(True)
            combo.clear()
            if optional:
                combo.addItem("— aucun —", "")
            for name in choices:
                combo.addItem(self._model_label(name, deployed, served, no_quota), name)
            combo.blockSignals(False)
            self._select_combo(combo, previous)

    def _load_azure_catalog(self, *, force: bool = False) -> None:
        """Interroge la ressource Azure ; sans clé ni endpoint, on ne peut rien."""
        endpoint = self._url_input.text().strip()
        api_key = self._key_input.text().strip()
        if not (endpoint and api_key):
            self._fill_azure_combos({})
            if force:
                self._set_status(
                    "Renseigne l'endpoint et la clé Azure avant d'actualiser.", C.ACC2)
            return
        if not force:
            # Au premier affichage, le cache disque suffit : pas d'appel réseau
            # à l'ouverture du panneau, la voix garde le CPU pour elle.
            try:
                from core.llm_client import azure_catalog
                self._fill_azure_combos(azure_catalog(endpoint, api_key))
            except Exception:
                self._fill_azure_combos({})
            return
        self._azure_refresh_btn.setEnabled(False)
        self._set_status("◌ Lecture du catalogue Azure…", C.TEXT_DIM)
        self._catalog_worker = _AzureCatalogWorker(endpoint, api_key, parent=self)
        self._catalog_worker.finished_ok.connect(self._on_catalog_loaded)
        self._catalog_worker.start()

    def _on_catalog_loaded(self, roles: dict, note: str) -> None:
        self._azure_refresh_btn.setEnabled(True)
        if not roles:
            self._set_status(f"✗ Catalogue Azure illisible : {note or 'aucune réponse'}", C.RED)
            return
        self._fill_azure_combos(roles)
        deployed = roles.get("deployed") or []
        total = sum(len(items) for role, items in roles.items()
                    if role not in ("deployed", "served", "no_quota"))
        self._set_status(
            f"✓ {len(deployed)} déploiements prêts, {total} modèles au catalogue. "
            "Les prêts sont en tête ; ceux marqués « à déployer » répondront "
            "seulement après création du déploiement.", C.GREEN)

    def _set_status(self, text: str, color: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(f"color: {color}; background: transparent;")

    def _verify_azure_deployment(self) -> None:
        """Confirme que le déploiement choisi répond vraiment.

        Le catalogue dit ce qu'Azure *peut* servir ; seul cet appel dit ce qui
        est déployé. Sans lui, l'échec n'apparaissait qu'à la première commande
        vocale, très loin de ce panneau.
        """
        deployment = self._combo_value(self._azure_deep_model) or self._model_text()
        self._probe_worker = _AzureProbeWorker(
            self._url_input.text().strip(), self._key_input.text().strip(),
            deployment, parent=self)
        self._probe_worker.finished_ok.connect(self._on_probe_finished)
        self._probe_worker.start()

    def _on_probe_finished(self, ok: bool, msg: str) -> None:
        prefix = "✓ Vérifié — " if ok else "⚠ Enregistré, mais "
        self._set_status(prefix + msg, C.GREEN if ok else C.ACC2)

    def _toggle_key_visibility(self):
        if self._key_input.echoMode() == QLineEdit.EchoMode.Password:
            self._key_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._show_key_btn.setText("🙈")
        else:
            self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._show_key_btn.setText("👁")

    def _azure_values(self) -> dict:
        """Valeurs locales Azure ; elles ne sont jamais écrites dans les logs."""
        return {
            "openai_key": self._key_input.text().strip(),
            "endpoint": self._url_input.text().strip(),
            "deployment": self._model_text(),
            "speech_key": self._azure_speech_key.text().strip(),
            "speech_region": self._combo_value(self._azure_speech_region),
            "speech_tier": self._azure_speech_tier.currentData(),
            "speech_verify": self._azure_speech_verify.isChecked(),
            "deep_model": self._combo_value(self._azure_deep_model),
            "code_model": self._combo_value(self._azure_code_model),
            "document_model": self._combo_value(self._azure_document_model),
            "image_model": self._combo_value(self._azure_image_model),
            "video_model": self._combo_value(self._azure_video_model),
        }

    def _toggle_voice_key_visibility(self):
        hidden = self._voice_key_input.echoMode() == QLineEdit.EchoMode.Password
        self._voice_key_input.setEchoMode(
            QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)

    def _test_voice_key(self):
        key = self._voice_key_input.text().strip()
        if not key:
            self._set_status("⚠ Colle d'abord la clé du second compte.", C.ACC2)
            return
        self._test_voice_btn.setEnabled(False)
        self._set_status("◌ Vérification de la clé voix auprès de Google…", C.TEXT_DIM)
        from core.live_model_policy import BALANCED_MODEL
        self._voice_worker = _KeyTestWorker("gemini", key, BALANCED_MODEL, "", parent=self)
        self._voice_worker.finished_ok.connect(self._on_voice_test_finished)
        self._voice_worker.start()

    def _on_voice_test_finished(self, ok: bool, msg: str):
        self._test_voice_btn.setEnabled(True)
        self._set_status(
            f"✓ Clé voix valide — {msg}" if ok else f"✗ Clé voix refusée — {msg}",
            C.GREEN if ok else C.RED,
        )

    def _persist_voice_key(self) -> bool:
        """Écrit la clé voix ; True si elle a changé (la voix doit se reconnecter)."""
        if self._selected_provider != "gemini":
            return False
        key = self._voice_key_input.text().strip()
        before = str(_read_full_config().get("gemini_live_api_key", "") or "").strip()
        if key == before:
            return False
        from core.llm_client import _write_config_patch
        if not _write_config_patch({"gemini_live_api_key": key}):
            raise RuntimeError("écriture de la clé voix refusée")
        _patch_cached_config({"gemini_live_api_key": key})
        return True

    def _test_key(self):
        pid = self._selected_provider
        info = self._PROVIDERS[pid]
        api_key = self._key_input.text().strip()
        model   = self._model_text() or info["default_model"]
        url     = self._url_input.text().strip() if info["url_editable"] else None
        if info["needs_key"] and not api_key:
            self._status_lbl.setText("⚠ Entrez une clé API avant de tester.")
            self._status_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
            return
        self._test_btn.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._status_lbl.setText("◌ Test en cours…")
        self._status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        self._worker = _KeyTestWorker(pid, api_key, model, url or "", parent=self)
        self._worker.finished_ok.connect(self._on_test_finished)
        self._worker.start()

    def _on_test_finished(self, ok: bool, msg: str):
        self._test_btn.setEnabled(True)
        self._apply_btn.setEnabled(True)
        icon = "✓" if ok else "✗"
        color = C.GREEN if ok else C.RED
        self._status_lbl.setText(f"{icon} {msg}")
        self._status_lbl.setStyleSheet(f"color: {color}; background: transparent;")

    def _save_provider_credentials(self):
        """Mémorise une clé sans forcer ce fournisseur comme cerveau actif."""
        pid = self._selected_provider
        info = self._PROVIDERS[pid]
        api_key = self._key_input.text().strip()
        model = self._model_text() or info["default_model"]
        if not info.get("key_field"):
            self._status_lbl.setText("Ce mode ne possède pas de clé à enregistrer.")
            return
        if info["needs_key"] and not api_key:
            self._status_lbl.setText("⚠ Entrez une clé API avant de l’enregistrer.")
            self._status_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
            return
        try:
            if pid == "azure_openai":
                from core.llm_client import save_azure_configuration
                if not save_azure_configuration(**self._azure_values()):
                    raise RuntimeError("écriture refusée")
                _patch_cached_config(self._azure_values() | {
                    "azure_openai_api_key": api_key,
                    "azure_openai_endpoint": self._url_input.text().strip().rstrip("/"),
                    "azure_openai_model": model,
                    "azure_speech_key": self._azure_speech_key.text().strip(),
                    "azure_speech_region": self._combo_value(self._azure_speech_region).lower(),
                    "azure_speech_tier": self._azure_speech_tier.currentData(),
                    "azure_speech_verify": self._azure_speech_verify.isChecked(),
                })
                self._status_lbl.setText("✓ Configuration Azure enregistrée.")
                self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
                self._refresh_provider_list()
                return
            from core.llm_client import save_provider_config
            url = self._url_input.text().strip() if info["url_editable"] else None
            if not save_provider_config(pid, api_key, model, url):
                raise RuntimeError("écriture refusée")
            _patch_cached_config({
                info["key_field"]: api_key,
                f"{pid}_model": model,
                **({"azure_openai_endpoint" if pid == "azure_openai" else "llm_url": url.rstrip("/")}
                   if url and info["url_editable"] else {}),
            })
            voice_changed = self._persist_voice_key()
            self._status_lbl.setText(
                f"✓ Clé {info['label']} enregistrée. Le mode Auto conserve son ordre de priorité."
                + (" Clé voix enregistrée : la voix se reconnecte avec le second compte."
                   if voice_changed else "")
            )
            self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
            self._refresh_provider_list()
            if voice_changed:
                self.voice_key_changed.emit()
        except Exception as exc:
            self._status_lbl.setText(f"✗ Échec de l’enregistrement : {exc}")
            self._status_lbl.setStyleSheet(f"color: {C.RED}; background: transparent;")

    def _apply_provider(self):
        pid  = self._selected_provider
        info = self._PROVIDERS[pid]
        if pid == "auto":
            try:
                from core.llm_client import main_brain_label, set_active_provider
                if not set_active_provider("auto"):
                    raise RuntimeError("changement de fournisseur refusé")
                _patch_cached_config({"llm_provider": "auto", "brain_provider": "auto"})
                self._status_lbl.setText(
                    f"✓ Mode automatique appliqué — cerveau actuel : {main_brain_label()}."
                )
                self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
                self._refresh_provider_list()
                self.provider_changed.emit(pid)
            except Exception as exc:
                self._status_lbl.setText(f"✗ Échec de l'application : {exc}")
                self._status_lbl.setStyleSheet(f"color: {C.RED}; background: transparent;")
            return
        api_key = self._key_input.text().strip()
        model   = self._model_text() or info["default_model"]
        url     = self._url_input.text().strip() if info["url_editable"] else None

        if info["needs_key"] and not api_key:
            self._status_lbl.setText("⚠ Une clé API est requise pour ce provider.")
            self._status_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
            return
        try:
            from core.llm_client import save_provider_key, set_active_provider
            if pid == "azure_openai":
                from core.llm_client import save_azure_configuration
                if not save_azure_configuration(**self._azure_values()):
                    raise RuntimeError("enregistrement Azure refusé")
            elif info.get("key_field") and api_key:
                if not save_provider_key(pid, api_key):
                    raise RuntimeError("enregistrement de la clé refusé")
            if not set_active_provider(pid, model=model, url=url):
                raise RuntimeError("changement de fournisseur refusé")
            patch = {
                "llm_provider": pid,
                "llm_model": model,
                f"{pid}_model": model,
            }
            if info.get("key_field") and api_key:
                patch[info["key_field"]] = api_key
            if pid == "azure_openai":
                patch.update({
                    "azure_speech_key": self._azure_speech_key.text().strip(),
                    "azure_speech_region": self._combo_value(self._azure_speech_region).lower(),
                    "azure_speech_tier": self._azure_speech_tier.currentData(),
                    "azure_speech_verify": self._azure_speech_verify.isChecked(),
                    "azure_deep_model": self._combo_value(self._azure_deep_model),
                    "azure_code_model": self._combo_value(self._azure_code_model),
                    "azure_document_model": self._combo_value(self._azure_document_model),
                    "azure_image_model": self._combo_value(self._azure_image_model),
                    "azure_video_model": self._combo_value(self._azure_video_model),
                })
            if url and info["url_editable"]:
                patch["azure_openai_endpoint" if pid == "azure_openai" else "llm_url"] = url.rstrip("/")
            _patch_cached_config(patch)
            self._persist_voice_key()  # la reconnexion vocale suit avec provider_changed
            # Dire exactement ce qui change : le fournisseur choisi devient le
            # cerveau de tout, et Gemini ne garde que la voix. Sans cette
            # phrase, l'utilisateur croit avoir changé la voix aussi.
            self._status_lbl.setText(
                f"✓ {info['label']} ({model}) pense désormais pour tout. "
                "La voix reste Gemini Live ; effectif à la reconnexion vocale."
                if pid != "gemini" else
                f"✓ Gemini ({model}) redevient le cerveau et la voix."
            )
            self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
            self._refresh_provider_list()
            self.provider_changed.emit(pid)
            if pid == "azure_openai":
                self._set_status("◌ Vérification du déploiement Azure…", C.TEXT_DIM)
                self._verify_azure_deployment()
        except Exception as e:
            self._status_lbl.setText(f"✗ Échec de l'application : {e}")
            self._status_lbl.setStyleSheet(f"color: {C.RED}; background: transparent;")
