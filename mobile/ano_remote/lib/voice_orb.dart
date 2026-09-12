import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/widget_previews.dart';

import 'theme.dart';
import 'voice_link.dart';

/// Le coeur visuel d'ANO Remote. Il ne traite pas le PCM : [level] est déjà
/// calculé et lissé par VoiceLink, ce qui garde l'animation fluide et légère.
class VoiceOrb extends StatefulWidget {
  const VoiceOrb({
    super.key,
    required this.mode,
    required this.level,
    this.error,
    this.onPushStart,
    this.onPushEnd,
    this.onToggleHandsFree,
  });

  final MicMode mode;
  final double level;
  final String? error;
  final VoidCallback? onPushStart;
  final VoidCallback? onPushEnd;
  final VoidCallback? onToggleHandsFree;

  @override
  State<VoiceOrb> createState() => _VoiceOrbState();
}

class _VoiceOrbState extends State<VoiceOrb>
    with SingleTickerProviderStateMixin {
  late final AnimationController _pulse = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1800),
  )..repeat();

  @override
  void dispose() {
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final handsFree = widget.mode == MicMode.hands;
    final active = widget.mode != MicMode.off;
    final error = widget.error?.isNotEmpty == true;
    final color = error
        ? Ano.red
        : active
        ? Ano.green
        : Ano.primary;
    final title = error
        ? 'MICRO INDISPONIBLE'
        : handsFree
        ? 'MAINS LIBRES ACTIF'
        : active
        ? 'PARLEZ…'
        : 'MAINTENIR POUR PARLER';
    final detail = error
        ? widget.error!
        : handsFree
        ? 'Touchez le cadenas pour couper le micro.'
        : 'Maintenez l’orbe, ou activez le mains libres.';

    return Semantics(
      button: true,
      label: handsFree
          ? 'Couper le micro mains libres'
          : 'Maintenir pour parler',
      child: RepaintBoundary(
        child: AnimatedBuilder(
          animation: _pulse,
          builder: (context, _) => LayoutBuilder(
            builder: (context, constraints) {
              final diameter = math
                  .min(constraints.maxWidth, constraints.maxHeight * .72)
                  .clamp(118.0, 244.0);
              return Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    GestureDetector(
                      onTapDown: handsFree
                          ? null
                          : (_) => widget.onPushStart?.call(),
                      onTapUp: handsFree
                          ? null
                          : (_) => widget.onPushEnd?.call(),
                      onTapCancel: handsFree ? null : widget.onPushEnd,
                      child: SizedBox(
                        width: diameter + 34,
                        height: diameter + 34,
                        child: CustomPaint(
                          painter: _OrbPainter(
                            phase: _pulse.value,
                            level: widget.level,
                            color: color,
                            active: active,
                            error: error,
                          ),
                          child: Center(
                            child: AnimatedContainer(
                              duration: const Duration(milliseconds: 140),
                              width: diameter * (.44 + widget.level * .10),
                              height: diameter * (.44 + widget.level * .10),
                              decoration: BoxDecoration(
                                shape: BoxShape.circle,
                                color: Ano.bg.withValues(alpha: .88),
                                border: Border.all(color: color, width: 2),
                              ),
                              child: Icon(
                                error
                                    ? Icons.mic_off_rounded
                                    : Icons.mic_rounded,
                                color: color,
                                size: diameter * .22,
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(height: 3),
                    Text(
                      title,
                      style: TextStyle(
                        color: color,
                        fontWeight: FontWeight.w800,
                        letterSpacing: 1.8,
                        fontSize: 11,
                      ),
                    ),
                    const SizedBox(height: 5),
                    Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.graphic_eq_rounded, color: color, size: 16),
                        const SizedBox(width: 6),
                        Flexible(
                          child: Text(
                            detail,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Ano.textDim,
                              fontSize: 11,
                            ),
                          ),
                        ),
                        const SizedBox(width: 5),
                        IconButton(
                          tooltip: handsFree
                              ? 'Couper le mains libres'
                              : 'Activer le mains libres',
                          onPressed: widget.onToggleHandsFree,
                          color: handsFree ? Ano.green : Ano.textDim,
                          icon: Icon(
                            handsFree
                                ? Icons.lock_rounded
                                : Icons.lock_open_rounded,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              );
            },
          ),
        ),
      ),
    );
  }
}

class _OrbPainter extends CustomPainter {
  const _OrbPainter({
    required this.phase,
    required this.level,
    required this.color,
    required this.active,
    required this.error,
  });
  final double phase;
  final double level;
  final Color color;
  final bool active;
  final bool error;

  @override
  void paint(Canvas canvas, Size size) {
    final center = size.center(Offset.zero);
    final base = math.min(size.width, size.height) * .30;
    final energy = active ? .08 + level * .19 : .035;
    for (var ring = 0; ring < 3; ring++) {
      final wobble =
          math.sin((phase + ring * .23) * math.pi * 2) * (active ? 7 : 3);
      final radius = base + ring * 16 + wobble + level * 16;
      canvas.drawCircle(
        center,
        radius,
        Paint()
          ..color = color.withValues(alpha: .34 - ring * .08)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.2 + level * 1.6,
      );
    }
    final glow = Paint()
      ..color = color.withValues(alpha: .08 + energy)
      ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 26);
    canvas.drawCircle(center, base * (1.2 + level * .22), glow);
    final dotPaint = Paint()
      ..color = color.withValues(alpha: error ? .85 : .58);
    for (var i = 0; i < 18; i++) {
      final angle =
          i * math.pi * 2 / 18 + phase * math.pi * 2 * (active ? 1 : .25);
      final radius =
          base + 31 + math.sin(phase * math.pi * 2 + i) * (5 + level * 13);
      final dot = Offset(
        center.dx + math.cos(angle) * radius,
        center.dy + math.sin(angle) * radius,
      );
      canvas.drawCircle(dot, 1.2 + level * 1.8, dotPaint);
    }
  }

  @override
  bool shouldRepaint(covariant _OrbPainter old) =>
      old.phase != phase ||
      old.level != level ||
      old.color != color ||
      old.active != active ||
      old.error != error;
}

@Preview(name: 'Orb — repos', group: 'ANO Remote', size: Size(360, 310))
Widget voiceOrbIdlePreview() =>
    const Scaffold(body: VoiceOrb(mode: MicMode.off, level: 0));

@Preview(name: 'Orb — mains libres', group: 'ANO Remote', size: Size(360, 310))
Widget voiceOrbLivePreview() =>
    const Scaffold(body: VoiceOrb(mode: MicMode.hands, level: .68));
