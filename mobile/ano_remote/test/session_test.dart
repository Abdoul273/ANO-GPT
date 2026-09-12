import 'package:ano_remote/net.dart';
import 'package:ano_remote/session.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  // La session crée un enregistreur audio, qui passe par un canal de plateforme.
  TestWidgetsFlutterBinding.ensureInitialized();

  test('les boutons caméra parlent la langue d’ANO-GPT', () {
    final session = AnoSession();
    addTearDown(session.dispose);

    // Ces phrases sont ce que le PC reçoit : elles doivent déclencher l'outil
    // camera_control, pas l'ouverture d'une application.
    expect(session.phraseFor('photo'), 'prends une photo');
    expect(session.phraseFor('video_start'), 'enregistre une vidéo');
    expect(session.phraseFor('video_stop'), 'arrête la vidéo');
  });

  test('une session neuve est hors ligne et sans micro ouvert', () {
    final session = AnoSession();
    addTearDown(session.dispose);

    expect(session.status, LinkStatus.offline);
    expect(session.connected, isFalse);
    expect(session.voice.streaming, isFalse);
    expect(session.camera.streaming, isFalse);
    expect(session.messages, isNotEmpty);
  });

  test('le socket de commande vise le bon chemin authentifié', () {
    final api = RemoteApi('https://192.168.1.137:8000', token: 'jeton-x');

    expect(api.socketUri('/ws').toString(),
        'wss://192.168.1.137:8000/ws?token=jeton-x');
    // Micro et caméra ont chacun leur canal : les mélanger enverrait du PCM
    // dans le décodeur JPEG.
    expect(api.socketUri('/ws/phone-audio').path, '/ws/phone-audio');
    expect(api.socketUri('/ws/phone-camera').path, '/ws/phone-camera');
  });

  test('le téléchargement d’une capture porte le jeton dans l’URL', () {
    final api = RemoteApi('http://192.168.1.5:8000', token: 'abc def');
    final uri = api.downloadUri('ano photo.jpg');

    expect(uri.path, '/uploads/ano%20photo.jpg');
    expect(uri.queryParameters['token'], 'abc def');
  });
}
