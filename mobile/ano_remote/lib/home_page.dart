import 'package:camera/camera.dart';
import 'package:flutter/material.dart';

import 'session.dart';
import 'theme.dart';
import 'voice_link.dart';
import 'voice_orb.dart';

/// Écran principal : parler, filmer, commander, revoir les captures.
class HomePage extends StatefulWidget {
  const HomePage({super.key, required this.session});

  final AnoSession session;

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final commandController = TextEditingController();
  int tab = 0;

  AnoSession get session => widget.session;

  @override
  void dispose() {
    commandController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Column(
          children: [
            _header(),
            if (session.pendingConfirmation != null) _confirmationBanner(),
            Expanded(
              child: IndexedStack(
                index: tab,
                children: [
                  _voiceTab(),
                  _phoneTab(),
                  _cameraTab(),
                  _galleryTab(),
                ],
              ),
            ),
          ],
        ),
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: tab,
        onDestinationSelected: (index) {
          setState(() => tab = index);
          if (index == 3) session.refreshCaptures();
        },
        backgroundColor: Ano.surface,
        indicatorColor: Ano.primary.withValues(alpha: 0.16),
        destinations: const [
          NavigationDestination(icon: Icon(Icons.graphic_eq), label: 'Parler'),
          NavigationDestination(
            icon: Icon(Icons.phone_outlined),
            selectedIcon: Icon(Icons.phone),
            label: 'Téléphone',
          ),
          NavigationDestination(
            icon: Icon(Icons.videocam_outlined),
            selectedIcon: Icon(Icons.videocam),
            label: 'Caméra',
          ),
          NavigationDestination(
            icon: Icon(Icons.photo_library_outlined),
            selectedIcon: Icon(Icons.photo_library),
            label: 'Captures',
          ),
        ],
      ),
    );
  }

  // ── bandeau d'état ─────────────────────────────────────────────────────

  Widget _header() => Container(
    padding: const EdgeInsets.fromLTRB(18, 12, 10, 12),
    decoration: const BoxDecoration(
      color: Ano.surface,
      border: Border(bottom: BorderSide(color: Ano.border)),
    ),
    child: Row(
      children: [
        Container(
          width: 9,
          height: 9,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: session.connected ? Ano.green : Ano.red,
            boxShadow: [
              BoxShadow(
                color: (session.connected ? Ano.green : Ano.red).withValues(
                  alpha: 0.6,
                ),
                blurRadius: 8,
              ),
            ],
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                session.serverName.isEmpty ? 'ANO-GPT' : session.serverName,
                style: const TextStyle(
                  fontWeight: FontWeight.w700,
                  letterSpacing: 1.5,
                ),
              ),
              Text(
                session.hint ?? session.locationStatus,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: Ano.textDim, fontSize: 11),
              ),
            ],
          ),
        ),
        // Trois actions dans un bandeau étroit : densité compacte, sinon les
        // icônes rognent le nom du PC sur un petit écran.
        _headerButton(
          tooltip: session.location.running
              ? 'Suivi GPS actif — couper'
              : 'Activer le suivi GPS continu',
          icon: session.location.running
              ? Icons.location_on
              : Icons.location_off_outlined,
          color: session.location.running ? Ano.green : Ano.textDim,
          onPressed: session.toggleTracking,
        ),
        _headerButton(
          tooltip: 'Réveiller',
          icon: Icons.power_settings_new,
          color: Ano.primary,
          onPressed: session.wake,
        ),
        _headerButton(
          tooltip: 'Déconnecter',
          icon: Icons.link_off,
          color: Ano.textDim,
          onPressed: session.disconnect,
        ),
      ],
    ),
  );

  Widget _confirmationBanner() {
    final pending = session.pendingConfirmation!;
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.fromLTRB(12, 10, 12, 0),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Ano.red.withValues(alpha: 0.12),
        border: Border.all(color: Ano.red.withValues(alpha: 0.65)),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            pending['title']?.toString() ?? 'CONFIRMATION REQUISE',
            style: const TextStyle(
              fontWeight: FontWeight.w700,
              color: Ano.text,
            ),
          ),
          const SizedBox(height: 5),
          Text(
            pending['detail']?.toString() ?? '',
            style: const TextStyle(color: Ano.textDim, fontSize: 12),
          ),
          const SizedBox(height: 10),
          Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              TextButton(
                onPressed: () => session.answerConfirmation(false),
                child: const Text('ANNULER'),
              ),
              const SizedBox(width: 8),
              FilledButton.icon(
                onPressed: () => session.answerConfirmation(true),
                icon: const Icon(Icons.verified_user, size: 18),
                label: const Text('CONFIRMER'),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _headerButton({
    required String tooltip,
    required IconData icon,
    required Color color,
    required VoidCallback onPressed,
  }) => IconButton(
    tooltip: tooltip,
    onPressed: onPressed,
    icon: Icon(icon, color: color, size: 21),
    visualDensity: VisualDensity.compact,
    padding: const EdgeInsets.symmetric(horizontal: 7),
    constraints: const BoxConstraints(minWidth: 36, minHeight: 36),
  );

  // ── onglet parole ──────────────────────────────────────────────────────

  Widget _voiceTab() => LayoutBuilder(
    builder: (context, constraints) {
      final wide = constraints.maxWidth >= 680 && constraints.maxHeight >= 540;
      final orb = _voiceOrb();
      if (wide) {
        return Row(
          children: [
            Expanded(flex: 6, child: _conversation()),
            const VerticalDivider(),
            SizedBox(
              width: 330,
              child: Column(
                children: [
                  Expanded(child: orb),
                  _commandBar(),
                ],
              ),
            ),
          ],
        );
      }
      final orbHeight = constraints.maxHeight < 600 ? 188.0 : 258.0;
      return Column(
        children: [
          SizedBox(height: orbHeight, child: orb),
          Expanded(child: _conversation()),
          _commandBar(),
        ],
      );
    },
  );

  Widget _voiceOrb() {
    final voice = session.voice;
    final hands = voice.mode == MicMode.hands;
    return VoiceOrb(
      mode: voice.mode,
      level: voice.level,
      error: voice.error,
      onPushStart: () {
        if (!hands) session.startVoice(MicMode.push);
      },
      onPushEnd: () {
        if (!hands) session.stopVoice();
      },
      onToggleHandsFree: () =>
          hands ? session.stopVoice() : session.startVoice(MicMode.hands),
    );
  }

  Widget _conversation() {
    final messages = session.messages;
    return ListView.builder(
      reverse: true,
      padding: const EdgeInsets.fromLTRB(14, 14, 14, 6),
      itemCount: messages.length,
      itemBuilder: (_, index) {
        final entry = messages[messages.length - 1 - index];
        final mine = entry.kind == 'user';
        final failed = entry.kind == 'error';
        final system = entry.kind == 'system';
        return Align(
          alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
          child: Container(
            constraints: const BoxConstraints(maxWidth: 320),
            margin: const EdgeInsets.symmetric(vertical: 4),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
            decoration: BoxDecoration(
              color: failed
                  ? Ano.red.withValues(alpha: 0.16)
                  : mine
                  ? Ano.primary.withValues(alpha: 0.18)
                  : Ano.surface,
              borderRadius: BorderRadius.circular(16),
              border: Border.all(
                color: failed
                    ? Ano.red.withValues(alpha: 0.5)
                    : mine
                    ? Ano.primary.withValues(alpha: 0.4)
                    : Ano.border,
              ),
            ),
            child: Text(
              entry.text,
              style: TextStyle(
                color: failed
                    ? const Color(0xFFFF9AA5)
                    : system
                    ? Ano.textDim
                    : Ano.text,
                fontSize: system ? 12 : 14,
                height: 1.35,
              ),
            ),
          ),
        );
      },
    );
  }

  // ignore: unused_element
  Widget _micBar() {
    final voice = session.voice;
    final live = voice.streaming;
    final hands = voice.mode == MicMode.hands;
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 4, 14, 4),
      child: AnoCard(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        accent: live ? Ano.green.withValues(alpha: 0.55) : null,
        child: Row(
          children: [
            // Appui maintenu : le micro ne vit que pendant la pression, ce qui
            // évite d'envoyer la pièce entière au PC sans s'en apercevoir.
            GestureDetector(
              onTapDown: (_) {
                if (!hands) session.startVoice(MicMode.push);
              },
              onTapUp: (_) {
                if (!hands) session.stopVoice();
              },
              onTapCancel: () {
                if (!hands) session.stopVoice();
              },
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 120),
                width: 62,
                height: 62,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: live
                      ? Ano.green.withValues(alpha: 0.22)
                      : Ano.surfaceHigh,
                  border: Border.all(
                    color: live ? Ano.green : Ano.border,
                    width: 2,
                  ),
                  boxShadow: live
                      ? [
                          BoxShadow(
                            color: Ano.green.withValues(
                              alpha: 0.25 + voice.level * 0.5,
                            ),
                            blurRadius: 12 + voice.level * 26,
                          ),
                        ]
                      : null,
                ),
                child: Icon(
                  live ? Icons.mic : Icons.mic_none,
                  color: live ? Ano.green : Ano.primary,
                  size: 28,
                ),
              ),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    live
                        ? (hands ? 'MAINS LIBRES' : 'PARLEZ…')
                        : 'MAINTENIR POUR PARLER',
                    style: TextStyle(
                      color: live ? Ano.green : Ano.textDim,
                      fontSize: 11,
                      fontWeight: FontWeight.w700,
                      letterSpacing: 1.6,
                    ),
                  ),
                  const SizedBox(height: 6),
                  ClipRRect(
                    borderRadius: BorderRadius.circular(4),
                    child: LinearProgressIndicator(
                      value: live ? voice.level.clamp(0.02, 1.0) : 0.0,
                      minHeight: 5,
                      backgroundColor: Ano.surfaceHigh,
                      valueColor: const AlwaysStoppedAnimation(Ano.green),
                    ),
                  ),
                  const SizedBox(height: 4),
                  const Text(
                    'Le micro du PC se coupe pendant ce temps.',
                    style: TextStyle(color: Ano.textDim, fontSize: 10.5),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            IconButton(
              tooltip: 'Mains libres',
              onPressed: () => hands
                  ? session.stopVoice()
                  : session.startVoice(MicMode.hands),
              icon: Icon(
                hands ? Icons.lock : Icons.lock_open,
                color: hands ? Ano.green : Ano.textDim,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _commandBar() => Padding(
    padding: const EdgeInsets.fromLTRB(14, 4, 14, 12),
    child: Row(
      children: [
        Expanded(
          child: TextField(
            controller: commandController,
            textInputAction: TextInputAction.send,
            onSubmitted: (_) => _send(),
            decoration: const InputDecoration(
              hintText: 'Écrire une commande…',
              contentPadding: EdgeInsets.symmetric(
                horizontal: 16,
                vertical: 14,
              ),
            ),
          ),
        ),
        const SizedBox(width: 8),
        IconButton.filled(
          onPressed: _send,
          style: IconButton.styleFrom(
            backgroundColor: Ano.primary,
            foregroundColor: Ano.bg,
            padding: const EdgeInsets.all(14),
          ),
          icon: const Icon(Icons.send),
        ),
      ],
    ),
  );

  void _send() {
    final text = commandController.text;
    commandController.clear();
    session.sendCommand(text);
  }

  // ── onglet téléphone ──────────────────────────────────────────────────

  Widget _phoneTab() {
    final phone = session.phone;
    return ListView(
      padding: const EdgeInsets.all(14),
      children: [
        const AnoLabel('Téléphonie Android'),
        const SizedBox(height: 10),
        AnoCard(
          accent: phone.ready ? Ano.green.withValues(alpha: 0.5) : null,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(
                    phone.ready
                        ? Icons.verified_user
                        : Icons.phone_locked_outlined,
                    color: phone.ready ? Ano.green : Ano.primary,
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text(
                      phone.ready ? 'PRÊT À APPELER' : 'AUTORISATION REQUISE',
                      style: TextStyle(
                        color: phone.ready ? Ano.green : Ano.primary,
                        fontWeight: FontWeight.w700,
                        letterSpacing: 1.2,
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 14),
              _permissionLine('Contacts Android', phone.contactsAllowed),
              _permissionLine('Passage d’appels', phone.callsAllowed),
              _permissionLine('Envoi de SMS', phone.smsAllowed),
              _permissionLine('Raccrochage', phone.hangupAllowed),
              _permissionLine(
                'Carte SIM / téléphonie',
                phone.telephonyAvailable,
              ),
              const SizedBox(height: 12),
              if (!phone.fullyGranted)
                FilledButton.icon(
                  onPressed: phone.telephonyAvailable
                      ? session.phone.requestPermissions
                      : null,
                  icon: const Icon(Icons.admin_panel_settings_outlined),
                  label: const Text('AUTORISER APPELS, SMS ET CONTACTS'),
                ),
            ],
          ),
        ),
        const SizedBox(height: 12),
        AnoCard(
          child: SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: phone.automaticCallsEnabled,
            onChanged: phone.telephonyAvailable
                ? session.phone.setAutomaticCalls
                : null,
            activeThumbColor: Ano.green,
            title: const Text('Appels, SMS et raccrochage par ANO'),
            subtitle: const Text(
              'ANO-GPT transmet seulement le nom prononcé et le texte du SMS. '
              'Ce téléphone cherche le contact dans Android et utilise sa '
              'propre carte SIM.',
              style: TextStyle(color: Ano.textDim, fontSize: 12, height: 1.4),
            ),
          ),
        ),
        const SizedBox(height: 12),
        AnoCard(
          child: Text(
            phone.error ?? phone.lastStatus,
            style: TextStyle(
              color: phone.error == null ? Ano.textDim : Ano.red,
              height: 1.4,
            ),
          ),
        ),
        const SizedBox(height: 14),
        const Text(
          'Exemple : « ANO, appelle le garage ». Si plusieurs contacts ou '
          'plusieurs numéros correspondent, aucun appel n’est lancé et ANO '
          'demande lequel utiliser.',
          style: TextStyle(color: Ano.textDim, fontSize: 12, height: 1.5),
        ),
      ],
    );
  }

  Widget _permissionLine(String label, bool granted) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 4),
    child: Row(
      children: [
        Icon(
          granted ? Icons.check_circle : Icons.cancel_outlined,
          color: granted ? Ano.green : Ano.red,
          size: 18,
        ),
        const SizedBox(width: 9),
        Text(label),
      ],
    ),
  );

  // ── onglet caméra ──────────────────────────────────────────────────────

  Widget _cameraTab() {
    final camera = session.camera;
    return SingleChildScrollView(
      padding: const EdgeInsets.all(14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          AnoCard(
            padding: EdgeInsets.zero,
            accent: camera.streaming ? Ano.green.withValues(alpha: 0.5) : null,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(17),
              child: AspectRatio(aspectRatio: 3 / 4, child: _viewfinder()),
            ),
          ),
          const SizedBox(height: 14),
          Row(
            children: [
              Expanded(
                child: FilledButton.icon(
                  onPressed: session.toggleCamera,
                  style: FilledButton.styleFrom(
                    backgroundColor: camera.streaming
                        ? Ano.red.withValues(alpha: 0.2)
                        : Ano.primary,
                    foregroundColor: camera.streaming ? Ano.red : Ano.bg,
                    padding: const EdgeInsets.symmetric(vertical: 16),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(14),
                    ),
                  ),
                  icon: Icon(
                    camera.streaming ? Icons.videocam_off : Icons.videocam,
                  ),
                  label: Text(camera.streaming ? 'ARRÊTER' : 'DIFFUSER AU PC'),
                ),
              ),
              if (camera.canSwitch) ...[
                const SizedBox(width: 10),
                IconButton.outlined(
                  onPressed: () => session.camera.switchCamera(session.api!),
                  // L'étiquette dit vers quoi on bascule, pas où l'on est :
                  // c'est ce que la personne cherche en appuyant.
                  tooltip: camera.isFront
                      ? 'Passer à la caméra arrière'
                      : 'Passer à la caméra frontale',
                  style: IconButton.styleFrom(
                    padding: const EdgeInsets.all(16),
                    side: const BorderSide(color: Ano.border),
                    foregroundColor: camera.isFront ? Ano.green : Ano.primary,
                  ),
                  icon: Icon(
                    camera.isFront
                        ? Icons.camera_front
                        : Icons.flip_camera_android,
                  ),
                ),
              ],
            ],
          ),
          const SizedBox(height: 14),
          const AnoLabel('Commandes'),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: _actionTile(
                  Icons.camera_alt,
                  'PHOTO',
                  () => session.requestCapture('photo'),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: _actionTile(
                  Icons.fiber_manual_record,
                  'VIDÉO',
                  () => session.requestCapture('video_start'),
                  color: Ano.red,
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: _actionTile(
                  Icons.stop,
                  'STOP',
                  () => session.requestCapture('video_stop'),
                ),
              ),
            ],
          ),
          const SizedBox(height: 14),
          const Text(
            'Ces boutons parlent à ANO-GPT : la capture est enregistrée sur le '
            'PC, quelle que soit la caméra utilisée. Vous pouvez aussi le dire '
            'à voix haute.',
            style: TextStyle(color: Ano.textDim, fontSize: 12, height: 1.5),
          ),
        ],
      ),
    );
  }

  Widget _viewfinder() {
    final camera = session.camera;
    final controller = camera.controller;
    if (camera.starting) {
      return const ColoredBox(
        color: Ano.surfaceHigh,
        child: Center(child: CircularProgressIndicator()),
      );
    }
    if (controller != null && controller.value.isInitialized) {
      return Stack(
        fit: StackFit.expand,
        children: [
          CameraPreview(controller),
          Positioned(
            left: 12,
            top: 12,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
              decoration: BoxDecoration(
                color: Colors.black.withValues(alpha: 0.55),
                borderRadius: BorderRadius.circular(20),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.circle, size: 8, color: Ano.green),
                  const SizedBox(width: 6),
                  Text(
                    'EN DIRECT · ${camera.isFront ? 'FRONTALE' : 'ARRIÈRE'}'
                    ' · ${camera.framesSent} img',
                    style: const TextStyle(fontSize: 10, letterSpacing: 1),
                  ),
                ],
              ),
            ),
          ),
        ],
      );
    }
    return ColoredBox(
      color: Ano.surfaceHigh,
      child: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                camera.error != null
                    ? Icons.videocam_off
                    : Icons.videocam_outlined,
                size: 46,
                color: camera.error != null ? Ano.red : Ano.primaryDim,
              ),
              const SizedBox(height: 14),
              Text(
                camera.error ??
                    'La caméra du téléphone s’affichera en grand sur le PC.',
                textAlign: TextAlign.center,
                style: TextStyle(
                  color: camera.error != null ? Ano.red : Ano.textDim,
                  fontSize: 12.5,
                  height: 1.5,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _actionTile(
    IconData icon,
    String label,
    VoidCallback onTap, {
    Color color = Ano.primary,
  }) => InkWell(
    onTap: onTap,
    borderRadius: BorderRadius.circular(14),
    child: AnoCard(
      padding: const EdgeInsets.symmetric(vertical: 16),
      child: Column(
        children: [
          Icon(icon, color: color),
          const SizedBox(height: 8),
          Text(
            label,
            style: const TextStyle(
              fontSize: 10.5,
              fontWeight: FontWeight.w700,
              letterSpacing: 1.4,
            ),
          ),
        ],
      ),
    ),
  );

  // ── onglet captures ────────────────────────────────────────────────────

  Widget _galleryTab() {
    final captures = session.captures;
    if (captures.isEmpty) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(32),
          child: Text(
            'Aucune capture pour l’instant.\nDites « prends une photo » pour '
            'commencer.',
            textAlign: TextAlign.center,
            style: TextStyle(color: Ano.textDim, height: 1.6),
          ),
        ),
      );
    }
    return RefreshIndicator(
      onRefresh: session.refreshCaptures,
      child: ListView.separated(
        padding: const EdgeInsets.all(14),
        itemCount: captures.length,
        separatorBuilder: (_, _) => const SizedBox(height: 8),
        itemBuilder: (_, index) {
          final entry = captures[index];
          final name = entry['name']?.toString() ?? '';
          final size = (entry['size'] as num?)?.toInt() ?? 0;
          final video = name.toLowerCase().endsWith('.mp4');
          return AnoCard(
            padding: EdgeInsets.zero,
            child: ListTile(
              leading: Icon(
                video ? Icons.movie_outlined : Icons.image_outlined,
                color: Ano.primary,
              ),
              title: Text(name, maxLines: 1, overflow: TextOverflow.ellipsis),
              subtitle: Text(
                _humanSize(size),
                style: const TextStyle(color: Ano.textDim, fontSize: 12),
              ),
            ),
          );
        },
      ),
    );
  }

  static String _humanSize(int bytes) {
    if (bytes >= 1024 * 1024) {
      return '${(bytes / (1024 * 1024)).toStringAsFixed(1)} Mo';
    }
    if (bytes >= 1024) return '${(bytes / 1024).toStringAsFixed(0)} Ko';
    return '$bytes o';
  }
}
