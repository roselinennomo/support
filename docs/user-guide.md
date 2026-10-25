# Guide utilisateur

## Accès au portail

Ouvrir l'adresse fournie par l'administrateur. Les comptes agent et administrateur sont créés par un administrateur. Les clients ne peuvent plus ouvrir un compte actif seuls : l'accès est lié à une entreprise cliente et doit être validé par l'équipe support.

Les mots de passe doivent comporter au moins 10 caractères. Après connexion, l'écran s'adapte au rôle. Le bouton de déconnexion est accessible en bas du menu latéral.


### Menu de navigation

Le menu de gauche est organisé en groupes selon votre profil : **Support** (file ou demandes, console d'aide), **Entreprise** (entreprises, utilisateurs, demandes d'accès, documents), **Service & facturation**, **Connaissances** (corpus RAG, KB constructeur), **Intelligence artificielle** (configuration, évaluation, coûts, diagnostic), **Système** (supervision) et **Mon compte**. Les groupes sans entrée pour votre profil sont masqués. Le bouton « Réduire » en haut du menu ne garde que les icônes (survolez-les pour le nom, un badge indique les demandes d'accès en attente); votre choix est mémorisé.
## Client

### Demander un accès client

1. Demander à votre contact support le code d’accès de votre entreprise. Le support doit d’abord créer l’entreprise dans le portail et affecter un ou plusieurs agents.
2. Depuis l’écran de connexion, cliquer sur **Demander un accès** et renseigner le code, votre nom, votre adresse e-mail et un mot de passe.
3. Votre demande apparaît dans la file de validation des agents affectés à l’entreprise.
4. Après vérification, un agent ou un administrateur approuve ou refuse la demande. En cas d’approbation, connectez-vous avec l’adresse e-mail et le mot de passe choisis.

Une fois le compte approuvé, vous voyez tous les incidents de votre entreprise, y compris ceux créés par vos collègues, et vous pouvez y répondre ou ajouter des pièces jointes. Les clients des autres entreprises n'y ont jamais accès.

Les listes longues (incidents, utilisateurs, entreprises, demandes d'accès, documents du corpus, recherches KB, coûts et traces IA, factures) sont paginées : la barre sous la liste indique la plage affichée, permet d'aller à la première, précédente, suivante ou dernière page, de choisir une page numérotée et de régler le nombre d'éléments par page; ce réglage est mémorisé par liste dans le navigateur. Un changement de filtre ou de recherche ramène à la première page.

Pendant le diagnostic guidé, vous pouvez choisir le **modèle IA** et activer ou non le **raisonnement** (réponse plus approfondie mais plus lente) depuis le formulaire de création, puis depuis le panneau de diagnostic tant que l’assistant ne travaille pas. Seuls les modèles proposés par l’administrateur apparaissent; si aucun choix n’est affiché, le modèle par défaut est utilisé.

Un compte en attente ou refusé ne peut pas se connecter ni créer d’incident. Si la demande tarde, contactez le support en indiquant l’entreprise et l’adresse e-mail utilisées.

### Créer une demande de support

1. Après validation du compte, cliquer sur **Nouvel incident**.
2. Saisir un objet clair, le produit Alcatel-Lucent concerné et si possible sa version logicielle.
3. Décrire les symptômes, leur date d'apparition, le périmètre (sites, postes, appels), les messages d'erreur et les vérifications déjà faites. Ne pas saisir de mot de passe ou de données personnelles qui ne sont pas nécessaires.
4. Envoyer la demande. Le ticket est enregistré immédiatement et l'assistant démarre un diagnostic conversationnel.

### Diagnostic guidé et pièces jointes

Dans **Mes demandes**, ouvrir le ticket pour échanger avec l'assistant. Il peut demander une précision, proposer une vérification de premier niveau ou demander un journal/capture. Répondre dans la conversation et joindre les éléments demandés (5 fichiers maximum par envoi, 10 Mo par fichier; extensions prises en charge : journaux, texte, PDF, images, PCAP et ZIP). Ne joignez pas de mots de passe, secrets ni configurations contenant des identifiants.

Le diagnostic s'appuie sur les documents techniques présents dans le corpus du support et peut citer un fichier et une page. Si les documents ne couvrent pas le problème, l'assistant doit le signaler plutôt que présenter une hypothèse comme un fait. Le modèle voit les noms des pièces jointes, mais pas encore leur contenu. Les fichiers sont conservés avec le ticket et pourront être téléchargés par l'équipe support. Si l'assistant recommande une expertise, le ticket reste en diagnostic et vous pouvez choisir de le transmettre à un agent après lui avoir répondu une première fois. Seule une panne du diagnostic entraîne un transfert automatique. Le transfert conserve la description, les messages et les pièces jointes.

### Suivre et échanger

La page **Mes demandes** présente les demandes et leur statut :

- **Diagnostic IA** : l'assistant échange avec vous; la demande n'est pas encore dans la file des agents.
- **Nouveau** : reçu par l'équipe.
- **En cours** : pris en charge.
- **En attente client** : une information ou action est attendue de votre part.
- **Escaladé** : statut hérité d'anciennes versions (escalade manuelle supprimée), conservé pour les tickets existants, qui peuvent en sortir normalement.
- **Résolu** : l'agent (ou l'assistant IA) a proposé une résolution documentée. Vérifiez la note; répondez pour rouvrir ou confirmez pour clôturer.
- **Clôturé** : vous avez confirmé que le traitement est terminé. Un ticket clôturé ne peut pas être rouvert.
- **Rejeté** : l'équipe ne peut pas prendre en charge la demande; le motif est communiqué. Créez une nouvelle demande si nécessaire.

Ouvrir une demande pour consulter la description, les recommandations affichées, la conversation, les pièces jointes et la chronologie, y compris pendant une escalade. Les messages envoyés depuis cette page sont visibles par vous et l'équipe. Les notes internes ne sont pas visibles par les clients.

## Agent support

La **File de support** liste les tickets accessibles à l'équipe. Utiliser la recherche ou le filtre d'état pour retrouver un dossier, ouvrir l'incident et examiner ses informations et son historique.

Dans une fiche, l'agent peut :

- faire progresser le statut selon les transitions autorisées;
- passer le ticket à **Résolu** avec une note expliquant le diagnostic et la solution (visible par le client); seul le client confirme ensuite la clôture;
- rejeter une demande hors périmètre ou invalide avec un motif obligatoire et visible par le client; un ticket rejeté est terminal;
- s'assigner l'incident ou l'attribuer à un agent;
- répondre dans la conversation (message visible au client);
- consulter et télécharger les pièces jointes reçues;
- activer **Note interne** avant l'envoi pour ajouter un échange réservé à l'équipe;
- demander une aide IA privée à partir de tout l'historique du ticket et du corpus documentaire; la réponse est enregistrée comme note interne et peut citer les références utilisées. Le bouton **Résumer le ticket** produit un résumé de l’état et des prochaines vérifications. Dans les deux cas, vous choisissez le modèle IA (et le raisonnement) parmi ceux que l’administrateur a affectés à l’équipe support;
- assigner le ticket (bloc « Prise en charge ») : un agent peut se l'assigner ou l'assigner à un collègue affecté à la même entreprise; seul un administrateur peut l'assigner à un administrateur (qui le prend à titre exceptionnel). Un client n'est jamais assignable;
- consulter le diagnostic IA, sans le considérer comme une preuve ou une procédure validée;
- recevoir automatiquement, lors d'un transfert client après au moins un échange avec l'IA, une note interne résumant l'historique et proposant les premières recommandations; en cas d'échec IA, une note interne invite à consulter l'historique;
Il n'y a pas d'escalade manuelle entre agents : l'escalade est le transfert de l'IA vers le support (voir ci-dessus). Les notes internes ne doivent pas contenir d'informations non pertinentes ou de secrets.

Les pièces jointes sont actuellement fournies au modèle par leur nom uniquement, pas par leur contenu. Téléchargez et examinez les fichiers vous-même avant de demander une analyse IA complémentaire.

### Client indépendant

Sans entreprise partagée, cochez « Je suis un client indépendant » à l'inscription : un espace individuel est créé, activé après validation de l'administrateur, avec sa propre facturation et l'accès à la Console d'aide (voir `independent-clients.md`).

### Console d'aide

Le menu « Console d'aide » ouvre un assistant technique indépendant des tickets (agents et administrateurs). Cliquez sur « Nouvelle conversation », puis choisissez :

- un **ticket** (facultatif) : son contexte est copié dans la conversation, sans le modifier; « Actualiser le ticket » recopie son état courant;
- une **entreprise** (facultatif, une seule par conversation) : ses documents privés s'ajoutent à la documentation Alcatel;
- les sources complémentaires : **recherche en direct sur le portail constructeur** et **recherche web** (seule votre question, sans e-mail ni adresse IP, est envoyée);
- le **modèle IA** et le raisonnement.

Chaque réponse cite ses sources. Vos conversations sont personnelles, conservées dans l'historique (recherche, pagination, suppression). Détails : `help-console.md`.

## Administrateur

L'administrateur dispose des fonctionnalités agent, d'une page **Équipe** et d'une page **Supervision** :

1. Saisir le nom, l'adresse e-mail, un mot de passe temporaire (10 caractères minimum) et le rôle.
2. Créer un compte **Agent support** ou **Administrateur**.
3. Consulter la console de supervision pour vérifier l'état de la base, de l'API, d'Ollama (génération), d'Ollama (embeddings), de Qdrant (RAG documentaire) et de la file de support.
4. Communiquer le compte par un canal approprié et demander au collaborateur de changer son mot de passe hors bande lorsque la politique interne le permet.

Le formulaire gère la création des comptes uniquement; désactivation, réinitialisation de mot de passe et gestion de sessions ne sont pas encore incluses dans cette version.

### Console de supervision

La page **Supervision** affiche :

- les incidents ouverts et escaladés;
- le nombre de comptes actifs et d'utilisateurs de la plateforme;
- l'état de la base de données, de l'API, du service Ollama de génération, de l'instance Ollama d'embeddings (modèle actif et téléchargement), de Qdrant (collection du modèle actif) et de la file de traitement. Le **Diagnostic IA** présente les mêmes éléments d'embeddings et de Qdrant, et **Coûts IA** propose les modèles Ollama détectés dans une liste déroulante pour saisir les tarifs.

Cette vue aide l'administration à repérer rapidement un dysfonctionnement ou une saturation du support sans quitter le portail.

### Entreprises et validation des accès

Les agents affectés et les administrateurs peuvent créer une entreprise dans **Entreprises**, puis sélectionner les agents qui en assureront le suivi. Le portail génère un code d’accès; copiez-le au moment de la création et transmettez-le au contact client par un canal sécurisé. Le code n’est stocké qu’en empreinte et ne peut pas être consulté à nouveau. Si le code est perdu ou doit être révoqué, générez-en un nouveau depuis la fiche de l’entreprise; l’ancien cesse alors de fonctionner.

Dans **Demandes d’accès**, les agents ne voient que les demandes concernant les entreprises qui leur sont affectées; les administrateurs voient toutes les demandes. Vérifiez que le demandeur appartient bien à l’entreprise avant d’approuver. Une approbation active immédiatement le compte; un refus bloque la connexion. Les agents ne voient et ne traitent que les tickets des entreprises auxquelles ils sont affectés.

Après la mise à jour depuis une ancienne version, les comptes clients qui n’avaient pas d’entreprise sont affichés sans entreprise dans la file d’administration. L’admin doit les rattacher à la bonne entreprise, puis les approuver; leurs tickets historiques restent conservés.
