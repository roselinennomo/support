from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str = "development-only-change-me"
    database_url: str = "sqlite:///./support.db"
    initial_admin_email: str = "admin@support.local"
    initial_admin_password: str = "ChangeMe123!"
    ai_provider: str = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b"
    ollama_embedding_model: str = "nomic-embed-text"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    rag_documents_dir: str = "../support_docs"
    rag_qdrant_url: str = "http://localhost:6333"
    rag_collection: str = "support_documents"
    rag_embedding_dimension: int = 768
    rag_top_k: int = 5
    rag_score_threshold: float = 0.35
    cors_origins: str = "http://localhost:8080,http://localhost:5173"
    token_expire_minutes: int = 720
    upload_dir: str = "./uploads"
    max_attachment_size: int = 10_485_760
    max_attachments_per_upload: int = 5


settings = Settings()
