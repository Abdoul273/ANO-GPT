import 'package:flutter/material.dart';

import 'home_page.dart';
import 'pairing_page.dart';
import 'session.dart';
import 'theme.dart';

// Réexports : point d'entrée unique pour les tests et les écrans.
export 'net.dart';
export 'session.dart';
export 'voice_link.dart';

void main() {
  runApp(const AnoRemoteApp());
}

class AnoRemoteApp extends StatefulWidget {
  const AnoRemoteApp({super.key});

  @override
  State<AnoRemoteApp> createState() => _AnoRemoteAppState();
}

class _AnoRemoteAppState extends State<AnoRemoteApp> {
  final AnoSession session = AnoSession();

  @override
  void dispose() {
    session.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
    debugShowCheckedModeBanner: false,
    title: 'ANO Remote',
    theme: Ano.theme(),
    // Un seul écouteur au sommet : chaque changement de session redessine
    // l'écran courant, sans que chaque widget s'abonne de son côté.
    home: AnimatedBuilder(
      animation: session,
      builder: (_, _) => session.connected
          ? HomePage(session: session)
          : PairingPage(session: session),
    ),
  );
}
