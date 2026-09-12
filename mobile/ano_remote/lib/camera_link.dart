import 'dart:async';
import 'dart:io';

import 'package:camera/camera.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'net.dart';

/// Pont vers la conversion NV21 → JPEG écrite en Kotlin.
const MethodChannel _frames = MethodChannel('ano.remote/frames');

/// Cadence d'envoi. Au-delà, le Wi-Fi et l'encodeur deviennent le facteur
/// limitant sans que l'œil y gagne quoi que ce soit.
const Duration _kFrameInterval = Duration(milliseconds: 66); // ~15 i/s

/// Diffuse la caméra du téléphone vers le conteneur plein cadre du PC.
class CameraLink extends ChangeNotifier {
  CameraLink();

  CameraController? _controller;
  WebSocket? _socket;
  List<CameraDescription> _cameras = const [];
  int _cameraIndex = 0;
  /// Objectif demandé. Le PC peut l'imposer (« passe sur la caméra frontale »)
  /// avant même que le flux ne démarre.
  CameraLensDirection _lens = CameraLensDirection.back;
  bool _streaming = false;
  bool _busy = false;          // une conversion est déjà en vol
  bool _starting = false;
  DateTime _lastSent = DateTime.fromMillisecondsSinceEpoch(0);
  int _sent = 0;
  String? _error;

  bool get streaming => _streaming;
  bool get starting => _starting;
  String? get error => _error;
  int get framesSent => _sent;
  CameraController? get controller => _controller;

  bool get canSwitch => _cameras.length > 1;

  /// Objectif courant, pour l'étiquette de l'écran caméra.
  CameraLensDirection get lens => _lens;
  bool get isFront => _lens == CameraLensDirection.front;

  /// Y a-t-il vraiment une caméra frontale sur cet appareil ?
  bool get hasFront => _cameras.any(
        (camera) => camera.lensDirection == CameraLensDirection.front,
      );

  Future<void> start(RemoteApi api, {CameraLensDirection? lens}) async {
    if (_streaming || _starting) return;
    _starting = true;
    _error = null;
    if (lens != null) _lens = lens;
    notifyListeners();
    try {
      if (_cameras.isEmpty) {
        _cameras = await availableCameras();
      }
      if (_cameras.isEmpty) {
        throw const RemoteException('Aucune caméra sur cet appareil.');
      }
      final controller = CameraController(
        _pickCamera(),
        ResolutionPreset.medium, // 640×480 : net à l'écran, léger sur le Wi-Fi
        enableAudio: false,
        imageFormatGroup: ImageFormatGroup.nv21,
      );
      await controller.initialize();

      final socket = await WebSocket.connect(
        api.socketUri('/ws/phone-camera').toString(),
        customClient: lanClient(),
      ).timeout(const Duration(seconds: 6));
      socket.done.then((_) => stop());

      _controller = controller;
      _socket = socket;
      _streaming = true;
      _starting = false;
      _sent = 0;
      notifyListeners();

      await controller.startImageStream(_onImage);
    } catch (exception) {
      _starting = false;
      _error = exception is RemoteException
          ? exception.message
          : exception.toString();
      await _release();
      notifyListeners();
    }
  }

  Future<void> stop() async {
    if (!_streaming && _controller == null) return;
    await _release();
    notifyListeners();
  }

  /// Caméra correspondant à l'objectif demandé.
  ///
  /// L'index n'est qu'un repli : sur les téléphones à trois capteurs arrière,
  /// faire tourner un index finit sur le grand-angle ou le macro alors que
  /// l'utilisateur a simplement dit « caméra frontale ».
  CameraDescription _pickCamera() {
    for (final camera in _cameras) {
      if (camera.lensDirection == _lens) return camera;
    }
    return _cameras[_cameraIndex % _cameras.length];
  }

  Future<void> switchCamera(RemoteApi api) async {
    if (!canSwitch) return;
    return setLens(
      api,
      isFront ? CameraLensDirection.back : CameraLensDirection.front,
    );
  }

  /// Bascule sur l'objectif demandé, en redémarrant le flux si besoin.
  Future<void> setLens(RemoteApi api, CameraLensDirection lens) async {
    if (lens == CameraLensDirection.front && !hasFront && _cameras.isNotEmpty) {
      _error = "Cet appareil n'a pas de caméra frontale.";
      notifyListeners();
      return;
    }
    if (lens == _lens && _streaming) return;
    _lens = lens;
    _cameraIndex = _cameras.indexWhere((c) => c.lensDirection == lens);
    if (_cameraIndex < 0) _cameraIndex = 0;
    if (_streaming) {
      // Un CameraController est lié à un capteur : changer d'objectif impose
      // de le refermer, il n'existe pas de bascule à chaud.
      await _release();
      await start(api, lens: lens);
    } else {
      notifyListeners();
    }
  }

  Future<void> _release() async {
    final controller = _controller;
    _controller = null;
    _streaming = false;
    _busy = false;
    try {
      if (controller != null) {
        if (controller.value.isStreamingImages) {
          await controller.stopImageStream();
        }
        await controller.dispose();
      }
    } catch (_) {
      // Caméra déjà rendue au système.
    }
    final socket = _socket;
    _socket = null;
    await socket?.close();
  }

  void _onImage(CameraImage image) {
    if (_busy || !_streaming) return;
    final now = DateTime.now();
    if (now.difference(_lastSent) < _kFrameInterval) return;
    _lastSent = now;
    _busy = true;
    unawaited(_sendFrame(image));
  }

  Future<void> _sendFrame(CameraImage image) async {
    try {
      final socket = _socket;
      if (socket == null || socket.readyState != WebSocket.open) return;
      // En NV21 le plugin livre un plan unique déjà entrelacé : exactement ce
      // qu'attend YuvImage côté Android.
      final bytes = image.planes.first.bytes;
      final description = _controller?.description;
      final sensor = description?.sensorOrientation ?? 0;
      final front = description?.lensDirection == CameraLensDirection.front;
      // Le capteur frontal est monté à l'envers du capteur arrière : appliquer
      // la même rotation aux deux livre un selfie tête en bas. Et le retourner
      // horizontalement donne à l'utilisateur l'image de miroir qu'il attend
      // en se filmant.
      final rotation = front ? (360 - sensor) % 360 : sensor;
      final jpeg = await _frames.invokeMethod<Uint8List>('nv21ToJpeg', {
        'bytes': bytes,
        'width': image.width,
        'height': image.height,
        'rotation': rotation,
        'mirror': front,
        'quality': 70,
      });
      if (jpeg == null || jpeg.isEmpty) return;
      if (socket.readyState == WebSocket.open) {
        socket.add(jpeg);
        _sent++;
        if (_sent % 15 == 0) notifyListeners();
      }
    } catch (exception) {
      _error = 'Encodage vidéo : $exception';
    } finally {
      _busy = false;
    }
  }

  @override
  void dispose() {
    _release();
    super.dispose();
  }
}
