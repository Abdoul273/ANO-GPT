import 'package:flutter/material.dart';

import 'net.dart';
import 'scanner_page.dart';
import 'session.dart';
import 'theme.dart';

/// Écran d'appairage : scanner, découverte automatique, ou saisie manuelle.
class PairingPage extends StatefulWidget {
  const PairingPage({super.key, required this.session});

  final AnoSession session;

  @override
  State<PairingPage> createState() => _PairingPageState();
}

class _PairingPageState extends State<PairingPage> {
  final addressController = TextEditingController();
  final keyController = TextEditingController();
  bool searching = false;

  AnoSession get session => widget.session;

  @override
  void initState() {
    super.initState();
    session.restore().then((base) {
      if (mounted && base.isNotEmpty) addressController.text = base;
    });
  }

  @override
  void dispose() {
    addressController.dispose();
    keyController.dispose();
    super.dispose();
  }

  Future<void> _search() async {
    setState(() => searching = true);
    await session.search();
    if (!mounted) return;
    setState(() {
      searching = false;
      if (session.discovered.length == 1) {
        addressController.text = session.discovered.first.baseUrl;
      }
    });
  }

  Future<void> _scan() async {
    final raw = await Navigator.of(context).push<String>(
      MaterialPageRoute(builder: (_) => const ScannerPage()),
    );
    if (raw == null || !mounted) return;
    try {
      final parsed = parseQrPayload(raw, fallbackAddress: addressController.text);
      addressController.text = parsed.address;
      if (parsed.key.isNotEmpty) keyController.text = parsed.key;
      await session.pair(
        parsed.address,
        parsed.key.isEmpty ? keyController.text.trim().toUpperCase() : parsed.key,
      );
    } on RemoteException catch (exception) {
      if (mounted) setState(() => session.error = exception.message);
    }
  }

  void _connect() => session.pair(
    addressController.text,
    keyController.text.trim().toUpperCase(),
  );

  @override
  Widget build(BuildContext context) {
    final busy = session.busy;
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 460),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const SizedBox(height: 12),
                  const Icon(Icons.hub_outlined, size: 62, color: Ano.primary),
                  const SizedBox(height: 18),
                  const Text(
                    'ANO REMOTE',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: 30,
                      fontWeight: FontWeight.w800,
                      letterSpacing: 6,
                    ),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    switch (session.status) {
                      LinkStatus.connecting => 'CONNEXION…',
                      LinkStatus.failed => 'ÉCHEC',
                      LinkStatus.online => 'CONNECTÉ',
                      LinkStatus.offline => 'NON CONNECTÉ',
                    },
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: Ano.green, letterSpacing: 3),
                  ),
                  const SizedBox(height: 30),
                  FilledButton.icon(
                    onPressed: busy ? null : _scan,
                    style: FilledButton.styleFrom(
                      backgroundColor: Ano.primary,
                      foregroundColor: Ano.bg,
                      padding: const EdgeInsets.symmetric(vertical: 18),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(16),
                      ),
                    ),
                    icon: const Icon(Icons.qr_code_scanner),
                    label: const Text(
                      'SCANNER LE QR CODE',
                      style: TextStyle(fontWeight: FontWeight.w800, letterSpacing: 1),
                    ),
                  ),
                  const SizedBox(height: 12),
                  OutlinedButton.icon(
                    onPressed: busy || searching ? null : _search,
                    style: OutlinedButton.styleFrom(
                      foregroundColor: Ano.primary,
                      side: const BorderSide(color: Ano.border),
                      padding: const EdgeInsets.symmetric(vertical: 16),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(16),
                      ),
                    ),
                    icon: searching
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.wifi_find),
                    label: const Text('TROUVER MON PC'),
                  ),
                  if (session.discovered.length > 1) ...[
                    const SizedBox(height: 12),
                    for (final server in session.discovered)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: AnoCard(
                          padding: EdgeInsets.zero,
                          child: ListTile(
                            leading: const Icon(Icons.computer, color: Ano.primary),
                            title: Text(server.name),
                            subtitle: Text(
                              server.baseUrl,
                              style: const TextStyle(color: Ano.textDim),
                            ),
                            onTap: () => setState(
                              () => addressController.text = server.baseUrl,
                            ),
                          ),
                        ),
                      ),
                  ],
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 20),
                    child: Row(
                      children: [
                        Expanded(child: Divider()),
                        Padding(
                          padding: EdgeInsets.symmetric(horizontal: 14),
                          child: Text('OU', style: TextStyle(color: Ano.textDim)),
                        ),
                        Expanded(child: Divider()),
                      ],
                    ),
                  ),
                  TextField(
                    controller: addressController,
                    keyboardType: TextInputType.url,
                    autocorrect: false,
                    decoration: const InputDecoration(
                      labelText: 'Adresse du PC',
                      hintText: '192.168.1.137',
                      helperText: 'Le schéma et le port sont détectés seuls.',
                      helperStyle: TextStyle(color: Ano.textDim, fontSize: 11),
                      prefixIcon: Icon(Icons.lan_outlined, color: Ano.primaryDim),
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: keyController,
                    textCapitalization: TextCapitalization.characters,
                    autocorrect: false,
                    onSubmitted: (_) => busy ? null : _connect(),
                    decoration: const InputDecoration(
                      labelText: 'Clé affichée par ANO-GPT',
                      prefixIcon: Icon(Icons.key_outlined, color: Ano.primaryDim),
                    ),
                  ),
                  if (session.hint != null) ...[
                    const SizedBox(height: 14),
                    Text(
                      session.hint!,
                      textAlign: TextAlign.center,
                      style: const TextStyle(color: Ano.primary, fontSize: 13),
                    ),
                  ],
                  if (session.error != null) ...[
                    const SizedBox(height: 14),
                    AnoCard(
                      accent: Ano.red.withValues(alpha: 0.4),
                      child: Text(
                        session.error!,
                        style: const TextStyle(
                          color: Color(0xFFFF9AA5),
                          height: 1.45,
                          fontSize: 12.5,
                        ),
                      ),
                    ),
                  ],
                  const SizedBox(height: 18),
                  FilledButton.icon(
                    onPressed: busy ? null : _connect,
                    style: FilledButton.styleFrom(
                      backgroundColor: Ano.surfaceHigh,
                      foregroundColor: Ano.primary,
                      padding: const EdgeInsets.symmetric(vertical: 16),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(16),
                        side: const BorderSide(color: Ano.border),
                      ),
                    ),
                    icon: busy
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.link),
                    label: const Text('CONNECTER'),
                  ),
                  const SizedBox(height: 22),
                  const Text(
                    'Le téléphone et le PC doivent être sur le même Wi-Fi.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Ano.textDim, height: 1.5, fontSize: 12),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
