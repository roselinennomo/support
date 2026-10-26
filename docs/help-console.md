# Console d'aide

Conversation libre avec l'assistant technique, réservée aux **administrateurs**, aux **agents support** et aux **clients indépendants** (voir `independent-clients.md`, périmètre limité à leur entreprise individuelle), indépendante des tickets. Menu : « Console d'aide ».

## Principes

- **Conversations personnelles** : chacun ne voit que les siennes (même l'administrateur ne lit pas celles des autres). Historique paginé, recherche par titre, suppression.
- **Périmètre fixé à la création** : aucune entreprise (connaissances communes seulement) ou **une seule** entreprise. Un administrateur peut choisir n'importe quelle entreprise active; un agent, seulement ses entreprises affectées. Une conversation ne mélange jamais deux entreprises.
- **Connaissances communes** (sans donnée personnelle) : corpus documentaire Alcatel et KB constructeur déjà indexée. Si une entreprise est choisie, ses **documents privés** (voir `company-docs.md`) sont ajoutés, en tête du contexte.
- **Mode ticket** : choisir un ticket copie son contexte (titre, produit, description, derniers échanges, noms des pièces jointes) dans la conversation et impose l'entreprise du ticket. Les **pièces jointes lisibles** du ticket (PDF, Word, PowerPoint, Excel, texte, configurations) sont lues et ajoutées au contexte (pastilles « du ticket »); les images et formats non pris en charge sont signalés à l'assistant comme non lisibles. Le ticket n'est jamais modifié; « Actualiser le ticket » recopie son état courant. Refusé si le client n'a pas consenti au traitement IA.
- **Sources complémentaires, au choix par conversation** :
  - *Portail constructeur en direct* : une recherche est lancée sur le Technical Knowledge Center et les Business Documents via le navigateur KB (même file d'attente, un seul worker); les éléments trouvés sont lus, dédupliqués et indexés comme pour un ticket. Disponible seulement si la KB constructeur est activée.
  - *Recherche web libre* : via une instance **SearXNG auto-hébergée** (gratuite). Désactivée tant que `WEB_SEARCH_URL` est vide. Seule la question, **expurgée** (e-mails, IP, MAC, téléphones), quitte l'application : jamais le contenu des documents, du ticket ou des documents d'entreprise.
- **Fichiers joints** : le bouton « Fichier » ajoute des documents au contexte de la conversation (PDF, Word, PowerPoint, Excel, texte, configurations; 5 par envoi, 10 par conversation, 10 Mo chacun). Seul le texte extrait est conservé, uniquement pour cette conversation (supprimé avec elle, jamais indexé ni partagé). Si les fichiers dépassent environ 12 000 caractères, seuls les passages les plus proches de la question sont transmis. Ils sont cités comme sources « Fichier joint ».
- **Raisonnement de l'IA** : quand le modèle l'expose (option « raisonnement » activée), il s'affiche sous la réponse dans un bloc repliable « Raisonnement de l'IA ». La réponse apparaît une fois terminée (pas de flux en direct).
- **Modèle** : choisi parmi ceux que l'administrateur a affectés au rôle « équipe support » (avec raisonnement activable), modifiable en cours de conversation.
- **Réponses asynchrones** : la réponse est calculée en tâche de fond, la page l'interroge toutes les 2,5 s. Une réponse interrompue par un redémarrage est marquée en erreur au démarrage suivant. Une seule réponse en attente par conversation.
- **Horodatage** : chaque message affiche son heure d'envoi; les réponses de l'IA affichent aussi leur temps de réponse (⏱, de l'envoi de la question à la fin du calcul).
- **Sources citées** : chaque réponse liste les extraits utilisés (corpus, documents d'entreprise, KB constructeur, web) avec lien quand il existe.

## Mesure et coûts

Chaque appel est enregistré dans `ai_executions` au nom de l'utilisateur avec `ticket_id` vide (colonne désormais facultative, migrée automatiquement). Les écrans « Évaluation LLM » et « Coûts IA » les affichent sous « Console d'aide ». Le contenu (prompt/réponse) n'est conservé que si l'utilisateur a consenti aux traces IA, avec la même rétention que les tickets.

## Diagnostic

Les échecs d'appels IA de la console (par modèle essayé) sont enregistrés dans `ai_failures`. La page **Diagnostic IA** affiche une section « Console d'aide » (appels réussis récents, compteurs sur 24 h) et filtre les « Échecs récents » par origine (tickets / console d'aide) et par utilisateur; l'état des fournisseurs et la supervision en tiennent compte. `GET /api/admin/ai/diagnostics?source=all|ticket|console&user_id=…`.

## Configuration

| Variable | Rôle |
| --- | --- |
| `WEB_SEARCH_URL` | URL de base de SearXNG (ex. `http://searxng:8080`, format JSON activé). Vide : recherche web désactivée. |
| `WEB_SEARCH_MAX_RESULTS` | Nombre de résultats web transmis au modèle (5 par défaut). |
| `HELP_HISTORY_MESSAGES` | Messages précédents fournis au modèle (12 par défaut). |

Le routage utilisé est la tâche `agent_assistance` du rôle équipe support (activation, ordre et délai configurés dans « Configuration IA »).
