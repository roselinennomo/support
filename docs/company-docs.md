# Documents propres à chaque entreprise

Chaque entreprise cliente peut déposer ses documents de contexte : architecture réseau, plan d'adressage, matrice de flux, schémas d'exploitation… L'assistant IA les utilise pour diagnostiquer ses incidents, **en complément** de la documentation commune (corpus Alcatel et KB constructeur), sans jamais les partager avec une autre entreprise.

## Qui peut faire quoi

| Rôle | Lister / télécharger | Ajouter / supprimer / réindexer |
|---|---|---|
| Utilisateur de l'entreprise (`customer`) | ses propres documents | oui |
| Administrateur | toute entreprise (`company_id` obligatoire) | oui |
| Agent support | entreprises qui lui sont affectées, **lecture seule** | **non** (403) |
| Autre entreprise | rien : `404`, sans révéler l'existence du document | non |

## Isolation

- Une **collection vectorielle par entreprise** : `support_company_<id entreprise>_<modèle d'embedding>`. La recherche ne reçoit qu'un identifiant d'entreprise et n'interroge que cette collection : aucun filtre oublié ne peut mélanger deux entreprises. Les extraits sont de plus revérifiés (`company_id` du point) et l'erreur est journalisée si un point étranger remonte.
- Ces collections sont **distinctes** du corpus documentaire (`support_documents_*`) et de la KB constructeur (`support_kb_*`).
- La portée (entreprise courante) est posée par le code appelant à partir du ticket ou de la conversation, jamais déduite du texte de la question. Sans entreprise, seuls les documents communs sont interrogés.
- Dans le prompt, les extraits sont étiquetés `[Source documentation de l'entreprise : fichier]` et placés **avant** les extraits communs (plus spécifiques).

## Dépôt

- Formats : PDF, Word (.docx), PowerPoint (.pptx), **Excel (.xlsx, lu ligne par ligne avec le nom de chaque feuille)**, texte (.txt, .md, .csv, .log, .conf, .cfg, .json, .yaml). Les images et PDF scannés n'ont pas de texte extractible : ils sont signalés « Aucun texte » et jamais indexés à vide (pas d'OCR).
- Limites (réglables) : `COMPANY_DOCS_MAX_FILE_MB` (10), `COMPANY_DOCS_MAX_FILES_PER_UPLOAD` (5), `COMPANY_DOCS_MAX_MB_PER_COMPANY` (500). Le proxy accepte 60 Mo par requête : relevez `client_max_body_size` si vous augmentez ces limites.
- **Pas de répétition** : un fichier identique (SHA-256) déjà présent dans l'entreprise est refusé avec le nom de l'original. Le même fichier peut en revanche exister dans deux entreprises différentes.
- Les fichiers **originaux** sont conservés dans `company_docs/<id entreprise>/<sha256>.<ext>` (dossier du projet, hors volumes Docker, ignoré par Git).
- L'indexation est asynchrone (état « Indexation… » puis « Indexé »). Elle reprend au démarrage de l'API et après un changement de modèle d'embedding.

## API

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/api/company-docs?company_id=` | Documents, quota, formats et droits (`can_edit`). `company_id` ignoré pour un client. |
| `POST` | `/api/company-docs` | Dépôt multipart (`files`, `company_id` pour un admin, `description`). Retourne `uploaded` et `skipped` avec la raison de chaque refus. |
| `DELETE` | `/api/company-docs/{id}` | Supprime le fichier, ses vecteurs et la ligne. |
| `GET` | `/api/company-docs/{id}/download` | Fichier original. |
| `POST` | `/api/company-docs/{id}/reindex` | Relance l'indexation d'un document en échec. |

## Sauvegarde

Les fichiers originaux survivent à `docker compose down -v` (dossier `company_docs/`), mais la liste des documents est en base : après une suppression complète, il faut redéposer les fichiers (la restauration automatique, comme pour `kb_archive/`, n'est pas encore faite pour cette fonction). Sauvegardez la base avec `pg_dump` pour conserver aussi cette liste.
