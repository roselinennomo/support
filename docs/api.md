# API REST et données

Base locale : `http://localhost:8000/api`. Avec Docker Desktop, passer par `http://localhost:8080/api`. La documentation OpenAPI interactive se trouve sur `/docs` côté API.

Toutes les routes métier requièrent un jeton Bearer, sauf demande d'accès, connexion et santé. Une demande d'accès ne délivre pas de jeton.

## Authentification

| Méthode | Route | Accès | Résultat |
|---|---|---|---|
| `POST` | `/auth/register` | public | Demande d'accès client avec `company_code`; réponse `202` sans jeton. |
| `POST` | `/auth/login` | public | Jeton Bearer et profil. |
| `GET` | `/auth/me` | connecté | Profil courant. |
| `PUT` | `/auth/password` | connecté | Vérifie l’ancien mot de passe, exige 10 caractères minimum pour le nouveau, invalide les autres sessions et retourne un nouveau jeton. |

Envoyer `Authorization: Bearer <access_token>`. Les jetons expirent après 12 heures par défaut (`TOKEN_EXPIRE_MINUTES`).

La validation accepte les adresses normales ainsi que le domaine interne `.local` utilisé pour le compte d'administration de démonstration (`admin@support.local`). L'adresse `.local` sert uniquement d'identifiant de connexion; elle ne reçoit pas de courriels.

## Entreprises et validation de compte

| Méthode | Route | Accès | Résultat |
|---|---|---|---|
| `GET` | `/companies` | agent/admin | Entreprises affectées à l'agent ou toutes pour un admin. |
| `POST` | `/companies` | agent/admin | Création d'entreprise et affectation d'agents; retourne un code d'invitation à transmettre une seule fois. |
| `POST` | `/companies/{id}/invitation-code` | agent affecté/admin | Révoque l'ancien code et en génère un nouveau, affiché une seule fois. |
| `GET` | `/users/requests` | agent/admin | Demandes en attente dans le périmètre de l'agent ou toutes pour un admin. |
| `PATCH` | `/users/{id}/company` | admin | Rattache un ancien compte client en attente à une entreprise existante. |
| `PATCH` | `/users/{id}/approval` | agent/admin affecté | Accepte `{"decision":"approve"}` ou `{"decision":"reject"}`. |

Exemple de demande publique d'accès :

```json
{
  "email": "client@entreprise.fr",
  "full_name": "Camille Martin",
  "password": "mot-de-passe-de-10-caracteres",
  "company_code": "code-transmis-par-le-support"
}
```

Le code brut est retourné uniquement lors de la création de l'entreprise; le serveur conserve son empreinte. La validation active le compte immédiatement. Le refus empêche toute connexion.

## Incidents

| Méthode | Route | Accès |
|---|---|---|
| `POST` | `/tickets` | client approuvé uniquement; crée un incident pour le client courant |
| `GET` | `/tickets?status=new&search=OXE` | connecté; clients limités aux dossiers de leur entreprise, agents à leurs entreprises, admins à tous |
| `GET` | `/tickets/{id}` | client de l'entreprise, agent de l'entreprise ou admin |
| `POST` | `/tickets/{id}/conversation` | client de l'entreprise; message multipart et fichiers optionnels durant le diagnostic |
| `POST` | `/tickets/{id}/handoff` | client de l'entreprise; transmet le diagnostic et ses pièces jointes à la file agent après une réponse d'un client à la première réponse publique de l'IA |
| `POST` | `/tickets/{id}/close` | client de l'entreprise; confirme et clôture un ticket résolu |
| `POST` | `/tickets/{id}/ai-assist` | agent/admin; demande une aide IA privée, réponse asynchrone `202` |
| `POST` | `/tickets/{id}/attachments` | client de l'entreprise, agent de l'entreprise ou admin; ajout de pièces jointes |
| `GET` | `/tickets/{id}/attachments/{attachment_id}` | client de l'entreprise, agent de l'entreprise ou admin; téléchargement protégé |
| `POST` | `/tickets/{id}/messages` | client de l'entreprise, agent de l'entreprise ou admin; visibilité `public` ou `internal` (agents uniquement pour interne) |
| `PATCH` | `/tickets/{id}/status` | agent de l'entreprise ou admin; transitions contrôlées, note de résolution ou motif de rejet requis |
| `POST` | `/tickets/{id}/escalate` | agent de l'entreprise ou admin |
| `PATCH` | `/tickets/{id}/assignment` | agent de l'entreprise ou admin; l'agent assigné doit être affecté à la même entreprise; assigner à un administrateur est réservé aux administrateurs (403 sinon); un client n'est jamais assignable (422) |

`GET /tickets` accepte `limit` (défaut 200, maximum 1000); l'interface charge jusqu'à 1000 incidents et les pagine côté client. Les traces et coûts IA se paginent côté serveur (`limit`, `offset`), de même que les recherches KB (`GET /admin/kb` : `jobs_limit` ≤ 100, `jobs_offset`, `jobs_status`; réponse `jobs_total` et `job_counts` par statut; `GET /admin/kb/debug` : `limit`, `offset`, `job_id` pour ouvrir une recherche hors de la page courante, réponse `jobs_total` et `focused_job`). Les clients d'une même entreprise voient et peuvent compléter les tickets de leurs collègues (les notes internes restent masquées); un client d'une autre entreprise reçoit `404`. Le consentement IA appliqué à un ticket reste celui de son auteur. Les recherches renvoient au plus 200 tickets, triés par dernière activité décroissante. Un statut incorrect produit une erreur de validation. Une ressource privée ou inconnue renvoie `404` afin de ne pas révéler son existence.

### Champs principaux

`POST /tickets` accepte immédiatement :

```json
{
  "title": "Les appels entrants n'aboutissent plus",
  "description": "Depuis ce matin, les appels externes vers le site principal échouent...",
  "product": "OXE",
  "version": "R12.4",
  "ticket_type": "incident",
  "priority": "p3"
}
```

`ticket_type` accepte `incident`, `problem` ou `request`; `priority` accepte `p1` à `p4`. Les valeurs par défaut préservent les anciens clients API. Si une politique SLA approuvée correspond à l'entreprise, au type et à la priorité, le ticket reçoit ses échéances à la création. Sans politique applicable, le ticket est créé sans échéances SLA.

La création renvoie le ticket en `ai_triage` et `ai_status=pending` sans attendre le fournisseur IA; le client peut donc commencer le suivi sans requête HTTP longue. L'assistant poursuit le diagnostic dans une tâche de fond et les consultations ultérieures exposent `ai_status` (`pending`, `complete`, `error`), `ai_summary`, `ai_recommendations`, `ai_escalation_recommended` et `ai_error` pour les rôles internes. Une recommandation d'escalade positionne `ai_escalation_recommended` et affiche un avertissement, sans changer le statut ni transmettre le ticket. Après que le client a répondu à la première réponse publique de l'IA, il peut choisir le transfert manuel. Une erreur du fournisseur transmet automatiquement le ticket à la file support.

Lors d'un transfert manuel par le client après un échange avec l'IA, une tâche de fond demande à l'IA de résumer l'historique et de proposer les premières recommandations. Le résultat est ajouté à la conversation comme note interne visible uniquement par les agents et administrateurs. Le transfert n'attend pas ce résumé; si la génération échoue, une note interne indique à l'équipe de consulter l'historique.

### Cycle de vie et transitions

Le cycle de traitement suit `new`/`waiting_support` → `in_progress` → `waiting_customer` → `waiting_support` → `in_progress` → `resolved` → `closed`. Une réponse publique du support ne change pas automatiquement le statut : l'agent choisit manuellement `waiting_customer` lorsqu'il attend une réponse du client. Après une réponse ou un ajout de pièce jointe par le client, le ticket passe à `waiting_support`. Une note interne ne change pas le statut. Les échéances SLA sont ancrées à la création et continuent de s'écouler pendant `waiting_customer` et `waiting_support`; les retours de statut vers `new` sont possibles pour réinitialiser le traitement. L'API refuse les autres transitions et ne laisse pas un agent clôturer directement un ticket.

Pour passer à `resolved`, l'agent doit fournir `resolution_note` (10 caractères minimum, visible par le client). L'IA peut proposer une résolution; sa réponse publique est alors enregistrée comme note de résolution, mais le client doit la confirmer via `POST /tickets/{id}/close`. Une réponse du client sur un ticket résolu le rouvre en `new`. Un ticket `closed` ne peut pas être rouvert.

Le statut `rejected` est réservé aux agents et administrateurs, nécessite `rejection_reason` (10 caractères minimum, visible par le client) et est terminal. Le client doit créer une nouvelle demande s'il souhaite poursuivre après un rejet. Les champs `resolution_note` et `rejection_reason` sont retournés avec le ticket.

Pour ajouter une pièce jointe à l'échange, envoyer `multipart/form-data` à `/tickets/{id}/conversation` avec `body` et zéro à cinq champs `files`. Extensions autorisées : `.csv`, `.jpg`, `.jpeg`, `.log`, `.pdf`, `.pcap`, `.png`, `.txt`, `.webp`, `.zip`; taille maximale par défaut : 10 Mio. Un message peut être omis si au moins un fichier est envoyé. La réponse contient les métadonnées des pièces jointes et leurs URL de téléchargement. L'accès aux fichiers applique les mêmes droits que l'accès au ticket.

L'assistance agent s'utilise avec `POST /tickets/{id}/ai-assist` :

```json
{"question": "Quelles vérifications sûres proposer ensuite ?"}
```

La réponse `202 {"status":"pending"}` signifie que le traitement est lancé. La réponse de l'IA apparaît ensuite comme un message `internal`, visible uniquement par les agents et administrateurs. Le modèle reçoit tout l'historique, y compris les notes internes, et les noms des pièces jointes; il n'en lit pas le contenu.

Il n'existe pas d'escalade manuelle entre agents (`POST /tickets/{id}/escalate` supprimée, le statut `escalated` n'est plus assignable) : l'escalade est le transfert de l'IA vers le support (`POST /tickets/{id}/handoff`). Les tickets déjà `escalated` restent lisibles et peuvent changer de statut.

## SLA, rapports et facturation

Les délais SLA sont exprimés en heures calendaires UTC, sans calendrier ouvré ni pause. Le client soumet une proposition par type/priorité; l'agent affecté à l'entreprise ou un administrateur peut l'approuver ou la refuser. Une nouvelle approbation remplace la politique active de même combinaison, sans modifier rétroactivement les tickets existants.

| Méthode | Route | Accès | Résultat |
|---|---|---|---|
| `GET` | `/service/sla?company_id=...` | client, agent, admin | Politiques SLA accessibles; un agent doit être affecté à l'entreprise sélectionnée. |
| `POST` | `/service/sla/proposals` | client | Soumet une politique avec type, priorité, heures de contournement/résolution et unités de pénalité. |
| `PATCH` | `/service/sla/{policy_id}` | agent affecté/admin | Décision `approve` ou `reject` et note facultative. |
| `POST` | `/tickets/{id}/sla/workaround` | agent de l'entreprise/admin | Enregistre l'instant de mise en place du contournement. |
| `POST` | `/tickets/{id}/sla/compliance` | agent de l'entreprise/admin | Enregistre une seule constatation avec `finding` (`nonconforming` ou `no_defect`) et une justification (5 caractères minimum). |
| `POST` | `/service/reports` | client, agent, admin | Lance un PDF en tâche de fond pour une période inclusive (`period_start`, `period_end`); agent/admin indiquent `company_id`. Réponse `202` avec l'identifiant de tâche. |
| `GET` | `/service/reports` | client, agent, admin | Tâches de rapport des entreprises accessibles et leur état (`pending`, `processing`, `completed`, `failed`). |
| `GET` | `/service/reports/{job_id}/download` | client, agent, admin autorisés | Télécharge le PDF lorsque la tâche est `completed`. |
| `GET` | `/billing/summary?company_id=...&year=2026&month=1` | client, agent, admin | Indicateurs du mois et estimation de facture; les agents indiquent leur entreprise. |
| `GET` | `/billing/invoices?company_id=...` | client, agent, admin | Historique des factures accessibles. |
| `GET` | `/billing/invoices/{id}/download` | client, agent, admin autorisés | Télécharge la facture PDF. |
| `GET` | `/billing/companies` | admin | Entreprises et configuration financière. |
| `PUT` | `/billing/companies/{company_id}` | admin | Configure les modes `per_ticket`/`monthly`, `prepaid`/`postpaid`, devise, tarifs, taxe et délai de paiement. |
| `POST` | `/billing/companies/{company_id}/credits` | admin | Attribue des `ticket_credits`, `month_credits` et un motif; au moins un crédit doit être positif. |
| `POST` | `/billing/invoices/{year}/{month}?company_id=...` | agent de l'entreprise/admin | Crée ou retourne la facture mensuelle PDF. |

En prépayé, la création d'un ticket consomme un crédit ticket disponible; à défaut, un crédit de mois non utilisé couvre le mois courant. En l'absence de l'un ou l'autre, la création est refusée avec HTTP `402`. Les rapports calculent les unités de dépassement de contournement et les constats client sur les tickets créés pendant la période demandée. La génération de rapports utilise une tâche de fond FastAPI et l'état ainsi que les PDF sont persistés en base.

Les factures PDF sont des documents de relevé à compléter par les informations légales et fiscales de l'émetteur avant émission officielle. Le tarif est conservé en centimes, la taxe en points de base et la devise en code ISO 4217.

## Comptes internes

| Méthode | Route | Accès |
|---|---|---|
| `GET` | `/users` | admin : tous les comptes; agent : clients de ses entreprises et agents ayant au moins une entreprise en commun |
| `POST` | `/users` | admin; crée un agent ou un admin |
| `PATCH` | `/users/{user_id}` | admin : modifie identité, rôle, activation, état, entreprise(s) et consentements IA; agent : modifie identité et activation uniquement dans son périmètre |
| `PUT` | `/users/{user_id}/password` | admin : tout compte non système; agent : clients ou autres agents de son périmètre |

Le mot de passe est fourni comme `{"temporary_password":"..."}` (10 caractères minimum), stocké sous forme de hachage et jamais retourné par l'API; la réinitialisation invalide les sessions en cours du compte. Les opérations de modification et de réinitialisation sont inscrites dans `user_admin_audit` sans enregistrer de mot de passe. Un agent ne peut pas modifier son propre compte via cette console, gérer les admins, ni agir sur des utilisateurs d’une entreprise non affectée. Le dernier admin actif ne peut pas être désactivé ni rétrogradé.

## Enrichissement par la KB constructeur

Les jobs retournés par `GET /admin/kb` et `GET /admin/kb/debug` incluent `ticket_reference`, `ticket_title` et `results` (une entrée par résultat : `source` `kb`/`document`, `status` `new`/`known`/`duplicate`/`filtered`/`empty`/`failed`, `title`, `url`, `detail`). Routes `/admin/kb`, `/admin/kb/settings`, `/admin/kb/session/check`, `/admin/kb/debug`, `/admin/kb/reindex` (admin) et `POST /tickets/{id}/kb-enrich` (agent/admin) : voir `docs/kb-scraper.md`. `GET /ai/options` retourne `kb_enabled` aux agents et admins.

## Routage des modèles IA

L'administration du routage est réservée aux admins. La configuration est stockée en base de données et appliquée aux nouveaux appels sans redémarrage de l'API.

| Méthode | Route | Accès | Résultat |
|---|---|---|---|
| `GET` | `/admin/ai-routing` | admin | Configuration courante, tâches, rôles et état/catalogue des fournisseurs. |
| `PUT` | `/admin/ai-routing` | admin | Enregistre l’interrupteur général et les affectations modèle/priorité par tâche et rôle. |
| `DELETE` | `/admin/ai-routing` | admin | Supprime la configuration enregistrée; les valeurs par défaut de l’environnement (`OLLAMA_MODEL`, `OLLAMA_THINK`, `OLLAMA_TIMEOUT_SECONDS`) s’appliquent de nouveau. Retourne la configuration résultante. |
| `GET` | `/ai/options` | connecté | Modèles que l’utilisateur peut choisir (`models` : `provider`, `model`, `supports_think`, `think` par défaut) et modèle par défaut (`default`) de son rôle, d’après les affectations de l’admin. |
| `PUT` | `/tickets/{id}/ai-preferences` | client de l’entreprise | Choisit le modèle (et `think`) du diagnostic du ticket : `{"provider","model","think"}`. `think` omis = valeur de l’admin. `422` si le modèle n’est pas affecté aux clients. |
| `GET` | `/admin/ai/diagnostics` | admin | État/catalogue des fournisseurs, règles de routage, derniers échecs, instance Ollama d'embeddings (`embedding` : modèle actif, dimension, collection, modèles installés) et état de Qdrant (`rag`); `reference` ajoute l’historique de diagnostic d’un ticket sans exposer prompts ni réponses. |

Les tâches sont `customer_diagnosis`, `agent_assistance`, `handoff_summary` et `ticket_analysis`; les rôles configurables sont `customer` et `staff` (agent/admin). Chaque règle activée nécessite au moins un modèle. Un maximum de quatre couples fournisseur/modèle peut être ordonné par règle; en cas d’échec, le serveur tente le suivant. Le service accepte les fournisseurs `ollama` et `anthropic`; les modèles Ollama personnalisés doivent déjà être installés sur Ollama, et Anthropic nécessite une clé API configurée. Le bouton général met en pause toute génération IA sans effacer les affectations; au moins une règle avec un modèle doit rester configurée pour enregistrer.

Chaque affectation accepte deux options facultatives :

```json
{ "provider": "ollama", "model": "deepseek-r1:1.5b", "think": false, "timeout_seconds": 120 }
```

- `think` (défaut `false`, Ollama uniquement) : active le raisonnement interne des modèles « thinking » (qwen3, deepseek-r1…). Sur CPU, il peut multiplier le temps de réponse par 10 ou plus (plus de 10 minutes mesurées pour un ticket avec qwen3:4b).
- `timeout_seconds` (défaut `120`, de `10` à `1800`) : délai maximum de l'appel au fournisseur avant de passer au modèle suivant.

Les configurations enregistrées avant l'introduction de ces options reçoivent automatiquement les valeurs par défaut.

### Choix du modèle par les utilisateurs

Le premier modèle de chaque règle est le modèle par défaut; l’utilisateur ne peut choisir que parmi les modèles que l’admin a affectés à son rôle (`customer` ou `staff`, toutes tâches actives confondues), jamais un identifiant libre. Le modèle choisi passe en tête de la priorité et les autres restent en secours; le délai maximal reste celui de l’admin. `think` n’est pris en compte que pour Ollama.

- **Client** : `POST /tickets` accepte `ai_model` (`{"provider","model","think"}`) et `PUT /tickets/{id}/ai-preferences` le modifie; le choix est conservé dans `ai_preferences` et utilisé pour l’analyse initiale et le diagnostic (tâches `ticket_analysis` et `customer_diagnosis`). Le résumé de transfert interne reste sur le routage de l’admin.
- **Agent/admin** : `POST /tickets/{id}/ai-assist` accepte `ai_model` et `kind`. `kind: "question"` (défaut) exige `question`; `kind: "summary"` génère le résumé du ticket (tâche `handoff_summary`, rôle `staff`) sans question.
Les échecs individuels des fournisseurs sont journalisés comme événements techniques du ticket (tâche, fournisseur, modèle, durée et message d’erreur), y compris si un modèle de secours répond ensuite. Ces événements ne conservent pas le prompt ou la réponse.

Le RAG documentaire et ses embeddings Ollama ne sont pas désactivés par cet interrupteur et ne font pas partie du routage des LLM de génération.

## Consentement, traces et coûts IA

Les comptes client disposent de deux consentements distincts, activés par défaut : le traitement du contenu des tickets par un LLM et la conservation des prompts/réponses pour l’évaluation. Le client peut les modifier depuis **Confidentialité IA**.

| Méthode | Route | Accès | Résultat |
|---|---|---|---|
| `PUT` | `/auth/ai-consent` | client | Met à jour les deux consentements. Le retrait de l’accord de conservation efface immédiatement prompts/réponses déjà stockés. |
| `GET` | `/admin/ai/catalog` | admin | Référentiel des clients, utilisateurs support et tickets disponible pour les filtres. |
| `GET` | `/admin/ai/traces` | admin | Liste paginée des appels consentis avec prompts/réponses, tokens, durée, fournisseur, ticket et acteurs. Filtres : `ticket_id`, `customer_id`, `actor_id`, `provider`, `model`, `task`, `start`, `end`; `limit` (max 200) et `offset`. |
| `PATCH` | `/admin/ai/traces/{execution_id}` | admin | Enregistre une note d’évaluation de 1 à 5 et un commentaire. |
| `GET` | `/admin/ai/costs` | admin | Détail paginé et coût estimé, filtrable par ticket, client, utilisateur, fournisseur, modèle, tâche et période. |
| `GET` / `PUT` | `/admin/ai/pricing` | admin | Lit ou met à jour les tarifs en USD par million de tokens entrée/sortie; la lecture retourne aussi `available_models` (modèles de génération détectés par fournisseur) pour les listes déroulantes. |

Le refus du traitement IA empêche les nouveaux appels sur le ticket; l’assistance humaine reste possible. Le refus de conservation des traces n’empêche pas le traitement IA si son consentement séparé reste activé : seuls les métadonnées, tokens et coûts restent enregistrés, sans contenu conversationnel. Le contenu de trace est automatiquement purgé après 30 jours. Les tarifs sont des estimations recalculées avec les prix administrés; les modèles Anthropic sans tarif configuré sont indiqués comme non tarifés. Ollama local est estimé à 0 USD de coût fournisseur.

## Administration RAG

Toutes les routes de gestion du corpus sont réservées aux administrateurs. Elles utilisent le dossier documentaire configuré par `RAG_DOCUMENTS_DIR` et la collection indiquée par `RAG_COLLECTION`.

| Méthode | Route | Accès | Résultat |
|---|---|---|---|
| `GET` | `/admin/rag` | admin | État Qdrant et embeddings, métriques du corpus, documents, sources supprimées, limites d'upload, état de la dernière synchronisation et `embedding_options` (modèles proposés : `installed`, `selected`, `dimension`). |
| `PUT` | `/admin/rag/embedding-model` | admin | Choisit le modèle d'embedding (`{"model":"bge-m3"}`, parmi `qwen3-embedding:0.6b`, `bge-m3`, `qwen3-embedding:4b`) et lance la réindexation (`202`). `409` si le modèle n'est pas téléchargé ou si une indexation est en cours, `422` s'il n'est pas pris en charge. |
| `DELETE` | `/admin/rag/documents` | admin | Supprime des documents : corps `{"names":["guide.md", ...]}` (chemins relatifs du corpus). Retourne `deleted` et `index_updated` (`false` si Qdrant est indisponible : l'index sera nettoyé à la prochaine synchronisation). `404` (aucune suppression) si un nom est inconnu ou hors du corpus, `409` pendant une indexation. |
| `GET` | `/admin/rag/index` | admin | État détaillé et progression de l’indexation en cours (route légère pour le suivi périodique). |
| `POST` | `/admin/rag/documents` | admin | Téléverse des fichiers avec le champ multipart `files`; extensions `.pdf`, `.md`, `.markdown`, `.txt`. |
| `POST` | `/admin/rag/index` | admin | Lance une synchronisation incrémentale en tâche de fond et retourne `202 {"status":"pending"}`. |
| `POST` | `/admin/rag/search` | admin | Teste une requête sur l'index existant sans synchroniser ni modifier le corpus. |

Le téléversement du corpus accepte au maximum `RAG_MAX_FILES_PER_UPLOAD` fichiers (50 par défaut), chacun limité par `RAG_MAX_FILE_SIZE` (50 Mio par défaut); les pièces jointes de tickets restent limitées par `MAX_ATTACHMENTS_PER_UPLOAD` et `MAX_ATTACHMENT_SIZE`. Dépasser ces limites renvoie `413`; nginx doit aussi autoriser la taille totale (voir `operations.md`). Un nom déjà présent provoque `409`; un type non pris en charge provoque `415`. L'indexation met à jour les sources modifiées, ajoute les nouvelles et supprime les sources absentes du dossier. Son état et son résultat sont disponibles via `GET /admin/rag`; comme les autres tâches de fond FastAPI du projet, ce suivi en mémoire n'est pas durable et ne survit pas au redémarrage du processus.

Exemple de recherche de contrôle :

```json
{"query": "Comment rétablir les appels entrants ?"}
```

La réponse donne les extraits, leur source/page et leur score de similarité. Cette route recherche uniquement dans l'index courant; elle n'indexe pas les documents en attente. La synchronisation automatique lors d'une demande d'assistance reste active.

L'inscription publique ne peut pas définir un rôle interne ou créer directement un compte approuvé.

`GET /api/admin/ai/diagnostics` accepte `source` (`all`, `ticket`, `console`) et `user_id` (utilisateur de la console) et renvoie un bloc `console` (activité hors ticket); les échecs de la console ont `source: "console"`.

Les réponses JSON des modèles (diagnostic client, analyse) sont extraites même entourées d'un bloc ```json ou de texte; sinon l'erreur enregistrée cite le début de la réponse reçue.

`GET /api/admin/ai/ticket-timeline?reference=` (administrateur) renvoie le parcours d'un ticket : `participants`, `entries` (horodatées, avec `from`/`to`, durée, état, détails sans contenu), `runs`, `latencies` (temps perçu, appels réussis, échecs, recherche documentaire, part non tracée) et `tracing_complete`. Voir `ticket-diagnostic.md`.

## Clients indépendants

`POST /api/auth/register` accepte `account_type` : `company` (défaut, `company_code` obligatoire) ou `independent` (entreprise individuelle créée, compte en attente). `UserOut.is_individual` et `CompanyOut.is_individual` l'indiquent; `POST /api/companies/{id}/invitation-code` répond 409 pour une entreprise individuelle. Voir `independent-clients.md`.

## Console d'aide

Réservée aux rôles `admin`, `agent` et aux clients indépendants (périmètre = leur entreprise); chaque conversation n'est visible que de son propriétaire. Voir `help-console.md`.

| Route | Rôle |
| --- | --- |
| `GET /api/help/options` | Entreprises sélectionnables, modèles IA, disponibilité de la recherche live et du web. |
| `GET /api/help/conversations?q=&limit=&offset=` | Conversations du propriétaire, paginées (`total_count`). |
| `POST /api/help/conversations` | Crée une conversation : `company_id` ou `ticket_id` (impose l'entreprise du ticket), `use_kb_live`, `use_web`, `model`. 404 si l'entreprise/ticket n'est pas accessible, 409 si option indisponible ou consentement IA refusé. |
| `GET/PATCH/DELETE /api/help/conversations/{id}` | Détail avec messages; `PATCH` : `title`, `use_kb_live`, `use_web`, `model`, `refresh_ticket`. |
| `POST /api/help/conversations/{id}/messages` | Ajoute une question (202) et lance la réponse en tâche de fond; 409 si une réponse est déjà en attente. Les messages assistant ont `status` `pending`, `done` ou `error`, `sources` (`kind` : `doc`, `company`, `kb`, `web`). |

`POST /api/help/conversations/{id}/files` (multipart `files`, 201) ajoute des fichiers au contexte et `DELETE /api/help/conversations/{id}/files/{file_id}` en retire un; le détail d'une conversation liste `files` et chaque message porte `thinking`.

`ai_executions.ticket_id` est facultatif : les appels de la console y sont mesurés sans ticket.

## Modèle logique

- `companies` : entreprise cliente, empreinte du code d'accès et agents affectés.
- `users` : identité, rôle, entreprise, état actif, état d'approbation et hachage de mot de passe scrypt.
- `tickets` : identifiant, référence lisible, propriétaire, agent assigné, description, état, priorité, champs IA et métadonnées d'escalade.
- `sla_policies`, `company_credit_grants`, `company_credit_usages`, `report_jobs`, `company_invoices` : contrats SLA, crédits prépayés, travaux PDF et factures persistées.
- `messages` : contenu, auteur et visibilité `public`/`internal`.
- `ticket_events` : événements d'audit (création, analyse, statut, affectation, messages, escalade).

Les références externes sont suivies en texte libre : aucune création ou synchronisation automatique dans un outil d'escalade n'est effectuée.
