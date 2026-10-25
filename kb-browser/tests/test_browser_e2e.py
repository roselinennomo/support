"""Test de bout en bout du scraper contre un faux portail KB local (nécessite Chromium installé)."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")

from app.browser import KBBrowser, NavigationFailed, SelectorFailed, SessionExpired  # noqa: E402
from app.config import Config  # noqa: E402

ARTICLE = (
    "<html><head><title>Article {n}</title></head><body><header>Menu</header>"
    "<article><h1>Titre {n}</h1><p>Contenu technique {n} sur le stacking OS6860E.</p></article>"
    "<footer>Pied</footer></body></html>"
)


class Portal(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, body: str, status: int = 200, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/login":
            return self._send("<form><input type='password' name='p'></form>")
        if "session=ok" not in self.headers.get("Cookie", ""):
            return self._send("", 302, {"Location": "/login"})
        if url.path == "/kb/":
            return self._send("<form action='/kb/search'><input name='q'><button>OK</button></form>")
        if url.path == "/kb/search":
            if "vide" in parse_qs(url.query).get("q", [""])[0]:
                return self._send("<div class='aucun'>Aucun résultat</div>")
            links = "".join(f"<a class='search-result' href='/kb/a/{n}'>Résultat {n}</a>" for n in (1, 2, 3, 4))
            return self._send(f"<div>{links}<a class='search-result' href='http://evil.example/x'>Pub</a></div>")
        # Source « documents » : liste filtrable côté client, fiche avec fichier téléchargeable.
        if url.path == "/docs/":
            rows = "".join(
                f"<tr><td>{n}</td><td><a href='/s/business-document/id{n}/doc-{n}'>{title}</a></td><td>{kind}</td></tr>"
                for n, (title, kind) in enumerate([
                    ("Guide SIP trunking", "PreSales > Documentation"),
                    ("Quoter SIP pricing", "PreSales;Price catalog;Pricing"),
                    ("Installation guide OXE", "PreSales > Design Guide"),
                ], start=1)
            )
            script = (
                "const box=document.querySelector('input');"
                "box.addEventListener('keyup',()=>{const q=box.value.toLowerCase();"
                "document.querySelectorAll('tr').forEach(r=>{r.style.display=r.innerText.toLowerCase().includes(q)?'':'none'})});"
            )
            return self._send(f"<html><body><input type='search' placeholder='Search this list...'><table>{rows}</table><script>{script}</script></body></html>")
        if url.path.startswith("/s/business-document/"):
            size = "9.5 MB" if url.path.endswith("doc-3") else "0.1 MB"
            target = "/file/data.xlsx" if url.path.endswith("doc-4") else "/file/guide.txt"
            return self._send(
                f"<html><body><h1>Titre {url.path[-1]}</h1><div>Business Document File</div><div>{size} - TEXT</div>"
                f"<lightning-icon title='Download' class='download_icon' onclick=\"location.href='{target}'\">Télécharger</lightning-icon>"
                "<div>About Description Procédure d'installation du trunk SIP.</div><footer>Terms of Use | GDPR</footer></body></html>"
            )
        if url.path == "/file/data.xlsx":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename=data.xlsx")
            self.end_headers()
            return self.wfile.write(b"PK\x03\x04 contenu binaire non lisible")
        if url.path == "/file/guide.txt":
            body = "Étape 1 : configurer le trunk SIP.\nÉtape 2 : vérifier l'enregistrement."
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename=guide.txt")
            self.end_headers()
            return self.wfile.write(body.encode())
        # Variante « Salesforce » : résultats sans href (clic = nouvel onglet), article en sections.
        if url.path == "/kb2/":
            return self._send("<form action='/kb2/search'><input name='q'></form>")
        if url.path == "/kb2/search":
            items = "".join(
                f"<div role='link' onclick=\"window.open('/kb2/a/{n}?q=stacking')\">Article {n}</div>" for n in (1, 2, 3)
            )
            return self._send(f"<div>{items}</div>")
        if url.path.startswith("/kb2/a/"):
            n = url.path.rsplit("/", 1)[1]
            return self._send(
                f"<html><body><header>Menu du portail</header>"
                f"<section class='slds-accordion__section'><h2>Summary</h2><p>Résumé de l'article {n}.</p></section>"
                f"<section class='slds-accordion__section'><h2>Solution</h2><p>Relancer le stacking {n}.</p></section>"
                f"<footer>Pied du portail</footer></body></html>"
            )
        if url.path.startswith("/kb/a/"):
            return self._send(ARTICLE.format(n=url.path.rsplit("/", 1)[1]))
        return self._send("introuvable", 404)


@pytest.fixture(scope="module")
def portal():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Portal)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def make_browser(base: str, tmp_path, **overrides) -> KBBrowser:
    values = dict(
        base_url=f"{base}/kb/", profile_dir=str(tmp_path / "profile"), headless=True,
        search_input_selector="input[name='q']", result_link_selector="a.search-result",
        no_results_selector=".aucun", article_selector="article", title_selector="h1",
        selector_timeout_ms=2000, nav_timeout_ms=5000, request_delay_ms=0, login_url_patterns=["login"],
    )
    values.update(overrides)
    return KBBrowser(Config(**values))


async def _login(browser: KBBrowser, base: str):
    context = await browser._get_context()
    await context.add_cookies([{"name": "session", "value": "ok", "url": base}])
    return context


def run(coro):
    return asyncio.run(coro)


def test_full_deterministic_flow_with_persistent_profile(portal, tmp_path):
    async def scenario():
        browser = make_browser(portal, tmp_path)
        try:
            assert await browser.session_state() == "expired"  # redirigé vers /login
            # Un onglet reste ouvert sur la page de connexion pour la reconnexion manuelle (noVNC).
            assert any(e["event"] == "login_page_shown" for e in browser.logbook.latest(20))
            assert any("/login" in page.url for page in (await browser._get_context()).pages)
            with pytest.raises(SessionExpired):
                await browser.search("stacking", 3)

            context = await _login(browser, portal)
            assert await browser.session_state() == "valid"

            hits = await browser.search("stacking OS6860E", 3)
            assert [hit["title"] for hit in hits] == ["Résultat 1", "Résultat 2", "Résultat 3"]  # limite + hôte externe écarté
            assert await browser.search("vide", 3) == []

            article = await browser.fetch_article(hits[0]["url"])
            assert article["title"] == "Titre 1" and "stacking OS6860E" in article["html"]
            assert "Menu" not in article["html"]

            await context.clear_cookies()  # session expirée en cours de route
            with pytest.raises(SessionExpired):
                await browser.fetch_article(hits[0]["url"])
            events = [entry["event"] for entry in browser.logbook.latest(100)]
            assert "session_expired" in events and "search_done" in events and "article_fetched" in events
        finally:
            await browser.close()

    run(scenario())


def test_failed_selector_is_reported_and_foreign_hosts_are_refused(portal, tmp_path):
    async def scenario():
        browser = make_browser(portal, tmp_path, result_link_selector="a.inexistant", no_results_selector="")
        try:
            await _login(browser, portal)
            with pytest.raises(SelectorFailed) as failure:
                await browser.search("stacking", 3)
            assert failure.value.selector == "a.inexistant"
            assert any(e["event"] == "selector_failed" and e["selector"] == "a.inexistant" for e in browser.logbook.latest(50))
            with pytest.raises(NavigationFailed):
                await browser.fetch_article("http://169.254.169.254/latest/meta-data")
        finally:
            await browser.close()

    run(scenario())


def test_session_cookies_survive_a_browser_restart(portal, tmp_path):
    """Régression : les cookies de session (sans expiration) étaient perdus au redémarrage du conteneur."""
    async def scenario():
        first = make_browser(portal, tmp_path)
        try:
            await _login(first, portal)
            assert await first.session_state() == "valid"
        finally:
            await first.close()  # sauvegarde les cookies dans le profil

        second = make_browser(portal, tmp_path)  # même profil, nouveau Chromium
        try:
            assert await second.session_state() == "valid"
            assert any(e["event"] == "cookies_restored" for e in second.logbook.latest(50))
        finally:
            await second.close()

        # Sans la sauvegarde, la session est bien perdue (preuve que le test est pertinent).
        (tmp_path / "profile" / "session-cookies.json").unlink()
        third = make_browser(portal, tmp_path)
        try:
            assert await third.session_state() == "expired"
        finally:
            await third.close()

    run(scenario())


def test_results_without_href_are_resolved_by_click_and_article_sections_are_joined(portal, tmp_path):
    async def scenario():
        browser = make_browser(
            portal, tmp_path, base_url=f"{portal}/kb2/", result_link_selector="[role='link']", no_results_selector="",
            article_selector="section.slds-accordion__section", article_join=True, article_format="text", settle_ms=0,
            results_refresh_ms=500,
        )
        try:
            await _login(browser, portal)
            hits = await browser.search("stacking", 2)
            assert [hit["title"] for hit in hits] == ["Article 1", "Article 2"]
            assert all(hit["url"].startswith(f"{portal}/kb2/a/") for hit in hits)

            article = await browser.fetch_article(hits[0]["url"])
            from html import unescape

            content = unescape(article["html"])  # mode texte : le contenu est échappé en HTML
            assert "Résumé de l'article 1" in content and "Relancer le stacking 1" in content
            assert "Menu du portail" not in article["html"] and "Pied du portail" not in article["html"]
            assert not any(page.url.startswith(f"{portal}/kb2/a/") for page in (await browser._get_context()).pages)  # onglets refermés
        finally:
            await browser.close()

    run(scenario())


def test_documents_source_searches_the_list_reads_the_sheet_and_downloads_the_file(portal, tmp_path):
    async def scenario():
        browser = make_browser(
            portal, tmp_path, docs_url=f"{portal}/docs/", docs_search_input_selector="input[type='search']",
            settle_ms=0, results_refresh_ms=800, docs_max_file_mb=1,
        )
        try:
            await _login(browser, portal)
            hits = await browser.search_documents("sip", 5)
            assert [hit["title"] for hit in hits] == ["Guide SIP trunking", "Quoter SIP pricing"]  # « Installation » filtré par la saisie
            assert "PreSales > Documentation" in hits[0]["meta"] and "Pricing" in hits[1]["meta"]
            assert all(hit["url"].startswith(f"{portal}/s/business-document/") for hit in hits)

            document = await browser.fetch_document(hits[0]["url"])
            assert "Procédure d'installation du trunk SIP" in document["metadata"]
            assert "Terms of Use" not in document["metadata"]  # coupé au pied de page
            assert document["file"]["name"] == "guide.txt"
            assert "configurer le trunk SIP" in document["file"]["text"]

            too_large = await browser.fetch_document(f"{portal}/s/business-document/id3/doc-3")
            assert too_large["file"] == {"skipped": "too_large", "size_mb": 9.5}  # annoncé 9,5 Mo > limite de 1 Mo
        finally:
            await browser.close()

    run(scenario())


def test_documents_source_requires_its_url(portal, tmp_path):
    async def scenario():
        browser = make_browser(portal, tmp_path, docs_url="")
        try:
            with pytest.raises(NavigationFailed):
                await browser.search_documents("sip", 3)
        finally:
            await browser.close()

    run(scenario())


def test_original_file_is_returned_for_the_local_archive_only_when_requested(portal, tmp_path):
    import base64
    import hashlib

    async def scenario():
        browser = make_browser(portal, tmp_path, docs_url=f"{portal}/docs/", settle_ms=0, docs_max_file_mb=1)
        try:
            await _login(browser, portal)
            plain = await browser.fetch_document(f"{portal}/s/business-document/id1/doc-1")
            assert "content_b64" not in plain["file"] and "configurer le trunk SIP" in plain["file"]["text"]

            kept = await browser.fetch_document(f"{portal}/s/business-document/id1/doc-1", include_file=True)
            original = base64.b64decode(kept["file"]["content_b64"])
            assert original.decode("utf-8").startswith("Étape 1 : configurer le trunk SIP.")
            assert kept["file"]["sha256"] == hashlib.sha256(original).hexdigest()
            assert "configurer le trunk SIP" in kept["file"]["text"]  # le texte reste extrait

            # Format sans extraction de texte (ex. Excel) : le fichier est tout de même conservé pour l'archive.
            binary = await browser.fetch_document(f"{portal}/s/business-document/id4/doc-4", include_file=True)
            assert binary["file"]["name"] == "data.xlsx" and binary["file"]["skipped"] == "unsupported_format"
            assert base64.b64decode(binary["file"]["content_b64"]).startswith(b"PK")
            assert "text" not in binary["file"]
        finally:
            await browser.close()

    run(scenario())
