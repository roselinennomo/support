# Alcatel OXE Support

Portail de support technique pour les incidents Alcatel-Lucent OXE et produits associés. Le projet fournit une interface client et agent, une API FastAPI, un workflow d'analyse LangGraph et un déploiement Docker Compose.

## Démarrage rapide (Docker Desktop)

1. Copier `.env.example` vers `.env`, puis modifier au minimum `SECRET_KEY` et `INITIAL_ADMIN_PASSWORD`.
2. Dans Docker Desktop, démarrer le projet depuis ce dossier :

   ```sh
   docker compose up --build
   ```

3. Ouvrir <http://localhost:8080>. L'interface est servie par Nginx et l'API est accessible sous `/api`.
4. Se connecter avec l'administrateur défini dans `.env`. Ollama est configuré par défaut avec `deepseek-r1:1.5b` et le raisonnement (`think`) activé; `qwen3:4b` est aussi téléchargé. L'administrateur règle les modèles, le raisonnement et le délai de chaque règle dans **Configuration IA**; les clients (diagnostic) et les agents (aide IA, résumé du ticket) choisissent ensuite parmi les modèles qu'il a affectés. Le premier démarrage télécharge ces modèles en arrière-plan, ce qui peut prendre du temps et plusieurs gigaoctets. L'interface et l'API démarrent sans attendre la fin du téléchargement.

Pour arrêter les services : `docker compose down`. Pour supprimer aussi les données locales : `docker compose down -v` (suppression irréversible des tickets et comptes de la base).

## Fonctionnalités principales

- Demandes de compte client avec authentification JWT seulement après validation du support.
- Accès client contrôlé : entreprise et code d’invitation, compte en attente, validation par un agent affecté ou un administrateur avant connexion.
- Gestion des entreprises clientes et affectation de leur équipe support.
- Dépôt d'incidents suivi d'un diagnostic conversationnel IA, puis suivi du statut et de l'historique.
- Échange guidé entre le client et l'IA : questions de clarification, étapes de premier niveau et ajout de pièces jointes.
- Si l'IA recommande une escalade, le ticket reste en diagnostic et propose au client de le transmettre à un agent après son premier échange; une panne IA entraîne un transfert automatique vers la file support.
- Cycle de vie contrôlé des tickets : transitions valides, note de résolution obligatoire, clôture confirmée par le client, réouverture avant clôture et rejet motivé.
- File de traitement des agents, recherche et filtres par statut.
- Assistance IA privée pour les agents, avec l'historique complet du ticket et des notes internes.
- Pièces jointes téléchargeables et conservées dans un volume Docker persistant.
- Escalade manuelle par l'agent avec équipe, motif et référence externe.
- Notes internes, changements de statut, attribution à un agent et historique d'audit.
- Rôles `customer`, `agent` et `admin`; les agents sont limités aux entreprises auxquelles ils sont affectés, et la création d'agents est réservée à l'administrateur.
- Choix du fournisseur IA par configuration (`ollama` par défaut ou `anthropic`).
- Diagnostic ticket (parcours de bout en bout, diagramme de séquence, temps perçu vs appels aux modèles) : voir [docs/ticket-diagnostic.md](docs/ticket-diagnostic.md).
- Clients indépendants (entreprise individuelle, facturation propre, console d'aide) : voir [docs/independent-clients.md](docs/independent-clients.md).
- Console d'aide (administrateurs, agents et clients indépendants) : conversations libres ou sur un ticket, connaissances communes + documents d'une entreprise, recherche live du portail constructeur et recherche web optionnelles : voir [docs/help-console.md](docs/help-console.md).
- Documents propres à chaque entreprise (privés, index séparé, déposés par l'entreprise ou un administrateur) : voir [docs/company-docs.md](docs/company-docs.md).
- Enrichissement optionnel par la KB constructeur (avec archive locale `kb_archive/` des fichiers originaux, restaurée automatiquement) via un navigateur Playwright conteneurisé (désactivé par défaut) : voir [docs/kb-scraper.md](docs/kb-scraper.md).
- RAG documentaire avec Qdrant, indexation incrémentale automatique des PDF, Markdown et fichiers texte dans `support_docs`, recherche de passages pertinents et références de sources dans les réponses IA.
- Console d’administration de supervision des services : santé de la base, de l’API, d’Ollama et de la file de support.

La création du ticket ne dépend pas de la réponse du modèle : elle est enregistrée et renvoyée avant le traitement IA en tâche de fond, ce qui évite de garder la requête HTTP ouverte pendant un appel lent (cause du 504). Une panne du fournisseur laisse le ticket enregistré et le transmet à la file support. Les pièces jointes sont accessibles à l'équipe, mais leur contenu n'est pas encore lu par le modèle. Les documents de `support_docs` alimentent le RAG; leur contenu est synchronisé à chaque requête IA (fichiers ajoutés, modifiés ou supprimés) et les extraits utilisés sont référencés par nom de fichier et page lorsque disponible.

Le dossier `support_docs/` est réservé aux documents fournis localement pour le RAG et n'est pas versionné. Ajoutez-y uniquement des documents que vous êtes autorisé à traiter; ne publiez pas de manuels tiers, de données client ou de documents confidentiels.

### Différence entre agent support et administrateur

- Agent support : traite les incidents de la file de support, les assigne, répond aux demandes, ajoute des notes internes et escalade vers une équipe experte.
- Administrateur : dispose des mêmes droits qu'un agent support, plus la gestion des comptes collaborateurs et l'accès à la console de supervision pour vérifier la santé des services et les indicateurs de support.

## Documentation

- [Guide d'architecture et de développement](docs/architecture.md)
- [Installation, configuration et dépannage](docs/operations.md)
- [Guide utilisateur par rôle](docs/user-guide.md)
- [API et modèle de données](docs/api.md)

## Développement local

API, depuis `backend/` :

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
uvicorn app.main:app --reload
```

Sans variable `DATABASE_URL`, l'API utilise SQLite dans `backend/support.db`. Sans Ollama accessible, la création d'incident fonctionne toujours et expose le statut d'analyse en erreur; définir `AI_PROVIDER=ollama` et `OLLAMA_BASE_URL` pour utiliser le modèle.

Interface, depuis `frontend/` :

```sh
npm install
npm run dev
```

Vite sert l'interface sur <http://localhost:5173> et redirige `/api` vers `http://localhost:8000`. Consultez les docs pour les variables, les comptes initiaux et les commandes de test.
