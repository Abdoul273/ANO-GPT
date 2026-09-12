import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import 'theme.dart';

class ScannerPage extends StatefulWidget {
  const ScannerPage({super.key});

  @override
  State<ScannerPage> createState() => _ScannerPageState();
}

class _ScannerPageState extends State<ScannerPage> {
  /// Contrôleur explicite : sans lui, une caméra refusée laissait un écran noir
  /// muet — le bouton « scanner » paraissait mort.
  MobileScannerController controller = MobileScannerController(
    detectionSpeed: DetectionSpeed.noDuplicates,
    formats: const [BarcodeFormat.qrCode],
  );
  bool detected = false;
  bool torchOn = false;

  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }

  Future<void> _restart() async {
    final previous = controller;
    setState(() {
      controller = MobileScannerController(
        detectionSpeed: DetectionSpeed.noDuplicates,
        formats: const [BarcodeFormat.qrCode],
      );
      detected = false;
    });
    await previous.dispose();
  }

  void _onDetect(BarcodeCapture capture) {
    if (detected) return;
    for (final barcode in capture.barcodes) {
      final value = barcode.rawValue;
      if (value != null && value.isNotEmpty) {
        detected = true;
        Navigator.of(context).pop(value);
        return;
      }
    }
  }

  Widget _error(BuildContext context, MobileScannerException exception) {
    final denied =
        exception.errorCode == MobileScannerErrorCode.permissionDenied;
    return ColoredBox(
      color: Ano.bg,
      child: Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                denied ? Icons.no_photography_outlined : Icons.videocam_off,
                size: 56,
                color: Ano.red,
              ),
              const SizedBox(height: 18),
              Text(
                denied ? 'Accès à la caméra refusé' : 'Caméra indisponible',
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 12),
              Text(
                denied
                    ? 'Autorisez la caméra dans Réglages ▸ Applications ▸ '
                          'ANO Remote ▸ Autorisations, puis réessayez. Sinon, '
                          'saisissez l’adresse et la clé à la main.'
                    : exception.errorDetails?.message ??
                          'Aucune caméra exploitable sur cet appareil. '
                              'Saisissez l’adresse et la clé à la main.',
                textAlign: TextAlign.center,
                style: const TextStyle(color: Ano.textDim, height: 1.5),
              ),
              const SizedBox(height: 24),
              FilledButton.icon(
                onPressed: _restart,
                icon: const Icon(Icons.refresh),
                label: const Text('RÉESSAYER'),
              ),
              const SizedBox(height: 8),
              TextButton(
                onPressed: () => Navigator.of(context).pop(),
                child: const Text('SAISIR À LA MAIN'),
              ),
            ],
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(
      backgroundColor: Ano.surface,
      title: const Text('Scanner le QR ANO-GPT'),
      actions: [
        IconButton(
          tooltip: 'Lampe',
          onPressed: () async {
            await controller.toggleTorch();
            if (mounted) setState(() => torchOn = !torchOn);
          },
          icon: Icon(torchOn ? Icons.flash_on : Icons.flash_off),
        ),
      ],
    ),
    body: Stack(
      fit: StackFit.expand,
      children: [
        MobileScanner(
          key: ValueKey(controller),
          controller: controller,
          onDetect: _onDetect,
          errorBuilder: _error,
          placeholderBuilder: (_) => const ColoredBox(
            color: Ano.bg,
            child: Center(child: CircularProgressIndicator()),
          ),
        ),
        IgnorePointer(
          child: Center(
            child: Container(
              width: 250,
              height: 250,
              decoration: BoxDecoration(
                border: Border.all(color: Ano.primary, width: 3),
                borderRadius: BorderRadius.circular(24),
              ),
            ),
          ),
        ),
        const Positioned(
          left: 24,
          right: 24,
          bottom: 44,
          child: IgnorePointer(
            child: Text(
              'Cadrez le QR affiché dans « Contrôle à distance »',
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
            ),
          ),
        ),
      ],
    ),
  );
}
