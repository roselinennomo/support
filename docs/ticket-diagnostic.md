# Diagnostic ticket (parcours de bout en bout)

Page **Diagnostic ticket** (menu Intelligence artificielle, administrateur) : reconstitue, instant par instant, ce qui s'est passé pour un ticket, et explique l'écart entre le **temps perçu** par l'utilisateur et les durées d'appel aux modèles affichées dans les traces et les coûts.

## Pourquoi les durées ne correspondent pas

« Évaluation LLM » et « Coûts IA » ne comptent que l'appel **réussi** d'un modèle. Le temps perçu comprend aussi :

- les **tentatives en échec** avant la réussite (par exemple un modèle qui dépasse son délai ou renvoie un JSON invalide, puis le suivant dans la priorité);
- la **recherche documentaire** (synchronisation de l'index, embeddings, Qdrant, documents d'entreprise, KB);
- le chargement du contexte, la file d'attente et l'enregistrement de la réponse.

## Contenu de la page

- **Synthèse** par réponse IA : temps perçu (du message déclencheur — création, message du client, transfert — à la réponse publiée), appels réussis, tentatives en échec (avec leurs modèles), recherche documentaire, part non tracée, barre de répartition et phrase d'explication.
- **Diagramme de séquence** entre Client/Agent, API, Tâche IA, RAG (embeddings, Qdrant), Fournisseur LLM, KB constructeur et Base de données (flèches rouges pour les échecs, décalage depuis la création, durées).
- **Chronologie** (cascade horizontale) avec une **échelle compressée** par défaut : les longues périodes sans aucune activité (plus de 15 s ou 3 % de la durée totale) sont raccourcies et repérées par une zone hachurée et le libellé « ⋯ durée »; les durées affichées à droite restent réelles. La bascule « Échelle réelle » rétablit les proportions exactes (choix mémorisé) et **tableau** paginé filtrable par type, avec les détails (modèle, délai maximal, raisonnement activé, tokens, nombre de résultats, erreur).
- Sources assemblées : messages, événements, étapes IA (`ai_steps`), recherches KB (`kb_jobs`). Aucun contenu de message, de prompt ni de réponse n'est affiché : durées, états et compteurs seulement.

## Traçage

Chaque traitement de fond (diagnostic client, aide IA agent, résumé de passation) enregistre ses étapes dans `ai_steps` :

| Étape | Contenu |
| --- | --- |
| `ctx.load` | chargement du ticket et de la conversation en base |
| `rag.sync` | synchronisation de l'index documentaire avant la recherche |
| `rag.embedding` | **chaque** calcul d'embedding (nombre de textes, caractères, modèle, instance Ollama) : une par recherche (corpus, documents d'entreprise, KB) |
| `rag.qdrant`, `rag.qdrant.company`, `rag.qdrant.kb` | interrogation de chaque collection (résultats trouvés) |
| `rag.search`, `rag.company`, `rag.kb` | recherche complète par source (corpus, entreprise, KB) |
| `fournisseur/modèle` | chaque tentative (délai maximal, raisonnement, tokens, taille de réponse, erreur) |
| `db.save_reply` | enregistrement de la réponse |

Conservation : 30 jours, comme les traces. Les intervalles sans étape propre sont déduits et affichés en **Attente** : file d'attente avant le démarrage de la tâche, temps du traitement non détaillé (construction du prompt, validation) et, pour un ticket non tracé, « préparation avant l'appel au modèle ». Cliquer sur une ligne du diagramme ou de la chronologie affiche tous ses détails.

Les tickets **antérieurs** au traçage n'ont pas ces étapes : la page les reconstitue à partir des appels réussis et des échecs consignés (durées estimées) et le signale; la recherche documentaire apparaît alors dans la part « non tracée ».

API : `GET /api/admin/ai/ticket-timeline?reference=OXE-…` (administrateur).
