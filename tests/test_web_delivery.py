"""Web delivery: cache policy, conditional revalidation, compression, and browser hardening.

These tests pin the boundary between private responses (HTML shells, auth, OAuth, API
JSON) and genuinely public frontend assets, and they pin the security headers that must
hold for every response. They also guard the two structural rules the policy depends on:
no inline script or style in the served pages, and no Cross-Origin-Opener-Policy that
would sever the OAuth popup's ``window.opener.postMessage`` return path.
"""

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay.app import (
    APP_CSP,
    DOCS_CSP,
    PERMISSIONS_POLICY,
    PUBLIC_ASSET_CACHE,
    add_vary,
    create_app,
    is_public_asset,
)

KEY = "test-service-key-with-at-least-32-characters"
ORIGIN = {"Origin": "http://localhost:8000"}
STATIC = Path(__file__).resolve().parent.parent / "relay" / "static"
GZIP = {"Accept-Encoding": "gzip"}
# httpx offers gzip by default, so an "uncompressed" baseline must opt out explicitly.
IDENTITY = {"Accept-Encoding": "identity"}
# httpx decodes the body, so Content-Length is the only honest wire-size signal.
# Every entry is above the 500-byte compression threshold; styles.css (265 bytes) is
# deliberately not here and is covered by the small-payload test below.
COMPRESSIBLE = ["account.css", "styles/workspace.css", "app/publishing.js", "app/core.js"]


def unlock(client):
    return client.post("/session", json={"service_key": KEY}, headers=ORIGIN)


def test_private_responses_stay_no_store(settings):
    """HTML shells, auth pages, API JSON, and errors must never be cached."""
    with TestClient(create_app(settings)) as client:
        for path in ("/", "/login", "/signup", "/try", "/oauth/complete", "/session", "/health"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["cache-control"] == "no-store", path
        unlock(client)
        assert client.get("/app").headers["cache-control"] == "no-store"
        assert client.get("/accounts").headers["cache-control"] == "no-store"
        assert client.get("/openapi.json").headers["cache-control"] == "no-store"
        # An unauthenticated API error is private too.
        assert client.get("/accounts", headers={"X-API-Key": "wrong"}).headers[
            "cache-control"
        ] == "no-store"


def test_html_under_static_is_not_an_asset(settings):
    """An HTML page living under /static must not inherit the asset cache policy."""
    with TestClient(create_app(settings)) as client:
        for page in ("index.html", "auth.html", "landing.html", "oauth.html", "try.html"):
            response = client.get(f"/static/{page}")
            assert response.status_code == 200, page
            assert response.headers["cache-control"] == "no-store", page


def test_missing_static_file_is_not_cacheable(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/static/does-not-exist.css")
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"


def test_public_assets_use_revalidating_cache_policy(settings):
    with TestClient(create_app(settings)) as client:
        for asset in ("styles.css", "app/core.js", "app.js", "favicon.svg", "oauth.css"):
            response = client.get(f"/static/{asset}")
            assert response.status_code == 200, asset
            assert response.headers["cache-control"] == PUBLIC_ASSET_CACHE, asset
            # Unversioned paths are never immutable.
            assert "immutable" not in response.headers["cache-control"], asset


def test_static_asset_conditional_revalidation_returns_304(settings):
    with TestClient(create_app(settings)) as client:
        first = client.get("/static/styles.css")
        etag, last_modified = first.headers["etag"], first.headers["last-modified"]
        assert etag and last_modified

        by_etag = client.get("/static/styles.css", headers={"If-None-Match": etag})
        assert by_etag.status_code == 304
        assert by_etag.content == b""
        assert by_etag.headers["etag"] == etag
        # A 304 still carries the policy, so the cache keeps the right directive.
        assert by_etag.headers["cache-control"] == PUBLIC_ASSET_CACHE

        by_date = client.get("/static/styles.css", headers={"If-Modified-Since": last_modified})
        assert by_date.status_code == 304

        stale = client.get("/static/styles.css", headers={"If-None-Match": '"stale"'})
        assert stale.status_code == 200


def test_compressible_assets_are_gzipped_and_vary_on_encoding(settings):
    with TestClient(create_app(settings)) as client:
        for asset in COMPRESSIBLE:
            plain = client.get(f"/static/{asset}", headers=IDENTITY)
            packed = client.get(f"/static/{asset}", headers=GZIP)
            assert packed.status_code == 200, asset
            assert packed.headers["content-encoding"] == "gzip", asset
            assert "accept-encoding" in packed.headers["vary"].lower(), asset
            # Content-Type survives compression.
            assert packed.headers["content-type"] == plain.headers["content-type"], asset
            # A client that did not offer gzip gets identity bytes.
            assert "content-encoding" not in plain.headers
            assert plain.headers["vary"] == "Accept-Encoding"
            # httpx decodes the transfer coding, so the delivered bytes must be identical.
            assert packed.content == plain.content, asset
            # Starlette streams files in 64 KiB chunks; a body sent with more_body=True
            # is chunked and carries no Content-Length. Compare wire size only when the
            # server could state it.
            if "content-length" in packed.headers:
                assert int(packed.headers["content-length"]) < int(
                    plain.headers["content-length"]
                ), asset


def test_large_asset_is_gzipped_even_when_streamed(settings):
    """A file above the 64 KiB chunk size is compressed and sent chunked."""
    with TestClient(create_app(settings)) as client:
        packed = client.get("/static/app/publishing.js", headers=GZIP)
        assert packed.status_code == 200
        assert packed.headers["content-encoding"] == "gzip"
        assert "accept-encoding" in packed.headers["vary"].lower()
        assert len(packed.content) > 64 * 1024


def test_small_assets_stay_uncompressed_but_still_vary(settings):
    """Below the 500-byte threshold compression costs more than it saves."""
    with TestClient(create_app(settings)) as client:
        packed = client.get("/static/styles.css", headers=GZIP)
        assert packed.status_code == 200
        assert "content-encoding" not in packed.headers
        assert packed.headers["vary"] == "Accept-Encoding"
        # A tiny binary asset is not compressible content at all.
        icon = client.get("/static/favicon.svg", headers=GZIP)
        assert "content-encoding" not in icon.headers


def test_compression_is_measured_on_the_real_stylesheet(settings):
    """The transfer saving is measured, not asserted from a guess."""
    with TestClient(create_app(settings)) as client:
        plain = client.get("/static/styles/workspace.css", headers=IDENTITY)
        packed = client.get("/static/styles/workspace.css", headers=GZIP)
        raw = int(plain.headers["content-length"])
        wire = int(packed.headers["content-length"])
        assert raw > 20_000
        assert wire < raw / 2


def test_304_keeps_vary_for_compressible_assets(settings):
    """A 304 has no body, so the compression middleware cannot add Vary to it."""
    with TestClient(create_app(settings)) as client:
        etag = client.get("/static/app/core.js").headers["etag"]
        cached = client.get(
            "/static/app/core.js", headers={**GZIP, "If-None-Match": etag}
        )
        assert cached.status_code == 304
        assert "accept-encoding" in cached.headers["vary"].lower()
        assert cached.headers["cache-control"] == PUBLIC_ASSET_CACHE


def test_images_and_ranges_are_not_compressed(settings):
    with TestClient(create_app(settings)) as client:
        icon = client.get("/static/favicon.svg", headers=GZIP)
        assert "content-encoding" not in icon.headers

        ranged = client.get("/static/styles/workspace.css", headers={**GZIP, "Range": "bytes=0-99"})
        assert ranged.status_code == 206
        assert "content-encoding" not in ranged.headers
        assert ranged.headers["content-range"].startswith("bytes 0-99/")
        assert len(ranged.content) == 100
        assert ranged.headers["content-type"].startswith("text/css")


def test_browser_hardening_headers_on_every_response(settings):
    with TestClient(create_app(settings)) as client:
        for path in ("/", "/login", "/static/styles.css", "/health", "/accounts"):
            response = client.get(path)
            assert response.headers["x-content-type-options"] == "nosniff", path
            assert response.headers["x-frame-options"] == "DENY", path
            assert response.headers["referrer-policy"] == "no-referrer", path
            assert response.headers["permissions-policy"] == PERMISSIONS_POLICY, path
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            # COOP would sever the OAuth popup's window.opener.postMessage return.
            assert "cross-origin-opener-policy" not in response.headers, path


def test_hsts_only_for_configured_https(settings):
    with TestClient(create_app(settings)) as client:
        assert "strict-transport-security" not in client.get("/").headers

    https = replace(settings, base_url="https://relay.example")
    with TestClient(create_app(https), base_url="https://relay.example") as client:
        response = client.get("/")
        assert response.headers["strict-transport-security"] == "max-age=31536000"
        # includeSubDomains is not asserted: this app does not own its parent domain.
        assert "includesubdomains" not in response.headers["strict-transport-security"].lower()


def test_application_csp_is_strict_and_served_on_pages(settings):
    with TestClient(create_app(settings)) as client:
        for path in ("/", "/login", "/signup", "/try", "/oauth/complete", "/static/index.html"):
            policy = client.get(path).headers["content-security-policy"]
            assert policy == APP_CSP, path
        assert "script-src 'self'" in APP_CSP
        assert "object-src 'none'" in APP_CSP
        assert "base-uri 'none'" in APP_CSP
        assert "connect-src 'self'" in APP_CSP
        assert "frame-ancestors 'none'" in APP_CSP
        assert "'unsafe-eval'" not in APP_CSP
        assert "'unsafe-inline'" not in APP_CSP
        assert "cdn." not in APP_CSP
        # Provider avatars and the public media bucket are https, and the stylesheet
        # background is a data: URI. Images are not script execution, so a broad
        # https: image source is safer than guessing provider host lists.
        assert "img-src 'self' data: https:" in APP_CSP
        assert "media-src 'self' https:" in APP_CSP


def test_served_pages_have_no_inline_script_or_style(settings):
    """The strict CSP is only honest if the pages really carry no inline code."""
    with TestClient(create_app(settings)) as client:
        for page in ("index.html", "auth.html", "landing.html", "oauth.html", "try.html"):
            html = client.get(f"/static/{page}").text
            assert "<style" not in html, page
            assert " style=" not in html, page
            assert "javascript:" not in html, page
            for line in html.splitlines():
                if "<script" in line:
                    assert "src=" in line, f"{page}: inline script: {line.strip()[:120]}"
                    assert "http://" not in line and "https://" not in line, page
        # The pre-paint theme bootstrap is a same-origin blocking script, not inline.
        index = client.get("/static/index.html").text
        assert '<script src="/static/theme-init.js"></script>' in index
        assert client.get("/static/theme-init.js").status_code == 200


def test_docs_keep_their_own_compatible_policy(settings):
    """FastAPI's bundled docs load CDN bundles and inline init; they are a scoped exception."""
    with TestClient(create_app(settings)) as client:
        for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-security-policy"] == DOCS_CSP, path
            assert "cdn.jsdelivr.net" in response.headers["content-security-policy"], path
        assert "swagger-ui" in client.get("/docs").text
        # ReDoc pulls Google-hosted webfonts and starts a blob: worker. Both are named
        # explicitly in the docs policy so the page actually renders.
        assert "https://fonts.googleapis.com" in DOCS_CSP
        assert "https://fonts.gstatic.com" in DOCS_CSP
        assert "worker-src 'self' blob:" in DOCS_CSP
        # The exception is path-scoped: application pages never get it.
        assert client.get("/").headers["content-security-policy"] == APP_CSP
        assert client.get("/openapi.json").headers["content-security-policy"] == APP_CSP
        # The docs policy never leaks into the app policy.
        assert "'unsafe-inline'" not in APP_CSP
        assert "cdn.jsdelivr.net" not in APP_CSP
        assert "blob:" not in APP_CSP


def test_oauth_completion_page_can_report_back_to_its_opener(settings):
    """The popup return path needs a loadable script and no COOP, on the same origin."""
    with TestClient(create_app(settings)) as client:
        page = client.get("/oauth/complete?provider=youtube&result=connected")
        assert page.status_code == 200
        assert "/static/oauth-complete.js" in page.text
        assert "/static/oauth.css" in page.text
        assert "cross-origin-opener-policy" not in page.headers
        # script-src 'self' permits the completion script, so the postMessage can run.
        assert "script-src 'self';" in page.headers["content-security-policy"]
        script = client.get("/static/oauth-complete.js")
        assert script.status_code == 200
        assert "javascript" in script.headers["content-type"]
        # The script posts only to its own origin and only a result status.
        assert "window.opener.postMessage" in script.text
        assert "window.location.origin" in script.text


def test_auth_pages_load_only_same_origin_assets(settings):
    with TestClient(create_app(settings)) as client:
        for page, asset in (("/login", "/static/auth.js"), ("/try", "/static/try.js")):
            html = client.get(page).text
            assert asset in html, page
            assert client.get(asset).status_code == 200, asset
        # auth.js also reports a restored session to a popup opener, same origin only.
        auth = client.get("/static/auth.js").text
        assert "relay:session-restored" in auth
        assert "location.origin" in auth


def test_asset_predicate_rejects_private_shapes():
    """The cache decision is a predicate over path, method, status, and cookies."""

    class Request:
        def __init__(self, path, method="GET"):
            self.method = method
            self.url = type("URL", (), {"path": path})()

    class Response:
        def __init__(self, status=200, headers=None):
            self.status_code = status
            self.headers = headers or {}

    assert is_public_asset(Request("/static/app/core.js"), Response())
    assert is_public_asset(Request("/static/favicon.svg"), Response())
    assert is_public_asset(Request("/static/styles.css", "HEAD"), Response())
    assert is_public_asset(Request("/static/styles.css"), Response(304))
    # Never an asset: HTML, non-GET, an error, a cookie, or a path outside /static.
    assert not is_public_asset(Request("/static/index.html"), Response())
    assert not is_public_asset(Request("/static/styles.css", "POST"), Response())
    assert not is_public_asset(Request("/static/styles.css"), Response(500))
    assert not is_public_asset(
        Request("/static/styles.css"), Response(headers={"set-cookie": "a=b"})
    )
    assert not is_public_asset(Request("/app"), Response())
    assert not is_public_asset(Request("/static/../.env"), Response())


def test_add_vary_does_not_duplicate():
    headers = {}
    add_vary(headers, "Accept-Encoding")
    add_vary(headers, "Accept-Encoding")
    assert headers["Vary"] == "Accept-Encoding"
    headers = {"vary": "Origin"}
    add_vary(headers, "Accept-Encoding")
    assert headers["vary"] == "Origin, Accept-Encoding"


@pytest.mark.parametrize("asset", ["styles.css", "app/core.js", "app.js", "styles/tokens.css"])
def test_every_public_asset_is_a_real_file(asset):
    """The allowlist must describe files that exist, not a wish list."""
    assert (STATIC / asset).is_file()
