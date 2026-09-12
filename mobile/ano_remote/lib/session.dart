import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:camera/camera.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'camera_link.dart';
import 'location_link.dart';
import 'net.dart';
import 'phone_link.dart';
import 'voice_link.dart';

enum LinkStatus { offline, connecting, online, failed }

class ChatEntry {
  ChatEntry(this.kind, this.text) : at = DateTime.now();
  final String kind; // user | jarvis | system | error
  final String text;
  final DateTime at;
}

/// État central de l'application : connexion, conversation, micro, caméra.
///
/// Un seul objet écoutable évite que chaque écran refasse sa propre connexion
/// et que les deux se marchent dessus.
class AnoSession extends ChangeNotifier {
  AnoSession() {
    voice.addListener(notifyListeners);
    camera.addListener(notifyListeners);
    location.addListener(notifyListeners);
    phone.addListener(notifyListeners);
    const MethodChannel(
      'ano.remote/system',
    ).setMethodCallHandler((_) async => null);
  }

  final VoiceLink voice = VoiceLink();
  final CameraLink camera = CameraLink();
  final LocationLink location = LocationLink();
  final PhoneLink phone = PhoneLink();

  RemoteApi? api;
  WebSocket? _socket;
  Timer? _reconnectTimer;

  LinkStatus status = LinkStatus.offline;
  String? error;
  String? hint;
  String serverName = '';
  final List<ChatEntry> messages = [
    ChatEntry('system', 'ANO Remote prêt. Scannez le QR code du PC.'),
  ];
  List<DiscoveredServer> discovered = const [];
  List<Map<String, dynamic>> captures = const [];
  Map<String, dynamic>? pendingConfirmation;

  bool get connected => status == LinkStatus.online;
  bool get busy => status == LinkStatus.connecting;

  // ── connexion ──────────────────────────────────────────────────────────

  Future<String> restore() async {
    final prefs = await SharedPreferences.getInstance();
    final base = prefs.getString('base_url') ?? '';
    final deviceToken = prefs.getString('device_token') ?? '';
    if (base.isEmpty || deviceToken.isEmpty) return base;

    _set(status: LinkStatus.connecting, hint: 'Reconnexion…');
    try {
      // L'adresse du PC peut avoir changé de port ou de schéma depuis la
      // dernière session : on re-teste toutes les variantes.
      final outcome = await probeServer(candidateUrls(base));
      final candidate = RemoteApi(outcome.baseUrl, deviceToken: deviceToken);
      await candidate.reconnect();
      await _finish(candidate, outcome.info);
    } catch (_) {
      _set(
        status: LinkStatus.offline,
        hint: 'Session précédente injoignable — appairez à nouveau.',
      );
    }
    return base;
  }

  Future<void> search() async {
    _set(hint: 'Recherche du PC sur le réseau…');
    try {
      final servers = await discoverServers();
      discovered = servers;
      _set(
        hint: servers.isEmpty
            ? 'Aucun PC trouvé. Lancez JARVIS, puis « Contrôle à distance ».'
            : servers.length == 1
            ? '${servers.first.name} trouvé — saisissez la clé affichée.'
            : '${servers.length} PC trouvés — choisissez le vôtre.',
      );
    } catch (exception) {
      _set(error: 'Recherche impossible : $exception');
    }
  }

  Future<void> pair(String address, String key) async {
    if (key.length < 4) {
      _set(error: 'Saisissez la clé affichée par ANO-GPT.');
      return;
    }
    _set(
      status: LinkStatus.connecting,
      error: null,
      hint: 'Recherche du serveur…',
    );
    try {
      final outcome = await probeServer(candidateUrls(address));
      _set(hint: 'Serveur trouvé — appairage…');
      final candidate = RemoteApi(outcome.baseUrl);
      await candidate.pair(key);
      await _finish(candidate, outcome.info);
    } catch (exception) {
      _set(
        status: LinkStatus.failed,
        hint: null,
        error: exception is RemoteException
            ? exception.message
            : exception.toString(),
      );
    }
  }

  Future<void> _finish(RemoteApi candidate, Map<String, dynamic> info) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('base_url', candidate.baseUrl);
    if (candidate.deviceToken.isNotEmpty) {
      await prefs.setString('device_token', candidate.deviceToken);
    }
    api = candidate;
    serverName = info['name']?.toString() ?? 'ANO-GPT';
    await _openControlSocket();
    await phone.initialize();
    // Le WebSocket qui pilote la SIM appartient au service Android, pas à
    // l'écran Flutter : Android peut donc mettre cette activité en veille sans
    // suspendre les appels, les SMS ou leur surveillance.
    await _startPhoneRelay();
    messages.add(ChatEntry('system', 'Connexion sécurisée avec $serverName.'));
    _set(status: LinkStatus.online, error: null, hint: null);

    // Le suivi démarre pendant que l'application est visible : c'est la seule
    // fenêtre où Android laisse lancer un service de localisation, et c'est ce
    // service qui fera vivre les relevés une fois l'écran quitté.
    if (await location.restorePreference()) {
      await location.start(candidate);
    }
    unawaited(refreshCaptures());
  }

  Future<void> disconnect() async {
    _reconnectTimer?.cancel();
    // Sans PC à qui parler, garder le service allumé ne ferait que vider la
    // batterie ; le choix de l'utilisateur reste mémorisé pour la prochaine fois.
    await location.stop(remember: false);
    await voice.stop();
    await camera.stop();
    await _socket?.close();
    try {
      await const MethodChannel('ano.remote/system').invokeMethod<void>(
        'stopPhoneRelay',
      );
    } catch (_) {}
    _socket = null;
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove('device_token');
    api = null;
    _set(status: LinkStatus.offline, error: null, hint: null);
  }

  // ── canal de contrôle ──────────────────────────────────────────────────

  Future<void> _openControlSocket() async {
    await _socket?.close();
    final current = api;
    if (current == null) return;
    _socket = await current.openSocket();
    _socket!.add(
      jsonEncode({
        'type': 'client_hello',
        // Ce canal sert à l'interface. Les capacités téléphone sont annoncées
        // par PhoneRelayService, unique propriétaire du tunnel de fond.
        'client': 'ano_remote_android_ui',
        'capabilities': [
          'human_confirmation',
        ],
      }),
    );
    _socket!.listen(
      _onControlMessage,
      onDone: _scheduleReconnect,
      onError: (_) => _scheduleReconnect(),
      cancelOnError: true,
    );
  }

  Future<void> _startPhoneRelay() async {
    final current = api;
    if (current == null || current.deviceToken.isEmpty) return;
    try {
      await const MethodChannel('ano.remote/system').invokeMethod<void>(
        'startPhoneRelay',
        {'base_url': current.baseUrl, 'device_token': current.deviceToken},
      );
    } catch (_) {
      // L'interface reste utilisable sur une ancienne version Android ; le
      // serveur indiquera alors honnêtement que le téléphone est hors ligne.
    }
  }

  void _onControlMessage(dynamic event) {
    try {
      final data = Map<String, dynamic>.from(
        jsonDecode(event as String) as Map,
      );
      switch (data['type']) {
        case 'log':
          messages.add(
            ChatEntry(
              data['speaker'] == 'jarvis' ? 'jarvis' : 'user',
              data['text']?.toString() ?? '',
            ),
          );
          notifyListeners();
        case 'sys':
          messages.add(ChatEntry('system', data['text']?.toString() ?? ''));
          notifyListeners();
        case 'request_location':
          unawaited(sendLocation(silent: true));
        case 'file_received':
          unawaited(refreshCaptures());
        case 'phone_camera':
          // Le PC pilote la caméra du téléphone : « ouvre l'appareil photo »
          // prononcé devant l'ordinateur doit allumer celle-ci sans que
          // l'utilisateur touche au téléphone.
          unawaited(
            _handleCameraCommand(
              data['action']?.toString() ?? '',
              data['lens']?.toString() ?? '',
            ),
          );
        case 'confirmation_request':
          pendingConfirmation = data;
          notifyListeners();
        case 'confirmation_closed':
          if (pendingConfirmation?['token'] == data['token']) {
            pendingConfirmation = null;
            notifyListeners();
          }
        default:
          break;
      }
    } catch (_) {
      // Message inattendu : l'ignorer vaut mieux que couper le canal.
    }
  }

  Future<void> _handleCameraCommand(String action, [String lens = '']) async {
    final current = api;
    if (current == null) return;
    switch (action) {
      case 'start':
        await camera.start(current, lens: _lensOf(lens));
        if (camera.streaming) {
          final which = camera.isFront ? 'frontale ' : '';
          messages.add(
            ChatEntry('system', 'Caméra ${which}allumée par ANO-GPT.'),
          );
          notifyListeners();
        }
      case 'stop':
        await camera.stop();
      case 'switch':
        await camera.switchCamera(current);
      case 'lens':
        // Ordre venu de la voix : « passe sur la caméra frontale ». Il peut
        // précéder l'allumage, auquel cas il ne fait que fixer l'objectif.
        final wanted = _lensOf(lens);
        if (wanted != null) await camera.setLens(current, wanted);
    }
  }

  CameraLensDirection? _lensOf(String value) {
    switch (value.trim().toLowerCase()) {
      case 'front':
        return CameraLensDirection.front;
      case 'back':
        return CameraLensDirection.back;
      default:
        return null;
    }
  }

  void _scheduleReconnect() {
    if (!connected || _reconnectTimer?.isActive == true) return;
    _set(hint: 'Reconnexion…');
    _reconnectTimer = Timer(const Duration(seconds: 3), () async {
      try {
        // Le jeton d'accès meurt avec le redémarrage du PC : en redemander un
        // avant de rouvrir le canal, sinon la reconnexion boucle sur un 401.
        await api?.reconnect();
        await _openControlSocket();
        _set(hint: null);
      } catch (_) {
        _reconnectTimer = null;
        _scheduleReconnect();
      }
    });
  }

  // ── actions ────────────────────────────────────────────────────────────

  Future<void> sendCommand(String text) async {
    final trimmed = text.trim();
    final current = api;
    if (trimmed.isEmpty || current == null) return;
    messages.add(ChatEntry('user', trimmed));
    notifyListeners();
    try {
      if (_socket?.readyState == WebSocket.open) {
        _socket!.add(jsonEncode({'type': 'command', 'text': trimmed}));
      } else {
        await current.post('/api/command', {'text': trimmed});
      }
    } catch (exception) {
      messages.add(ChatEntry('error', exception.toString()));
      notifyListeners();
    }
  }

  void answerConfirmation(bool accepted) {
    final pending = pendingConfirmation;
    if (pending == null) return;
    final token = pending['token']?.toString() ?? '';
    pendingConfirmation = null;
    if (token.isNotEmpty && _socket?.readyState == WebSocket.open) {
      _socket!.add(
        jsonEncode({
          'type': 'confirmation_response',
          'token': token,
          'accepted': accepted,
        }),
      );
    }
    messages.add(
      ChatEntry(
        'system',
        accepted
            ? 'Action confirmée sur le téléphone.'
            : 'Action annulée sur le téléphone.',
      ),
    );
    notifyListeners();
  }

  Future<void> wake() async {
    try {
      await api?.post('/api/wake');
      messages.add(ChatEntry('system', 'Signal de réveil envoyé.'));
    } catch (exception) {
      messages.add(ChatEntry('error', exception.toString()));
    }
    notifyListeners();
  }

  /// Demande une action caméra au PC, qui décide de la source à utiliser.
  Future<void> requestCapture(String action) async {
    try {
      await api?.post('/api/command', {'text': phraseFor(action)});
      messages.add(ChatEntry('user', phraseFor(action)));
    } catch (exception) {
      messages.add(ChatEntry('error', exception.toString()));
    }
    notifyListeners();
  }

  /// Traduit un bouton en phrase adressée à ANO-GPT.
  String phraseFor(String action) => switch (action) {
    'photo' => 'prends une photo',
    'video_start' => 'enregistre une vidéo',
    'video_stop' => 'arrête la vidéo',
    'open' => 'ouvre la caméra du téléphone',
    'front' => 'passe sur la caméra frontale du téléphone',
    'back' => 'passe sur la caméra arrière du téléphone',
    'close' => 'ferme la caméra',
    _ => action,
  };

  Future<void> startVoice(MicMode mode) async {
    final current = api;
    if (current == null) return;
    await voice.start(current, mode);
    if (voice.error != null) {
      messages.add(ChatEntry('error', voice.error!));
      notifyListeners();
    }
  }

  Future<void> stopVoice() => voice.stop();

  Future<void> toggleCamera() async {
    final current = api;
    if (current == null) return;
    if (camera.streaming) {
      await camera.stop();
    } else {
      await camera.start(current);
      if (camera.error != null) {
        messages.add(ChatEntry('error', camera.error!));
        notifyListeners();
      }
    }
  }

  Future<void> refreshCaptures() async {
    try {
      captures = await api?.files() ?? const [];
      notifyListeners();
    } catch (_) {
      // Galerie indisponible : ce n'est pas une raison de couper la session.
    }
  }

  /// Phrase d'état du GPS, affichée dans le bandeau.
  String get locationStatus => location.status;

  /// Relevé immédiat, demandé par le PC ou par le bouton.
  Future<void> sendLocation({bool silent = false}) async {
    final current = api;
    if (current == null) return;
    final outcome = await location.sendNow(current);
    if (!silent && location.error != null) {
      messages.add(ChatEntry('error', outcome));
    }
    notifyListeners();
  }

  /// Active ou coupe le suivi continu, celui qui survit à la sortie de l'appli.
  Future<void> toggleTracking() async {
    final current = api;
    if (current == null) return;
    if (location.running) {
      await location.stop();
      messages.add(ChatEntry('system', 'Suivi GPS arrêté.'));
    } else {
      await location.start(current);
      messages.add(
        ChatEntry(
          location.error == null ? 'system' : 'error',
          location.error ??
              'Suivi GPS actif — il continue même quand vous quittez l’application.',
        ),
      );
    }
    notifyListeners();
  }

  void _set({
    LinkStatus? status,
    String? error,
    String? hint,
    bool clearError = false,
  }) {
    if (status != null) this.status = status;
    if (error != null || clearError) this.error = error;
    this.hint = hint;
    notifyListeners();
  }

  @override
  void dispose() {
    _reconnectTimer?.cancel();
    _socket?.close();
    voice.dispose();
    camera.dispose();
    location.dispose();
    phone.dispose();
    super.dispose();
  }
}
