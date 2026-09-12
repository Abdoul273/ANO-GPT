import 'dart:async';
import 'dart:io';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:record/record.dart';

import 'net.dart';

/// Fréquence attendue par ANO-GPT côté PC (SEND_SAMPLE_RATE).
///
/// Une autre valeur ferait parler l'utilisateur trop vite ou trop lentement
/// aux oreilles du modèle, qui ne rééchantillonne pas.
const int kMicSampleRate = 16000;

enum MicMode {
  /// Micro fermé : le PC reprend la main sur son propre micro.
  off,

  /// Bouton maintenu : le micro ne vit que le temps de l'appui.
  push,

  /// Mains-libres : le micro reste ouvert jusqu'à extinction explicite.
  hands,
}

/// Achemine le micro du téléphone vers ANO-GPT.
///
/// Tant que des paquets arrivent, le PC coupe son propre micro : c'est la
/// simple présence du flux qui bascule l'écoute, sans négociation.
class VoiceLink extends ChangeNotifier {
  VoiceLink({
    AudioRecorder Function()? recorderFactory,
    Future<WebSocket> Function(RemoteApi)? connect,
  }) : _recorderFactory = recorderFactory ?? AudioRecorder.new,
       _connect = connect ?? ((api) => api.openSocket('/ws/phone-audio'));

  final AudioRecorder Function() _recorderFactory;
  final Future<WebSocket> Function(RemoteApi) _connect;
  AudioRecorder? _instance;
  AudioRecorder get _recorder => _instance ??= _recorderFactory();
  WebSocket? _socket;
  StreamSubscription<dynamic>? _socketEvents;
  StreamSubscription<Uint8List>? _subscription;
  Future<void> _operations = Future<void>.value();
  int _generation = 0;
  bool _disposed = false;
  bool _closing = false;
  MicMode _mode = MicMode.off;
  double _level = 0.0;
  String? _error;

  MicMode get mode => _mode;
  bool get streaming => _mode != MicMode.off;
  double get level => _level;
  String? get error => _error;
  Future<bool> hasPermission() => _recorder.hasPermission();

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  Future<void> start(RemoteApi api, MicMode mode) {
    if (mode == MicMode.off) return stop();
    if (_disposed) return Future<void>.value();
    final generation = ++_generation;
    return _operations = _operations.then((_) async {
      if (_disposed || generation != _generation) return;
      if (streaming) {
        _mode = mode;
        _notify();
        return;
      }
      _error = null;
      try {
        if (!await _recorder.hasPermission()) {
          throw const RemoteException('Micro refusé. Autorisez-le dans Réglages.');
        }
        if (_disposed || generation != _generation) return;
        final socket = await _connect(api);
        _socket = socket;
        if (_disposed || generation != _generation) {
          await _close();
          return;
        }
        // Écouter le WebSocket est indispensable pour recevoir sa fermeture.
        _socketEvents = socket.listen(
          (_) {},
          onError: (Object error) => _connectionLost(socket, 'Connexion micro interrompue.'),
          onDone: () => _connectionLost(socket, 'Canal micro fermé par le PC. Reconnectez le micro.'),
        );
        final stream = await _recorder.startStream(
          const RecordConfig(
            encoder: AudioEncoder.pcm16bits,
            sampleRate: kMicSampleRate,
            numChannels: 1,
            streamBufferSize: 2048,
            echoCancel: true,
            noiseSuppress: true,
            autoGain: true,
          ),
        );
        if (_disposed || generation != _generation) {
          await _close();
          return;
        }
        _mode = mode;
        _notify();
        _subscription = stream.listen(
          (chunk) {
            if (!identical(_socket, socket) || socket.readyState != WebSocket.open) return;
            socket.add(chunk);
            _updateLevel(chunk);
          },
          onError: (Object error) => _connectionLost(socket, 'Le microphone Android s’est arrêté.'),
          onDone: () {
            if (!_closing) _connectionLost(socket, 'Le microphone Android s’est arrêté.');
          },
        );
      } catch (exception) {
        if (!_disposed && generation == _generation) {
          _error = exception is RemoteException
              ? exception.message : 'Impossible d’ouvrir le micro. Vérifiez la connexion au PC.';
        }
        await _close();
      }
    });
  }

  void _connectionLost(WebSocket socket, String message) {
    if (_closing || _disposed || !identical(socket, _socket)) return;
    _error = message;
    unawaited(stop());
  }

  Future<void> stop() {
    ++_generation; // Annule aussi une permission/connexion encore en attente.
    return _operations = _operations.then((_) => _close());
  }

  Future<void> _close() async {
    _closing = true;
    try {
      // Le plugin livre les derniers échantillons pendant stop(). Annuler la
      // souscription d'abord supprimait la fin de la phrase.
      try { await _instance?.stop(); } catch (_) {}
      await _subscription?.cancel();
      _subscription = null;
      final socket = _socket;
      _socket = null;
      try { await socket?.close().timeout(const Duration(seconds: 2)); } catch (_) {}
      await _socketEvents?.cancel();
      _socketEvents = null;
    } finally {
      _mode = MicMode.off;
      _level = 0.0;
      _closing = false;
      _notify();
    }
  }

  /// Amplitude efficace du bloc, lissée pour éviter un halo qui clignote.
  void _updateLevel(Uint8List chunk) {
    if (chunk.length < 2) return;
    final samples = chunk.buffer.asInt16List(
      chunk.offsetInBytes,
      chunk.lengthInBytes ~/ 2,
    );
    var sum = 0.0;
    // Un échantillon sur huit suffit à mesurer un niveau et divise le coût.
    var counted = 0;
    for (var i = 0; i < samples.length; i += 8) {
      final value = samples[i] / 32768.0;
      sum += value * value;
      counted++;
    }
    if (counted == 0) return;
    final rms = math.sqrt(sum / counted);
    final target = (rms * 3.2).clamp(0.0, 1.0);
    _level = _level * 0.7 + target * 0.3;
    _notify();
  }

  @override
  void dispose() {
    _disposed = true;
    unawaited(stop().then((_) async => _instance?.dispose()));
    super.dispose();
  }
}
