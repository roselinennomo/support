"""Navigation Playwright déterministe sur le portail KB, avec profil Chromium persistant.

Le parcours est strict et prévisible (aucun LLM ici) : injection de la requête dans le champ de
recherche, extraction des N premiers liens, lecture du conteneur principal d'un article. Toute
redirection vers une page de connexion/MFA interrompt l'opération (`SessionExpired`).
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from collections import deque
from html import escape
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from app.config import Config
from app.files import extract_file_text, is_supported, parse_size_mb
from app.rules import clean_hits, host_allowed, is_login_url

logger = logging.getLogger("kb-browser")


class SessionExpired(Exception):
    """La page affichée est une connexion/MFA : le profil n'est plus authentifié."""


class SelectorFailed(Exception):
    def __init__(self, selector: str, url: str, message: str = ""):
        super().__init__(message or f"Sélecteur introuvable : {selector}")
        self.selector, self.url = selector, url


class NavigationFailed(Exception):
    def __init__(self, url: str, message: str):
        super().__init__(message)
        self.url = url


class LogBook:
    """Journal circulaire des évènements de navigation, exposé à la console de debug."""

    def __init__(self, size: int = 500):
        self._entries: deque[dict] = deque(maxlen=size)

    def add(self, level: str, event: str, **fields) -> None:
        self._entries.append({"ts": datetime.now(timezone.utc).isoformat(), "level": level, "event": event, **fields})
        logger.log(getattr(logging, level.upper(), logging.INFO), "%s %s", event, fields)

    def latest(self, limit: int) -> list[dict]:
        return list(self._entries)[-max(1, min(limit, 500)):][::-1]


class KBBrowser:
    def __init__(self, config: Config, logbook: LogBook | None = None):
        self._config = config
        self.logbook = logbook or LogBook()
        self._playwright = None
        self._context = None
        self._home = None  # onglet laissé ouvert : c'est lui que l'admin voit et utilise via noVNC
        self._lock = asyncio.Lock()  # un profil Chromium ne supporte qu'une opération à la fois
        self._last_nav = 0.0

    @property
    def launched(self) -> bool:
        return self._context is not None

    # ------------------------------------------------------------------ cycle de vie
    async def _get_context(self):
        if self._context is not None:
            try:
                _ = self._context.pages  # lève si le navigateur a été fermé/planté
                return self._context
            except Exception:
                self.logbook.add("warning", "browser_restart")
                self._context = None
                self._home = None
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            self._config.profile_dir,
            headless=self._config.headless,
            locale="fr-FR",
            viewport={"width": 1366, "height": 860},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        await self._restore_cookies(self._context)
        self.logbook.add("info", "browser_started", profile=self._config.profile_dir, headless=self._config.headless)
        return self._context

    # Chromium ne conserve pas les cookies « de session » (sans date d'expiration) à sa fermeture, même
    # avec un profil persistant : le SSO obligerait à refaire le MFA à chaque redémarrage du conteneur.
    # On les sauvegarde donc dans le volume et on les rejoue au lancement.
    @property
    def _cookie_file(self) -> Path:
        return Path(self._config.profile_dir) / "session-cookies.json"

    async def _restore_cookies(self, context) -> None:
        try:
            if self._cookie_file.is_file():
                cookies = json.loads(self._cookie_file.read_text(encoding="utf-8"))
                await context.add_cookies(cookies)
                self.logbook.add("info", "cookies_restored", count=len(cookies))
        except Exception as exc:
            self.logbook.add("warning", "cookies_restore_failed", message=str(exc)[:200])

    async def _save_cookies(self) -> None:
        """Sauvegarde les cookies du profil (fichier lisible par le seul utilisateur du conteneur)."""
        if self._context is None:
            return
        try:
            cookies = await self._context.cookies()
            if not cookies:
                return
            self._cookie_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._cookie_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(cookies), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self._cookie_file)
        except Exception as exc:
            self.logbook.add("warning", "cookies_save_failed", message=str(exc)[:200])

    async def _home_page(self):
        context = await self._get_context()
        if self._home is None or self._home.is_closed():
            blank = next((page for page in context.pages if not page.is_closed()), None)
            self._home = blank or await context.new_page()
        return self._home

    async def show_login(self) -> None:
        """Affiche la page de connexion dans l'onglet visible (noVNC) pour que l'admin s'authentifie.

        Best-effort : une erreur ici ne doit jamais masquer l'expiration de session qui l'a déclenchée.
        """
        try:
            home = await self._home_page()
            if not is_login_url(home.url, self._config.login_url_patterns):
                await home.goto(self._config.base_url, wait_until="domcontentloaded", timeout=self._config.nav_timeout_ms)
            await home.bring_to_front()
            self.logbook.add("info", "login_page_shown", url=home.url.split("?", 1)[0])
        except Exception as exc:
            self.logbook.add("warning", "login_page_failed", message=str(exc)[:200])

    async def close(self) -> None:
        if self._context is not None:
            await self._save_cookies()
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _open(self, url: str):
        """Ouvre `url` dans un nouvel onglet après un court délai (politesse envers le portail)."""
        if not host_allowed(url, self._config.hosts()):
            raise NavigationFailed(url, f"Hôte non autorisé : {url}")
        wait = self._config.request_delay_ms / 1000 - (time.monotonic() - self._last_nav)
        if wait > 0:
            await asyncio.sleep(wait)
        context = await self._get_context()
        page = await context.new_page()
        page.set_default_timeout(self._config.selector_timeout_ms)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self._config.nav_timeout_ms)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            await page.close()
            self.logbook.add("error", "navigation_failed", url=url, message=str(exc)[:300])
            raise NavigationFailed(url, f"Navigation impossible : {exc}") from exc
        finally:
            self._last_nav = time.monotonic()
        self.logbook.add("info", "navigated", url=url, final_url=page.url)
        return page

    # ------------------------------------------------------------------ session
    async def _is_expired(self, page) -> bool:
        if is_login_url(page.url, self._config.login_url_patterns):
            return True
        if self._config.logged_in_selector:
            return await page.query_selector(self._config.logged_in_selector) is None
        if self._config.login_selector and await page.query_selector(self._config.login_selector):
            return True
        return False

    async def session_state(self) -> str:
        async with self._lock:
            page = await self._open(self._config.base_url)
            try:
                expired = await self._is_expired(page)
            finally:
                await page.close()
        state = "expired" if expired else "valid"
        self.logbook.add("warning" if expired else "info", "session_checked", state=state)
        if expired:
            await self.show_login()
        else:
            await self._save_cookies()
        return state

    async def _ensure_session(self, page) -> None:
        if await self._is_expired(page):
            self.logbook.add("error", "session_expired", url=page.url.split("?", 1)[0])
            await self.show_login()
            raise SessionExpired(f"Redirection vers une page de connexion/MFA ({page.url}).")

    # ------------------------------------------------------------------ recherche & article
    async def search(self, query: str, limit: int) -> list[dict]:
        started = time.monotonic()
        async with self._lock:
            page = await self._open(self._config.search_url or self._config.base_url)
            try:
                before = await self._perform_search(
                    page, query, self._config.search_input_selector, self._config.search_submit_selector,
                )
                await self._wait_for_results(page, before)
                raw = await self._resolve_urls(page, await self._extract_hits(page), limit)
            finally:
                await page.close()
        await self._save_cookies()
        hits = clean_hits(raw, limit, self._config.hosts())
        self.logbook.add(
            "info", "search_done", query=query, hits=len(hits), urls=[hit["url"] for hit in hits],
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return hits

    async def _perform_search(self, page, query: str, input_selector: str, submit_selector: str) -> str:
        """Saisit la requête comme un utilisateur et la soumet. Retourne le 1er résultat d'avant la recherche."""
        config = self._config
        await self._ensure_session(page)
        if config.ready_selector:
            try:
                await page.wait_for_selector(config.ready_selector, state="attached", timeout=config.selector_timeout_ms)
            except PlaywrightTimeout:
                await self._ensure_session(page)
                self.logbook.add("warning", "ready_selector_timeout", selector=config.ready_selector, url=page.url)
        await page.wait_for_timeout(config.settle_ms)  # laisse l'application finir de s'initialiser
        search_input = page.locator(input_selector).first
        await self._require(page, search_input, input_selector)
        before = await self._first_result_text(page)
        await search_input.click()
        await search_input.press("Control+A")
        await search_input.press("Delete")
        await search_input.press_sequentially(query, delay=20)  # vrais évènements clavier (Coveo, React…)
        if submit_selector:
            submit = page.locator(submit_selector).first
            await self._require(page, submit, submit_selector)
            await submit.click()
        else:
            await search_input.press("Enter")
        return before

    async def _first_result_text(self, page) -> str:
        try:
            first = page.locator(self._config.result_link_selector).first
            return (await first.inner_text(timeout=1500)).strip() if await first.count() else ""
        except Exception:
            return ""

    async def _resolve_urls(self, page, raw_hits: list[dict], limit: int) -> list[dict]:
        """Complète les résultats sans lien `href` : l'application ouvre l'article au clic (nouvel onglet)."""
        context = page.context
        resolved = []
        for index, hit in enumerate(raw_hits[:limit]):
            if hit.get("url") or not hit.get("title"):
                resolved.append(hit)
                continue
            popup = None
            try:
                async with context.expect_page(timeout=8000) as popup_info:
                    await page.locator(self._config.result_link_selector).nth(index).click()
                popup = await popup_info.value
                await popup.wait_for_url(lambda url: url != "about:blank", timeout=8000)
                resolved.append({**hit, "url": popup.url})
            except (PlaywrightTimeout, PlaywrightError) as exc:
                self.logbook.add("warning", "result_url_unresolved", title=hit["title"][:80], message=str(exc)[:160])
                resolved.append(hit)
            finally:
                if popup is not None and not popup.is_closed():
                    await popup.close()
            await page.wait_for_timeout(600)
        return resolved

    async def _extract_hits(self, page) -> list[dict]:
        return await page.eval_on_selector_all(
            self._config.result_link_selector,
            """els => els.map(e => ({title: (e.innerText || e.title || '').trim(),
                url: e.href || (e.closest('a') ? e.closest('a').href : '') || e.getAttribute('data-href') || ''}))""",
        )

    async def snapshot(
        self, query: str, input_selector: str = "", submit_selector: str = "", probe: str = "",
        open_result: int = -1, wait_ms: int = 6000,
    ) -> dict:
        """Diagnostic : exécute une recherche puis décrit la page obtenue et la photographie.

        Sert à choisir les sélecteurs CSS d'un portail sans deviner son HTML. `input_selector` et
        `submit_selector` remplacent temporairement la configuration; `probe` est un texte visible
        (ex. le titre d'un résultat) dont on veut connaître le HTML et les ancêtres.
        """
        input_selector = input_selector or self._config.search_input_selector
        submit_selector = submit_selector or self._config.search_submit_selector
        async with self._lock:
            page = await self._open(self._config.search_url or self._config.base_url)
            try:
                await self._ensure_session(page)
                await self._perform_search(page, query, input_selector, submit_selector)
                await page.wait_for_timeout(wait_ms)
                info = {
                    "url": page.url,
                    "title": await page.title(),
                    "headings": await page.eval_on_selector_all(
                        "h1, h2, h3", "els => els.slice(0, 20).map(e => (e.innerText || '').trim().slice(0, 100))"),
                    "candidates": await self._count_candidates(page),
                    "text_sample": ((await page.inner_text("body")) or "")[:1200],
                }
                search_input = page.locator(input_selector).first
                info["search_box"] = await search_input.evaluate(
                    "e => { let n = e; for (let i = 0; i < 3 && n.parentElement; i++) n = n.parentElement; return n.outerHTML.slice(0, 1400); }"
                ) if await search_input.count() else ""
                info["hits"] = await self._extract_hits(page)
                if probe and open_result < 0:
                    info["probe"] = await self._describe_probe(page, probe)
                if open_result >= 0:
                    info["opened"] = await self._open_result(page, open_result, probe)
                else:
                    await page.screenshot(path="/tmp/snapshot.png", full_page=False)
            finally:
                await page.close()
        self.logbook.add("info", "snapshot_done", query=query, url=info["url"].split("?", 1)[0])
        return info

    # ------------------------------------------------------------------ source « documents »
    async def search_documents(self, query: str, limit: int) -> list[dict]:
        """Filtre la liste de documents avec le champ « Search this list… » et retourne les premières lignes.

        Chaque résultat porte `meta` : le texte des colonnes de la ligne (type, langue, date…), que le
        backend utilise pour écarter les documents purement commerciaux (grilles de prix, RFP…).
        """
        config = self._config
        if not config.docs_url:
            raise NavigationFailed("", "La source documents n'est pas configurée (KB_DOCS_URL).")
        started = time.monotonic()
        row_selector = config.docs_row_link_selector
        async with self._lock:
            page = await self._open(config.docs_url)
            try:
                await self._ensure_session(page)
                try:
                    await page.wait_for_selector(row_selector, state="attached", timeout=config.selector_timeout_ms)
                except PlaywrightTimeout:
                    await self._ensure_session(page)
                    self.logbook.add("error", "selector_failed", selector=row_selector, url=page.url)
                    raise SelectorFailed(row_selector, page.url, "Aucune ligne de document trouvée (sélecteur à vérifier).") from None
                await page.wait_for_timeout(config.settle_ms)
                box = page.locator(config.docs_search_input_selector).first
                await self._require(page, box, config.docs_search_input_selector)
                before = await self._first_text(page, row_selector)
                await box.click()
                await box.press("Control+A")
                await box.press("Delete")
                await box.press_sequentially(query, delay=20)
                await box.press("Enter")
                deadline = time.monotonic() + config.results_refresh_ms / 1000
                while time.monotonic() < deadline:  # la liste se filtre : attendre que la 1re ligne change
                    await page.wait_for_timeout(400)
                    if await self._first_text(page, row_selector) != before:
                        break
                rows = await page.eval_on_selector_all(
                    row_selector,
                    """els => els.filter(a => a.offsetWidth || a.offsetHeight || a.getClientRects().length)
                        .map(a => { const row = a.closest('tr, [role="row"]');
                        const cells = row ? [...row.querySelectorAll('td, th, [role="gridcell"], [role="rowheader"]')]
                            .map(c => (c.innerText || '').trim().replace(/\\s+/g, ' ')) : [];
                        return {title: (a.innerText || '').trim(), url: a.href, meta: cells.join(' | ')}; })""",
                )
            finally:
                await page.close()
        await self._save_cookies()
        hits = clean_hits(rows, limit, config.hosts())
        self.logbook.add(
            "info", "documents_search_done", query=query, hits=len(hits), urls=[hit["url"] for hit in hits],
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return hits

    async def _first_text(self, page, selector: str) -> str:
        """Texte du premier élément VISIBLE (une liste filtrée garde souvent ses lignes masquées dans le DOM)."""
        try:
            texts = await page.eval_on_selector_all(
                selector,
                "els => els.filter(e => e.offsetWidth || e.offsetHeight || e.getClientRects().length)"
                ".slice(0, 1).map(e => (e.innerText || '').trim())",
            )
            return texts[0] if texts else ""
        except Exception:
            return ""

    async def fetch_document(self, url: str, include_file: bool = False) -> dict:
        """Lit la fiche d'un document (métadonnées) et télécharge son fichier si sa taille et son format le permettent.

        Avec `include_file`, le fichier original est aussi renvoyé (base64 + SHA-256) pour être archivé localement,
        y compris dans un format dont le texte n'est pas extractible.
        """
        config = self._config
        started = time.monotonic()
        async with self._lock:
            page = await self._open(url)
            try:
                await self._ensure_session(page)
                await page.wait_for_timeout(config.settle_ms * 2)
                body = (await page.inner_text("body")) or ""
                metadata = self._slice_between(body, config.docs_start_marker, config.docs_end_marker)
                heading = page.locator("h1").first
                title = (await heading.inner_text()).strip() if await heading.count() else ""
                file = await self._download_attachment(page, metadata, include_file)
                final_url = page.url
            finally:
                await page.close()
        self.logbook.add(
            "info", "document_fetched", url=final_url.split("?", 1)[0], metadata_chars=len(metadata),
            file=file.get("name") or file.get("skipped", ""), file_chars=len(file.get("text", "")),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return {"url": final_url, "title": title, "metadata": metadata, "file": file}

    @staticmethod
    def _slice_between(text: str, start: str, end: str) -> str:
        begin = text.find(start) if start else 0
        begin = 0 if begin < 0 else begin
        finish = text.find(end, begin) if end else -1
        return text[begin: finish if finish > begin else len(text)].strip()[:12000]

    async def _download_attachment(self, page, metadata: str, include_file: bool = False) -> dict:
        """Télécharge le fichier joint (si présent, de taille raisonnable et d'un format lisible) et en extrait le texte."""
        config = self._config
        control = page.locator(config.docs_download_selector).first
        if not await control.count():
            return {"skipped": "no_attachment"}
        size_mb = parse_size_mb(metadata)
        if size_mb is not None and size_mb > config.docs_max_file_mb:
            self.logbook.add("info", "file_skipped", reason="too_large", size_mb=round(size_mb, 1))
            return {"skipped": "too_large", "size_mb": round(size_mb, 1)}
        try:
            async with page.expect_download(timeout=config.docs_download_timeout_ms) as download_info:
                await control.click()
            download = await download_info.value
            name = download.suggested_filename
            path = Path(await download.path())
            size = path.stat().st_size
            if size > config.docs_max_file_mb * 1024 * 1024:
                path.unlink(missing_ok=True)
                return {"name": name, "skipped": "too_large", "size_mb": round(size / 1048576, 1)}
            result: dict = {"name": name, "size": size}
            try:
                if include_file:  # le fichier original part vers l'archive locale, puis il est supprimé d'ici
                    data = await asyncio.to_thread(path.read_bytes)
                    result["sha256"] = hashlib.sha256(data).hexdigest()
                    result["content_b64"] = base64.b64encode(data).decode("ascii")
                if is_supported(name):
                    text = await asyncio.to_thread(extract_file_text, path, name)
                    result["text"] = text
                    if not text:
                        result["skipped"] = "no_text"
                else:
                    self.logbook.add("info", "file_skipped", reason="unsupported_format", name=name)
                    result["skipped"] = "unsupported_format"
            finally:
                path.unlink(missing_ok=True)
            return result
        except PlaywrightTimeout:
            self.logbook.add("warning", "download_timeout", url=page.url.split("?", 1)[0])
            return {"skipped": "download_failed"}
        except Exception as exc:
            self.logbook.add("warning", "download_failed", message=f"{type(exc).__name__}: {exc}"[:200])
            return {"skipped": "download_failed"}

    async def inspect(self, url: str, wait_ms: int = 6000, scroll: bool = True) -> dict:
        """Diagnostic : ouvre une page et la décrit (titres, champs, éléments répétés, liens), sans rechercher."""
        async with self._lock:
            page = await self._open(url)
            try:
                await self._ensure_session(page)
                await page.wait_for_timeout(wait_ms)
                if scroll:  # les listes longues se chargent souvent au défilement
                    await page.mouse.wheel(0, 2500)
                    await page.wait_for_timeout(1500)
                info = {
                    "url": page.url,
                    "title": await page.title(),
                    "headings": await page.eval_on_selector_all(
                        "h1, h2, h3", "els => els.slice(0, 25).map(e => (e.innerText || '').trim().slice(0, 100))"),
                    "inputs": await page.eval_on_selector_all(
                        "input, select, textarea",
                        """els => els.filter(e => e.offsetWidth || e.offsetHeight).slice(0, 20).map(e => ({
                            tag: e.tagName.toLowerCase(), id: e.id, type: e.getAttribute('type'),
                            placeholder: e.getAttribute('placeholder'), cls: String(e.className).slice(0, 90)}))"""),
                    "candidates": await self._count_candidates(page),
                    "links": await page.eval_on_selector_all(
                        "a[href]",
                        """els => els.filter(e => e.offsetWidth || e.offsetHeight).slice(0, 60).map(e =>
                            ({text: (e.innerText || '').trim().slice(0, 80), href: e.href}))"""),
                    "text_sample": ((await page.inner_text("body")) or "")[:2500],
                    # Contrôles de téléchargement possibles (titre, libellé ARIA ou icône « download »).
                    "download_hints": await page.eval_on_selector_all(
                        "[title*='ownload' i], [aria-label*='ownload' i], [icon-name*='ownload' i], "
                        "[data-name*='ownload' i], a[download], lightning-button-icon, lightning-icon",
                        """els => els.filter(e => /ownload/i.test((e.getAttribute('title') || '') + (e.getAttribute('aria-label') || '')
                            + (e.getAttribute('icon-name') || '') + (e.getAttribute('data-name') || '') + (e.getAttribute('alternative-text') || '')))
                            .slice(0, 8).map(e => ({tag: e.tagName.toLowerCase(), cls: String(e.className).slice(0, 80), title: e.getAttribute('title'),
                              aria: e.getAttribute('aria-label'), icon: e.getAttribute('icon-name'), parent: e.parentElement ? e.parentElement.tagName.toLowerCase()
                              + '.' + String(e.parentElement.className).slice(0, 60) : ''}))"""),
                }
                await page.screenshot(path="/tmp/snapshot.png", full_page=False)
            finally:
                await page.close()
        self.logbook.add("info", "inspect_done", url=info["url"].split("?", 1)[0])
        return info

    async def _open_result(self, page, index: int, probe: str = "") -> dict:
        """Diagnostic : clique sur le n-ième résultat et décrit la page ouverte (nouvel onglet ou même onglet)."""
        context = page.context
        before_pages = list(context.pages)
        before_url = page.url
        await page.locator(self._config.result_link_selector).nth(index).click()
        await page.wait_for_timeout(6000)
        opened = [candidate for candidate in context.pages if candidate not in before_pages]
        target = opened[0] if opened else page
        try:
            containers = []
            for selector in ("article", "main", "#content", "lightning-formatted-rich-text", ".slds-rich-text-editor__output",
                             "[class*='article']", "[class*='knowledge']", "[class*='content']", ".slds-card", "h1"):
                described = await target.eval_on_selector_all(
                    selector,
                    """els => els.filter(e => e.offsetWidth || e.offsetHeight).slice(0, 3).map(e =>
                        ({tag: e.tagName.toLowerCase(), cls: String(e.className).slice(0, 90), id: e.id,
                          chars: (e.innerText || '').length, text: (e.innerText || '').trim().slice(0, 70)}))""",
                )
                if described:
                    containers.append({"selector": selector, "elements": described})
            await target.screenshot(path="/tmp/snapshot.png", full_page=False)
            return {
                "new_tab": bool(opened), "url_changed": target.url != before_url, "url": target.url,
                "title": await target.title(), "containers": containers,
                "probe": await self._describe_probe(target, probe) if probe else None,
                "text_sample": ((await target.inner_text("body")) or "")[:900],
            }
        finally:
            if opened:
                await target.close()

    @staticmethod
    async def _count_candidates(page) -> list[dict]:
        """Nombre d'éléments visibles pour des sélecteurs usuels de titres de résultats."""
        selectors = [
            "a[href]", "a", "[role='link']", "[role='button']", "button", "lightning-formatted-url",
            "h2", "h3", "h4", "article", "li", ".slds-media__body", ".slds-text-heading_small",
            "[class*='result']", "[class*='title']", "[class*='article']", "[data-id]",
        ]
        counted = []
        for selector in selectors:
            try:
                sample = await page.eval_on_selector_all(
                    selector,
                    """els => { const v = els.filter(e => e.offsetWidth || e.offsetHeight);
                    return {count: v.length, sample: v.slice(0, 3).map(e => (e.innerText || '').trim().slice(0, 60))}; }""",
                )
                if sample["count"]:
                    counted.append({"selector": selector, **sample})
            except Exception:
                continue
        return counted

    @staticmethod
    async def _describe_probe(page, text: str) -> dict:
        try:
            target = page.get_by_text(text, exact=False).first
            await target.wait_for(state="attached", timeout=4000)
            return await target.evaluate(
                """e => { const chain = []; let n = e;
                for (let i = 0; i < 12 && n && n.tagName; i++, n = n.parentElement) {
                  chain.push({tag: n.tagName.toLowerCase(), id: n.id || '', cls: String(n.className || '').slice(0, 110), chars: (n.innerText || '').length, nav: (n.innerText || '').includes('Skip to Navigation'),
                    role: n.getAttribute('role') || '', href: n.getAttribute('href') || '',
                    data: Object.fromEntries([...n.attributes].filter(a => a.name.startsWith('data-')).map(a => [a.name, a.value.slice(0, 60)]))}); }
                return {html: e.outerHTML.slice(0, 500), chain}; }"""
            )
        except Exception as exc:
            return {"error": f"élément introuvable pour « {text} » : {str(exc)[:120]}"}

    async def _wait_for_results(self, page, before: str = "") -> None:
        config = self._config
        selector = config.result_link_selector
        try:
            await page.wait_for_selector(selector, timeout=config.selector_timeout_ms)
        except PlaywrightTimeout:
            await self._ensure_session(page)  # une redirection MFA tardive explique souvent l'absence de résultat
            if config.no_results_selector and await page.query_selector(config.no_results_selector):
                return
            self.logbook.add("error", "selector_failed", selector=selector, url=page.url)
            raise SelectorFailed(selector, page.url, "Aucun lien de résultat trouvé (sélecteur à vérifier).")
        # Les résultats d'avant la recherche sont déjà affichés : attendre qu'ils soient remplacés.
        deadline = time.monotonic() + config.results_refresh_ms / 1000
        while before and time.monotonic() < deadline:
            if await self._first_result_text(page) != before:
                return
            if config.no_results_selector and await page.query_selector(config.no_results_selector):
                return
            await page.wait_for_timeout(400)
        if before:
            self.logbook.add("warning", "results_not_refreshed", message="Les résultats n'ont pas changé après la recherche.")

    async def fetch_article(self, url: str) -> dict:
        started = time.monotonic()
        async with self._lock:
            page = await self._open(url)
            try:
                await self._ensure_session(page)
                container = page.locator(self._config.article_selector).first
                await self._require(page, container, self._config.article_selector)
                html = await self._read_article(page, container)
                title_locator = page.locator(self._config.title_selector).first
                title = ""
                if await title_locator.count():
                    title = (await title_locator.inner_text()).strip()
                title = title or (await page.title()).strip()
                final_url = page.url
            finally:
                await page.close()
        self.logbook.add("info", "article_fetched", url=final_url, size=len(html),
                         duration_ms=round((time.monotonic() - started) * 1000))
        return {"url": final_url, "title": title, "html": html}

    async def _read_article(self, page, first) -> str:
        """Contenu de l'article : un élément ou, avec `article_join`, toutes les sections qui correspondent."""
        config = self._config
        if config.article_join:
            await page.wait_for_timeout(config.settle_ms)  # sections chargées en différé
        locator = page.locator(config.article_selector)
        if config.article_format == "text":
            texts = await (locator.evaluate_all("els => els.map(e => e.innerText || '')") if config.article_join
                           else first.evaluate("e => [e.innerText || '']"))
            return "\n".join(f"<section><pre>{escape(text)}</pre></section>" for text in texts if text.strip())
        if config.article_join:
            return "\n".join(await locator.evaluate_all("els => els.map(e => e.outerHTML)"))
        return await first.inner_html()

    async def _require(self, page, locator, selector: str) -> None:
        """Attend un élément visible; sinon journalise et lève `SelectorFailed` (ou `SessionExpired`)."""
        try:
            await locator.wait_for(state="attached", timeout=self._config.selector_timeout_ms)
        except PlaywrightTimeout:
            await self._ensure_session(page)
            self.logbook.add("error", "selector_failed", selector=selector, url=page.url)
            raise SelectorFailed(selector, page.url) from None
