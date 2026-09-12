import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// Ports exposés par ANO-GPT : 8000 (principal) et 8001 (alias HTTPS).
const List<int> kServerPorts = [8000, 8001];

/// Port UDP de découverte automatique du PC.
const int kDiscoveryPort = 8009;
const String kDiscoveryMagic = 'ANO-GPT-DISCOVER';

class RemoteException implements Exception {
  const RemoteException(this.message);
  final String message;
  @override
  String toString() => message;
}

/// Hôtes joignables uniquement sur un réseau local ou personnel.
///
/// Le certificat d'ANO-GPT est auto-signé : on ne l'accepte que pour ce type
/// d'adresse, jamais pour un hôte public.
bool isLocalHost(String host) {
  final trimmed = host.trim().toLowerCase();
  if (trimmed.isEmpty) return false;
  if (trimmed == 'localhost' || trimmed.endsWith('.local')) return true;

  final parts = trimmed.split('.');
  if (parts.length != 4) return false;
  final octets = parts.map(int.tryParse).toList();
  if (octets.any((value) => value == null || value < 0 || value > 255)) {
    return false;
  }
  final a = octets[0]!, b = octets[1]!;
  return a == 10 ||
      a == 127 ||
      (a == 172 && b >= 16 && b <= 31) ||
      (a == 192 && b == 168) ||
      (a == 169 && b == 254) || // lien-local (partage de connexion)
      (a == 100 && b >= 64 && b <= 127); // CGNAT : hotspots opérateur, Tailscale
}

HttpClient lanClient() {
  final client = HttpClient();
  client.connectionTimeout = const Duration(seconds: 4);
  client.badCertificateCallback = (_, host, port) =>
      isLocalHost(host) && kServerPorts.contains(port);
  return client;
}

/// Traduit une panne réseau brute en explication actionnable.
String describeFailure(Object error, String url) {
  if (error is RemoteException) return error.message;
  if (error is TimeoutException) {
    return '$url n’a pas répondu à temps.';
  }
  if (error is HandshakeException) {
    return '$url : le certificat local a été refusé.';
  }
  if (error is SocketException) {
    final code = error.osError?.errorCode;
    if (code == 111) {
      return '$url : connexion refusée — ANO-GPT n’écoute pas sur ce port.';
    }
    if (code == 113 || code == 101) {
      return '$url : aucune route — le téléphone n’est pas sur ce réseau.';
    }
    return '$url : ${error.osError?.message ?? error.message}';
  }
  return '$url : $error';
}

/// Serveur ANO-GPT repéré sur le réseau.
class DiscoveredServer {
  const DiscoveredServer({
    required this.name,
    required this.baseUrl,
    required this.host,
  });

  final String name;
  final String baseUrl;
  final String host;
}

/// Diffuse une sonde UDP et collecte les PC qui répondent.
///
/// C'est le chemin sans saisie : plus d'IP à recopier, donc plus d'erreur
/// « serveur introuvable » due à une adresse périmée.
Future<List<DiscoveredServer>> discoverServers({
  Duration timeout = const Duration(seconds: 3),
}) async {
  final socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
  socket.broadcastEnabled = true;
  final found = <String, DiscoveredServer>{};
  final completer = Completer<List<DiscoveredServer>>();

  socket.listen((event) {
    if (event != RawSocketEvent.read) return;
    final datagram = socket.receive();
    if (datagram == null) return;
    try {
      final payload = Map<String, dynamic>.from(
        jsonDecode(utf8.decode(datagram.data)) as Map,
      );
      if (payload['service'] != 'ano-gpt') return;
      final host = (payload['host']?.toString().isNotEmpty ?? false)
          ? payload['host'].toString()
          : datagram.address.address;
      final scheme = payload['scheme']?.toString() ?? 'https';
      final port = (payload['port'] as num?)?.toInt() ?? kServerPorts.first;
      final base = '$scheme://$host:$port';
      found[base] = DiscoveredServer(
        name: payload['name']?.toString() ?? 'PC ANO-GPT',
        baseUrl: base,
        host: host,
      );
    } catch (_) {
      // Datagramme étranger : on l'ignore.
    }
  });

  final probe = utf8.encode(kDiscoveryMagic);
  final targets = <InternetAddress>[InternetAddress('255.255.255.255')];
  final prefixes = <String>{};
  try {
    for (final interface in await NetworkInterface.list(
      type: InternetAddressType.IPv4,
      includeLoopback: false,
    )) {
      for (final address in interface.addresses) {
        final octets = address.address.split('.');
        if (octets.length != 4) continue;
        final prefix = '${octets[0]}.${octets[1]}.${octets[2]}';
        // Certains routeurs bloquent 255.255.255.255 mais laissent passer le
        // broadcast dirigé du sous-réseau.
        targets.add(InternetAddress('$prefix.255'));
        prefixes.add(prefix);
      }
    }
  } catch (_) {
    // Liste d'interfaces indisponible : le broadcast global suffira.
  }

  void emit() {
    for (final target in targets) {
      try {
        socket.send(probe, target, kDiscoveryPort);
      } catch (_) {
        // Interface sans broadcast : on tente les suivantes.
      }
    }
    // Beaucoup de box Wi-Fi filtrent la diffusion. Un balayage unicast du
    // sous-réseau (254 datagrammes, quelques kilo-octets) traverse ce filtre
    // et reste le seul chemin fiable sur ces réseaux.
    for (final prefix in prefixes) {
      for (var host = 1; host < 255; host++) {
        try {
          socket.send(probe, InternetAddress('$prefix.$host'), kDiscoveryPort);
        } catch (_) {
          // Adresse injoignable : on continue le balayage.
        }
      }
    }
  }

  emit();
  Timer(const Duration(milliseconds: 800), emit);
  Timer(timeout, () {
    socket.close();
    if (!completer.isCompleted) {
      completer.complete(found.values.toList());
    }
  });
  return completer.future;
}

/// Construit toutes les adresses plausibles pour une saisie utilisateur.
///
/// Forcer `https` sur le port 8000 faisait échouer la connexion sans
/// explication dès que le PC servait en clair.
List<String> candidateUrls(String raw) {
  var text = raw.trim();
  if (text.isEmpty) {
    throw const RemoteException('Saisissez l’adresse du PC.');
  }
  text = text.replaceAll(RegExp(r'/+$'), '');

  final hasScheme = text.contains('://');
  final parsed = Uri.tryParse(hasScheme ? text : 'https://$text');
  if (parsed == null || parsed.host.isEmpty) {
    throw const RemoteException('Adresse invalide (ex. 192.168.1.137:8000).');
  }
  if (!isLocalHost(parsed.host)) {
    throw const RemoteException(
      'Adresse hors réseau local. Utilisez l’IP affichée par ANO-GPT.',
    );
  }

  final schemes = hasScheme
      ? [parsed.scheme, parsed.scheme == 'https' ? 'http' : 'https']
      : ['https', 'http'];
  final ports = <int>[
    if (parsed.hasPort) parsed.port,
    ...kServerPorts,
  ];

  final urls = <String>[];
  for (final port in ports) {
    for (final scheme in schemes) {
      final candidate = '$scheme://${parsed.host}:$port';
      if (!urls.contains(candidate)) urls.add(candidate);
    }
  }
  return urls;
}

/// Extrait adresse et clé d'un QR code ANO-GPT.
///
/// Accepte le lien `/auto-login?key=…` affiché par le PC, une adresse nue
/// (`192.168.1.137:8000`, que `Uri` ne sait pas interpréter seule) ou une
/// simple clé d'appairage.
({String address, String key}) parseQrPayload(
  String raw, {
  String fallbackAddress = '',
}) {
  final text = raw.trim();
  if (text.isEmpty) {
    throw const RemoteException('QR code vide.');
  }

  final parsed = Uri.tryParse(text);
  if (parsed != null && parsed.host.isNotEmpty) {
    final key = parsed.queryParameters['key']?.trim().toUpperCase() ?? '';
    final scheme = parsed.scheme.isEmpty ? 'https' : parsed.scheme;
    final port = parsed.hasPort ? parsed.port : kServerPorts.first;
    return (address: '$scheme://${parsed.host}:$port', key: key);
  }

  // `host:port` ou `host` sans schéma.
  final bare = RegExp(r'^([A-Za-z0-9._-]+)(?::(\d{2,5}))?$').firstMatch(text);
  if (bare != null && isLocalHost(bare.group(1)!)) {
    final port = bare.group(2) ?? '${kServerPorts.first}';
    return (address: '${bare.group(1)}:$port', key: '');
  }

  if (RegExp(r'^[A-Za-z0-9]{4,12}$').hasMatch(text)) {
    return (address: fallbackAddress, key: text.toUpperCase());
  }
  throw const RemoteException('Ce QR code ne vient pas d’ANO-GPT.');
}

class RemoteApi {
  RemoteApi(this.baseUrl, {this.token = '', this.deviceToken = ''});

  final String baseUrl;
  String token;
  String deviceToken;

  Uri uri(String path) => Uri.parse('$baseUrl$path');

  /// URL WebSocket authentifiée pour un chemin donné.
  Uri socketUri(String path) {
    final source = Uri.parse(baseUrl);
    return source.replace(
      scheme: source.scheme == 'https' ? 'wss' : 'ws',
      path: path,
      queryParameters: {'token': token},
    );
  }

  Future<Map<String, dynamic>> request(
    String method,
    String path, {
    Map<String, dynamic>? body,
    bool authenticated = false,
    Duration timeout = const Duration(seconds: 8),
  }) async {
    final client = lanClient();
    try {
      final request = await client.openUrl(method, uri(path)).timeout(timeout);
      request.headers.contentType = ContentType.json;
      if (authenticated && token.isNotEmpty) {
        request.headers.set(HttpHeaders.authorizationHeader, 'Bearer $token');
      }
      if (body != null) request.write(jsonEncode(body));
      final response = await request.close().timeout(timeout);
      final text = await utf8.decoder.bind(response).join();
      final data = text.isEmpty
          ? <String, dynamic>{}
          : Map<String, dynamic>.from(jsonDecode(text) as Map);
      if (response.statusCode < 200 || response.statusCode >= 300) {
        throw RemoteException(
          data['error']?.toString() ?? 'Erreur HTTP ${response.statusCode}',
        );
      }
      return data;
    } finally {
      client.close(force: true);
    }
  }

  /// Vérifie que c'est bien ANO-GPT qui répond, et pas un autre service.
  Future<Map<String, dynamic>> health() async {
    final data = await request(
      'GET',
      '/api/health',
      timeout: const Duration(seconds: 4),
    );
    if (data['service'] != 'ano-gpt') {
      throw const RemoteException('Ce port est occupé par un autre service.');
    }
    return data;
  }

  Future<void> pair(String key) async {
    final data = await request('POST', '/api/pair', body: {'key': key});
    token = data['token']?.toString() ?? '';
    deviceToken = data['device_token']?.toString() ?? '';
    if (token.isEmpty || deviceToken.isEmpty) {
      throw const RemoteException('Réponse d’appairage incomplète.');
    }
  }

  Future<void> reconnect() async {
    final data = await request(
      'POST',
      '/api/device-login',
      body: {'device_token': deviceToken},
    );
    token = data['token']?.toString() ?? '';
    if (token.isEmpty) throw const RemoteException('Reconnexion refusée.');
  }

  Future<void> post(String path, [Map<String, dynamic>? body]) async {
    await request('POST', path, body: body, authenticated: true);
  }

  Future<List<Map<String, dynamic>>> files() async {
    final data = await request('GET', '/api/files', authenticated: true);
    final raw = data['files'];
    if (raw is! List) return const [];
    return raw
        .whereType<Map>()
        .map((entry) => Map<String, dynamic>.from(entry))
        .toList();
  }

  /// Lien de téléchargement d'une capture (jeton en paramètre : une balise de
  /// téléchargement ne peut pas porter d'en-tête).
  Uri downloadUri(String name) => Uri.parse(
    '$baseUrl/uploads/${Uri.encodeComponent(name)}?token=${Uri.encodeComponent(token)}',
  );

  Future<List<int>> download(String name) async {
    final client = lanClient();
    try {
      final request = await client.getUrl(downloadUri(name));
      final response = await request.close().timeout(
        const Duration(seconds: 20),
      );
      if (response.statusCode != 200) {
        throw RemoteException('Téléchargement refusé (${response.statusCode}).');
      }
      final chunks = <int>[];
      await for (final chunk in response) {
        chunks.addAll(chunk);
      }
      return chunks;
    } finally {
      client.close(force: true);
    }
  }

  Future<WebSocket> openSocket([String path = '/ws']) {
    return WebSocket.connect(
      socketUri(path).toString(),
      customClient: lanClient(),
    ).timeout(const Duration(seconds: 8));
  }
}

class ProbeOutcome {
  const ProbeOutcome(this.baseUrl, this.info);
  final String baseUrl;
  final Map<String, dynamic> info;
}

/// Sonde toutes les adresses en parallèle : quatre essais successifs de quatre
/// secondes donnaient une attente insupportable avant le message d'erreur.
Future<ProbeOutcome> probeServer(List<String> candidates) async {
  final results = List<Map<String, dynamic>?>.filled(candidates.length, null);
  final failures = List<String>.filled(candidates.length, '');

  await Future.wait(<Future<void>>[
    for (var index = 0; index < candidates.length; index++)
      RemoteApi(candidates[index])
          .health()
          .then<void>(
            (info) => results[index] = info,
            onError: (Object error) =>
                failures[index] = describeFailure(error, candidates[index]),
          ),
  ]);

  for (var index = 0; index < candidates.length; index++) {
    final info = results[index];
    if (info != null) return ProbeOutcome(candidates[index], info);
  }

  throw RemoteException(
    'ANO-GPT est resté injoignable.\n\n'
    '${failures.where((line) => line.isNotEmpty).join('\n')}\n\n'
    'Vérifiez que JARVIS est lancé sur le PC et que le téléphone est sur le '
    'même Wi-Fi (le mode « point d’accès invité » isole les appareils).',
  );
}
