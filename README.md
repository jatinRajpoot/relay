# Relay OAuth & Publishing

Python 3.12+ / FastAPI service for connecting multiple YouTube and Instagram accounts. Access tokens, Google refresh tokens, expiry times, granted/requested scopes, and account identifiers persist in SQLite. Tokens and temporary PKCE verifiers are encrypted with Fernet; API responses never include tokens. Existing provider credentials are read from `.env`.

## Run locally (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'
.\.venv\Scripts\python scripts/setup_local.py
.\.venv\Scripts\python -m uvicorn relay.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

Open <http://localhost:8000> for the Connections frontend. Click **Connect Instagram** or **Connect YouTube**, unlock once with `SERVICE_API_KEY` from `.env`, and sign in with the provider. The frontend uses an encrypted HttpOnly browser session lasting eight hours; the key and provider tokens are never placed in browser storage. **Lock workspace** signs this browser out without disconnecting your accounts.

The page shows your connected accounts, expiration status, reconnect/disconnect actions, a setup guide with copyable callback URLs, and a post history for Instagram and YouTube publishing completed through Relay. Connect opens a centered 520×720 sign-in popup before waiting for any network requests, including when you first unlock the workspace. The dashboard stays open. Google explicitly asks you to choose an account and grant consent. Browsers may display a separate tab instead of a small window; allow popups for your workspace. Provider pages run in the sign-in window, rather than an embedded iframe.

After consent or cancellation, the popup reports its result to the dashboard using `postMessage`. The dashboard validates the exact origin, window reference and provider before accepting the result, closes the popup and refreshes the account list. Only a result status crosses this channel; tokens remain on the server. If a browser severs the opener connection, the completion page offers a manual return to the workspace; refresh the account list there.

Existing API clients still get JSON callbacks. `?ui=true` opts into dashboard redirects; `?ui=true&popup=true` opts into `/oauth/complete` with a result status. State records preserve this choice server-side.

The API reference remains at <http://localhost:8000/docs>. Click **Authorize** and paste `SERVICE_API_KEY` to use it. This key controls every connected account. `setup_local.py` adds missing service settings without replacing your Instagram/YouTube credentials. It is safe to rerun. For a fresh checkout copy `.env.example` to `.env` first and fill in provider credentials.

The default SQLite file is `data/relay.sqlite3`. Preserve `TOKEN_ENCRYPTION_KEY`; changing it makes existing tokens unreadable. `.env`, virtual environments and local databases are ignored by Git. Disable access logging as shown because OAuth callbacks contain authorization codes. Avoid enabling HTTP debug logs, which may contain provider tokens.

## Using the workspace

The workspace is a dark-first studio UI with a light theme. The theme follows your OS on first load and is remembered per browser after that; toggle it with the sun icon in the sidebar, `⌘J` / `Ctrl+J`, or the command menu.

Press `⌘K` / `Ctrl+K` (or the search icon) for the command menu: jump to any page, create a post, connect a channel, open the setup guide, or toggle the theme. Outside text fields, `n` opens the composer and `g` followed by `h`/`n`/`l`/`c`/`a`/`y` jumps to Home, Create, Media, Channels, Analytics, or History.

Home is a status dashboard: channel, media and published counts, the setup checklist, recent media, connected channels, and the last three published posts. **Create post** keeps media, channels, and details on one page with a sticky preview and publish panel, so you can scroll back and change anything before sending. Instagram still requires a separate confirmation before a post goes live; the delivery panel tracks each destination individually.

On screens narrower than 720px the sidebar is replaced by a bottom tab bar and the composer stacks into a single column.

## Configure the provider dashboards

### YouTube

1. Enable **YouTube Data API v3** and **YouTube Analytics API** in your Google Cloud project.
2. Configure the OAuth consent screen and add your Google account as a test user while testing.
3. Use a **Web application** OAuth client with the ID/secret in `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET`.
4. Register this exact authorized redirect URI: `http://localhost:8000/oauth/youtube/callback`.

Open [Google Cloud → Credentials](https://console.cloud.google.com/apis/credentials), edit the **same Web application OAuth client** whose credentials are in `.env`, add this under **Authorized redirect URIs** (not JavaScript origins), and save. Creating credentials alone does not register a callback. The scheme, hostname, port, path, case and trailing slash must match exactly. Once you switch to a public HTTPS BASE_URL, also register `https://YOUR-HOST/oauth/youtube/callback`.

The service requests `youtube.upload` for uploading and `youtube.readonly` to identify the selected channel, plus offline access and explicit consent to obtain a refresh token. Grant both scopes and select an account that has a YouTube channel. Google may impose consent-screen/testing/verification restrictions; refresh tokens can expire or be revoked. An API key alone cannot authorize uploads. `YOUTUBE_API_KEY` is intentionally unused.

### Instagram

1. Use a Business or Creator Instagram account and a Meta app configured for **Instagram API with Instagram Login**.
2. Put the **Instagram App ID / Instagram App Secret** from that setup in `INSTAGRAM_APP_ID` / `INSTAGRAM_APP_SECRET`. Facebook Login app credentials and deprecated Basic Display flows are different.
3. Configure `instagram_business_basic` and `instagram_business_content_publish`. Development accounts need the appropriate app/tester role and accepted invitations. Access to other users' accounts may require Meta review.
4. Register an exact public HTTPS callback such as `https://your-public-host/oauth/instagram/callback`. Use an HTTPS tunnel or hosted instance for local testing. Tunnel setup is external to this service; see [ngrok's official setup guide](https://ngrok.com/docs/start) for one option.
5. Set `BASE_URL=https://your-public-host` in `.env`, restart, and open the frontend through that same HTTPS host before authorizing. Register the corresponding HTTPS YouTube callback too if using that host for YouTube. Remove or update any old per-provider redirect overrides in `.env`.

In [Meta for Developers](https://developers.facebook.com/apps/), select your app's **Instagram → API setup with Instagram login → Business login settings**, and add the exact HTTPS callback under **OAuth redirect URIs**. The local HTTP Instagram URL is displayed for diagnosis in the setup guide, but the frontend prevents starting that invalid setup and disables its copy button until HTTPS is configured. The service can identify local configuration problems, but cannot inspect or automatically change the callback allowlist saved in Google or Meta.

Optional `YOUTUBE_REDIRECT_URI` and `INSTAGRAM_REDIRECT_URI` override the defaults. The host used for `/authorize` and the callback must be the same so the browser receives its OAuth cookie. Keep the callback paths `/oauth/youtube/callback` and `/oauth/instagram/callback`. Do not mix `localhost`, `127.0.0.1`, and a tunnel hostname within one flow.

Instagram uses a code → short-lived token → long-lived token exchange. Long-lived tokens typically last 60 days and can be refreshed when at least 24 hours old, before expiry. Instagram does not issue a Google-style refresh token. Provider expiry values are stored rather than hardcoded.

## Connect accounts

In `/docs`, execute `POST /oauth/youtube/authorize` or `POST /oauth/instagram/authorize`. Open the returned `authorization_url` in **the same browser** within 10 minutes. Sign in and consent; the callback saves encrypted tokens and returns account metadata and its local `id`.

OAuth state is random, hashed in SQLite, bound to an HttpOnly browser cookie, expires after 10 minutes, and is consumed atomically once. Google additionally uses PKCE S256. A callback does not require an API key because its state/cookie binds it to an authenticated start request. Starting with curl and then opening the URL in an unrelated browser will fail cookie verification.

Use `GET /accounts` to list connected accounts. Each account has its own local ID, separate from its provider `external_id`. Reconnecting the same provider account updates its tokens and preserves the local ID and existing refresh token when no replacement is returned.

## View channel analytics

Open **Analytics** in the workspace to choose a 7, 30, or 90 day period. YouTube shows channel totals, views, likes, comments, subscriber gains, and top videos from YouTube Analytics. Instagram shows likes and comments on media published during the selected period, ranked by interactions. Instagram reach and impressions are not included. Analytics are fetched from the connected providers and require an unlocked workspace; YouTube's Analytics API must be enabled in the Google Cloud project.

Access tokens refresh automatically **when used** near expiry (YouTube: two minutes, Instagram: one day). `POST /accounts/{id}/refresh` explicitly refreshes and saves the result. There is no background scheduler: an unused Instagram connection that expires must be reauthorized. A provider revocation or Google's `invalid_grant` also requires reconnecting.

`DELETE /accounts/{id}` removes local tokens and associated Instagram jobs. Revoke provider consent separately in your Google/Instagram account settings if needed.

## Upload a YouTube video

`POST /youtube/videos` accepts multipart form data. In Swagger, select your video file and supply the local YouTube account ID and title. It streams the spooled upload to a YouTube resumable session and returns the video ID/URL. Privacy defaults to `private`; choose `public` or `unlisted` explicitly.

The frontend's **Upload to YouTube** form offers the same flow: choose a connected channel, select a video, add its title and optional description, and choose visibility. The response includes a link to the uploaded video.

```powershell
$headers = @{ 'X-API-Key' = $env:SERVICE_API_KEY }
# PowerShell 7. Load the key from .env into your shell privately first.
Invoke-RestMethod -Method Post -Uri http://localhost:8000/youtube/videos -Headers $headers -Form @{
    account_id = 'LOCAL_YOUTUBE_ACCOUNT_ID'
    title = 'My first video'
    description = 'Uploaded with Relay'
    privacy_status = 'private'
    made_for_kids = 'false'
    file = Get-Item 'C:\Videos\clip.mp4'
}
```

Uploads default to a 2 GiB service limit (`MAX_UPLOAD_BYTES`). Files spool to temporary disk and are streamed in 1 MiB blocks. This first version uses the resumable protocol but does not persist sessions or resume interrupted transfers automatically. Check your YouTube Studio after a timeout before retrying, because the provider may have completed the upload. Shorts are uploaded as videos; YouTube determines classification. Community/text posts are not implemented.

## Publish an Instagram image or Reel

Meta fetches the media itself, so it needs a publicly accessible HTTPS URL (JPEG image, or MP4 video for Reels). The frontend can upload these files to a configured public-read Google Cloud Storage bucket and use its standard `https://storage.googleapis.com/...` URL. Configure `INSTAGRAM_MEDIA_BUCKET` and grant the service identity object creation access; the bucket needs public object viewer access so Meta can fetch the media. The API URL workflow below remains available for media hosted elsewhere.

The frontend's **Content** section uploads JPEG photos and video files to the media library. In **Publish**, select a saved item for Instagram or YouTube. Instagram media needs a publicly accessible HTTPS URL, so the configured bucket must grant public object viewer access. YouTube videos are read from the same bucket and streamed into YouTube's resumable upload. The deployment limit is 20 MiB; local uploads use `MAX_UPLOAD_BYTES`.

Instagram first sends the media to Meta for processing; that step does not publish the post. The **Publishing status** area shows each account as processing, ready but not live, publishing, or confirmed published. Processing statuses refresh automatically about once a minute. When a post is ready, select **Publish ready posts**; the row changes to **Published** only after Relay receives confirmation.

Open **History** to review the latest 200 posts published through Relay. Instagram posts are added after publishing completes; YouTube uploads are added after the upload succeeds. Previously published Instagram jobs are included when the service upgrades its local database. Disconnecting an account removes its local history records along with its saved connection.

1. `POST /instagram/posts` creates an **unpublished** container and saves its job in SQLite:

   ```json
   {
     "account_id": "LOCAL_INSTAGRAM_ACCOUNT_ID",
     "media_type": "REELS",
     "media_url": "https://your-cdn.example/clip.mp4",
     "caption": "Published with Relay"
   }
   ```

   Use `"media_type": "IMAGE"` and an image URL for a photo post.
2. Use the returned job `id` with `GET /instagram/posts/{id}`. Poll at most once per minute until `status` is `ready`; videos can take several minutes. `failed` / `expired` are terminal states.
3. `POST /instagram/posts/{id}/publish` checks readiness and publishes immediately, returning the saved `media_id`. Calling it again after success returns the saved result without publishing again.

The job atomically claims publication to prevent simultaneous requests from publishing twice. If publication fails ambiguously, its status becomes `unknown`: poll status to reconcile whether Meta published it and check Instagram before taking further action. The service blocks automatic retries of unknown jobs. A process crash during a publish may leave `publishing`; a later status check asks Meta to confirm it, and Relay records the post if Meta reports it as published. Images and Reels are supported; carousels and Stories are not implemented.

## API reference

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/health` | Local service liveness |
| POST | `/oauth/{provider}/authorize` | Generate browser-bound consent URL |
| GET | `/oauth/{provider}/callback` | Exchange authorization code and store tokens |
| GET | `/accounts` | List metadata, without tokens |
| POST | `/accounts/{id}/refresh` | Refresh stored authorization |
| DELETE | `/accounts/{id}` | Remove local connection |
| GET | `/analytics?days=30` | Fetch provider metrics for 7, 30, or 90 days |
| GET | `/posts` | List the latest 200 posts published through Relay |
| GET | `/content` | List saved media library items |
| POST | `/content` | Upload JPEG or video content to the media library |
| POST | `/youtube/videos` | Multipart video upload or saved content selection |
| POST | `/instagram/posts` | Create image/Reel container |
| GET | `/instagram/posts/{id}` | Fetch and persist container status |
| POST | `/instagram/posts/{id}/publish` | Publish ready container |

Account, OAuth-start, upload, and publishing operations require `X-API-Key` or an unlocked browser session. Cookie-authenticated mutations require an `Origin` matching `BASE_URL`, protecting against cross-site requests; header-authenticated API clients keep their original behavior. The frontend assets and `/session` status are public, but expose no service key, client secrets, or provider tokens. `POST /session` validates the service key to unlock, and `DELETE /session` locks the browser. Open the page on the exact configured `BASE_URL` so its origin and callback cookies match.

This is a **single-operator backend**, not a multi-tenant user-login system. Anyone holding the service key can access all connections. Bind locally by default. Before deploying publicly, add user authentication/account ownership, request rate limits, HTTPS and a reverse-proxy upload/body-size limit (especially for chunked requests), and use one worker: token-refresh locks are per process. Use a dedicated secret manager for deployed credentials. SQLite data is local and not a substitute for a backed-up production database.

Provider errors are sanitized instead of returning raw responses that may contain tokens. HTTP 409 usually means consent must be renewed or the resource is not ready; HTTP 502 means a provider request failed. No real uploads or account authorizations run during tests.

## Cloud Run deployment

Deployed in project `aitryout-485605`, Mumbai (`asia-south1`):
<https://relay-42733239266.asia-south1.run.app>

The service uses request-based billing, minimum **0** instances, maximum **1** instance,
0.25 vCPU, 256 MiB memory, concurrency 1, and one worker. Cold starts are expected.
The cloud upload limit is **20 MiB**, including a separate 1 MiB multipart allowance.
Cloud Run HTTP/1 request limits and in-memory temporary files make the local 2 GiB
upload default unsuitable here. Large video uploads need a different upload design.

The private `aitryout-485605-relay-data` bucket stores encrypted SQLite snapshots.
Each database transaction downloads the latest snapshot and uploads it only if changed,
with a generation precondition preventing stale revisions from overwriting newer data.
This is intended for a small single-operator workspace; keep concurrency and maximum
instances at one. Storage/API errors fail the request rather than silently losing writes.
Local runs continue to use local SQLite. Existing local connections are not migrated;
connect accounts on the deployed site.

The public `aitryout-485605-relay-media` bucket stores content uploaded from the
frontend. Objects are readable by anyone with the URL so Meta can retrieve Instagram media. YouTube uploads read saved video files from this bucket. The
`relay-run` service identity can create objects; the frontend receives standard HTTPS
`storage.googleapis.com` URLs. Local runs use ADC from the configured gcloud account.

Credentials load from Secret Manager `relay-config`, version 1, mounted at `/secrets/relay/.env`.
Unlock with your existing local `SERVICE_API_KEY`. No credentials, databases, or virtual
environments are included in the public repository or container. The dedicated runtime
identity can access only this app's secret and bucket through its assigned grants.

Register these exact callback URLs in the corresponding provider dashboards:

- Google: `https://relay-42733239266.asia-south1.run.app/oauth/youtube/callback`
- Meta: `https://relay-42733239266.asia-south1.run.app/oauth/instagram/callback`

Pushes to `main` automatically deploy the source to Cloud Run through
`.github/workflows/deploy-cloud-run.yml`. GitHub Actions authenticates with Google
Cloud using Workload Identity Federation; no service-account key is stored in GitHub.
The deploy identity is restricted to this repository's `main` push workflow. For a
manual deployment, run `./scripts/deploy.ps1` from the repository root. Storage, Secret
Manager, image storage, builds, requests, and network usage can incur charges even with
minimum instances zero.
No Cloud SQL, load balancer, scheduler, or always-on VM is provisioned.

## Run checks

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check relay tests scripts
node --test tests/test_oauth_popup.mjs
```

Provider references: [Google server-side OAuth](https://developers.google.com/youtube/data/guides/auth/server-side-web-apps), [YouTube uploads](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert), [YouTube resumable protocol](https://developers.google.com/youtube/data/guides/using_resumable_upload_protocol), [Meta Instagram Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login), [Meta content publishing](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/content-publishing), [Meta's official Postman collection](https://www.postman.com/meta/workspace/instagram/documentation/23987686-9386f468-7714-490f-9bfc-9442db5c8f00).
