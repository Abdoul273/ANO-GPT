import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:geolocator/geolocator.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'net.dart';

/// Cadence des relevés en arrière-plan.
///
/// Trente secondes suffisent à suivre un déplacement réel et laissent la radio
/// se rendormir entre deux points ; à cinq secondes la batterie fondait pour
/// une précision dont personne ne profite.
const Duration kTrackingInterval = Duration(seconds: 30);

const MethodChannel _system = MethodChannel('ano.remote/system');

/// Suivi GPS continu, qui survit à la sortie de l'application.
///
/// Android gèle les minuteries dès que l'activité passe en arrière-plan : la
/// version précédente, bâtie sur `Timer.periodic`, s'arrêtait donc à la
/// seconde où l'utilisateur quittait l'écran. La seule construction qui tient
/// est un service de premier plan de type `location`, avec sa notification
/// permanente — c'est ce que démarre `getPositionStream` quand on lui passe
/// une `ForegroundNotificationConfig`.
class LocationLink extends ChangeNotifier {
  LocationLink();

  static const String _prefKey = 'tracking_enabled';

  StreamSubscription<Position>? _subscription;
  RemoteApi? _api;
  bool _sending = false;
  bool _enabled = false;

  Position? _last;
  DateTime? _lastSentAt;
  int _sent = 0;
  String? _error;

  bool get enabled => _enabled;
  bool get running => _subscription != null;
  Position? get last => _last;
  int get sent => _sent;
  String? get error => _error;

  /// Phrase courte affichée dans le bandeau d'état.
  String get status {
    if (_error != null) return _error!;
    if (!_enabled) return 'Suivi GPS désactivé';
    if (_last == null) return 'Recherche du signal GPS…';
    final accuracy = _last!.accuracy.round();
    final at = _lastSentAt;
    if (at == null) return 'GPS ±$accuracy m';
    final age = DateTime.now().difference(at);
    if (age.inMinutes >= 1) return 'GPS ±$accuracy m · il y a ${age.inMinutes} min';
    return 'GPS ±$accuracy m · ${age.inSeconds} s';
  }

  /// Rétablit le choix de l'utilisateur au lancement suivant.
  Future<bool> restorePreference() async {
    final prefs = await SharedPreferences.getInstance();
    // Le suivi continu est le comportement attendu : on ne le coupe que si
    // l'utilisateur l'a explicitement refusé.
    _enabled = prefs.getBool(_prefKey) ?? true;
    return _enabled;
  }

  Future<void> _remember(bool value) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_prefKey, value);
  }

  /// Démarre le suivi. À appeler pendant que l'application est visible :
  /// Android n'autorise le démarrage d'un service de localisation que depuis
  /// le premier plan.
  Future<void> start(RemoteApi api) async {
    _api = api;
    if (running) return;

    _error = null;
    try {
      await _ensurePermissions();
    } catch (exception) {
      _error = exception is RemoteException
          ? exception.message
          : exception.toString();
      notifyListeners();
      return;
    }

    _enabled = true;
    unawaited(_remember(true));

    try {
      _subscription = Geolocator.getPositionStream(
        locationSettings: _settings(),
      ).listen(
        _onPosition,
        onError: (Object error) {
          _error = 'GPS interrompu : $error';
          notifyListeners();
        },
        cancelOnError: false,
      );
      notifyListeners();
    } catch (exception) {
      _error = 'Suivi impossible : $exception';
      notifyListeners();
    }
  }

  Future<void> stop({bool remember = true}) async {
    await _subscription?.cancel();
    _subscription = null;
    if (remember) {
      _enabled = false;
      unawaited(_remember(false));
    }
    notifyListeners();
  }

  LocationSettings _settings() {
    if (!kIsWeb && Platform.isAndroid) {
      return AndroidSettings(
        accuracy: LocationAccuracy.high,
        // La notification permanente n'est pas une décoration : c'est elle qui
        // fait vivre le service quand l'écran s'éteint.
        foregroundNotificationConfig: const ForegroundNotificationConfig(
          notificationTitle: 'ANO Remote — position partagée',
          notificationText: 'ANO-GPT suit votre position sur le réseau local.',
          notificationChannelName: 'Suivi de position ANO-GPT',
          enableWakeLock: true,
          enableWifiLock: true, // sans lui, le Wi-Fi coupe en veille profonde
          setOngoing: true,
          color: Color(0xFF00D4FF),
        ),
        intervalDuration: kTrackingInterval,
      );
    }
    return LocationSettings(
      accuracy: LocationAccuracy.high,
      distanceFilter: 0,
    );
  }

  Future<void> _ensurePermissions() async {
    if (!await Geolocator.isLocationServiceEnabled()) {
      throw const RemoteException('Activez la localisation du téléphone.');
    }
    var permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) {
      permission = await Geolocator.requestPermission();
    }
    if (permission == LocationPermission.denied ||
        permission == LocationPermission.deniedForever) {
      throw const RemoteException(
        'Permission GPS refusée. Autorisez-la dans les réglages Android.',
      );
    }
    // Sans cette autorisation la notification reste invisible sur Android 13+,
    // et une notification invisible fait tomber le service à l'arrêt.
    await requestNotificationPermission();
  }

  static Future<void> requestNotificationPermission() async {
    try {
      await _system.invokeMethod('requestNotificationPermission');
    } on MissingPluginException {
      // Autre plateforme, ou canal absent : sans effet.
    } catch (_) {
      // Refus de l'utilisateur : le service tourne quand même.
    }
  }

  void _onPosition(Position position) {
    _last = position;
    notifyListeners();
    unawaited(_push(position));
  }

  /// Envoie la position au PC, avec une reprise unique si le jeton a expiré.
  Future<void> _push(Position position) async {
    final api = _api;
    if (api == null || _sending) return;
    _sending = true;
    try {
      await _post(api, position);
      _sent++;
      _lastSentAt = DateTime.now();
      _error = null;
    } on RemoteException catch (exception) {
      // Le PC a redémarré : le jeton n'est plus valable, on en redemande un
      // plutôt que d'abandonner le suivi jusqu'au prochain appairage.
      if (exception.message.contains('401') ||
          exception.message.contains('Non autorisé')) {
        try {
          await api.reconnect();
          await _post(api, position);
          _sent++;
          _lastSentAt = DateTime.now();
          _error = null;
        } catch (_) {
          _error = 'PC injoignable';
        }
      } else {
        _error = 'PC injoignable';
      }
      notifyListeners();
    } catch (_) {
      _error = 'PC injoignable';
      notifyListeners();
    } finally {
      _sending = false;
    }
  }

  Future<void> _post(RemoteApi api, Position position) => api.post(
    '/api/location',
    {
      'lat': position.latitude,
      'lon': position.longitude,
      'accuracy_m': position.accuracy,
    },
  );

  /// Relevé immédiat, à la demande du PC ou de l'utilisateur.
  Future<String> sendNow(RemoteApi api) async {
    _api = api;
    try {
      await _ensurePermissions();
      final position = _last ??
          await Geolocator.getCurrentPosition(
            locationSettings: const LocationSettings(
              accuracy: LocationAccuracy.best,
              timeLimit: Duration(seconds: 15),
            ),
          );
      await _post(api, position);
      _last = position;
      _sent++;
      _lastSentAt = DateTime.now();
      _error = null;
      notifyListeners();
      return 'Position envoyée (±${position.accuracy.round()} m).';
    } catch (exception) {
      final message = exception is RemoteException
          ? exception.message
          : exception.toString();
      _error = message;
      notifyListeners();
      return message;
    }
  }

  @override
  void dispose() {
    _subscription?.cancel();
    super.dispose();
  }
}
