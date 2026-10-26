# Installation, configuration et dépannage

## Lancer dans Docker Desktop

1. Installer Docker Desktop et attendre que son moteur soit démarré.
2. Copier le modèle d'environnement : `cp .env.example .env`.
3. Dans `.env`, remplacer `SECRET_KEY` par une valeur aléatoire longue et `INITIAL_ADMIN_PASSWORD` par un mot de passe unique. Garder les identifiants Postgres de développement uniquement pour un poste local.
4. Depuis la racine du projet : `docker compose up --build`.
5. Ouvrir <http://localhost:8080>. Les logs s'affichent dans Docker Desktop ou via `docker compose logs -f api web db ollama`.

Au premier lancement, deux instances Ollama téléchargent leurs modèles en parallèle : `ollama` (chat : `deepseek-r1:1.5b` et `qwen3:4b`, volume `ollama-data`) et `ollama-embed` (embeddings : `qwen3-embedding:0.6b`, `bge-m3` et `qwen3-embedding:4b`, volume `ollama-embed-data`), en parallèle du démarrage de l'API et de l'interface. Qdrant conserve les vecteurs dans `qdrant-data`; le dossier `support_docs` de la racine est partagé en écriture avec l'API pour permettre aux administrateurs d'y déposer des documents. La taille des téléchargements et les performances varient selon la machine. Tant que les modèles ou Qdrant ne sont pas disponibles, les analyses RAG peuvent échouer, mais le portail reste accessible et les tickets sont conservés. Une machine disposant de peu de mémoire peut nécessiter un modèle Qwen3 plus petit configuré dans `.env`. Les modèles locaux ne requièrent pas de clé API.

`docker compose up --build` démarre les services `db`, `ollama`, `ollama-model`, `ollama-embed`, `ollama-embed-model`, `qdrant`, `api` et `web` automatiquement. Seuls PostgreSQL prêt et le démarrage sain de l'API conditionnent les services en aval; le téléchargement des modèles et l'indexation documentaire ne sont pas des prérequis au lancement du portail. Les services `ollama-model` et `ollama-embed-model` qui terminent avec le code `0` sont des conteneurs de tâche ponctuelle et leur état `Exited (0)` est normal.

Arrêt sans effacer les données : `docker compose down`. Effacement des tickets, comptes et modèle téléchargé : `docker compose down -v` — opération irréversible.

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `SECRET_KEY` | valeur de développement dans le code | Clé de signature JWT. En déploiement, utiliser une valeur secrète longue et unique. |
| `DATABASE_URL` | SQLite local | Compose définit PostgreSQL. Format `postgresql+psycopg://user:password@host:5432/db`. |
| `INITIAL_ADMIN_EMAIL` | `admin@support.local` | Administrateur créé au premier démarrage si l'adresse n'existe pas déjà. |
| `INITIAL_ADMIN_PASSWORD` | `ChangeMe123!` | Mot de passe de l'administrateur initial. À modifier impérativement avant le démarrage. |
| `AI_PROVIDER` | `ollama` | `ollama` ou `anthropic`. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | URL de l'API Ollama. Dans Compose, `http://ollama:11434`. |
| `OLLAMA_MODEL` | `deepseek-r1:1.5b` | Modèle local par défaut, à télécharger et à utiliser tant que l'administrateur n'a pas enregistré sa propre configuration. |
| `OLLAMA_THINK` | `true` | Raisonnement activé par défaut pour le modèle Ollama de la configuration initiale. |
| `OLLAMA_TIMEOUT_SECONDS` | `300` | Délai maximal par défaut d'un appel Ollama dans la configuration initiale (10 à 1800). |
| `OLLAMA_EXTRA_MODELS` | `qwen3:4b` | Modèles Ollama supplémentaires, séparés par des espaces : téléchargés par `ollama-model` et, dans la configuration initiale, affectés à chaque règle après le modèle principal (sans raisonnement). Ils sont ainsi proposés aux clients et aux agents. |
| `OLLAMA_KEEP_ALIVE` | `30m` | Durée de maintien du modèle en mémoire après un appel (évite le rechargement à froid). |
| `OLLAMA_NUM_CTX` | `8192` | Taille de contexte demandée à Ollama, pour éviter la troncature silencieuse des prompts incluant les extraits RAG. |
| `OLLAMA_EMBEDDING_BASE_URL` | `http://ollama-embed:11434` dans Compose | Instance Ollama dédiée aux embeddings. Vide : l'instance de chat (`OLLAMA_BASE_URL`) est utilisée. |
| `OLLAMA_EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Modèle d'embedding initial, utilisé tant que l'administrateur n'en a pas choisi un autre dans la page RAG (y compris avec Anthropic). |
| `OLLAMA_EXTRA_EMBEDDING_MODELS` | `bge-m3 qwen3-embedding:4b` | Autres modèles d'embedding téléchargés par `ollama-embed-model` et sélectionnables dans la page RAG. |
| `RAG_COLLECTION` | `support_documents` | Collection Qdrant du corpus. |
| `RAG_EMBEDDING_DIMENSION` | `1024` | Dimension utilisée uniquement pour un modèle absent du catalogue (Qwen3 Embedding 0.6B/4B et BGE-M3 ont leur dimension connue : 1024, 2560, 1024). |
| `RAG_TOP_K` | `5` | Nombre maximal d'extraits documentaires transmis à l'IA. |
| `RAG_SCORE_THRESHOLD` | `0.35` | Score minimal de similarité Qdrant; les résultats plus faibles ne sont pas fournis au modèle. |
| `ANTHROPIC_API_KEY` | vide | Clé API si `AI_PROVIDER=anthropic`. À ne jamais publier dans le dépôt ou dans l'interface. |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Identifiant de modèle Anthropic autorisé par le compte. |
| `ANTHROPIC_MAX_TOKENS` | `4096` | Longueur maximale d'une réponse Anthropic, en jetons (avant 4096 elle était fixée à 900 et coupait les réponses longues). Une réponse coupée est signalée par un avertissement à la fin du message. Le coût ne dépend que des jetons réellement produits. |
| `CORS_ORIGINS` | ports locaux 8080 et 5173 | Origines autorisées, séparées par des virgules. |
| `UPLOAD_DIR` | `./uploads` | Répertoire des pièces jointes; Compose le monte sur le volume `support-uploads`. |
| `MAX_ATTACHMENT_SIZE` | `10485760` | Taille maximale en octets par fichier (10 Mio par défaut). |
| `MAX_ATTACHMENTS_PER_UPLOAD` | `5` | Nombre maximal de fichiers dans un envoi. |
| `RAG_MAX_FILE_SIZE` | `52428800` | Taille maximale en octets par document du corpus RAG (50 Mio). Distincte des pièces jointes de tickets. |
| `RAG_MAX_FILES_PER_UPLOAD` | `50` | Nombre maximal de documents du corpus RAG dans un envoi. |

Les variables d'environnement du système prennent priorité sur le fichier `.env`. Compose lit également ce fichier pour les substitutions `${...}`.

### Modèles IA par tâche

Dans **Configuration IA** (admin), choisir Ollama ou Anthropic/Claude pour chacune des tâches (diagnostic client, assistance agent, résumé de transfert, analyse structurée) et pour le client ou l'équipe support. Il est possible de saisir un identifiant de modèle personnalisé et de réordonner jusqu'à quatre modèles : en cas d'échec, le suivant est essayé. Les modèles Ollama personnalisés doivent être téléchargés dans le service Ollama avant leur sélection; Anthropic requiert une clé `ANTHROPIC_API_KEY`.

La configuration est conservée en base. Le bouton d'assistance IA général met en pause la génération sans supprimer les affectations; au moins une règle avec un modèle doit rester enregistrée. Le moteur d'embeddings et l'indexation RAG ne sont pas concernés par ce bouton.

### Consentement client, traces et estimation des coûts IA

Chaque client dispose du menu **Confidentialité IA**. Les consentements de traitement IA et de conservation des prompts/réponses sont distincts et initialisés à `true`. Si le traitement IA est refusé, les nouveaux tickets vont directement à l’équipe support et les appels d’assistance IA sont bloqués. Si seule la conservation des traces est refusée, l’assistance IA reste disponible, mais aucun prompt ni réponse n’est écrit dans les traces; les tokens et métadonnées nécessaires au suivi des coûts demeurent enregistrés. Le retrait du consentement aux traces efface leur contenu historique. Les contenus sont aussi purgés automatiquement à partir de 30 jours.

Les menus admin **Évaluation LLM** et **Coûts IA** proposent des filtres client, utilisateur, ticket, fournisseur, modèle, tâche et période. Les traces consenties peuvent être notées de 1 à 5 et commentées. Dans les coûts, les appels Ollama local sont estimés à 0 USD; les tarifs Anthropic doivent être saisis/actualisés en USD par million de tokens entrée et sortie. Les montants sont des estimations recalculées au tarif configuré, pas la facture contractuelle; un modèle sans tarif est signalé comme non tarifé.

### SLA, rapports PDF et facturation entreprise

Le menu **SLA, rapports & factures** est disponible pour les clients, agents et administrateurs. Un client peut proposer un délai de contournement, un délai cible de résolution et les pénalités en U par type et priorité. L'agent rattaché à l'entreprise ou un administrateur approuve ou refuse la proposition. Les délais commencent à la création du ticket, en heures calendaires UTC; les tickets antérieurs à l'approbation ne sont pas modifiés.

Dans le détail d'un ticket soumis à SLA, un agent peut enregistrer la mise en place d'un contournement ou constater un ticket non conforme / sans défaut, avec justification. Un constat client ne peut être enregistré qu'une fois. Les rapports de période sont calculés de façon asynchrone; leur état s'actualise automatiquement et le PDF est téléchargeable une fois prêt. Ils regroupent les tickets créés pendant la période, leurs indicateurs SLA et les U de pénalité.

Après un commentaire public, l'agent choisit manuellement **En attente client** dans le sélecteur de statut lorsqu'il attend une réponse. La réponse du client, y compris un ajout de pièce jointe, place le ticket en **En attente support**. Une note interne ne change pas le statut. Ces états d'attente ne suspendent pas les échéances ni le calcul des U SLA.

Les agents et clients peuvent consulter les estimations et factures de leur entreprise; un agent peut générer la facture mensuelle. Le menu admin **Facturation** configure le mode par ticket ou mensuel, le prépaiement/post-paiement, les montants, la taxe et les crédits. En prépayé, les crédits ticket sont consommés en premier; si aucun crédit ticket n'est disponible, un crédit de mois couvre le mois courant. Sans crédit disponible, la création d'un ticket client renvoie HTTP 402. Un crédit de mois couvre les créations du mois via une consommation unique.

Les rapports et factures PDF ne remplacent pas un système comptable : les coordonnées légales de l'émetteur, mentions réglementaires, taxes applicables et processus de numérotation doivent être vérifiés et complétés avant émission officielle. Les heures ouvrées, jours fériés, pauses SLA, pénalités contractuelles automatisées et règlement en ligne ne sont pas gérés.

### Corpus documentaire RAG

Un administrateur peut ouvrir **Corpus RAG** dans le menu pour consulter l'état de Qdrant et du modèle d'embedding, les documents, les extraits indexés et les sources en attente de suppression. La page permet d'ajouter des PDF textuels, fichiers `.md`, `.markdown` et `.txt` (5 fichiers maximum par envoi, 10 Mio maximum par fichier), de lancer la synchronisation et de tester une requête sur l'index existant. Les noms de fichiers déjà présents ne sont pas remplacés; supprimer ou mettre à jour les documents directement dans `support_docs/` à la racine du projet si nécessaire. Le montage Docker autorise l'écriture par l'API dans ce dossier.

Avant chaque réponse client ou aide IA agent, le backend calcule les empreintes des fichiers : seuls les documents nouveaux/modifiés sont réindexés et les documents supprimés sont retirés de Qdrant. Aucune reconstruction de l'image API n'est nécessaire. Les changements effectués sur l'hôte sont donc pris en compte au prochain tour d'IA. La synchronisation manuelle est utile pour appliquer immédiatement des changements et contrôler l'index. Pendant l'indexation, la page montre une animation, la phase en cours et la progression par document; les compteurs détaillés sont rafraîchis à la fin. La tâche d'arrière-plan et son état en mémoire ne survivent pas au redémarrage; la synchronisation automatique au prochain appel IA reste disponible. Le premier indexage peut prendre plus de temps lorsque le corpus est volumineux.

L'extraction PDF ne fait pas d'OCR. Les scans sans couche texte, pages PDF illisibles et formats non pris en charge ne fournissent pas d'extraits; les pièces jointes du ticket ne sont pas intégrées au RAG. Les pages illisibles et les fichiers sans texte extractible produisent un avertissement dans les logs API. Les références sont ajoutées au contexte de génération sous la forme nom du document et numéro de page lorsque disponible. Pour réinitialiser uniquement l'index, arrêter le projet puis supprimer le volume `qdrant-data` avec une commande ciblée de suppression de volume après confirmation du volume exact; les documents sources restent intacts.

Pour le compte de démonstration, les adresses internes en `.local` (par exemple `admin@support.local`) sont acceptées par l'API. Cette adresse sert d'identifiant de connexion et n'est pas destinée à recevoir des e-mails.

### Utiliser Anthropic

Dans `.env`, choisir `AI_PROVIDER=anthropic` et renseigner `ANTHROPIC_API_KEY`. Redémarrer l'API avec `docker compose up -d --build api`. Le service Ollama continue d'exister dans le Compose de développement mais n'est pas appelé par le graphe Anthropic.

### Enrichissement par la KB constructeur

Optionnel et désactivé par défaut (`ENABLE_KB_BROWSER_SCRAPER=false`). Activation, connexion MFA via noVNC, sélecteurs CSS, métriques et dépannage : voir `docs/kb-scraper.md`. Le navigateur est dans le profil Compose `kb` (`docker compose --profile kb up -d`); son profil Chromium est conservé dans le volume `kb-browser-profile` (à sauvegarder comme les autres volumes : il contient une session authentifiée). Variables : `ENABLE_KB_BROWSER_SCRAPER`, `KB_BROWSER_URL`, `KB_BROWSER_TOKEN`, `KB_LOGIN_URL`, `KB_MAX_ARTICLES` (API) et `KB_BASE_URL`, `KB_*_SELECTOR`, `KB_BROWSER_VNC_PASSWORD` (navigateur).

### Que sauvegarder avant de tout supprimer ?

Les documents d'entreprise gardent leurs fichiers originaux dans `company_docs/` (voir `company-docs.md`). `docker compose down -v` supprime tous les volumes (tickets, comptes, vecteurs, modèles Ollama, session du navigateur KB). Seuls les dossiers du projet survivent : `support_docs/` (corpus RAG) et `kb_archive/` (éléments de la KB constructeur avec leurs fichiers originaux, restaurés automatiquement au démarrage; voir `kb-scraper.md`). Pour conserver aussi les tickets et les comptes : `docker compose exec -T db pg_dump -U support support > sauvegarde.sql`. Les vecteurs et les modèles se recalculent ou se retéléchargent.

### Choisir le modèle d'embedding et supprimer des documents

> **Performance** : l'indexation d'un gros corpus est coûteuse en CPU. L'instance `ollama-embed` est plafonnée (`OLLAMA_EMBED_CPUS`, 3 par défaut) pour ne pas affamer le chat, et la réponse IA n'attend jamais une réindexation volumineuse : au-delà de 2 documents à mettre à jour, la synchronisation passe en arrière-plan et la recherche utilise l'index partiel jusqu'à la fin. Après un changement de modèle d'embedding, les réponses IA s'appuient donc sur un corpus incomplet tant que l'indexation n'est pas terminée (suivi dans la page Corpus RAG). Les tickets laissés « en réflexion » par un redémarrage de l'API sont relancés automatiquement au démarrage.

Dans **Corpus RAG**, la section **Modèle d'embedding** propose Qwen3 Embedding 0.6B (défaut), BGE-M3 et Qwen3 Embedding 4B. Un modèle non téléchargé sur l'instance `ollama-embed` est grisé (`docker compose exec ollama-embed ollama pull <modèle>`). Le changement lance la réindexation du corpus dans la collection de ce modèle (`support_documents_<modèle>`); l'ancienne collection est conservée et réutilisable, mais n'est plus alimentée. Le 4B donne en général de meilleurs résultats au prix d'une indexation nettement plus lente et d'environ 2,5 Go de mémoire.

Les documents se suppriment un par un ou par sélection multiple dans **Documents du corpus**. La suppression retire le fichier de `support_docs` et ses extraits de la collection active; les autres collections sont nettoyées à leur prochaine synchronisation. Elle est refusée pendant une indexation. Si Qdrant est indisponible, le fichier est tout de même supprimé et l'index est nettoyé à la synchronisation suivante.

Après la mise à jour depuis une version utilisant `nomic-embed-text`, le nouveau modèle utilise une nouvelle collection vide : lancer **Synchroniser l'index**. L'ancienne collection `support_documents` peut être supprimée de Qdrant.

### Erreur 413 lors d'un envoi de fichiers

Trois limites s'appliquent et doivent rester cohérentes :

1. **nginx** (`frontend/nginx.conf`, `client_max_body_size`) : taille totale de la requête. 2560 Mo pour `/api/admin/rag/documents` (50 × 50 Mio) et 60 Mo pour le reste de l'API (5 × 10 Mio). Sans cette directive nginx n'accepte que 1 Mo et répond une page HTML « 413 Request Entity Too Large ».
2. **API, nombre de fichiers** : `RAG_MAX_FILES_PER_UPLOAD` (corpus) ou `MAX_ATTACHMENTS_PER_UPLOAD` (tickets).
3. **API, taille par fichier** : `RAG_MAX_FILE_SIZE` (corpus) ou `MAX_ATTACHMENT_SIZE` (tickets). Le message d'erreur est alors en français et nomme le document.

Si vous relevez les variables de l'API, relevez aussi `client_max_body_size` (nombre de fichiers × taille maximale), puis `docker compose up -d --build web api`. Le corpus est écrit sur disque par blocs : un envoi de 2,5 Go ne charge pas 2,5 Go en mémoire, mais exige l'espace disque correspondant dans `support_docs`.

### Activer la recherche web de la console d'aide

La console d'aide (`help-console.md`) peut compléter ses réponses par une recherche web gratuite via une instance **SearXNG** auto-hébergée, jamais fournie par défaut. Déployer SearXNG avec le format JSON autorisé (`search.formats: [html, json]` dans `settings.yml`), puis renseigner `WEB_SEARCH_URL` (par exemple `http://searxng:8080`) dans `.env` et relancer `docker compose up -d api`. Tant que la variable est vide, la case « Recherche web » est grisée. Seule la question, expurgée des e-mails, adresses IP/MAC et numéros de téléphone, est envoyée.

### Configurer un modèle Ollama différent

Changer `OLLAMA_MODEL` dans `.env`, par exemple un autre modèle Qwen3 adapté à la mémoire disponible, puis lancer `docker compose up -d ollama-model api`. Si le nom du modèle est modifié, le nouveau modèle sera téléchargé et conservé dans le volume Ollama. Pour télécharger d'autres modèles sans changer le modèle par défaut, les lister dans `OLLAMA_EXTRA_MODELS` (`qwen3:4b` par défaut).

Les variables `OLLAMA_*` ne définissent que la configuration IA **initiale**. Dès que l'administrateur enregistre **Configuration IA**, la configuration en base prévaut. Le bouton **Rétablir les valeurs par défaut** (ou `DELETE /api/admin/ai-routing`) supprime la configuration enregistrée et réapplique ces variables.

### Régler le raisonnement (think) et le délai par modèle

Dans **Configuration IA**, chaque modèle affecté à une règle expose deux réglages : **Raisonnement (think)** et **Délai max (s)** (10 à 1800 s; 120 s si le champ est absent d'une ancienne configuration). La configuration initiale applique `OLLAMA_THINK` et `OLLAMA_TIMEOUT_SECONDS`. Sur CPU, le raisonnement de qwen3 est particulièrement coûteux ; le désactiver pour ce modèle sauf besoin précis : le raisonnement caché peut représenter plus de 90 % des tokens générés (11 minutes mesurées avec qwen3:4b sur CPU pour un ticket de test, dont ~3 300 tokens de raisonnement pour ~200 de réponse utile). Augmenter le délai pour un modèle lent ou lorsque `think` est activé. Le délai est toujours celui de l'administrateur : les utilisateurs ne peuvent choisir ni le délai ni un modèle absent de la configuration. Le proxy nginx du frontend attend jusqu'à 300 s.

## Dépannage

### L'interface ne s'ouvre pas

- Vérifier l'état Docker Desktop et `docker compose ps`.
- Vérifier que le port `8080` n'est pas déjà utilisé.
- Lire les logs : `docker compose logs --tail=100 web api`.
- Le frontend est servi sur le port hôte 8080, et non sur le port 5173 (réservé au mode développeur).

### Écran de connexion mais API indisponible

- Vérifier `docker compose logs --tail=150 api db`.
- Attendre que `db` soit `healthy` et `api` démarré.
- Vérifier le proxy Nginx : `docker compose exec web wget -qO- http://api:8000/api/health`.
- Redémarrer sans supprimer la base : `docker compose restart api web`.

### Analyse IA en erreur ou longue

- Lire l'erreur affichée sur la fiche et les logs `docker compose logs --tail=200 api ollama ollama-model`.
- La création renvoie maintenant le ticket avant l'appel au modèle; une analyse lente ne doit plus provoquer un 504 sur `POST /tickets`. Le ticket reste en diagnostic IA et peut être transmis au support en cas d'erreur du fournisseur.
- Les tâches FastAPI en arrière-plan ne survivent pas à un redémarrage du conteneur. Si l'état reste `pending`, inspecter les logs API et utiliser le transfert manuel pour ne pas bloquer la prise en charge; pour une charge de production, utiliser une file durable.
- Confirmer que `AI_PROVIDER` est `ollama` et que `OLLAMA_BASE_URL` est `http://ollama:11434`.
- Vérifier si `ollama-model` a terminé et si le modèle est présent : `docker compose exec ollama ollama list`.
- Le RAG requiert également Qdrant et le modèle d'embedding actif sur l'instance dédiée. Vérifier `docker compose logs --tail=100 qdrant api ollama-embed ollama-embed-model`, `docker compose ps` et que le modèle apparaît dans `docker compose exec ollama-embed ollama list`. Une incompatibilité de dimension d'embedding ou un Qdrant indisponible est consigné dans les logs API.
- Si les réponses ne citent pas de documentation utile, vérifier que les documents sont bien dans `support_docs/` et que leur PDF contient du texte sélectionnable; inspecter les logs `api` lors de la synchronisation.
- Sur une machine sans ressources suffisantes, employer un modèle plus petit ou Anthropic.
- Pour Anthropic, vérifier la clé, les droits de compte, le nom du modèle et l'accès Internet du conteneur API.

### Pièce jointe manquante ou impossible à télécharger

- Vérifier les logs API et que le volume `support-uploads` est monté sur `/app/uploads`.
- Le stockage comprend les métadonnées en base et les fichiers dans le volume; sauvegarder les deux pour une restauration cohérente.
- Seuls les types/extensions documentés dans le guide API sont acceptés. Limites par défaut : 5 fichiers par envoi, 10 Mio par fichier.
- L'IA reçoit le nom du fichier seulement. Le client et l'agent peuvent le télécharger via l'URL protégée du ticket.

### Mot de passe admin oublié

Un administrateur peut ouvrir **Utilisateurs**, définir un mot de passe temporaire d'au moins 10 caractères pour un compte non système, puis le transmettre à son titulaire par un canal sûr. Un agent peut le faire pour les clients et les autres agents de ses entreprises. Le mot de passe est haché et n'est jamais restitué par l'API; l'action apparaît dans `user_admin_audit`, sans valeur secrète. Le dernier administrateur actif ne peut pas être désactivé ni rétrogradé depuis la console.

### Erreurs de base / évolution du schéma

Consulter les logs API puis PostgreSQL. Au démarrage, l'application ajoute les champs d'entreprise et d'approbation manquants aux utilisateurs existants. Les anciens comptes clients sans entreprise sont automatiquement placés en attente : un administrateur doit les rattacher à une entreprise dans **Demandes d’accès**, puis les approuver. Les tickets déjà enregistrés sont conservés.

Le MVP utilise `create_all` et une mise à niveau additive ciblée; ce mécanisme ne remplace pas Alembic pour les futures évolutions. Avant un changement de modèle sur une base conservée, préparer une migration versionnée et une sauvegarde. Ne pas employer `docker compose down -v` pour corriger une erreur de schéma si les données sont à garder.

## Sauvegardes locales

Pour exporter la base PostgreSQL du Compose :

```sh
docker compose exec -T db pg_dump -U support support > support-backup.sql
```

Restauration (destructrice pour la base cible) :

```sh
docker compose exec -T db psql -U support support < support-backup.sql
```

Protéger ces fichiers comme des données client. Prévoir une sauvegarde séparée du volume Ollama uniquement si le coût du re-téléchargement du modèle est important.

## Sécurité et mise en production

Ce Compose est destiné au développement et à la validation sur Docker Desktop, pas à une exposition Internet sans durcissement. Avant production : secrets gérés hors `.env` et hors dépôt, HTTPS derrière un proxy, mot de passe et comptes DB dédiés, politique de sauvegarde testée, limitation de débit et politique de rétention, journalisation centralisée sans secrets, mises à jour d'images contrôlées, migrations, surveillance et revue de sécurité. Les tickets peuvent contenir des données sensibles : limiter leur contenu aux informations techniques nécessaires.
