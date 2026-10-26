from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "development-only-change-me"
    database_url: str = "sqlite:///./support.db"
    initial_admin_email: str = "admin@support.local"
    initial_admin_password: str = "ChangeMe123!"
    ai_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "deepseek-r1:1.5b"
    # Valeurs appliquées à la configuration IA créée par défaut (modifiables ensuite par l'admin).
    # Modèles supplémentaires (séparés par des espaces) affectés par défaut à chaque règle, après le modèle principal.
    ollama_extra_models: str = "qwen3:4b"
    ollama_think: bool = True
    ollama_timeout_seconds: int = 300
    ollama_embedding_model: str = "qwen3-embedding:0.6b"
    # Instance Ollama dédiée aux embeddings (vide : même instance que le chat).
    ollama_embedding_base_url: str = ""
    ollama_keep_alive: str = "30m"
    ollama_num_ctx: int = 8192
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    # Plafond de longueur d'une réponse (le coût ne dépend que des jetons réellement produits).
    anthropic_max_tokens: int = 4096
    rag_documents_dir: str = "../support_docs"
    rag_qdrant_url: str = "http://localhost:6333"
    rag_collection: str = "support_documents"
    rag_embedding_dimension: int = 1024
    # Limites d'envoi du corpus documentaire (distinctes de celles des pièces jointes de tickets).
    rag_max_file_size: int = 52_428_800
    rag_max_files_per_upload: int = 50
    rag_top_k: int = 5
    # Documents propres à chaque entreprise : index privé, fichiers originaux hors des volumes Docker.
    company_collection: str = "support_company"
    company_docs_dir: str = "../company_docs"
    company_docs_max_file_mb: int = 10
    company_docs_max_files_per_upload: int = 5
    company_docs_max_mb_per_company: int = 500
    rag_score_threshold: float = 0.35
    # Console d'aide : recherche web libre via une instance SearXNG auto-hébergée (vide : désactivée).
    web_search_url: str = ""
    web_search_max_results: int = 5
    help_history_messages: int = 12
    # --- Enrichissement par la KB externe (navigateur conteneurisé), désactivé par défaut ---
    enable_kb_browser_scraper: bool = False
    kb_browser_url: str = "http://kb-browser:8090"
    kb_browser_token: str = ""
    kb_login_url: str = ""  # URL noVNC affichée à l'admin pour se reconnecter (MFA)
    kb_collection: str = "support_kb"
    kb_max_articles: int = 3
    # Source « documents » (Business Documents : guides, présentations, packs) : désactivée par défaut.
    kb_docs_enabled: bool = False
    kb_docs_max_results: int = 2
    # Types écartés (texte de la ligne, séparés par des virgules) : documents commerciaux sans intérêt pour le support.
    # Archive locale (fichiers originaux + texte extrait) : dossier monté hors des volumes Docker.
    kb_archive_enabled: bool = True
    kb_archive_dir: str = "../kb_archive"
    kb_archive_max_mb: int = 2048
    kb_docs_exclude_types: str = "Price catalog,Pricing,Offer/Proposal,RFP,Quoter,Commercial"
    kb_browser_timeout_seconds: int = 90
    kb_refetch_after_hours: int = 168
    kb_queue_size: int = 50
    cors_origins: str = "http://localhost:8080,http://localhost:5173"
    token_expire_minutes: int = 720
    upload_dir: str = "./uploads"
    max_attachment_size: int = 10_485_760
    max_attachments_per_upload: int = 5


settings = Settings()
