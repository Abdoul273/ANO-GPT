import 'package:flutter/material.dart';

/// Palette reprise de l'interface JARVIS : cyan sur bleu nuit, accents chauds
/// réservés à ce qui est en cours (enregistrement, micro ouvert).
abstract final class Ano {
  static const bg = Color(0xFF04080D);
  static const surface = Color(0xFF0A131C);
  static const surfaceHigh = Color(0xFF0F1C27);
  static const border = Color(0xFF12324A);
  static const primary = Color(0xFF00D4FF);
  static const primaryDim = Color(0xFF0A7E99);
  static const accent = Color(0xFFFF6B00);
  static const green = Color(0xFF00FF88);
  static const red = Color(0xFFFF3355);
  static const text = Color(0xFFDDEAF2);
  static const textDim = Color(0xFF6E8797);

  static ThemeData theme() {
    final base = ThemeData(
      brightness: Brightness.dark,
      useMaterial3: true,
      colorScheme: ColorScheme.fromSeed(
        seedColor: primary,
        brightness: Brightness.dark,
        surface: surface,
      ),
      scaffoldBackgroundColor: bg,
    );
    return base.copyWith(
      textTheme: base.textTheme.apply(bodyColor: text, displayColor: text),
      cardTheme: const CardThemeData(
        color: surface,
        elevation: 0,
        margin: EdgeInsets.zero,
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: surfaceHigh,
        hintStyle: const TextStyle(color: textDim),
        labelStyle: const TextStyle(color: textDim),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: border),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: primary),
        ),
      ),
      dividerTheme: const DividerThemeData(color: border, space: 1),
    );
  }
}

/// Carte sombre à liseré, brique visuelle unique de toute l'application.
class AnoCard extends StatelessWidget {
  const AnoCard({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(16),
    this.accent,
  });

  final Widget child;
  final EdgeInsets padding;
  final Color? accent;

  @override
  Widget build(BuildContext context) => Container(
    padding: padding,
    decoration: BoxDecoration(
      color: Ano.surface,
      borderRadius: BorderRadius.circular(18),
      border: Border.all(color: accent ?? Ano.border),
    ),
    child: child,
  );
}

/// Titre de section : petites capitales espacées, comme sur le PC.
class AnoLabel extends StatelessWidget {
  const AnoLabel(this.text, {super.key, this.trailing});

  final String text;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) => Row(
    children: [
      Text(
        text.toUpperCase(),
        style: const TextStyle(
          color: Ano.textDim,
          fontSize: 11,
          fontWeight: FontWeight.w700,
          letterSpacing: 2,
        ),
      ),
      const Spacer(),
      ?trailing,
    ],
  );
}
