# Architecture et guide de développement

## Vue d'ensemble

```text
Navigateur (React/Vite) ── Nginx :8080 ── FastAPI :8000 ── PostgreSQL :5432
                                            ├── LangGraph ── Ollama « chat » :11434 (deepseek-r1:1.5b par défaut, qwen3:4b disponible)
                                            ├── LangGraph ── Anthropic Messages API (option)
                                            ├── KB (option) ── kb-browser :8090 (Playwright, profil persistant) ── KB constructeur
                                            └── RAG ── Ollama « embed » :11434 (qwen3-embedding:0.6b par défaut, bge-m3, qwen3-embedding:4b)
                                                   ├── Qdrant :6333 (une collection par modèle d'embedding)
                                                   └── ./support_docs (PDF, Markdown, texte)
```

L'application est découpée en trois services de déploiement :

- `frontend/` : SPA React, construite avec Vite puis servie par Nginx. Nginx relaie `/api/*` vers le conteneur API; le navigateur n'appelle pas directement le fournisseur de modèle.
- `backend/app/` : API REST FastAPI, validation Pydantic, authentification JWT et autorisations par rôle.
- PostgreSQL : utilisateurs, tickets, messages, pièces jointes (métadonnées) et événements. Des volumes Compose conservent les données et les fichiers entre redémarrages.

Un module optionnel (`backend/app/kb/`, conteneur `kb-browser`) enrichit le RAG avec la KB constructeur dans un index Qdrant séparé; voir `docs/kb-scraper.md`. Deux conteneurs Ollama coexistent : `ollama` (génération) et `ollama-embed` (embeddings RAG); `ollama-model` télécharge les modèles de conversation configurés et `ollama-embed-model` ceux d'embedding. Qdrant persiste les vecteurs dans le volume `qdrant-data`. LangGraph fournit des graphes distincts pour le diagnostic conversationnel du client, l'aide IA privée à l'agent et l'analyse structurée historique. Le routage de génération est persisté en base par tâche et rôle, avec ordre de priorité, repli vers le modèle suivant et interrupteur général. Chaque client dispose de consentements séparés (traitement IA et conservation des traces), activés par défaut. La table `ai_executions` enregistre les tokens, la durée, le modèle, le ticket et les acteurs; le prompt et la réponse ne sont conservés que si le consentement aux traces est actif, pour 30 jours maximum. `ai_model_prices` contient les prix USD administrés, utilisés pour les estimations de coûts. Avant chaque génération, `app/rag.py` compare les empreintes SHA-256 des fichiers pris en charge dans `support_docs`, réindexe les fichiers modifiés/ajoutés, retire les sources supprimées, puis cherche les extraits sémantiquement pertinents. Les documents sont découpés par page en extraits avec chevauchement; les réponses reçoivent les références fichier/page associées. Le fournisseur d'embeddings reste Ollama (instance dédiée) même si le fournisseur de génération est Anthropic.

## Cycle de vie d'un incident

1. L'équipe support crée une entreprise, l'affecte à un ou plusieurs agents et transmet son code d'accès au contact client.
2. Le client dépose une demande d'accès avec ce code et ses identifiants. Le compte est `pending` : aucune session ni création de ticket n'est permise.
3. Un agent affecté ou un administrateur vérifie la demande et l'approuve ou la refuse. Seul un compte `approved` peut se connecter.
4. Le client approuvé crée un incident (produit, version, titre, description). Avec le consentement de traitement IA (activé par défaut), l'API enregistre le ticket en `ai_triage` et lance le diagnostic après la réponse HTTP. Sans ce consentement, le ticket est transmis directement à la file support.
5. Le graphe de diagnostic examine la conversation publique. L'assistant peut demander une information, proposer une action sûre et intégrer le nom des pièces jointes. À chaque réponse client, l'IA peut reprendre le diagnostic.
6. Si l'IA recommande une escalade, le ticket reste dans `ai_triage` et l'interface propose un transfert manuel après la première réponse du client. Seule une panne IA entraîne un transfert automatique dans la file (`new`). Messages, fichiers et chronologie sont conservés.
7. Un agent lit l'historique complet et peut demander une aide IA privée. Le modèle reçoit les messages publics et internes, ainsi que les noms de fichiers; sa réponse est enregistrée comme message interne invisible au client.
8. L'agent traite le ticket selon les transitions autorisées, répond publiquement ou en interne, l'assigne ou enregistre une escalade externe. Le passage à `resolved` impose une note publique décrivant la solution; le rejet (`rejected`) impose un motif public et est terminal.
9. Le client peut répondre à un ticket résolu pour le rouvrir en `new`, ou confirmer la résolution pour le passer à `closed`. La clôture est réservée au client; un ticket clos ou rejeté est terminal.
10. Le client voit ses messages publics, les pièces jointes, les événements de suivi et l'état d'escalade. Les notes internes ne lui sont jamais renvoyées.

Les administrateurs peuvent consulter et annoter les traces consenties dans **Évaluation LLM**, et filtrer les consommations ainsi qu’ajuster les tarifs par modèle dans **Coûts IA**. Le retrait du consentement de conservation efface le contenu des traces historiques du client; les compteurs d’usage sans contenu restent disponibles pour la comptabilité estimative.

Les accords SLA sont négociés par entreprise, type de ticket (`incident`, `problem`, `request`) et priorité (`p1` à `p4`). Les propositions du client nécessitent une validation interne; leur approbation s'applique aux nouveaux tickets uniquement. Les rapports de période sont générés en tâche de fond et leur état ainsi que leur PDF sont conservés en base. Les factures utilisent les paramètres de chaque entreprise; les crédits prépayés sont consommés lors de la création du ticket.

États : `ai_triage`, `new`, `in_progress`, `waiting_support`, `waiting_customer`, `escalated`, `resolved`, `closed`, `rejected`. Les tickets `ai_triage` restent dans l'espace client jusqu'à la fin ou au transfert; les agents ne les voient qu'après transfert. Une réponse publique du support laisse le statut inchangé; l'agent le passe manuellement à `waiting_customer` lorsqu'il attend le client. Une réponse client fait passer le ticket à `waiting_support`; les notes internes ne modifient pas l'état. Les échéances SLA continuent de s'écouler indépendamment de ces états d'attente. L'API valide explicitement les transitions, exige une note de résolution ou un motif de rejet, et réserve la clôture au client.

Les fichiers sont écrits sous `UPLOAD_DIR` avec un nom généré, une liste d'extensions autorisées et une limite par fichier/envoi. En Compose, le chemin est monté sur le volume `support-uploads`. Les métadonnées restent en base. Les fichiers ne sont pas envoyés au fournisseur IA et aucun OCR/extracteur de journaux n'est intégré.

## Rôles et frontières d'accès

- `customer` : associé à une entreprise et approuvé par le support; voit et complète les tickets de son entreprise (y compris ceux de ses collègues), crée des tickets et publie des messages publics.
- `agent` : gère les entreprises auxquelles il est affecté, valide les demandes d'accès correspondantes, traite les tickets de leurs clients et administre les comptes clients/agents des entreprises de son périmètre (hors comptes admin et son propre compte).
- `admin` : mêmes droits support que l'agent sur toutes les entreprises, gestion globale des utilisateurs et paramètres, réinitialisation des mots de passe, configuration de facturation et crédits, et accès à la supervision.

Les endpoints passent par `app/auth.py` (`get_current_user`, `require_roles`). Les requêtes client vérifient le propriétaire du ticket; les requêtes agent vérifient l'affectation à l'entreprise. Les changements de comptes et réinitialisations de mots de passe sont audités dans `user_admin_audit`, sans journaliser les secrets. Les codes d'inscription sont transmis une seule fois et stockés sous forme d'empreinte SHA-256. Ne pas exposer de nouveaux champs internes dans les schémas destinés au client sans revoir `_serialize` dans `app/tickets.py`.

Les adresses suivent la validation standard, avec une exception limitée au suffixe `.local` pour l'identifiant d'administration de démonstration. Elles ne sont pas utilisées pour l'envoi de courriels dans cette version.

## Organisation du code

- `app/config.py` : configuration chargée depuis l'environnement.
- `app/database.py`, `app/models.py` : SQLAlchemy et persistance.
- `app/schemas.py` : entrées/sorties HTTP, contraintes de validation.
- `app/security.py`, `app/auth.py` : hachage scrypt, JWT, contrôle d'accès.
- `app/ai.py` : fournisseurs de modèle, prompts, graphes LangGraph et validation de leurs sorties.
- `app/tickets.py`, `app/users.py` : opérations métier HTTP.
- `app/service_management.py` : politiques SLA, consommation des crédits, génération des rapports PDF et facturation.
- `app/help_console.py`, `app/web_search.py` : console d'aide (conversations personnelles, réponses en tâche de fond, sources citées) et client SearXNG; les appels IA sans ticket sont mesurés avec `ai_executions.ticket_id` vide (voir `help-console.md`).
- `app/main.py` : assemblage, initialisation de schéma et compte admin initial.
- `frontend/src/App.jsx` : écrans, navigation et appels HTTP.
- `frontend/src/styles.css` : système visuel responsive.

## RAG et corpus documentaire

Indexation (corpus, KB, documents d'entreprise) : elle ne s'exécute **jamais dans la requête d'un utilisateur** au-delà de ~1 Mo de fichiers à traiter (sinon arrière-plan), elle écrit dans Qdrant **lot par lot** (reprise exacte après une interruption : seuls les extraits manquants sont recalculés; un document n'est « indexé » que lorsque tous ses extraits sont présents, sinon « Indexation partielle »), un seul lot d'indexation est calculé à la fois pour laisser un emplacement libre aux questions, et l'embedding d'une question est calculé une fois pour les trois recherches (cache de 60 s). Au démarrage, l'API reprend en arrière-plan l'indexation interrompue.

- Monter les documents dans `support_docs` (bind mount en lecture seule dans Compose). Formats indexés : PDF avec texte extractible, `.md`, `.markdown` et `.txt`; les PDF scannés nécessitent un OCR non encore intégré. Une page PDF illisible est ignorée avec un avertissement en log; un fichier sans texte extractible n'alimente pas le contexte.
- À chaque appel de diagnostic ou d'aide agent, synchroniser le corpus par empreinte, puis intégrer les meilleurs extraits dans le prompt. Le premier appel peut être plus long lors de l'indexation initiale ou après un grand ajout de documents.
- Qdrant stocke les vecteurs et métadonnées dans le volume `qdrant-data`; les documents originaux restent dans `support_docs`. Pour reconstruire l'index, retirer uniquement le volume vectoriel puis redémarrer; cela supprime l'index, pas le corpus ni les tickets.
- Les embeddings sont calculés par une instance Ollama **distincte** de celle du chat (`ollama-embed`, volume `ollama-embed-data`), quelle que soit la configuration du LLM de génération : une indexation volumineuse n'entre pas en concurrence avec les réponses IA et les deux types de modèles ne s'évincent pas de la mémoire. Sans `OLLAMA_EMBEDDING_BASE_URL`, l'API retombe sur l'instance de chat (développement local). L'administrateur choisit le modèle (Qwen3 Embedding 0.6B par défaut, BGE-M3 ou Qwen3 Embedding 4B) dans la page RAG; le choix est conservé en base (`rag_config`). Les vecteurs de modèles différents n'étant pas comparables, chaque modèle a sa collection Qdrant `<RAG_COLLECTION>_<modèle>` et sa dimension (1024, 1024 et 2560) : changer de modèle réindexe le corpus sans détruire l'index précédent. Le Qdrant du Compose est privé au réseau Docker, sans port publié.
- Le modèle reçoit les extraits retrouvés, pas les fichiers complets. Seuls les documents texte sont indexés; pièces jointes ticket, PDF scannés et images ne sont pas analysés.
- Vérifier la pertinence sur des incidents réels validés par les experts et retirer/segmenter les documents non pertinents. Le RAG réduit les réponses sans fondement sans garantir leur exactitude; l'IA doit citer les références utiles et signaler les lacunes du corpus.

Pour ajouter un fournisseur, implémenter une fonction qui retourne le texte du modèle, valider sa réponse dans `_analyze`, puis configurer le choix dans `Settings`. Pour ajouter des nœuds LangGraph, conserver des états typés et tester chaque transition avec des fournisseurs simulés.

## Développement et tests

Backend, depuis `backend/` :

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
uvicorn app.main:app --reload
```

Frontend, depuis `frontend/` :

```sh
npm install
npm run dev
npm run build
```

La configuration locale par défaut utilise `sqlite:///./support.db`; le fichier est créé au démarrage. Pour les scénarios API automatisés, isoler les bases et surcharger `get_db` afin d'éviter les données de développement.

API interactive : <http://localhost:8000/docs>. Vérification de disponibilité : `GET /api/health`.

## Limites connues

- Les tâches de fond FastAPI évitent de bloquer la requête HTTP, mais ne sont pas durables : un redémarrage peut interrompre un appel. Pour l'exploitation, les remplacer par une file persistante (Celery/RQ avec Redis ou équivalent).
- Les pièces jointes sont persistées et téléchargeables, mais leur contenu n'est pas analysé par l'IA. Les documents scannés nécessitent encore un OCR. Les SLA utilisent des heures calendaires sans calendrier ouvré ni pause; la facturation PDF requiert encore les mentions légales/fiscales de l'émetteur avant émission officielle. Il n'y a pas de notifications e-mail ou de synchronisation réelle avec un outil externe d'escalade.
- Les transitions de statut suivent une matrice métier explicite dans `app/tickets.py`; elles ne sont pas configurables par l'administrateur.
- Les nouveaux objets sont créés au démarrage avec `create_all`; une mise à niveau additive ajoute les champs d'approbation aux bases existantes. Pour une exploitation durable et le versionnement des migrations, ajouter Alembic avant les évolutions ultérieures.
