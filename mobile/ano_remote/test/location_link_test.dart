import 'package:ano_remote/location_link.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('le suivi continu est actif par défaut', () async {
    // L'utilisateur qui appaire son téléphone veut que sa position suive ;
    // lui demander d'armer un interrupteur serait un piège.
    expect(await LocationLink().restorePreference(), isTrue);
  });

  test('un refus explicite est mémorisé entre deux lancements', () async {
    SharedPreferences.setMockInitialValues({'tracking_enabled': false});
    expect(await LocationLink().restorePreference(), isFalse);
  });

  test('arrêter le suivi enregistre le choix', () async {
    final link = LocationLink();
    await link.restorePreference();
    await link.stop();

    expect(link.enabled, isFalse);
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getBool('tracking_enabled'), isFalse);
  });

  test('une coupure de session n’efface pas la préférence', () async {
    final link = LocationLink();
    await link.restorePreference();

    // La déconnexion coupe le service pour épargner la batterie, mais elle ne
    // vaut pas refus : au rebranchement, le suivi doit repartir seul.
    await link.stop(remember: false);

    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getBool('tracking_enabled'), isNot(false));
  });

  test('l’état est lisible avant le premier point', () {
    final link = LocationLink();
    expect(link.running, isFalse);
    expect(link.status, contains('désactivé'));
    expect(link.sent, 0);
  });

  test('la cadence de fond ménage la batterie', () {
    // Trente secondes : assez pour suivre un trajet, assez rare pour laisser
    // la radio se rendormir entre deux points.
    expect(kTrackingInterval.inSeconds, 30);
  });
}
