# Clients indépendants

Un client indépendant est un client **sans entreprise partagée** : il a son propre espace, ses propres documents et sa propre facturation.

## Modèle

- Pas de nouveau rôle : c'est un client (`customer`) rattaché à une **entreprise individuelle** (`companies.is_individual = true`) qui ne contient que lui. Tickets, SLA, rapports, factures, crédits et documents d'entreprise fonctionnent donc comme pour toute entreprise, avec une facturation propre (mode, tarifs, paiement) réglée par l'administrateur dans **Facturation**.
- Une entreprise individuelle n'a **pas de code d'invitation** (« Nouveau code » est refusé, 409) : personne ne peut la rejoindre.

## Inscription et validation

1. Sur l'écran « Demander un accès », cocher « Je suis un client indépendant ». Aucun code d'entreprise n'est demandé (`POST /api/auth/register` avec `account_type: "independent"`).
2. L'entreprise individuelle « *Nom* (indépendant) » est créée (suffixe numérique en cas d'homonyme). Le compte reste **en attente**.
3. L'administrateur voit la demande (badge « Indépendant ») dans « Demandes d'accès » et l'approuve; les agents ne la voient pas tant qu'ils ne sont pas affectés à cette entreprise.
4. **À faire par l'administrateur** : affecter au moins un agent support à l'entreprise individuelle (Utilisateurs → agent → entreprises affectées) pour que les tickets soient pris en charge, et régler sa facturation.

## Console d'aide

Les clients indépendants (et eux seuls parmi les clients) disposent de la **Console d'aide** (voir `help-console.md`), limitée à leur périmètre :

- l'entreprise est toujours la leur (les documents de « Documents entreprise » sont utilisés automatiquement); toute autre entreprise est refusée;
- le mode ticket n'accepte que leurs tickets et **ne copie jamais les notes internes** de l'équipe support;
- le modèle IA est choisi parmi ceux affectés au rôle **client** dans « Configuration IA » (tâche diagnostic client);
- recherche live du portail constructeur et recherche web (si configurée) disponibles comme pour l'équipe;
- les appels sont mesurés au nom du client (Coûts IA).
