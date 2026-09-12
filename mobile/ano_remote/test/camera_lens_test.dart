import 'package:ano_remote/camera_link.dart';
import 'package:ano_remote/net.dart';
import 'package:ano_remote/session.dart';
import 'package:camera/camera.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('le téléphone démarre sur son objectif principal', () {
    final link = CameraLink();
    addTearDown(link.dispose);

    // Comme l'application photo du téléphone : personne n'attend un selfie
    // en ouvrant la caméra.
    expect(link.lens, CameraLensDirection.back);
    expect(link.isFront, isFalse);
  });

  test('sans caméra frontale, la demande est refusée et expliquée', () async {
    final link = CameraLink();
    addTearDown(link.dispose);

    // Aucune caméra n'a été énumérée : rien à basculer, et surtout pas de
    // plantage silencieux.
    await link.setLens(RemoteApi('http://127.0.0.1:1', token: 'jeton'),
        CameraLensDirection.front);
    expect(link.streaming, isFalse);
  });

  test('les ordres du PC nomment l’objectif attendu', () {
    final session = AnoSession();
    addTearDown(session.dispose);

    // Ce sont les phrases envoyées par les boutons du téléphone ; elles
    // doivent viser l'objectif, pas une application externe.
    expect(session.phraseFor('front'),
        'passe sur la caméra frontale du téléphone');
    expect(session.phraseFor('back'),
        'passe sur la caméra arrière du téléphone');
  });
}
