# Installation, configuration et dépannage

## Lancer dans Docker Desktop

1. Installer Docker Desktop et attendre que son moteur soit démarré.
2. Copier le modèle d'environnement : `cp .env.example .env`.
3. Dans `.env`, remplacer `SECRET_KEY` par une valeur aléatoire longue et `INITIAL_ADMIN_PASSWORD` par un mot de passe unique. Garder les identifiants Postgres de développement uniquement pour un poste local.
4. Depuis la racine du projet : `docker compose up --build`.
5. Ouvrir <http://localhost:8080>. Les logs s'affichent dans Docker Desktop ou via `docker compose logs -f api web db ollama`.

Au premier lancement, Ollama télécharge `qwen3:4b` et `nomic-embed-text` dans le volume `ollama-data`, en parallèle du démarrage de l'API et de l'interface. Qdrant conserve les vecteurs dans `qdrant-data`; le dossier `support_docs` de la racine est partagé en écriture avec l'API pour permettre aux administrateurs d'y déposer des documents. La taille des téléchargements et les performances varient selon la machine. Tant que les modèles ou Qdrant ne sont pas disponibles, les analyses RAG peuvent échouer, mais le portail reste accessible et les tickets sont conservés. Une machine disposant de peu de mémoire peut nécessiter un modèle Qwen3 plus petit configuré dans `.env`. Les modèles locaux ne requièrent pas de clé API.

`docker compose up --build` démarre les services `db`, `ollama`, `ollama-model`, `qdrant`, `api` et `web` automatiquement. Seuls PostgreSQL prêt et le démarrage sain de l'API conditionnent les services en aval; le téléchargement des modèles et l'indexation documentaire ne sont pas des prérequis au lancement du portail. Le service `ollama-model` qui termine avec le code `0` est un conteneur de tâche ponctuelle et son état `Exited (0)` est normal.

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
| `OLLAMA_MODEL` | `qwen3:4b` | Modèle local à télécharger et à utiliser. |
| `OLLAMA_EMBEDDING_MODEL` | `nomic-embed-text` | Modèle Ollama utilisé pour produire les vecteurs RAG, y compris avec Anthropic. Le modèle configuré doit produire `RAG_EMBEDDING_DIMENSION` dimensions. |
| `RAG_COLLECTION` | `support_documents` | Collection Qdrant du corpus. |
| `RAG_EMBEDDING_DIMENSION` | `768` | Dimension attendue du modèle d'embedding `nomic-embed-text`. |
| `RAG_TOP_K` | `5` | Nombre maximal d'extraits documentaires transmis à l'IA. |
| `RAG_SCORE_THRESHOLD` | `0.35` | Score minimal de similarité Qdrant; les résultats plus faibles ne sont pas fournis au modèle. |
| `ANTHROPIC_API_KEY` | vide | Clé API si `AI_PROVIDER=anthropic`. À ne jamais publier dans le dépôt ou dans l'interface. |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Identifiant de modèle Anthropic autorisé par le compte. |
| `CORS_ORIGINS` | ports locaux 8080 et 5173 | Origines autorisées, séparées par des virgules. |
| `UPLOAD_DIR` | `./uploads` | Répertoire des pièces jointes; Compose le monte sur le volume `support-uploads`. |
| `MAX_ATTACHMENT_SIZE` | `10485760` | Taille maximale en octets par fichier (10 Mio par défaut). |
| `MAX_ATTACHMENTS_PER_UPLOAD` | `5` | Nombre maximal de fichiers dans un envoi. |

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

### Configurer un modèle Ollama différent

Changer `OLLAMA_MODEL` dans `.env`, par exemple un autre modèle Qwen3 adapté à la mémoire disponible, puis lancer `docker compose up -d ollama-model api`. Si le nom du modèle est modifié, le nouveau modèle sera téléchargé et conservé dans le volume Ollama.

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
- Le RAG requiert également Qdrant et `nomic-embed-text`. Vérifier `docker compose logs --tail=100 qdrant api ollama-model`, `docker compose ps` et que `nomic-embed-text` apparaît dans `docker compose exec ollama ollama list`. Une incompatibilité de dimension d'embedding ou un Qdrant indisponible est consigné dans les logs API.
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
