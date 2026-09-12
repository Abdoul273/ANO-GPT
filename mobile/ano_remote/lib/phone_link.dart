import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

class PhoneLink extends ChangeNotifier {
  static const _channel = MethodChannel('ano.remote/system');
  static const _enabledKey = 'automatic_phone_calls_enabled';

  bool contactsAllowed = false;
  bool callsAllowed = false;
  bool smsAllowed = false;
  bool hangupAllowed = false;
  bool telephonyAvailable = true;
  bool automaticCallsEnabled = false;
  bool busy = false;
  String? error;
  String lastStatus = 'Téléphonie non configurée.';

  bool get permissionsGranted => contactsAllowed && callsAllowed;
  // Le raccrochage n'existe pas avant Android 9 : l'exiger bloquerait un
  // téléphone plus ancien parfaitement capable d'appeler et d'écrire.
  bool get fullyGranted => permissionsGranted && smsAllowed;
  bool get ready =>
      telephonyAvailable && permissionsGranted && automaticCallsEnabled;

  Future<void> initialize() async {
    final prefs = await SharedPreferences.getInstance();
    automaticCallsEnabled = prefs.getBool(_enabledKey) ?? false;
    await refreshPermissions();
  }

  Future<void> refreshPermissions() async {
    try {
      final raw = await _channel.invokeMapMethod<String, dynamic>(
        'phonePermissionStatus',
      );
      _applyStatus(raw ?? const {});
    } on PlatformException catch (exception) {
      error = exception.message ?? 'Téléphonie Android indisponible.';
    }
    notifyListeners();
  }

  Future<void> requestPermissions() async {
    try {
      final raw = await _channel.invokeMapMethod<String, dynamic>(
        'requestPhonePermissions',
      );
      _applyStatus(raw ?? const {});
      lastStatus = permissionsGranted
          ? 'Contacts et appels autorisés.'
          : 'Les deux permissions sont nécessaires.';
    } on PlatformException catch (exception) {
      error = exception.message ?? 'Permissions refusées.';
    }
    notifyListeners();
  }

  Future<void> setAutomaticCalls(bool enabled) async {
    if (enabled && !permissionsGranted) {
      await requestPermissions();
      if (!permissionsGranted) return;
    }
    automaticCallsEnabled = enabled;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_enabledKey, enabled);
    lastStatus = enabled
        ? 'Appels automatiques autorisés depuis ce PC appairé.'
        : 'Appels automatiques désactivés.';
    notifyListeners();
  }

  Future<Map<String, dynamic>> execute(
    String target, {
    int selection = 0,
  }) async {
    if (!automaticCallsEnabled) {
      return const {
        'ok': false,
        'status': 'disabled',
        'message': 'Activez les appels automatiques dans ANO Remote.',
      };
    }
    if (!permissionsGranted) {
      return const {
        'ok': false,
        'status': 'permission_required',
        'message': 'Autorisez Contacts et Appels dans ANO Remote.',
      };
    }
    if (busy) {
      return const {
        'ok': false,
        'status': 'busy',
        'message': 'Une demande d’appel est déjà en cours.',
      };
    }
    busy = true;
    error = null;
    notifyListeners();
    try {
      final raw = await _channel.invokeMapMethod<String, dynamic>(
        'resolveAndCall',
        {'target': target, 'selection': selection},
      );
      final outcome = Map<String, dynamic>.from(raw ?? const {});
      lastStatus = outcome['message']?.toString() ?? 'Commande traitée.';
      if (outcome['ok'] != true) error = lastStatus;
      return outcome;
    } on PlatformException catch (exception) {
      error = exception.message ?? 'Échec de la téléphonie Android.';
      return {'ok': false, 'status': 'failed', 'message': error};
    } finally {
      busy = false;
      notifyListeners();
    }
  }

  /// Raccroche l'appel en cours. Sans opt-in, le PC ne peut pas couper une
  /// conversation à la place de l'utilisateur.
  Future<Map<String, dynamic>> hangUp() async {
    if (!automaticCallsEnabled) {
      return const {
        'ok': false,
        'status': 'disabled',
        'message': 'Activez les appels automatiques dans ANO Remote.',
      };
    }
    if (!hangupAllowed) {
      return const {
        'ok': false,
        'status': 'permission_required',
        'message': 'Autorisez la gestion des appels dans ANO Remote.',
      };
    }
    return _invoke('endCall', const {}, 'Raccrochage impossible.');
  }

  /// Recherche dans le carnet Android. Aucune permission d'appel requise :
  /// répondre « ai-je ce contact ? » ne compose aucun numéro.
  Future<Map<String, dynamic>> searchContacts(String query) async {
    if (!contactsAllowed) {
      return const {
        'ok': false,
        'status': 'permission_required',
        'message': 'Autorisez l’accès aux Contacts dans ANO Remote.',
      };
    }
    return _invoke('searchContacts', {
      'query': query,
    }, 'Recherche de contacts impossible.');
  }

  /// SMS envoyé par la SIM, destinataire résolu côté Android.
  Future<Map<String, dynamic>> sendSms(
    String target,
    String body, {
    int selection = 0,
  }) async {
    if (!automaticCallsEnabled) {
      return const {
        'ok': false,
        'status': 'disabled',
        'message': 'Activez les commandes téléphoniques dans ANO Remote.',
      };
    }
    if (!smsAllowed || !contactsAllowed) {
      return const {
        'ok': false,
        'status': 'permission_required',
        'message': 'Autorisez Contacts et SMS dans ANO Remote.',
      };
    }
    return _invoke('resolveAndSendSms', {
      'target': target,
      'selection': selection,
      'body': body,
    }, 'Envoi du SMS impossible.');
  }

  Future<Map<String, dynamic>> _invoke(
    String method,
    Map<String, dynamic> args,
    String fallback,
  ) async {
    try {
      final raw = await _channel.invokeMapMethod<String, dynamic>(method, args);
      final outcome = Map<String, dynamic>.from(raw ?? const {});
      lastStatus = outcome['message']?.toString() ?? 'Commande traitée.';
      if (outcome['ok'] != true) error = lastStatus;
      notifyListeners();
      return outcome;
    } on PlatformException catch (exception) {
      error = exception.message ?? fallback;
      notifyListeners();
      return {'ok': false, 'status': 'failed', 'message': error};
    }
  }

  void _applyStatus(Map<String, dynamic> status) {
    contactsAllowed = status['contacts'] == true;
    callsAllowed = status['calls'] == true;
    // RECEIVE_SMS est demandé avec SEND_SMS : sans les deux, ANO Remote ne
    // peut pas offrir un relais SMS bidirectionnel fiable.
    smsAllowed = status['sms'] == true && status['receive_sms'] == true;
    hangupAllowed = status['hangup'] == true;
    telephonyAvailable = status['telephony'] != false;
    error = null;
  }
}
