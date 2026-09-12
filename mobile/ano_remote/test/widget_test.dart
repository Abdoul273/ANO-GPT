import 'dart:io';

import 'package:ano_remote/net.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('accepte les adresses locales, refuse les adresses publiques', () {
    expect(isLocalHost('192.168.1.137'), isTrue);
    expect(isLocalHost('10.0.0.2'), isTrue);
    expect(isLocalHost('172.20.10.3'), isTrue); // partage de connexion iPhone
    expect(isLocalHost('100.90.1.4'), isTrue); // CGNAT / Tailscale
    expect(isLocalHost('127.0.0.1'), isTrue);
    expect(isLocalHost('ano-linux.local'), isTrue);
    expect(isLocalHost('8.8.8.8'), isFalse);
    expect(isLocalHost('172.32.0.1'), isFalse);
    expect(isLocalHost('exemple.com'), isFalse);
  });

  test('essaie les deux schémas et les deux ports', () {
    // Sans schéma : HTTPS d'abord, mais HTTP reste tenté — c'est ce repli qui
    // manquait quand le PC servait en clair.
    final candidates = candidateUrls('192.168.1.137');
    expect(candidates.first, 'https://192.168.1.137:8000');
    expect(candidates, contains('http://192.168.1.137:8000'));
    expect(candidates, contains('https://192.168.1.137:8001'));
  });

  test('respecte le port saisi tout en gardant les ports connus en repli', () {
    final candidates = candidateUrls('http://192.168.1.137:8001');
    expect(candidates.first, 'http://192.168.1.137:8001');
    expect(candidates, contains('https://192.168.1.137:8001'));
    expect(candidates, contains('http://192.168.1.137:8000'));
  });

  test('rejette une adresse vide ou publique', () {
    expect(() => candidateUrls('   '), throwsA(isA<RemoteException>()));
    expect(() => candidateUrls('exemple.com'), throwsA(isA<RemoteException>()));
  });

  test('lit les différentes formes de QR code', () {
    final link = parseQrPayload('http://192.168.1.137:8000/auto-login?key=AB3D9F');
    expect(link.address, 'http://192.168.1.137:8000');
    expect(link.key, 'AB3D9F');

    // Adresse nue : `Uri` la range en chemin, il faut la rattraper.
    final bare = parseQrPayload('192.168.1.137:8001');
    expect(bare.address, '192.168.1.137:8001');
    expect(bare.key, isEmpty);

    final keyOnly = parseQrPayload('ab3d9f', fallbackAddress: '192.168.1.5');
    expect(keyOnly.address, '192.168.1.5');
    expect(keyOnly.key, 'AB3D9F');

    expect(
      () => parseQrPayload('https://exemple.com/promo'),
      returnsNormally, // hôte présent : rejeté plus tard par candidateUrls
    );
    expect(() => parseQrPayload('  '), throwsA(isA<RemoteException>()));
  });

  test('décrit précisément une connexion refusée', () {
    final message = describeFailure(
      const SocketException('refused', osError: OSError('refused', 111)),
      'http://192.168.1.137:8000',
    );
    expect(message, contains('connexion refusée'));
    expect(message, contains('192.168.1.137:8000'));
  });
}
