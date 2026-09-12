# ANO Remote

Télécommande Android d'ANO-GPT : parler, filmer, commander, revoir les captures.

## Fonctions

**Connexion**
- découverte automatique du PC par UDP (port 8009) et balayage du sous-réseau,
  pour traverser les box qui filtrent la diffusion ;
- appairage par QR code, par découverte, ou à la main ;
- schéma et port détectés seuls : `https`/`http` × `8000`/`8001` sondés en
  parallèle ;
- certificat auto-signé accepté uniquement sur une adresse locale et ces ports ;
- reconnexion persistante, et diagnostic nommant chaque adresse essayée.

**Parole**
- micro maintenu (appui long) ou mains-libres verrouillé ;
- PCM 16 bits, 16 kHz mono, diffusé sur `/ws/phone-audio` ;
- **le micro du PC se coupe tant que le téléphone parle**, et le rend dès que
  le flux se tait ;
- indicateur de niveau sonore en direct.

**Caméra**
- la caméra du téléphone se diffuse vers le **conteneur plein cadre du PC** ;
- conversion NV21 → JPEG en Kotlin natif (`ano.remote/frames`), ~15 images/s ;
- le PC pilote la caméra à distance : dire « ouvre la caméra du téléphone »
  devant l'ordinateur l'allume sans toucher au téléphone ;
- bascule avant/arrière, viseur local compact.

**Position**
- suivi continu par **service de premier plan Android** : il continue quand
  vous quittez l'application, écran éteint compris ;
- notification permanente « ANO Remote — position partagée » : c'est elle qui
  fait vivre le service, Android arrête un service que l'utilisateur ne voit
  pas ;
- verrous CPU et Wi-Fi, sinon la radio coupe en veille profonde et les relevés
  s'accumulent sans jamais partir ;
- un relevé toutes les 30 s, jeton renouvelé tout seul si le PC redémarre ;
- bouton de bascule dans le bandeau ; le choix est mémorisé.

**Captures**
- photo et vidéo déclenchées à la voix ou depuis l'appli ;
- tout est écrit dans `JARVIS Uploads` sur le PC ;
- galerie dans l'appli, rafraîchie dès qu'une capture arrive.

## Architecture

| Fichier | Rôle |
| --- | --- |
| `lib/net.dart` | découverte, sondage, `RemoteApi`, analyse des QR |
| `lib/session.dart` | état central : connexion, conversation, ordres du PC |
| `lib/voice_link.dart` | micro → `/ws/phone-audio` |
| `lib/camera_link.dart` | caméra → `/ws/phone-camera` |
| `lib/location_link.dart` | suivi GPS de premier plan → `/api/location` |
| `lib/home_page.dart` | onglets Parler / Caméra / Captures |
| `lib/pairing_page.dart` | appairage |
| `lib/theme.dart` | palette et briques visuelles |

## Installation

1. Redémarrer ANO-GPT : le certificat HTTPS local est généré au premier
   démarrage.
2. **Ouvrir les ports une fois** si le PC utilise firewalld :
   `sudo firewall-cmd --permanent --add-port=8000/tcp --add-port=8001/tcp --add-port=8009/udp && sudo firewall-cmd --reload`
3. Installer `build/app/outputs/flutter-apk/app-release.apk`.
4. Dans ANO-GPT, ouvrir **Contrôle à distance**, puis scanner le QR.
5. Autoriser caméra, micro et localisation à la première demande.

## Dépannage

| Symptôme | Cause | Correctif |
| --- | --- | --- |
| « Connexion refusée » | JARVIS éteint ou autre port | Vérifier la ligne `[Dashboard] https://…` |
| « Aucune route » | Isolation client du Wi-Fi | Désactiver le mode invité du routeur |
| « n'a pas répondu à temps » | Pare-feu | Voir l'étape 2 ci-dessus |
| Aucun PC trouvé | Diffusion filtrée | Saisir l'adresse à la main |
| Écran du scanner noir | Caméra refusée | L'écran affiche la raison et un bouton **Réessayer** |
| Conteneur PC noir en source téléphone | ANO Remote fermé | ANO-GPT le dit désormais explicitement |
| La position se fige quand on quitte l'appli | Notification du service refusée | Autoriser les notifications d'ANO Remote ; sans elle Android arrête le service |
| La position se fige après plusieurs heures | Optimisation de batterie du constructeur | Réglages ▸ Batterie ▸ ANO Remote ▸ « Sans restriction » (obligatoire sur Xiaomi, Huawei, Samsung) |

## Développement

```bash
flutter analyze
flutter test
flutter build apk --release
```
