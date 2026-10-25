# Enrichissement par la KB constructeur (navigateur Playwright)

Fonction **optionnelle** qui complète le RAG avec les articles de la base de connaissances Alcatel, récupérés par un navigateur Chromium conteneurisé. Désactivée par défaut; si elle est coupée ou en panne, l'assistance IA et le RAG fonctionnent exactement comme avant.

## Principe : approche « sandwich »

```text
Création du ticket ─► 1re réponse IA (inchangée) ─► [tâche de fond] file d'attente KB (1 worker)
                                                          │
 1. AMONT   LLM : entités techniques + requête (repli par règles si le LLM échoue; e-mails, IP, MAC et
            téléphones masqués avant tout envoi vers l'extérieur)
 2. MILIEU  kb-browser (Playwright, 100 % déterministe) : champ de recherche → N premiers liens → articles
            session expirée → arrêt immédiat, alerte
 3. HYGIÈNE nettoyage (sans menus/en-têtes/pieds/scripts) → empreinte SHA-256 → déduplication
 4. INDEX   collection Qdrant dédiée « support_kb_<modèle d'embedding> » (jamais mélangée au corpus)
 5. AVAL    retrieve_context() ajoute les extraits KB aux prompts existants (diagnostic client, aide agent)
            + note interne listant les articles trouvés
```

L'enrichissement démarre **après** la première réponse IA (les tâches de fond s'exécutent à la suite) : la création d'un ticket n'ajoute aucune latence et la première réponse n'attend pas la KB. Les réponses suivantes et l'aide aux agents en profitent. Un agent peut aussi relancer la recherche (bouton « Chercher dans la KB constructeur » du ticket).

## Activation

1. Dans `.env` : `ENABLE_KB_BROWSER_SCRAPER=true`, `KB_BASE_URL`, `KB_BROWSER_TOKEN` (jeton partagé API ↔ navigateur) et `KB_BROWSER_VNC_PASSWORD` (voir `.env.example`).
2. Adapter les sélecteurs CSS au portail réel (`KB_SEARCH_INPUT_SELECTOR`, `KB_RESULT_LINK_SELECTOR`, `KB_ARTICLE_SELECTOR`, `KB_NO_RESULTS_SELECTOR`, `KB_LOGGED_IN_SELECTOR`…). La console de débogage indique le sélecteur en échec.
3. Démarrer : `docker compose --profile kb up -d --build` (le navigateur est dans un profil optionnel; l'API n'en dépend pas).
4. Se connecter une fois (MFA) : ouvrir <http://localhost:6080/vnc.html> (noVNC, mot de passe `KB_BROWSER_VNC_PASSWORD`, limité à `127.0.0.1`), se connecter à la KB dans le navigateur affiché, puis **Vérifier la session** dans la page admin **KB constructeur**. Le profil Chromium est conservé dans le volume `kb-browser-profile` : les cookies survivent aux redémarrages.

### Configuration du Technical Knowledge Center (Salesforce / Coveo)

Valeurs validées sur le portail réel (à placer dans `.env`) :

```text
KB_BASE_URL=https://myportal.al-enterprise.com/s/technical-knowledge-center
KB_SEARCH_INPUT_SELECTOR=input.searchbox__input-with-button
KB_SEARCH_SUBMIT_SELECTOR=button.searchbox__submit-button
KB_RESULT_LINK_SELECTOR=[role='link']
KB_READY_SELECTOR=[role='link']
KB_ARTICLE_SELECTOR=section.slds-accordion__section
KB_ARTICLE_JOIN=true
KB_ARTICLE_FORMAT=text
```

Particularités du portail : le rendu est asynchrone (le navigateur attend `KB_READY_SELECTOR` puis tape la requête touche par touche); les titres de résultats n'ont pas de `href` (l'URL de chaque article est obtenue en cliquant, ce qui ouvre un nouvel onglet `…/s/customer-care-article-detail?recordId=…`); le texte des articles est dans des composants à Shadow DOM, lisibles uniquement en mode texte (`KB_ARTICLE_FORMAT=text`); l'article est découpé en sections (Summary, Affected Systems, Description…) toutes réunies par `KB_ARTICLE_JOIN`.

**Requêtes en anglais** : la KB est rédigée en anglais et y classe bien mieux les requêtes anglaises (mesuré : « pilot not receiving calls » renvoie 4 articles pertinents, la formulation française 1 sur 4). Le LLM est donc invité à répondre en anglais; sans LLM exploitable, un glossaire français→anglais du vocabulaire téléphonie (pilote, appels, poste, tonalité…) construit la requête, gère la négation (« ne … plus » → « not ») et conserve les sigles (VLAN, SIP…). Les noms de produit génériques (OXE) sont omis car ils diluent le classement.

## Source « documents » (Business Documents)

En plus des articles du Technical Knowledge Center, l'enrichissement peut puiser dans la liste des **Business Documents** (guides, fiches techniques, présentations, packs d'interopérabilité) : <https://myportal.al-enterprise.com/s/pre-sales-documents>. Elle est désactivée par défaut.

```text
KB_DOCS_ENABLED=true                      # API : active la source
KB_DOCS_URL=https://myportal.al-enterprise.com/s/pre-sales-documents   # navigateur
KB_DOCS_MAX_RESULTS=2                     # documents lus par ticket
KB_DOCS_EXCLUDE_TYPES=Price catalog,Pricing,Offer/Proposal,RFP,Quoter,Commercial
KB_DOCS_MAX_FILE_MB=15                    # taille maximale d'un fichier joint téléchargé
```

Déroulement : la requête est saisie dans le champ « Search this list… » (si la liste ne renvoie rien, nouvel essai avec les deux mots les plus distinctifs); les lignes dont le type est commercial (grilles de prix, RFP, devis…) sont **écartées avant tout téléchargement**; pour les meilleures restantes, le navigateur lit la fiche (titre, description, mots-clés, type, release produit) puis télécharge le fichier joint et en extrait le texte.

Formats lus : PDF (80 premières pages), PowerPoint (.pptx), Word (.docx), texte, et **archives ZIP** (jusqu'à 12 documents lisibles, sans archive imbriquée, taille décompressée plafonnée à 80 Mo pour se prémunir des archives piégées). Les autres formats (Excel, exécutables…) et les fichiers au-dessus de la taille maximale sont ignorés : seule la fiche est alors indexée. Le texte indexé est plafonné à 200 000 caractères par document.

**Pas de répétitions** : un élément n'est pas relu ni réindexé s'il est déjà connu (même URL, récente), si son contenu est identique à un élément déjà indexé (articles et documents confondus), ou s'il porte le même titre normalisé qu'un élément d'une autre URL (ex. trois fiches « 6860N GOLDEN RFP »). Chaque cas est tracé dans le détail de la recherche (« Déjà connu », « Doublon évité »).

La note interne laissée aux agents sépare **Articles de la base de connaissances** et **Documents constructeur**; les extraits injectés dans les prompts sont étiquetés « KB constructeur » ou « documentation constructeur ».

## Deux niveaux d'interrupteur

| Niveau | Où | Effet |
|---|---|---|
| Drapeau serveur | `ENABLE_KB_BROWSER_SCRAPER` | `false` : module totalement inerte (aucun appel, aucune file, rien dans la supervision). |
| Interrupteur en direct | Page admin **KB constructeur** | Coupe/rétablit l'enrichissement sans redémarrage; requiert le drapeau serveur. |

## Résilience (repli transparent)

| Situation | Comportement |
|---|---|
| Module coupé / consentement IA absent | Job `skipped`, aucun appel externe. |
| Conteneur hors ligne, timeout | Job `skipped` (`unavailable`); RAG standard seul. |
| Session expirée (redirection vers login/MFA, avant ou pendant la navigation) | Job `failed` (`session_expired`), arrêt immédiat, alerte enregistrée (bandeau admin, supervision en alerte, log d'erreur). Les jobs suivants sont ignorés tant que la session n'est pas rétablie. |
| Sélecteur introuvable / page vide | Article en échec avec le sélecteur et l'URL journalisés; les autres articles continuent. |
| Qdrant/Ollama indisponible à l'indexation | Article non marqué « indexé »; le job ne casse rien. |
| Lecture KB (prompts) en panne | Aucun extrait KB, le RAG documentaire répond seul. |

Aucune erreur du module ne remonte au parcours ticket : l'enqueue ne lève jamais et chaque job est protégé.

## Archive locale (`kb_archive/`)

Les éléments récupérés sont aussi copiés dans le dossier **`kb_archive/` de votre projet**, monté dans l'API et placé **hors des volumes Docker** : il survit à `docker compose down -v`. Il contient :

```text
kb_archive/
  items/<clé>.json          URL, titre, source, empreinte et texte extrait (sert à restaurer la base)
  texts/<titre>__<clé>.txt  le même texte, lisible directement
  files/<sha256>.<ext>      fichier ORIGINAL téléchargé (PowerPoint, PDF, ZIP, Excel…), dédupliqué par empreinte
  README.txt
```

- **Quand** : à chaque indexation d'un article ou d'un document. Pour un document, le navigateur renvoie aussi le fichier d'origine (jusqu'à `KB_DOCS_MAX_FILE_MB`, 15 Mo), y compris dans un format dont le texte n'est pas extractible (Excel…); il est vérifié par son SHA-256 puis supprimé du conteneur.
- **Pas de répétition** : une clé dérivée de l'URL (un seul exemplaire par élément) et des fichiers nommés par leur empreinte (deux fiches pointant vers le même fichier le partagent).
- **Plafond** : `KB_ARCHIVE_MAX_MB` (2048 par défaut). Une fois atteint, le texte continue d'être archivé mais pas les nouveaux fichiers originaux (signalé dans le détail de la recherche).
- **Restauration automatique** : à chaque démarrage de l'API, les éléments de l'archive absents de la base sont recréés, puis la réindexation recalcule leurs vecteurs. Après un `down -v`, la base de connaissances se reconstruit donc seule, **sans retourner sur le portail ni refaire le MFA**. Si le module KB est désactivé, les éléments sont restaurés mais indexés plus tard (bouton « Réindexer »).
- **Éléments antérieurs à l'archive** : le bouton « Archiver les éléments déjà indexés » copie leur texte (leurs fichiers d'origine n'avaient pas été conservés).
- **Désactivation** : `KB_ARCHIVE_ENABLED=false`.

À savoir : `kb_archive/` est ignoré par Git (ces documents viennent d'un portail à accès restreint). Pour ne rien perdre, sauvegardez ce dossier comme `support_docs/`, ainsi que le volume du profil navigateur si vous voulez conserver la session MFA. Les vecteurs (Qdrant) ne sont pas archivés : ils se recalculent.

## Session MFA et profil persistant

Chromium ne conserve pas les cookies « de session » (sans date d'expiration) à sa fermeture, même avec un profil persistant : le SSO obligerait à refaire le MFA à chaque redémarrage du conteneur. Les cookies sont donc sauvegardés dans le volume (`session-cookies.json`, droits 600) après chaque opération réussie et à l'arrêt, puis rejoués au lancement. Un test avec un vrai Chromium le vérifie. La session reste soumise à la durée de vie côté SSO : si elle expire, la page admin passe en « Session expirée » et un onglet de connexion reste ouvert dans noVNC.

## Diagnostic des sélecteurs (nouveau portail)

`POST /snapshot` du conteneur (jeton `X-KB-Token`) exécute une recherche réelle puis retourne l'URL obtenue, les éléments candidats (titres, nombre d'éléments visibles par sélecteur courant), le HTML autour de la zone de saisie et, avec `probe` (texte visible) ou `open_result` (n° de résultat à ouvrir), la chaîne d'ancêtres d'un élément et la structure de la page d'un article. `GET /snapshot.png` renvoie la capture d'écran. Les paramètres `input_selector` et `submit_selector` testent un sélecteur sans modifier la configuration.

## Données

- **Index séparés** : corpus documentaire = `support_documents_<modèle>`; KB = `support_kb_<modèle>` (réglable avec `KB_COLLECTION`). Chaque modèle d'embedding a ses collections; un changement de modèle réindexe aussi les articles KB déjà conservés, sans re-scraper.
- **Déduplication** : par URL canonique (sans fragment ni paramètres de suivi), puis par empreinte du texte nettoyé (même contenu sous une autre URL = non réindexé). Un article connu récupéré il y a moins de `KB_REFETCH_AFTER_HOURS` (168 h) n'est même pas re-scrapé. La similarité d'embeddings n'est pas utilisée.
- **Tables** : `kb_settings` (interrupteur, état de session, dernière alerte), `kb_articles` (texte nettoyé, empreinte, modèle indexé), `kb_jobs` (journal et métriques).

## Console admin (page « KB constructeur »)

- **Statut et contrôle** : interrupteur, navigateur en ligne/hors ligne, session valide/expirée, file d'attente, lien de reconnexion MFA, vérification de session, réindexation.
- **Métriques** : recherches effectuées, taux de succès/échec, durée moyenne, éléments indexés (articles et documents).
- **Dernières recherches** : une carte par recherche avec le ticket concerné (lien vers le ticket), la requête envoyée au portail et son origine (IA ou règles), un bilan (sources, indexés, déjà connus, durée) et, au dépliage, le sort de chaque résultat (indexé, déjà connu, doublon évité, écarté, échec) avec lien vers la page du portail. Filtres : toutes, réussies, échecs, ignorées. Le bouton « Déboguer cette recherche » ouvre la console sur cette recherche.
- **Console de débogage** : liste des recherches à gauche, détail à droite (requête, résultats, sélecteur CSS en échec, erreur, URLs consultées) et **journal du navigateur limité à la période de la recherche choisie**; un second onglet donne le journal complet.

## API

| Méthode | Route | Accès | Rôle |
|---|---|---|---|
| `GET` | `/api/admin/kb` | admin | Statut, session, métriques, 20 derniers jobs. |
| `PUT` | `/api/admin/kb/settings` | admin | `{"enabled": bool}`; `409` si le drapeau serveur est à `false`. |
| `POST` | `/api/admin/kb/session/check` | admin | Vérifie la session du navigateur et met à jour l'état/alerte. |
| `GET` | `/api/admin/kb/debug` | admin | Journal du navigateur et 50 derniers jobs détaillés (URLs, sélecteur en échec, erreur). |
| `POST` | `/api/admin/kb/reindex` | admin | Réindexe les articles KB pour le modèle d'embedding actif. |
| `POST` | `/api/tickets/{id}/kb-enrich` | agent/admin | Relance la recherche KB d'un ticket (`202`). |
| `GET` | `/api/admin/kb/archive` | admin | Contenu de l'archive locale (éléments, fichiers, taille, plafond). |
| `POST` | `/api/admin/kb/archive/export` | admin | Archive (texte) les éléments déjà en base mais absents de l'archive. |
| `POST` | `/api/admin/kb/archive/restore` | admin | Recrée en base les éléments archivés manquants et planifie la réindexation. |

Service `kb-browser` (réseau Docker uniquement, en-tête `X-KB-Token`) : `GET /health`, `GET /session`, `POST /search`, `POST /article`, `GET /logs`.

## Architecture du code

- `backend/app/kb/interfaces.py` : contrats (`KBScraper`, `QueryBuilder`, `KBIndex`) et exceptions; `client.py` (HTTP), `query.py`, `index.py` (Qdrant) en sont les implémentations, toutes remplaçables et mockables.
- `backend/app/kb/service.py` : orchestration à dépendances injectées (testée sans navigateur, LLM ni Qdrant); `jobs.py` : file à un worker; `flags.py`, `retrieval.py`, `router.py`.
- `kb-browser/` : service FastAPI + Playwright (`browser.py` navigation déterministe, `rules.py` règles pures, `config.py`).

## Sécurité et conformité

- Le navigateur ne navigue que vers les hôtes autorisés (`KB_ALLOWED_HOSTS`, par défaut l'hôte de `KB_BASE_URL`) : pas de SSRF depuis une URL de résultat.
- L'API du navigateur n'est pas publiée; seul noVNC l'est, sur `127.0.0.1`, avec mot de passe (sans mot de passe, noVNC n'est pas démarré). Le conteneur ne reçoit aucun secret de l'API.
- La requête de recherche est construite à partir de données du ticket et envoyée à un portail externe : e-mails, IP, MAC et téléphones sont masqués; le traitement exige le consentement IA du client.
- Le contenu de la KB est une donnée non fiable : il est injecté dans les prompts avec les mêmes garde-fous que le corpus documentaire.
- Vérifiez que l'usage d'un navigateur automatisé est compatible avec les conditions d'utilisation de la KB et du compte employé. Le scraper est volontairement sobre : une seule navigation à la fois, délai entre pages (`KB_REQUEST_DELAY_MS`), 3 articles par ticket par défaut (`KB_MAX_ARTICLES`).

## Tests

- `backend/tests/test_kb.py` et `test_api.py` : nettoyage, requête, orchestration (déduplication, session expirée, panne, sélecteurs), file d'attente, drapeaux, API admin, déclenchement à la création de ticket.
- `kb-browser/tests/` : règles pures, API HTTP et parcours complet avec un **vrai Chromium** contre un faux portail local (redirection de connexion, expiration en cours de route, sélecteur cassé, hôte interdit). Lancer : `cd kb-browser && pip install -r requirements.txt && playwright install chromium && pytest`.
