import 'package:ano_remote/theme.dart';
import 'package:ano_remote/voice_link.dart';
import 'package:ano_remote/voice_orb.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('l’orb expose les commandes vocales mains libres', (
    tester,
  ) async {
    var pushStarted = false;
    var pushEnded = false;
    var toggled = false;
    await tester.pumpWidget(
      MaterialApp(
        theme: Ano.theme(),
        home: Scaffold(
          body: VoiceOrb(
            mode: MicMode.off,
            level: 0,
            onPushStart: () => pushStarted = true,
            onPushEnd: () => pushEnded = true,
            onToggleHandsFree: () => toggled = true,
          ),
        ),
      ),
    );

    expect(find.text('MAINTENIR POUR PARLER'), findsOneWidget);
    await tester.tapAt(tester.getCenter(find.byType(VoiceOrb)));
    await tester.pump();
    expect(pushStarted, isTrue);
    expect(pushEnded, isTrue);

    await tester.tap(find.byIcon(Icons.lock_open_rounded));
    expect(toggled, isTrue);
  });
}
