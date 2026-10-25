"""Archive locale des éléments KB : fichiers originaux + texte extrait, hors des volumes Docker.

Le dossier (`kb_archive/` à la racine du projet, monté dans l'API) survit à `docker compose down -v` :
après une reconstruction complète, la base est restaurée depuis l'archive sans retourner sur le
portail (donc sans nouveau MFA) puis les vecteurs sont recalculés par la réindexation habituelle.

Disposition :
    kb_archive/
      items/<clé>.json          métadonnées + texte extrait (source de la restauration)
      texts/<titre>__<clé>.txt  même texte, lisible directement
      files/<sha256>.<ext>      fichier original téléchargé, dédupliqué par empreinte
La clé est dérivée de l'URL : un même élément n'est jamais archivé deux fois.
"""

import hashlib
import json
import logging
import re
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KBArticle

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
README = (
    "Archive locale des éléments récupérés dans la base de connaissances constructeur.\n"
    "- items/  : un fichier JSON par élément (URL, titre, texte extrait); sert à restaurer la base.\n"
    "- texts/  : le même texte, lisible directement.\n"
    "- files/  : fichiers originaux téléchargés (PowerPoint, PDF, ZIP…), nommés par leur empreinte SHA-256.\n"
    "Ce dossier est ignoré par Git : ne le publiez pas, ces documents proviennent d'un portail à accès restreint.\n"
)


class KBArchive:
    def __init__(self, root: Path, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = max_bytes

    @classmethod
    def from_settings(cls) -> "KBArchive | None":
        """Archive configurée, ou None si désactivée (KB_ARCHIVE_ENABLED=false)."""
        if not settings.kb_archive_enabled:
            return None
        return cls(Path(settings.kb_archive_dir), settings.kb_archive_max_mb * 1024 * 1024)

    # ------------------------------------------------------------------ écriture
    def save_item(
        self,
        *,
        url: str,
        title: str,
        source: str,
        content: str,
        content_hash: str,
        file_name: str = "",
        file_bytes: bytes | None = None,
        first_seen_at: datetime | None = None,
    ) -> dict:
        """Archive un élément (texte toujours, fichier original si le plafond le permet).

        Retourne {"key", "file": "stored" | "reused" | "skipped_cap" | "none"}. Ne lève pas pour un plafond atteint.
        """
        self._ensure_layout()
        key = item_key(url)
        file_info, file_status = None, "none"
        if file_bytes:
            file_info, file_status = self._store_file(file_name, file_bytes)
        record = {
            "version": SCHEMA_VERSION,
            "url": url,
            "title": title,
            "source": source,
            "content_hash": content_hash,
            "content": content,
            "file": file_info,
            "first_seen_at": (first_seen_at or datetime.now(timezone.utc)).isoformat(),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_atomic(self.root / "items" / f"{key}.json", json.dumps(record, ensure_ascii=False, indent=2))
        self._write_text_copy(key, title, url, content)
        return {"key": key, "file": file_status}

    def _store_file(self, file_name: str, data: bytes) -> tuple[dict | None, str]:
        digest = hashlib.sha256(data).hexdigest()
        stored_name = f"{digest}{safe_suffix(file_name)}"
        target = self.root / "files" / stored_name
        info = {"name": Path(file_name).name[:200] or stored_name, "sha256": digest, "size": len(data), "stored_as": stored_name}
        if target.exists():
            return info, "reused"  # même contenu déjà archivé : aucune répétition
        if self.max_bytes and self.size_bytes() + len(data) > self.max_bytes:
            logger.warning("KB archive full (%s MB): original file %s not kept", self.max_bytes // 1048576, file_name)
            return None, "skipped_cap"
        self._write_atomic(target, data)
        return info, "stored"

    def _write_text_copy(self, key: str, title: str, url: str, content: str) -> None:
        directory = self.root / "texts"
        for stale in directory.glob(f"*__{key}.txt"):  # le titre a pu changer : un seul exemplaire par élément
            stale.unlink(missing_ok=True)
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:60] or "document"
        self._write_atomic(directory / f"{slug}__{key}.txt", f"{title}\n{url}\n\n{content}\n")

    def _ensure_layout(self) -> None:
        for name in ("items", "texts", "files"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        readme = self.root / "README.txt"
        if not readme.exists():
            readme.write_text(README, encoding="utf-8")

    @staticmethod
    def _write_atomic(target: Path, payload: str | bytes) -> None:
        """Écriture atomique : un arrêt brutal ne laisse jamais un fichier tronqué."""
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False, suffix=".tmp") as handle:
            handle.write(payload.encode("utf-8") if isinstance(payload, str) else payload)
            temporary = Path(handle.name)
        temporary.replace(target)

    # ------------------------------------------------------------------ lecture
    def iter_items(self) -> Iterator[dict]:
        """Éléments archivés valides; un fichier illisible ou d'un format inconnu est ignoré, pas fatal."""
        directory = self.root / "items"
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or not record.get("url") or "content" not in record:
                    raise ValueError("enregistrement incomplet")
                yield record
            except (OSError, ValueError) as exc:
                logger.warning("KB archive item ignored (%s): %s", path.name, exc)

    def size_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file()) if self.root.is_dir() else 0

    def stats(self, detailed: bool = False) -> dict:
        """Chiffres de l'archive. La version légère compte les fichiers sans les lire (appelée à chaque rafraîchissement)."""
        items_dir, files_dir = self.root / "items", self.root / "files"
        stats = {
            "items": len(list(items_dir.glob("*.json"))) if items_dir.is_dir() else 0,
            "files": len(list(files_dir.glob("*"))) if files_dir.is_dir() else 0,
            "size_bytes": self.size_bytes(),
            "max_bytes": self.max_bytes,
        }
        if detailed:
            records = list(self.iter_items())
            stats["items"] = len(records)
            stats["by_source"] = {source: sum(1 for item in records if item.get("source") == source) for source in ("kb", "document")}
        return stats

    # ------------------------------------------------------------------ synchronisation avec la base
    def restore(self, db: Session) -> int:
        """Recrée en base les éléments archivés qui y manquent (sans toucher aux existants). Retourne le nombre ajouté."""
        known = set(db.scalars(select(KBArticle.url)).all())
        restored = 0
        for record in self.iter_items():
            if record["url"] in known:
                continue
            db.add(KBArticle(
                url=record["url"][:1000],
                title=str(record.get("title", ""))[:500],
                content=str(record["content"]),
                content_hash=str(record.get("content_hash") or content_hash_of(str(record["content"]))),
                source=record.get("source") if record.get("source") in {"kb", "document"} else "kb",
                chunk_count=0,
                indexed_model="",  # vecteurs à recalculer : repris par la réindexation
                first_seen_at=_parse_date(record.get("first_seen_at")),
            ))
            known.add(record["url"])
            restored += 1
        if restored:
            db.commit()
        return restored

    def export_all(self, db: Session) -> int:
        """Archive (texte seulement) les éléments déjà en base mais absents de l'archive. Retourne le nombre ajouté."""
        archived = {item_key(record["url"]) for record in self.iter_items()}
        exported = 0
        for article in db.scalars(select(KBArticle)).all():
            if article.content and item_key(article.url) not in archived:
                self.save_item(
                    url=article.url, title=article.title, source=article.source or "kb", content=article.content,
                    content_hash=article.content_hash, first_seen_at=article.first_seen_at,
                )
                exported += 1
        return exported


def item_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


def content_hash_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_suffix(file_name: str) -> str:
    """Extension assainie (jamais de chemin ni de caractères spéciaux issus du nom fourni par le portail)."""
    suffix = Path(file_name).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ""


def _parse_date(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def restore_on_startup(session_factory) -> int:
    """Au démarrage de l'API : restaure ce qui manque en base et planifie la réindexation. Ne lève jamais."""
    try:
        archive = KBArchive.from_settings()
        if archive is None or not archive.root.is_dir():
            return 0
        with session_factory() as db:
            restored = archive.restore(db)
        if restored:
            from app.kb.jobs import enqueue_reindex

            logger.warning("KB archive: %s élément(s) restauré(s) en base; réindexation planifiée", restored)
            enqueue_reindex()
        return restored
    except Exception:
        logger.exception("Could not restore the KB archive")
        return 0
