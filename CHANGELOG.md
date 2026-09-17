# Changelog

All notable changes to this project will be documented in this file.

## [2.14.1] - 2026-09-17

### Performance & Optimization
- **Sticker Pre-Scaling Before Matting**:
  - Automatically downscale input images to max 512px *before* feeding them into `rembg` neural network matting.
  - Reduces CPU and RAM consumption by over 90% when users submit multi-megapixel smartphone photos (12–48 MP), cutting processing time down to under a second.
- **Lightweight Model Default (`u2netp`)**:
  - Switched default `rembg` model from the heavy `bria-rmbg-2.0` (~1.02 GB download) to `u2netp` (**only 4.7 MB**).
  - Configurable via `REMBG_MODEL` environment variable (defaults to `u2netp`).
- **Persistent Model Cache Storage**:
  - Configured `U2NET_HOME` and `REMBG_HOME` pointing to `/app/data/u2net` (`./data/u2net` on the host).
  - Models are downloaded exactly once and persist across container restarts, rebuilds, and updates.
- **Optional Background Removal (`ENABLE_REMBG`)**:
  - Background removal can be completely disabled for resource-constrained hosts via `ENABLE_REMBG=false`.
- **Global Concurrency Lock & Dedicated Cooldown**:
  - Added global concurrency lock (`_rembg_global_lock`) ensuring only one background removal runs at a time across the bot.
  - Split cooldowns: 5 seconds for `/sticker` and 15 seconds for `/stickernobg` with independent anti-spam tracking.

### Bug Fixes
- **Virus Command Attachment Detection**:
  - Added strict type checking (`isinstance(..., (str, int))`) for `view_type` and `download_state` to prevent false positive attachment detections in unit tests.

## [2.14.0] - 2026-09-17

### Features
- **Sticker Generation (`/sticker` & `/stickernobg`)**:
  - Convert any attached or replied image into a WebP sticker formatted to standard 512px dimensions compatible with Delta Chat, Telegram, and Signal.
  - `/sticker`: Generates a sticker preserving the original image and background.
  - `/stickernobg`: Generates a sticker with background removed via `rembg` (also accepts `/sticker nobg` or `/sticker --nobg`).
  - Supports invocation as a reply/quote to an image message or as the caption of a newly sent image.
  - Automatic EXIF orientation transpose (`ImageOps.exif_transpose`) to keep smartphone photos correctly oriented.
  - Non-blocking asynchronous worker thread with immediate `⏳` acknowledgment and `☑️`/`❌` reaction updates.
  - Independent 5-second anti-spam cooldown per chat with automatic delayed execution queuing.
  - Graceful degradation when `rembg` is not installed, recommending `/sticker` as a fallback.

### Dependencies & Containerization
- Added `rembg[cpu]>=2.0.50` to `requirements.txt` and relaxed `pillow>=10.4.0`.
- Added `libgomp1` to Debian package dependencies in `Dockerfile` for ONNX Runtime CPU inference support.

## [2.13.3] - 2026-09-17

### Tests
- **Fix `test_handle_background` assertion** (`test_webpreview.py`): replaced the stale `resp.path` attribute check (removed from `aiohttp.web.FileResponse`) with a type assertion on the response object.

## [2.13.2] - 2026-09-17

### Security & Hardening
- **Rate Limiting on All Public Web Endpoints** (`#5`):
  - Extended `@rate_limited` decorator coverage to every public-facing HTTP handler: `handle_icon`, `handle_background`, `handle_index`, `handle_qr_svg`, `handle_qr_png`, `handle_channel_qr_png`, `handle_channel_qr_svg`, `handle_channel_avatar`, `handle_channel_rss`, `handle_media_file`, `handle_ap_actor`, `handle_ap_outbox`, `handle_ap_followers`, `handle_ap_following`, `handle_ap_post`, `handle_webfinger`, `handle_nodeinfo_discovery`, `handle_nodeinfo`, `handle_api_v1_instance`.
  - Removed duplicate inline `check_rate_limit` calls from previously partially-protected handlers.
- **Trusted Proxy IP Extraction** (`#7`):
  - Added `_get_client_ip()` function: only honours `X-Forwarded-For` when the direct peer IP belongs to a trusted local reverse proxy (`127.0.0.1`, `::1`, `localhost`), preventing IP spoofing by external clients.
  - Added `_TRUSTED_PROXIES` set for configurable trusted proxy list.
- **ActivityPub Key Ownership Verification** (`#8`):
  - Added `is_key_owned_by_actor(key_id, key_doc, actor)` function in `activitypub.py` to verify that a resolved signing public key actually belongs to the claiming actor (via `owner`, embedded publicKey, actor `id`/`url` matching, or key URI fragment).
  - Integrated ownership check into `handle_ap_inbox()` after successful key resolution; rejects requests where the key's declared owner does not match the activity actor with HTTP 401.
- **Caddy X-Forwarded-Proto Header** (`#6`):
  - Added `header_up X-Forwarded-Proto {scheme}` to `Caddyfile` reverse proxy configuration so the upstream application can reliably detect HTTPS.

### Bounded Memory & SQL Queries
- **Bounded `_qr_cache`** (`#1`): Capped QR code cache at 200 items with FIFO eviction.
- **Bounded `_cmping_server_status`/`_cmping_server_errors`** (`#4`): Added `_set_cmping_server_status()` helper capping those dicts at 500 entries.
- **Bounded SQL Queries** (`#3`):
  - `get_all_cmping_results()` now adds `ORDER BY checked_at DESC LIMIT ?` (default 500).
  - `get_cmping_incident_downtime_events()` now adds `LIMIT ?` (default 500).

### Performance & Debug
- **Guarded Debug Logging** (`#2`): Wrapped hot-path `logger.debug()` calls with `logger.isEnabledFor(logging.DEBUG)` guards to avoid f-string evaluation overhead when debug is off.

### Web & Accessibility
- **QR Modal Focus Trap** (`#9`):
  - Added `openQrModal()` / `closeQrModal()` JS functions to both landing page and channel preview page that capture the opener button's focus, move focus into the modal on open, trap Tab/Shift-Tab within modal focusable elements, and restore focus on close.
  - Pressing Escape now correctly calls `closeQrModal()` and returns focus.
- **Inline Copy Feedback on QR Modal** (`#10`):
  - Replaced `alert('Link copied to clipboard!')` with inline `✓ Copied!` button-text feedback via new `copyJoinLink()` function; reverts after 2 seconds without blocking the page.
- **RSS Language Tag Removed** (`#13`):
  - Removed hardcoded `<language>en</language>` tag from RSS channel XML, since channel content language is user-determined.

## [2.13.1] - 2026-09-16

### Security & Hardening
- **Restricted Database File Permissions**:
  - Enforced POSIX `0o600` permissions on the primary SQLite database file and WAL sidecars (`-wal`, `-shm`) during database initialization and connection setup, protecting stored plaintext ActivityPub private keys and configuration data.
- **Strict Static Asset Whitelisting**:
  - Restricted `handle_icon` and `handle_background` to explicit filename allowlists (`_ALLOWED_ICON_FILENAMES`, `_ALLOWED_BG_FILENAMES`) to prevent unauthorized file access or path disclosure.
- **ActivityPub Foreign Target Inbox Validation & Backfill Rate Limiting**:
  - Enforced host matching between the follower actor URI and target delivery inbox URI in `handle_ap_inbox()`, rejecting delivery and backfill attempts to foreign/victim inboxes.
  - Added concurrency throttling (`asyncio.Semaphore(2)`) and a 10-second per-channel cooldown to follower post backfilling to prevent inbox bombing and outbound resource exhaustion.
- **RSS CDATA Breakout Prevention**:
  - Implemented `_escape_cdata()` to escape `]]>` sequences into `]]]]><![CDATA[>` across channel titles, descriptions, and post contents in RSS XML generation, preventing feed breakage and malformed XML.

### Bug Fixes & API Consistency
- **Mastodon/Pleroma `GET /api/v1/instance` Admin Email Resolution**:
  - Fixed configuration key lookup in `handle_api_v1_instance()` to check `admin_dc_email` prior to `admin_email`, accurately returning the bot administrator's contact email.

### Web & RSS Polish
- **Per-Post Permalinks & HTML Anchors**:
  - Added unique per-post permalink URIs (`<link>` and `<guid isPermaLink="true">` with `#post-{msg_id}`) to RSS 2.0 items and matching `id="post-{msg_id}"` anchor attributes to web preview articles for direct message linking.
- **Zero-Repaint Background Rendering**:
  - Replaced `background-attachment: fixed` with a fixed pseudo-element (`body::before`) across landing, preview, and error page templates, eliminating high scrolling repaint costs.
- **Responsive Layout & Accessibility Polish**:
  - Improved channel grid layout responsiveness on small viewports (<340px) using `minmax(min(280px, 100%), 1fr)`.
  - Upgraded `--text-muted` color to `#aebac1` achieving WCAG AA contrast ratio (>4.5:1).
  - Added `@media (prefers-color-scheme: light)` support for automated light theme rendering across landing and preview pages.

## [2.13.0] - 2026-09-16

### Security & Hardening
- **Environment File Gitignore (`S4`)**:
  - Added `.env` to `.gitignore` to prevent accidental credential, token, and API key exposure while retaining `.env.example`.
- **Host Header & Base-URL Poisoning Protection (`S5`)**:
  - Validated incoming `X-Forwarded-Host` and `Host` headers in `_get_base_url()` against strict URL scheme and hostname syntax rules (`SAFE_HOST_REGEX`), preventing cache and link poisoning when `BASE_URL` is unset.
  - Added `header_up Host {host}` and `header_up X-Forwarded-Host {host}` to `Caddyfile` reverse proxy configuration.
- **ActivityPub Replay Prevention & KeyId/Actor Binding (`S6`)**:
  - Implemented 300s TTL HTTP signature anti-replay cache (`check_and_record_signature_replay()`) in `activitypub.py` tracking signature digests.
  - Enforced strict origin matching between signing `keyId` URI and activity `actor` URI in `handle_ap_inbox()`, rejecting spoofed cross-origin signatures.

### UI & UX Improvements
- **Independent Command Cooldowns (`U5`)**:
  - Decoupled cooldown state tracking into separate dictionaries (`_chat_bounce_anti_spam`, `_chat_top_anti_spam`, `_chat_invite_anti_spam`) so executing `/bounce`, `/top`, or `/invite` never blocks another command.
- **Safe Bare `/away` Query (`U6`)**:
  - Invoking bare `/away` without arguments now safely displays the user's current away status or usage instructions without inadvertently clearing away state or sending `_is back_` notifications.
- **Community Multi-Chat Age Indicators (`U7`)**:
  - Replaced circle emojis with colored squares (`🟥🟧🟨🟩🟦🟪🟫⬛⬜`) for contacts who are active across multiple community chats, visually distinguishing experienced community members.
  - Streamlined new member welcome greetings by removing redundant `(💬 X)` tokens.
- **Social Media Metadata for Web Previews (`U8`)**:
  - Added OpenGraph (`og:title`, `og:description`, `og:image`, `og:type`) and Twitter Card (`twitter:card`) meta tags to the channel directory landing page.
- **Accurate RSS Enclosure Sizes (`U9`)**:
  - Resolved true file byte sizes on disk or in-memory buffers for `<enclosure length="...">` attributes in `generate_rss_xml()`, replacing placeholder zero values.
- **Accessible QR Code Modals (`U10`)**:
  - Added ARIA accessibility attributes (`role="dialog"`, `aria-modal="true"`, `aria-labelledby="qr-modal-title"`) and ESC key event listener to QR join modals across landing and channel preview pages.
- **Dynamic Fediverse Domain Resolution (`U11`)**:
  - Resolved Fediverse domain dynamically from server configuration without hardcoded `dc.gluek.info` fallbacks, hiding the Fediverse button gracefully if no public domain is configured.
- **Whole-Word Away Mention Matching (`U12`)**:
  - Replaced substring matching in group mention detection with word-boundary regex (`(?i)(?<!\w)name(?!\w)`), eliminating false positive away alerts (e.g. "Dan" matching "Daniel").
- **Command Documentation Clarifications (`U13`, `U14`)**:
  - Updated `/help` descriptions: `/contact<ID>` clearly indicates sharing a contact card, and `/relays` accurately reflects scanning for public Russian mail providers.
- **Role and Status Badges (`U15`)**:
  - Added visual user badges in `/search` and `/bounce` output: `👑` (Bot Administrator), `⭐` (Autokick Ignored), and `💤` (Away).

### Performance & Resource Optimization
- **Adaptive Polling Backoff (`P3`)**:
  - Implemented adaptive sleep intervals in background channel join and message resend workers (`bg_channel_join_worker`, `bg_resend_worker`), scaling sleep up to 30s when queues are idle.
- **Bounded Read Query Limits (`P4`)**:
  - Added default safety `limit` parameters to database read functions (`get_all_catalog_chats`, `get_all_catalog_channels`, `get_all_transport_stats`, `get_all_autokick_ignored_fingerprints`, `get_all_active_cmping_incidents`, `get_ap_followers`, `get_ap_follower_inboxes`).
- **In-Memory Cache Pruning (`P5`)**:
  - Bounded `_cmping_last_results` cache to 500 entries with automatic pruning of oldest entries.
  - Pruned server status and error records from `_cmping_server_status` and `_cmping_server_errors` when servers are removed via `/cmpingdel`.
- **Lazy Debug Log Formatting (`P6`)**:
  - Protected expensive key fingerprint formatting and admin lookup debug logging behind `logger.isEnabledFor(logging.DEBUG)` guards.

## [2.12.9] - 2026-09-16

### Performance & Scalability
- **SQLite Reader Connection Pooling (`P1`)**:
  - Implemented thread-safe LIFO read connection pool (`_ReaderConnectionPool`) and connection wrapper (`_PooledConnection`) with automatic pool invalidation on dynamic database switches.
  - Added `_reader_connection()` context manager and refactored all 46 read operations in `database.py` to reuse pooled connections.
  - Accelerated unit test suite execution from 33.0s down to 2.7s (>12x speedup) while guaranteeing concurrent reader isolation under SQLite WAL mode without thread contention.
  - Updated `close_db()` to safely drain and close pooled reader handles alongside the primary writer handle.
- **Non-Blocking Background Workers & Offloaded I/O (`P2`)**:
  - **VirusTotal Inspection (`/virus`)**: Decoupled message quote resolution and file attachment downloads (`_get_msg_file_info`) from the Delta Chat event handler thread into background daemon worker `bg_virus_worker`. Command handler responds immediately with `⏳` reaction (or synchronous usage message for empty invocations) without stalling incoming message processing.
  - **Channel Post Media Ingestion**: Offloaded `_ingest_channel_post` (file attachment downloading and Pillow WebP image compression) from `handle_all_messages` to a dedicated daemon thread, preventing media uploads from blocking the main Delta Chat event loop.
  - **CMPing Worker Concurrency Bounding**: Pinned `_run_cmping_subprocess` strictly to background worker thread and capped multi-server relay check thread pool (`ThreadPoolExecutor`) concurrency to `min(4, len(bot_domains))`, preventing system load and process spikes.
  - **Async-Safe QR Code Generation**: Wrapped synchronous Pillow and SVG QR generation calls (`_generate_qr_bytes`) in `handle_qr_svg`, `handle_qr_png`, `handle_channel_qr_png`, and `handle_channel_qr_svg` with `await asyncio.to_thread(...)`, keeping the aiohttp web server event loop responsive.

## [2.12.8] - 2026-09-16

### ActivityPub Federation & Resilience
- **RFC 9421 HTTP Message Signatures Support**:
  - Implemented `extract_signature_info()` and full verification for RFC 9421 signatures (`Signature-Input` + `Signature: sig1=...`), including `@method`, `@target-uri`, `@path`, `@authority`, `created` timestamp verification, and signature parameters base string construction.
  - Eliminated `Missing keyId in Signature header` errors when modern Fediverse instances (e.g. Mastodon 4.3+, 4.4-alpha) retry or send requests using RFC 9421.
- **Graceful Handling of Deletion Activities for Gone/Suspended Remote Actors**:
  - Prevented infinite retry loops and 401 spam when remote instances send `Delete(Actor)` for deleted or suspended accounts whose actor endpoints return HTTP 410 (Gone) or 403 (Forbidden).
  - Cleaned up matching follower records from SQLite database and responded with `202 Accepted` after validating that the deletion request and keyId share the same origin host.
  - Downranked expected actor lookup status codes (403, 404, 410) from `WARNING` to `INFO` in logging.
- **Follower Public Key Local Caching**:
  - Added `follower_public_key` storage in `ap_followers` database table with automatic schema migration.
  - Caches follower public key PEM upon initial `Follow` activity to allow immediate local cryptographic verification for subsequent requests (`Undo`, `Delete`) without requiring outbound network requests.

## [2.12.7] - 2026-09-16

### UI & UX Improvements
- **Documentation Accuracy (`U1`)**:
  - Updated `/help` cooldown description and `README.md` to reflect real command limits (60s shared group cooldown, 15s for `/cmping` and `/slap`, 10s for `/search`) instead of obsolete 10-minute claims.
- **Graceful Handling of Empty Channel Invite Links (`U2`)**:
  - Web preview channels without configured invite links now render a clean disabled button (`Invite Link Unavailable`) instead of a broken empty link (`<a href="">`).
  - Suppressed the "Show QR Code" button and join modal when no invite link exists, preventing broken QR image tags or copying empty strings.
  - Preserved RSS feed discoverability even when direct chat invites are disabled.
- **Landing Page QR Modal Fallback (`U3`)**:
  - When the bot's own invite link is not yet configured, the landing page hero section displays a disabled `Bot Link Unavailable` button.
  - The landing page QR modal displays an informative fallback message (`ℹ️ Bot invite link is not configured yet. Please check back later.`) rather than an empty modal box or broken image.
- **Non-Intrusive Command Cooldowns & Execution Queue (`U4`)**:
  - Replaced group-wide cooldown notification spam (`⌛️ Please wait ...`) with a silent `⏳` reaction on the caller's message.
  - Implemented asynchronous command queuing (`_queue_delayed_command`): commands triggered during an active cooldown are scheduled to run automatically as soon as the cooldown window expires.
  - Coalesced concurrent invocations from multiple members during cooldown so the command executes once when ready.
  - Upon successful delayed execution, reactions automatically update to `☑️` across all queued messages (or `❌` on error).

## [2.12.6] - 2026-09-16

### Security & Hardening
- **SSRF Mitigation on Remote Actor and Key Resolution (`S1`)**:
  - Added strict URL validation (`is_safe_url()`) enforcing `http`/`https` schemes and blocking private, loopback, link-local, multicast, and reserved IP addresses (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254`, `::1`, `::ffff:...`).
  - Blocked internal local hostnames (`localhost`, `*.local`, `*.internal`, `*.lan`, `*.home.arpa`) and resolved DNS queries to ensure target IPs are public before outbound requests.
  - Applied SSRF checks across `fetch_remote_actor()`, `resolve_public_key()`, and `deliver_to_inbox()`.
- **Pre-Resolution Fast Signature Validation (`S1`)**:
  - Added `validate_signature_cheap()` to reject forged or expired requests before attempting outbound network calls for remote `keyId` resolution.
  - Enforces `Date` header freshness window (±300 seconds) and verifies request body `Digest` (SHA-256) prior to actor fetching.
- **Request Body Limits & Rate Limiting (`S2`)**:
  - Configured `client_max_size = 256 KB` on `web.Application` to block oversized payloads across all web routes.
  - Enforced 64 KB maximum request body size on ActivityPub inboxes (`POST /c/{token}/inbox`, `POST /inbox`) returning `413 Payload Too Large`.
  - Implemented thread-safe sliding window rate limiter (`check_rate_limit`) per client IP (with `X-Forwarded-For` proxy support) returning `429 Too Many Requests` with `Retry-After: 60` headers:
    - ActivityPub inboxes: 60 requests / minute.
    - Web channel preview & landing page: 120 requests / minute.
    - Media file downloads: 120 requests / minute.
- **Dependency Version Pinning & Security Baselines (`S3`)**:
  - Pinned CVE-free dependency floors in `requirements.txt` (`aiohttp>=3.10.5,<4.0.0`, `qrcode>=7.4.2,<8.0.0`, `pillow>=10.4.0,<11.0.0`, `cryptography>=42.0.0,<45.0.0`).
  - Pinned `cmping` dependency to exact commit SHA `0b67259a8b057678b746797cc80fc1ee35bce9ae` (`v0.17.2`).

## [2.12.5] - 2026-09-16

### Fediverse / ActivityPub Improvements
- **Recent Posts Backfill on Follow**:
  - Automatically delivers up to 10 recent channel posts in chronological order (oldest to newest) to a new follower's inbox immediately after sending `Accept(Follow)`.
  - Ensures new followers on GoToSocial, Mastodon, and other Fediverse instances immediately see recent content in their timeline and profile view instead of an empty "Nothing to show" state.
- **Delta Chat Wallpaper as Profile Header Banner**:
  - Added `"image"` field to Actor JSON pointing to `/background.jpg` (`mediaType: "image/jpeg"`).
  - GoToSocial and Mastodon now display the Delta Chat wallpaper as the profile cover/banner image instead of a default solid gray box.
- **Standalone Note `@context`**:
  - Added `"@context": "https://www.w3.org/ns/activitystreams"` to ActivityStreams `Note` objects in `build_note()`.
  - Fixes direct note dereferencing (`GET /c/{token}/posts/{msg_id}`) and search bar lookups in GoToSocial which require valid JSON-LD context on standalone objects.
- **Mastodon-Compatible Instance Endpoint (`GET /api/v1/instance`)**:
  - Added `GET /api/v1/instance` (and `/{slash:/*}api/v1/instance`) returning Mastodon v1 instance metadata (domain URI, title, description, version, admin contact, thumbnail, channel count, total status count).
  - Allows GoToSocial's admin panel Instances Search to recognize and register `dc.gluek.info` with full metadata and stats.

## [2.12.4] - 2026-09-16

### Fediverse / ActivityPub Fixes
- **Outbox Pagination (`OrderedCollectionPage`)**:
  - Implemented W3C ActivityPub compliant pagination on `GET /c/{token}/outbox`. Root collection now includes `first` and `last` page links (`/c/{token}/outbox?page=true`).
  - Added `OrderedCollectionPage` response for paginated requests with `partOf`, `totalItems`, and `orderedItems`. Resolves "Nothing to show" on GoToSocial and Mastodon profile views so recent toots are properly dereferenced and displayed on the profile page.
- **Trailing Slash Sanitization & Multi-Slash Route Normalization**:
  - Automatically strip trailing slashes and whitespace from `BASE_URL` and `database.get_config("base_url")` across `_ingest_channel_post`, `build_actor_json`, `build_note`, and `_deliver_post` to prevent double slashes in actor URLs (`//c/{token}`) and public key identifiers.
  - Added multi-slash route patterns (`/{slash:/*}c/{token}`, `/{slash:/*}c/{token}/actor`, `/{slash:/*}inbox`, etc.) so that requests with leading multiple slashes (e.g. `////c/{token}`) sent by reverse proxies or Go HTTP clients resolve with 200 OK instead of failing with 404.

## [2.12.3] - 2026-09-16

### Fediverse / ActivityPub Federation
- **Shared Inbox Support (`POST /inbox`)**:
  - Added route `POST /inbox` to receive activities sent to the instance-level `sharedInbox` advertised in Actor JSON.
  - Automatically extracts target channel token from the incoming activity's `object` / `target` properties (supporting `Follow`, `Undo`, and `Delete`).
- **Following Endpoint (`GET /c/{token}/following`)**:
  - Implemented `GET /c/{token}/following` returning an empty `OrderedCollection` (`totalItems: 0`) as requested by GoToSocial and other Fediverse servers during actor profile discovery.
- **Robust Remote Key Resolution & Authorized Fetch**:
  - Added `resolve_public_key` with support for standalone PublicKey endpoints (like GoToSocial `/main-key`) and embedded `publicKey` objects (like Mastodon `#main-key`).
  - Added support for Authorized Fetch (HTTP Signatures on GET) when resolving remote follower profiles to obtain their personal `inbox` and `sharedInbox`.
  - Addressed `Accept(Follow)` activity directly with `to: [follower_id]` and delivered to the follower's inbox.
  - Supported SHA-512 in HTTP signature verification alongside SHA-256.

## [2.12.2] - 2026-09-15

### Web Preview & Fediverse UI
- **Fediverse Handle Tag with 1-Click Copy in Web Preview Header**:
  - Replaced duplicate `📡 RSS Feed` link in the top-right header with an interactive Fediverse channel tag (`@<token>@<domain>`, e.g., `@twniAE9eNajd@dc.gluek.info`).
  - Clicking the tag copies the handle to clipboard and provides immediate visual feedback (`✓ Copied!`), allowing users to easily paste the handle into their Mastodon or Fediverse search bar to follow the channel.
  - The dedicated `📡 RSS Feed` action button remains in the main channel action row.

## [2.12.1] - 2026-09-15

### Bug Fixes
- **Python 3.11 Module-Level Type Annotation Fix**:
  - Added `from __future__ import annotations` to `activitypub.py`.
  - Removed evaluated string-union annotations on private module globals (`_http_session`, `_delivery_queue`, `_web_loop`) to resolve `TypeError: unsupported operand type(s) for |: 'str' and 'NoneType'` when starting the bot in Docker (Python 3.11).

## [2.12.0] - 2026-09-15

### Fediverse / ActivityPub Federation
- **Full Fediverse Channel Federation (`@<token>@<domain>`)**:
  - Every cataloged Delta Chat channel functions as an autonomous Fediverse actor of type `Service` (displaying the 🤖 Bot badge on Mastodon and compatible platforms).
  - Users on Mastodon, Pleroma, Misskey, Friendica, and other Fediverse instances can discover channels using standard WebFinger queries (`RFC 7033`) via `GET /.well-known/webfinger?resource=acct:<token>@<domain>`.
  - Channel preview URLs (`/c/{token}`) implement standard Content Negotiation: requests with `Accept: application/activity+json` or `application/ld+json` return the full ActivityStreams 2.0 Actor representation with public key, avatar icon, inbox, outbox, and followers collection endpoints.
- **Cryptographic HTTP Signatures (`draft-cavage-http-signatures`)**:
  - Independent RSA-2048 keypairs generated per channel actor and persisted in the SQLite database (`ap_actor_keys`).
  - Strict verification of incoming `Signature`, `Digest` (SHA-256), and `Date` (±300s window) headers on Actor inboxes to guard against replay and spoofing attacks.
  - Automatic outgoing request signing for deliveries and `Accept(Follow)` notifications.
- **Federated Follower Management & Outbox**:
  - `POST /c/{token}/inbox` handles `Follow` activities by recording the follower in `ap_followers` and asynchronously returning a cryptographically signed `Accept` activity to the follower's inbox.
  - Handles `Undo(Follow)` and `Delete(Actor)` events to keep follower rosters clean and GDPR-compliant.
  - `GET /c/{token}/outbox` serves an `OrderedCollection` of recent notes; `GET /c/{token}/followers` reports total follower count.
  - `GET /c/{token}/posts/{msg_id}` returns individual Note objects.
- **Asynchronous Background Delivery Pipeline**:
  - New channel posts ingested via `_ingest_channel_post` are automatically queued and broadcast as `Create(Note)` activities to remote follower inboxes with deduplicated `sharedInbox` delivery.
  - Rich text formatting with autolinked URLs and media enclosures (WebP images, MP4 videos, audio, and documents).
- **GoToSocial-Modeled `robots.txt` & NodeInfo 2.0**:
  - Replaced simple disallow-all `robots.txt` with a comprehensive, GoToSocial-modeled configuration blocking abusive AI scrapers and SEO crawlers while permitting channel previews (`/c/`) and media (`/media/`).
  - Added NodeInfo 2.0 discovery (`/.well-known/nodeinfo` and `/nodeinfo/2.0`) for discovery by Fediverse crawlers and directory indexers.

## [2.11.6] - 2026-09-15

### Channel Catalog & Privacy
- **Unlisted Channels by Default (`/dchanneladd`)**:
  - Channels added via `/dchanneladd <URL>` are now initialized in **unlisted** mode by default (`is_public = 0`).
  - Unlisted channels retain their own unique web preview page (`/c/{token}`) and standard RSS feed (`/c/{token}/rss.xml`), and the bot ingests new messages and attachments continuously.
  - Excluded from public directory listings: unlisted channels are hidden from the bot's web landing page (`/` and `/c/`) and the public `/dchannels` command in group chats and for non-admin users.
- **Dynamic Catalog Visibility Toggles (`/dchannelpub<ID>on` & `/dchannelpub<ID>off`)**:
  - Added administrative commands to toggle channels between public and unlisted at any time.
  - Toggling public status automatically updates the catalog and invalidates web preview and landing page caches immediately.
  - `/dchanneladd` confirmation message now directly provides the preview link, RSS link, and suggests `/dchannelpub<ID>on` if the administrator wishes to make the channel publicly discoverable.
- **Admin Direct Message Catalog Visibility**:
  - When the bot administrator queries `/dchannels` in a private 1-on-1 direct message, all registered channels are shown, with unlisted channels clearly marked with `🔒 [Unlisted]` and quick-action `/dchannelpub<ID>on` command links.
  - In group chats or when queried by non-admin members, only public channels are displayed to prevent leakage.
  - Direct queries via `/dchannel<ID>` for unlisted channels are restricted to bot administrators, preventing numeric ID enumeration by regular users.

## [2.11.5] - 2026-09-15

### Web Preview & Landing Redesign
- **Authentic Delta Chat Styling & System Typography**:
  - Replaced generic AI-generated neon gradients and external Google Fonts (`Outfit`) with clean, privacy-respecting native system typography (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif`) inspired by [gluek.info](https://gluek.info).
  - Integrated official Delta Chat dark wallpaper pattern with doodle envelopes, speech bubbles, and symbols on `#19232b`, served with immutable caching via `/background.jpg` and `/static/background.jpg`.
  - Formatted channel posts into authentic Delta Chat messenger message bubbles (`#232d36`, rounded 12px) with sender names highlighted in Delta Chat blue (`#53bdeb`), quotes styled with vertical accent bars, monospace code blocks, and bottom-right timestamps with checkmarks (`✓`).
  - Unified aesthetics across the channel catalog landing page, channel preview view, tombstone, and 404 pages with zero third-party font tracking.

## [2.11.4] - 2026-09-15

### Channel Catalog Automation
- **Automatic Channel Removal on Bot Ejection**:
  - Automatically detects when the bot is removed from a channel by the channel owner/admin (via `SystemMessageType.MEMBER_REMOVED_FROM_GROUP`, self contact ID `1`, or system removal messages).
  - Soft-deletes the channel from the public catalog (`/dchannels`, `/c/`), invalidates preview and RSS caches immediately, and serves a graceful tombstone message on preview URLs without attempting to re-join.
  - Automatically keeps catalog channel member counts synchronized on member join/leave events.

## [2.11.3] - 2026-09-15

### Media Optimization & Post Cleanup
- **Automatic WebP Image Compression**:
  - Attached images (`.jpg`, `.jpeg`, `.png`, `.bmp`) in channel posts are automatically compressed to modern WebP format (`quality=80`, `method=3`, `max_dim=1600`) via Pillow on ingestion.
  - Reduces media traffic by 70–90% for web preview pages and RSS feed readers.
  - Animated GIFs and SVGs are preserved in their native formats.
  - Graceful fallback to original image format and file serving if compression fails or Pillow is unavailable.
  - Backwards-compatible resolution in `handle_media_file` allows requests for `.jpg`/`.png` to seamlessly serve `.webp` when available.
- **Delta Chat Attachment Fallback Stripping (`DC_FALLBACK_PATTERN`)**:
  - Automatically filters out Delta Chat core email fallback placeholders (e.g. `[Image – 304.26 KiB]`, `[Document - file.pdf]`) from message text, preventing redundant metadata labels from cluttering post previews when attachments are displayed natively while preserving authentic author text.

## [2.11.2] - 2026-09-15

### Improvements & UI
- **Spacious `/dchannels` Catalog Layout**:
  - Re-architected `/dchannels` output with a clear multi-line format separating command/title, description, and preview link onto distinct lines.
  - Channels are now cleanly delineated with blank lines (`\n\n`) for optimal readability in Delta Chat mobile and desktop clients.
- **Forgejo Mirror in Channel Previews**:
  - Added the Forgejo repository mirror link (`https://git.gluek.info/gluek/deltachat_bouncer`) alongside version number in the footer of public channel preview pages (`/c/{token}`).

## [2.11.1] - 2026-09-15

### Web Service & Channel Previews
- **Rich Markdown Post Formatting (`format_markdown_html`)**:
  - Full CommonMark and Delta Chat formatting engine with support for bold (`**text**`, `__text__`), italic (`*text*`, `_text_`), strikethrough (`~~text~~`), inline code (`` `code` ``), fenced code blocks with syntax styling (` ```lang ... ``` `), blockquotes (`> text`), spoilers (`||spoiler||` with click-to-reveal), and links (`[label](url)` and autolinked URLs).
  - 100% XSS immunity via strict HTML escaping and javascript/data URI protocol stripping.
  - Channel preview post timeline and RSS feed item descriptions (`<![CDATA[ ... ]]>`) now render rich markdown identically to ArcaneChat / Delta Chat clients.
- **In-Memory Caching Layer & High Load Optimization**:
  - Added fast in-memory response caches with TTL and ETag headers for channel web preview pages (60s TTL) and RSS feeds (120s TTL), returning `304 Not Modified` on matching `If-None-Match`.
  - Added in-memory binary caching for QR code generation (SVG and PNG) to eliminate repeated CPU-bound QR rendering.
  - Event-driven cache invalidation hooks automatically purge channel caches on new incoming posts (`_ingest_channel_post`), channel removals (`/dchannelremove`), or base URL changes (`/url`).
- **Crawler & Bot Blocking (`robots.txt`)**:
  - Configured `robots.txt` (`/robots.txt`) with `User-agent: *\nDisallow: /\n` to block search engine scrapers and AI crawlers from generating unnecessary traffic.

## [2.11.0] - 2026-09-15

### Web Service & Channel Previews
- **Public Channel Web Preview (`/c/{token}`)**:
  - Implemented an embedded, lightweight aiohttp web server providing instant web preview pages for channels registered in `/dchannels`.
  - Each channel is assigned a unique, unguessable 12-character base62 token.
  - Channel preview pages feature a modern dark-mode aesthetic (matching Uptime Bot), displaying channel name, description, member count, avatar, and a live message timeline.
  - Interactive Delta Chat join dialog with dynamic QR code generation (SVG and PNG download options), deep links (`https://i.delta.chat/#...` and `OPEN-CHAT:...`), and one-click clipboard copying.
  - Media file support: attached photos, videos, voice notes, audio, and documents are displayed inline and served directly from a dedicated media cache directory (`CHANNEL_MEDIA_DIR`).
- **Standard RSS 2.0 Feeds (`/c/{token}/rss.xml`)**:
  - Full-fidelity RSS feed at `/c/{token}/rss.xml` (with convenient `/c/{token}/rss` redirect).
  - Includes channel metadata, item timestamps (RFC 822), author names, message contents, and media enclosures for RSS readers.
- **Graceful Channel Removal & Tombstone**:
  - Channels removed from the catalog via `/dchannelremove` are soft-deleted (`is_deleted = 1`).
  - Accessing a removed channel's preview URL displays a clean tombstone notice (*"The channel has been removed from the public catalog and is no longer available for preview."*) instead of a generic 404.
- **Core Handshake Backfill**:
  - Automatically backfills the initial batch of up to 10 messages provided by the Delta Chat core handshake upon joining via `/dchanneladd <url>`.
  - Real-time ingestion stores subsequent incoming channel messages up to a sliding window of 100 posts per channel.
- **Base Web URL Configuration (`/url`)**:
  - Added `/url [url]` admin command to view or configure the public base URL stored in database settings.
  - Supports fallback to `BASE_URL` environment variable.
  - Updated `/dchannels` and `/dchannel<ID>` commands to provide preview URLs alongside join links.
- **Landing Page (`/`)**:
  - Public landing page introducing Bouncer Bot capabilities, active channel counts, and Delta Chat connection instructions.

## [2.10.1] - 2026-09-10

### Improvements
- **In-Place Live Message Updates for VirusTotal Scans (`send_edit_request`)**:
  - When submitting a new URL or file for analysis (404 in VirusTotal database), the bot now posts an immediate interim status message (`Analysis is in progress...`) and sets the `⏳` reaction.
  - The worker polls VirusTotal in the background for up to 12 attempts (~3 minutes with the 15-second rate limiter).
  - Upon completion, the bot updates the interim message in-place via Delta Chat RPC `send_edit_request` with the complete vendor detections and analysis report, and sets the final reaction (`☑️`, `⚠️`, `🚨`).
  - If analysis exceeds 12 polling attempts, the message is updated with a timeout notice directing the user to the web report (`⚪️`).
- **Clean Markdown URL Formatting**:
  - Removed enclosing backticks around URLs in reports to prevent Delta Chat markdown parsers from appending `%60` to links.

## [2.10.0] - 2026-09-10

### Security & VirusTotal Inspection
- **VirusTotal Link & File Inspection (`/virus`)**:
  - Added `/virus <url>` command to inspect links for phishing, malware, and threats via VirusTotal API v3.
  - Added reply inspection: replying to a message with `/virus` automatically detects and scans either an attached file or the first URL in the quoted message.
  - Added direct file inspection: sending a message with an attached file and `/virus` caption triggers a file scan.
  - **On-Demand Attachment Download**: Automatically downloads full message attachments via Delta Chat RPC (`download_full_message`) when messages arrive without auto-downloaded blobs (`download_limit=1`).
  - **Hash-First Querying**: Computes SHA-256 locally and checks existing VirusTotal reports first, avoiding redundant file uploads and conserving bandwidth.
  - **Direct Upload Fallback**: If a file (up to 32 MB) is not present in the VirusTotal database, it is uploaded via multipart/form-data and polled until analysis completes.
  - **Global Rate Limiting & Queueing**: Enforces a strict 1-check-per-15-seconds rate limit across the entire bot to safely respect VirusTotal free tier limits (4 lookups/min). Additional incoming requests receive a queue notification (`⏳ Another VirusTotal check is in progress, your request is queued...`) and are processed in FIFO order.
  - **Visual Progress & Reactions**: Sets `⏳` reaction on trigger message during queuing and scanning, updating to `☑️` (clean), `⚠️` (suspicious), `🚨` (malicious), or `❌` (error) upon completion.
  - Configurable via `VIRUSTOTAL_API_KEY` in `.env` or container environment.

## [2.9.3] - 2026-09-10

### User Experience & Command Reporting
- **Transparent Observation Progress in `/bounce`**:
  - In group chats with `/autokick` enabled, `/bounce` now distinguishes between truly active members and silent members under observation.
  - When members have not reached the warning threshold yet, `/bounce` displays an **Observation in progress** status reporting the number of silent members and the countdown to the earliest warning window (e.g. at day 83 of observation) instead of misleadingly claiming all users are active.
  - Added total member count to the all-active report when every member has verified recent activity.
  - Added observation summary note to the warning report when additional silent members remain under observation.
- **Informative Metrics in `/autokick` Status**:
  - Displays the number of days the bot has monitored the group.
  - Displays the count of silent members under observation and the countdown until their earliest warning.
  - Reports the count of members currently in the warning window (< 7d to kick).
- **Accurate Candidate Reason Formatting**:
  - Updated candidate descriptions from `never seen in Xd since joined` to `never seen in Xd of observation`, reflecting actual observation time rather than join time.
- **Autokick Overview Helper (`_get_chat_autokick_overview`)**:
  - Unified autokick metrics collection and candidate evaluation into a single-pass helper reused across `/bounce`, `/autokick`, and background monitor routines.

## [2.9.2] - 2026-09-10

### Performance & Database Architecture
- **Persistent Writer Connection**:
  - Replaced ad-hoc connection creation on every write with a dedicated persistent writer connection protected by `_write_lock` and standard transactional context manager (`_writer_transaction()`), eliminating connection teardown churn and disk sync overhead.
- **Strict `PRAGMA synchronous = NORMAL` & `busy_timeout`**:
  - Enforced `PRAGMA synchronous = NORMAL;`, `PRAGMA journal_mode = WAL;`, and `PRAGMA busy_timeout = 5000;` on all database connections (both readers and writer) across the entire codebase.
- **Concurrent Non-Blocking WAL Reads**:
  - Replaced the global `_lock` on read operations with independent read connections, allowing concurrent readers to execute simultaneously alongside the writer without lock contention.
- **Batch Contact Seeding**:
  - Replaced N+1 individual insert queries in catalog member count refresh (`_refresh_catalog_member_counts`) with `ensure_contacts_first_seen_batch()`, recording contacts in a single atomic transaction.

## [2.9.1] - 2026-09-09

### Security Hardening & Concurrency Fixes
- **Fix Undefined `resilient_lock`**:
  - Declared `resilient_lock` globally, fixing runtime `NameError` crash during resilient sending and message failover.
  - Synchronized transport switching and message sending during resilient delivery.
- **Server Domain Input Validation (`/cmping`)**:
  - Added strict regex domain validation (`DOMAIN_REGEX`) to `/cmping`, `/cmpingadd`, and `/cmpingdel` commands to prevent command injection and malformed subprocess parameters.
- **Credential Protection (`/addtransport`)**:
  - Enforced that `/addtransport` can only be executed in private 1-on-1 chats with the bot, preventing accidental password leakage in group chats.
- **Rate Limiting (`/slap`)**:
  - Added 15-second cooldown per chat for `/slap` commands.
- **Sanitized User Error Messages**:
  - Removed internal exception and traceback disclosures from user-facing error messages in `/kick`, `/invite`, `/relays`, `/approve`, `/decline`, and `/addtransport`.
- **Bounded Message Deduplication**:
  - Replaced full set clearing with an `OrderedDict` maintaining up to 2000 recent message IDs with FIFO eviction, preventing duplicate command execution.

### High-Load & Database Optimizations
- **SQLite WAL & Concurrency PRAGMAs**:
  - Enabled `PRAGMA journal_mode = WAL`, `PRAGMA synchronous = NORMAL`, `cache_size = -4000`, and `busy_timeout = 5000` for high concurrency and performance under load.
- **Buffered Transport Statistics**:
  - Buffered sent/received message statistics in memory, flushed to SQLite in a single transaction every 30 seconds instead of executing database writes on every individual message.
- **Database Indexes**:
  - Added indexes on `autokick_warnings(chat_id)`, `pending_requests(chat_id, approved)`, `away_notifications(away_updated_at)`, and `cmping_history(checked_at)`.
- **Automated Data Pruning**:
  - Added periodic pruning in background monitor loop for `away_notifications` and `cmping_history` older than 30 days.
  - Added periodic in-memory cleanup of stale anti-spam timestamps and unused domain locks.

## [2.9.0] - 2026-09-09

### Added
- **Two-Stage Inactivity Warning System for `/autokick`:**
  - Inactive members now receive a direct 1-on-1 private notification from the bot before being kicked, warning them of impending removal and inviting them to stay by posting in the group.
  - The group chat receives a daily summary broadcast (at most once every 24 hours) listing all members in the warning zone with days remaining until removal.
  - **Kick Protection:** Members are now strictly kicked only after receiving a warning and passing a minimum 24-hour grace period.
  - Dynamic warning threshold: warnings begin at `autokick_days - 7` days for thresholds $> 7$ days (i.e. 7 days before kick), and at `autokick_days - 1` days for thresholds $\le 7$ days.
  - Warnings are automatically cleared as soon as a user sends a message in any shared chat.
- **Cryptographic Fingerprint Ignore List (`/autokick ignore`):**
  - Added `/autokick ignore <email/nick/id>` for bot administrators to look up a member, extract their cryptographic key fingerprint, and permanently exempt them (e.g. service bots or quiet system accounts) from auto-kick.
  - Added `/autokick unignore <fingerprint/email/nick>` to remove exemptions.
  - Added `/autokick ignore` / `/autokick ignore list` to display all currently exempted fingerprints and notes.
- **Exemption for `/away` Users:**
  - Members with an active `/away` vacation/absence status are automatically exempt from inactivity warnings and auto-kicks.
- **`/bounce` Integration with `/autokick` Thresholds:**
  - In group chats where `/autokick` is active, `/bounce` now shows members currently in the warning window (< 7 days or < 1 day remaining until auto-kick).
  - In groups where `/autokick` is disabled, `/bounce` continues to report inactivity based on the default 21-day threshold.
- **Grace Period Protection for New Members:**
  - Utilizes `contact_first_seen` tracking so newly joined members who have not yet spoken (`last_seen == 0`) are protected by a grace period based on their join time, preventing premature autokicking.
- **Database Schema Upgrades:**
  - Added `last_autokick_warn_at` column to `chats` table.
  - Added `autokick_warnings` and `autokick_ignored_fingerprints` tables.

## [2.8.2] - 2026-09-07

### Added
- **Options.json Fallback for Display Name & Status Text**:
  - `on_init` now checks `/data/options.json` fallback when `DISPLAY_NAME` or `STATUS_TEXT` are not set in environment variables.

## [2.8.1] - 2026-08-24

### Added
- **Reopen Resolved CMPing Incidents on Flapping (1-Hour Window):**
  - If a server recovers but goes down again within **1 hour (3600s)** of its resolution, the bot reopens the existing incident instead of creating new ones.
  - The alert message is edited in-place from `Resolved` back to `Ongoing`.
- **Database Schema Migration Safety:**
  - Placed index creations after column migrations to guarantee error-free upgrades on legacy databases.

## [2.8.0] - 2026-08-24

### Added
- **1-Hour CMPing Incident Clustering Window:**
  - Outage events occurring within **1 hour (3600s)** of prior failures are grouped into the ongoing incident.
  - Server outages occurring **more than 1 hour** after previous failures spawn a **brand new CMPing incident** with a fresh alert message.
  - Each incident tracks and resolves its specific affected servers independently.

## [2.7.1] - 2026-08-20

### Added
- **Concise Resolved Incident Messages:**
  - Upon incident resolution, the updated message now displays only the specific servers that experienced downtime during the incident (`Recovered Servers:`).
- **Tiered Rate-Limiting for Live CMPing Incident Message Edits:**
  - Standardized progressive duration-based throttling for live incident updates:
    - **First minute (< 60s):** updates at most once every **15 seconds**.
    - **1 to 5 minutes (60s – 300s):** updates at most once every **30 seconds**.
    - **5 minutes to 1 hour (300s – 3600s):** updates at most once every **1 minute**.
    - **1 to 24 hours (3600s – 86400s):** updates at most once every **5 minutes**.
    - **Over 24 hours (> 86400s):** updates at most once every **1 hour**.
  - Server status transitions (failures, recoveries, domain deletions, resolutions) continue to trigger immediate in-place edits with zero delay.

## [2.7.0] - 2026-08-19

### Added
- **Automatic Inactive Member Auto-Kick (`/autokick`):** Added background member cleanup for inactive group participants. Disabled by default, configurable by bot administrators per group:
  - `/autokick` / `/autokick status`: Check current auto-kick status and threshold.
  - `/autokick on`: Enable auto-kick with default 90-day inactivity threshold.
  - `/autokick <days>`: Enable auto-kick with a custom threshold in days (e.g. `/autokick 30`).
  - `/autokick off`: Disable auto-kick.
  - Automatically purges inactive members (> configured threshold) during background monitor cycles with grace period observation for never-seen members.
- **Manual Member Kick (`/kick`):** Added `/kick <user_id>` command for bot administrators in group chats:
  - Supports contact ID numbers (e.g. `/kick 123`), `/contact123` formatted links, email/name queries, multiple IDs, and quoting/replying to messages.
  - Prevents kicking the bot itself or bot administrators.
- **Persistent Autokick Configuration:** Added `autokick_days` column to the `chats` table to persist autokick settings across restarts.

### Changed
- **Updated `/bounce` Inactivity Threshold to 21 Days:** Increased the inactivity detection threshold from 14 days to 21 days (the typical mail server retention window for unretrieved messages) and aligned grace periods and report calculations.

### Added
- **Affected Server Breakdown in `/cmpingevents`:** Enhanced `/cmpingevents` (and `/cmevents` / `/cmpingincidents`) to display all currently failing servers and their error reasons directly in the incident log for ongoing incidents.
- **Incident Detail Lookup by ID (`/cmpingevents <id>`):** Added support for inspecting a specific incident by ID, displaying real-time outage details (if ongoing) or historical outage timeline and error reasons for all affected servers (if resolved).
- **Incident Resolution Summary:** Saved the list of affected servers into the incident record upon resolution for quick log inspection.

## [2.6.0] - 2026-08-19

### Added
- **CMPing Incident-Based Alerting:** Replaced noisy individual `🔴 UNHEALTHY` / `🟢 HEALTHY` alert messages with an incident-based lifecycle. When monitored servers experience connectivity failures, the bot opens a single ongoing incident (`🚨 CMPing Incident #X — Ongoing`) per subscribed report chat.
- **In-Place Dynamic Message Editing:** Incident messages are dynamically updated in-place via `send_edit_request` to reflect ongoing failures, partial recoveries (`⚠️ Ongoing (Partial Recovery)`), and complete resolution (`✅ Resolved`) with total outage duration.
- **Root-Cause Failure Isolation:** When a source server fails connecting with all peer targets in a check cycle (e.g. source host network partition or DNS failure), the bot attributes the outage to the source node rather than mislabeling all healthy target peers as down.
- **Incident & History Commands:**
  - Added `/cmpingevents` (aliases: `/cmpingincidents`, `/cmevents`) to view recent CMPing incidents and active outage status.
  - Added `/cmpinghistory [server]` (alias: `/cmhistory`) to inspect downtime logs, error reasons, and outage durations for monitored servers.
- **Downtime Events Persistence:** Added `cmping_downtime_events`, `cmping_incidents`, and `cmping_incident_messages` database tables for persistent outage tracking.
- **Unit Test Suite:** Added unit test suite in `tests/test_bouncer.py` covering incident operations, dynamic message editing, root-cause isolation, and commands.

## [2.5.18] - 2026-07-23

### Added
- **Startup Version Check & Logging:** Added version detection and startup logging for Bouncer Bot, DeltaChat Core (`deltachat_core_version`), RPC Client, `deltabot-cli`, and `cmping` packages.

## [2.5.17] - 2026-07-20

### Added
- **Server Filter and Chronological Sorting for `/cmpingstatus`:** Added an optional `[server]` argument to filter CMPing status results (partial match) and sorted the output lines chronologically from newest to oldest pings.
- **Public CMPing Read Commands:** Removed the admin-only restriction from `/cmpinglist`, `/cmpingstatus`, and `/cmpingfail` (and `/cmfaillist` alias), making monitoring status visible to all group members, while keeping write/configuration commands strictly administrative.

## [2.5.16] - 2026-07-19

### Changed
- **Use mrgluek-cmping fork:** Updated `requirements.txt` to install the `cmping` dependency from `mrgluek/cmping` instead of `chatmail/cmping`, allowing the bot to benefit from registration fallback improvements and correct `dclogin` overrides.

## [2.5.15] - 2026-07-06

### Fixed
- **Fix Dependency Conflict/NameError:** Pinned `deltabot-cli==8.1.2` and `deltachat2[full]<1.0.0` in `requirements.txt` to resolve dependency conflicts and avoid the `ChatType` NameError/ImportError bugs introduced in newer, incompatible versions of `deltachat2`.

## [2.5.14] - 2026-07-03

### Fixed
- **Zombie Process Reaping:** Enabled `init: true` in Docker Compose to automatically reap zombie `deltachat-rpc-server` processes spawned by `cmping` checks, preventing PID limit exhaustion (`RuntimeError: can't start new thread`).

## [2.5.13] - 2026-06-30

### Fixed
- **Ignore Local Thread/System Errors in Monitoring:** Prevent local thread spawning and system resource limits (like `RuntimeError: can't start new thread`) from marking monitored mail domains as unhealthy.

## [2.5.12] - 2026-06-25

### Changed
- **Bidirectional Suffix Matching:** Suffix matching is now bidirectional (e.g. `@w` or `@webpreview` will match WebPreview bot).
- **Smart Group Chat Command Filtering:** The bot now automatically ignores unaddressed general `/help` and `/stats` commands in group chats if other bots are present in the chat.

## [2.5.11] - 2026-06-25

### Added
- **Target-Specific Command Suffixes:** Added support for addressing this bot specifically in group chats using `/command@boun` or `/command@stew` suffixes.

## [2.5.10] - 2026-06-23

### Changed
- **Log Level Adjustment:** Changed the log level of custom message processing log lines from `INFO` to `DEBUG` to keep standard runtime logs cleaner.

## [2.5.9] - 2026-06-23

### Fixed
- **Local Join Event Event Loop Fix:** In newer versions of the `deltachat2` library, the event loop does not process `MSGS_CHANGED` events to dispatch new messages. Locally generated system messages (like when the bot completes the securejoin handshake and adds a member itself) only emit `MSGS_CHANGED` events rather than `INCOMING_MSG` events. Monkey-patched `Bot.run_until` to process `MSGS_CHANGED` events (using deduplication to prevent duplicate message handling) to restore join event detection for securejoins.

## [2.5.8] - 2026-06-23

### Fixed
- **Join Event Welcome & Invalidation:** Fixed a bug where system info messages (like a member joining via securejoin invite) were ignored by the bot's event processor. The `deltachat2` library's `_process_message` event filter is now monkey-patched to ensure system messages (where `from_id <= 9`, e.g. when the bot itself is the inviter) are processed correctly across all library versions. This restores welcome greetings and invite link invalidation/deletion on member join.

## [2.5.7] - 2026-06-23

### Changed
- **Single-use Invite Invalidation and Deletion:** For private group chats, the invite link generated via `/invite` is now single-use and will be automatically deleted from the group chat (both the message text and QR code image) as soon as a new member joins. If the message is unencrypted (e.g. in new groups without established key exchanges) and cannot be deleted for everyone due to core security rules, it falls back to editing the text of the message to show that the link has expired.

## [2.5.6] - 2026-06-17

### Changed
- **Enhance `/slap` Command:** The `/slap` command now supports replying to a message. If you reply to a message and run `/slap` (without arguments), the bot will slap the author of that message and post the slap as a reply.

## [2.5.5] - 2026-06-16

### Changed
- **Enhance `/bounce` Command:** The `/bounce` command now supports checking the activity of a specific user. Specify a user by passing their name/email/ID as an argument (e.g., `/bounce username`) or by replying to their message. Running `/bounce` without arguments still triggers the full chat inactivity report.

### Fixed
- **E2E Failover Loop & Key Fallback**:
  - Added fallback support for both `chat_id` and `chatId` keys in message snapshots to prevent `chat 'Unknown' (ID: None)` errors.
  - Downgraded permanent E2E and resend logs to `WARNING`.
  - Removed administrative failover alert messages completely, relying entirely on structured logging to prevent any potential loop risks.


## [2.5.4] - 2026-06-16

### Changed
- **CMPing Error Clearing:** When a server recovers, its previous failures are now deleted from the database and memory instead of being replaced by virtual "0.0 ms" success entries. This prevents confusing 0.0 ms values in `/cmpingstatus` reports.

## [2.5.3] - 2026-06-16


### Added
- **Automatic Transport Failover:** Implemented a robust, event-driven transport failover mechanism. The bot now listens to the core's `MSG_FAILED` event. When a message fails to deliver, it automatically switches `configured_addr` to the next configured backup transport, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread. The failover process is limited to a maximum of 10 attempts per message to prevent infinite loops, and the administrator is alerted only on the first failure.

## [2.5.2] - 2026-06-16

### Added
- **New Command `/slap [username]`:** Finds the last message from the specified user in the chat and replies to it with `_<sender_name> slaps <username> around a bit with a large trout_`. If no message is found, it sends a self-slap action: `_<sender_name> slaps themself around a bit with a large trout_`.
- **New Command `/me <action>`:** Performs an IRC-style action, sending a message formatted as `_<sender_name> <action>_` in italics.
- **New Commands `/away [text]` and `/back`:** Allows users to set or clear their away status. When away, if someone mentions them by their full name or replies to their message, the bot automatically sends a private message notification to the sender: `_<away_user_name> is away: <text>_`.


## [2.5.1] - 2026-06-13

### Added
- **New Admin Command `/cmpingfail [server]`:** Shows currently failing monitored links, grouped by server, with an optional filter by server name (supports partial matches). The old command `/cmfaillist` is kept as a hidden alias for backwards compatibility.



- **CMPing Results & Rotation Persistence:** Save and load monitoring results state and the current round-robin index (`_cmping_monitor_index`) to SQLite database. This ensures the bot remembers which servers were already down and resumes the rotation exactly where it left off when it restarts (e.g. during code updates/deployments), avoiding duplicate failure alerts and preventing rotation starvation.

- **Server Health Alerting:** Refactored notifications to alert at the **server/host level** instead of individual path pairs. The bot now tracks the overall health of each server, generating an alert only when a server first becomes `UNHEALTHY` (fails to send or receive mail) or when it is fully restored to `HEALTHY` (all links working again). Alerts now include the check direction (incoming/outgoing) and the partner server where the failure occurred. This prevents notification spam during round-robin rotations.
- **Robust Monitoring Retry:** If a monitoring check fails (initially run with `-c 1`), the bot now waits **10 seconds** (instead of 2s) and retries with a "heavy" test of **5 pings** (`-c 5`) and a longer 90s timeout. If at least 1 of the 5 pings succeeds, the failure is treated as transient and filtered out, further reducing false alerts.

- **CMPing Status Latency Visualization:** Added visual latency indicators (circles) to successful checks in `/cmpingstatus` output: `<2000ms` is 🟢, `2000-4000ms` is 🟡, `4000-6000ms` is 🟠, and `>6000ms` is 🔴.
- **CMPing History and Avg Latency in `/cmpinglist`:** Monitored servers in `/cmpinglist` now display their average latency calculated over the last 100 successful checks, followed by the corresponding color-coded circle (🟢/🟡/🟠/🔴 using the same mail-tailored thresholds: `<2000ms` is 🟢, `2000-4000ms` is 🟡, `4000-6000ms` is 🟠, and `>6000ms` is 🔴) and the number of samples formatted as `(🏓 count)`, or `⚪️ no data` if no measurements have been collected yet. Measurements are persisted in the `cmping_history` table (capped at 5,000 entries).





### Changed
- **Robust Updates:** `update.sh` now uses `git reset --hard` instead of `git pull` to gracefully handle force-pushed updates on production instances.


## [2.5.0] - 2026-06-13


### Added
- **Periodic CMPing Monitoring System:**
  - Automatic server connectivity monitoring using a round-robin algorithm: each cycle picks the next server as "source" and checks its connectivity with all other servers.
  - Uses `-c 1` for speed (~3–6 min per cycle). Full mesh coverage in N×interval (e.g., 5 hours for 20 servers at 30-min intervals).
  - Retry on failure to filter transient issues — only reports confirmed failures.
  - Alerts are sent **only on state changes** (OK→FAIL or FAIL→OK) to avoid spam.
  - Recovery notifications when a previously failing pair starts working again.
  - Configurable interval via `CMPING_MONITOR_INTERVAL` env var (default: 1800s / 30 min). Set to `0` to disable.
- **New Admin Commands:**
  - `/cmpingadd <server>` — Add a server to the connectivity monitoring rotation.
  - `/cmpingdel <server>` — Remove a server from monitoring.
  - `/cmpinglist` — Show all monitored servers (transport + manual), pair count, and rotation info.
  - `/cmpingstatus` — Show full monitoring results matrix with per-pair status and check age.
  - `/cmreport <on/off>` — Subscribe/unsubscribe current chat to receive monitoring alerts.

### Changed
- **Refactored** transport domain extraction into shared `_get_bot_domains()` helper, reducing code duplication between `/cmping` and the monitoring system.
- **CMPing timeout** increased from 30s to 60s for subprocess execution.
- **Max 5 servers** limit added per `/cmping` request.

## [2.2.0] - 2026-06-13

### Added
- **ChatMail Ping Command (`/cmping`):**
  - Added `/cmping <server1> <server2> ...` command to ping chatmail relays (transports) to/from specified target servers using the `cmping` utility.
  - Features real-time reaction-based status updates: displays `⏳` while pinging, `☑️` on success, and `❌` on all-failed.
  - Runs ping tests asynchronously in a non-blocking background thread.
  - Enforces a debounce cooldown of 15 seconds per chat.

## [2.1.0] - 2026-06-11

### Added
- **Channels Catalog System:**
  - Added `/dchannels` command for users to list all cataloged channels, displaying their names and descriptions.
  - Added `/dchanneladd <URL>` admin command to join and register a new channel to the catalog in the background (supporting both group-based and contact-based channel/bot invitation QR codes).
  - Added `/dchannelremove [ID]` admin command to remove a channel from the catalog by ID or by the current chat ID.
  - Added `/dchanneldesc<ID> <text>` admin command to set or update the description of a cataloged channel directly from chat.
  - Added dynamic routing for `/dchannel<ID>` commands to return the invitation URL, resolving raw protocols (like `OPEN-CHAT:`, `OPEN:`, or `dcqr://`) into clickable `https://i.delta.chat/#` URLs.
- **Chat Catalog System:**
  - Added `/chatdesc<ID> <text>` admin command to set or update the description of a cataloged group chat directly from chat.

## [2.0.1] - 2026-06-11

### Fixed
- **Custom Welcome Messages:**
  - Fixed welcome greetings for new members by adding a fallback name/display name search among group contacts when `info_contact_id` is not populated, and corrected the RPC lookup method to `lookup_contact_id_by_addr`.

## [2.0.0] - 2026-06-11

### Added
- **Chat Catalog System:**
  - Added `/chats` command for users to list all cataloged public and private groups, including descriptions and member counts. Private chats are marked with a lock emoji `🔐`.
  - Added `/chatadd [description]` admin command to register the current group to the catalog. If `[description]` is not provided, the bot queries the group's description from the Delta Chat core.
  - Added `/chatremove` admin command to remove the current group from the catalog.
  - Added `/private <on/off>` admin command to toggle cataloged chat privacy:
    - **Public chats:** Requests via `/chat<ID>` immediately return a group invite link.
    - **Private chats:** Requests via `/chat<ID>` require manual approval from existing members in the target group chat.
  - Added join approval and decline workflow: members reply with `/approve<ID>` to approve or `/decline<ID> [reason]` to decline. Approving sends a single-use SecureJoin invite link to the applicant, while declining notifies the applicant in a private message with an optional reason.
  - Implemented automatic securejoin link revocation to prevent reuse of invite links in private groups.
  - Integrated dynamic membership tracking (via info message hooks) to keep the catalog `member_count` database column updated in real-time.
- **Custom Welcome Messages:**
  - Added `/welcome` admin command to manage new member greeting messages (`/welcome on`, `/welcome off`, `/welcome on <custom_text>`).
  - Automatically greets new members with their chat presence count and optional custom rules text when they join the group.
- **English Localization:**
  - Standardized all bot outputs, error messages, and command responses to English.

## [1.7.0] - 2026-06-05

### Added
- **DPI Bypass Hack:** Integrated a patched `deltachat-rpc-server` binary into the Docker setup to bypass SSL DPI connection blocks when communicating with chatmail.
- **Resilient Sending Mode:** Added `/resilient` admin command to configure resilient mode (accepts `on`/`off`/`1`/`0`/`true`/`false`, or no arguments to query current status). When enabled, each outgoing message is sent through all configured mail relays using resending mechanism in a non-blocking background thread to bypass chatmail blocking issues without causing UI delays, while ensuring deduplication into a single message bubble on the recipient client.

## [1.6.5] - 2026-06-03

### Added

- Added global `/search` feature for the bot administrator. Sending `/search <query>` to the bot in a private message now searches for matching users across all group chats where the bot is connected, grouping results and displaying which chats each contact belongs to.
- Enabled `/contact<ID>` command in private messages for the bot administrator, allowing the admin to retrieve contact vCards for users found via global search.

## [1.6.4] - 2026-06-03

### Fixed

- Added `pillow` to `requirements.txt` to resolve `No module named 'PIL'` error and enable QR code image generation for `/invite`.
- Implemented robust in-flight exception handling in `_send` to intercept "not a member of the chat" errors immediately, avoiding retry loops and transport rotations without breaking commands in active chats (reverted unstable `can_send` checks).
- Fixed attempt counting logic in `_send` failure logging to correctly report actual attempts.

## [1.6.3] - 2026-06-02

### Added

- Added `/invite` command to generate and send a SecureJoin invite link and PNG QR code image for the current group chat. Available to all users with a 10-minute cooldown (admins are exempt).

## [1.6.2] - 2026-05-20

### Added

- Upgraded `/search` command to natively support searching by domains and partial strings (e.g. `/search @testrun.org @chatmail.uk`), and configured the reply-based parser to automatically extract `@domain` handles from quoted messages.

### Changed

- Configured automatic deletion of old messages (`delete_device_after` set to 36 hours / 129600 seconds) to prevent the main SQLite database (`dc.db`) from bloating while buffering /top 24h stats.
- Disabled automatic download of message attachments and media files (`download_limit` set to 1 byte) to completely stop the `dc.db-blobs` directory from growing.

## [1.6.1] - 2026-05-20

### Added

- Enhanced `/search` command with reply-based lookups: replying to any message with `/search` will automatically extract and search for all email addresses contained in the quoted message.
- Upgraded `/search` to scan all configured transports/aliases (secondary addresses) for group contacts by parsing contact encryption info, rather than just matching against the primary address.
- Updated `/search` output to list all associated transport addresses for matched contacts.

## [1.6.0] - 2026-05-20

### Added

- Added `/search <email1> <email2> ...` command to search for group members by one or more emails simultaneously (via case-insensitive substring matching). Matches are formatted exactly like `/bounce` reports with bold names, contact links, and last-seen activity timestamps. Available to all users with a 10-minute cooldown.

## [1.5.0] - 2026-05-19

### Added

- Added complete set of in-chat transport management commands matching `tgbridge`:
  - `/addtransport <payload>` to dynamically add backup mail relays via chatmail URI or credentials.
  - `/setprimary <addr>` to switch the primary active mail relay (`configured_addr`).
- Implemented `transport_stats` SQLite database table and statistics tracking.
- Upgraded `/transports` command to show connectivity status, primary/backup labels, message counts (sent/received), and last sent/received timestamps.
- Upgraded `/rmtransport <addr>` command with full validation checks and last-transport protection.

## [1.4.0] - 2026-05-16

### Changed

- Removed automatic daily reports in all chats. Reports are now only sent upon manual request.
- Refactored background loop into a silent monitor task that only initializes tracking for new groups.
- Separated inactivity reports from activity rankings: `/bounce` now only shows inactive users, while `/top` remains for rankings.
- Increased inactivity threshold from 7 days to 14 days.

## [1.3.0] - 2026-05-11

### Added

- Added `/relays` command to find group members using regular mail providers (Yandex, Mail.ru, Rambler). Available to all users with a 10-minute cooldown.
- Added `/contact<ID>` command to share contact objects via dynamic links in reports.
- Enhanced all reports with rich formatting (bold names, square brackets for timestamps) and contact links.
- Enhanced `/relays` to automatically extract secondary addresses (transports) for all contacts by parsing encryption info.
- Added daily status reports (heartbeats) that post group statistics even if no one is inactive yet.
- Added `/top` command to show the most active members in the last 24 hours. Available to all users with a 10-minute cooldown.
- Enhanced all reports with rich formatting (bold names, square brackets for timestamps), contact links, and activity rankings.

### Changed

- Improved background loop diagnostics by logging specific errors for all RPC fallback methods.
- Fixed critical bug in `/contact<ID>` command by implementing manual vCard generation and sharing (resolved `MsgData` and `MessageViewtype` incompatibilities).
- Fixed `last_seen` extraction for contacts by properly handling `AttrDict` and dictionary fallbacks.
- Reduced log noise by changing admin fingerprint mismatch warnings to info level.

## [1.3.1] - 2026-05-12

### Changed

- Reduced inactivity threshold from 30 days to 7 days for more aggressive group management.
- Removed activity ranking from manual `/bounce` reports to keep them focused (ranking is still available via `/top`).
- Updated reports to dynamically reflect the current inactivity threshold in days.

### Fixed

- Fixed "Method not found" error in daily inactivity check by expanding RPC method fallbacks (added `get_chat_list_ids`).
- Added detailed logging for RPC method attempts to improve diagnostics.

## [1.2.0] - 2026-05-07

### Added

- Added Multi-Transport support (Backup Relays).
- Added `/transports` and `/rmtransport` commands for admin.
- Added `init transport` CLI command.
- Improved `on_start` logs to display all configured relays.

## [1.1.0] - 2026-05-07

### Added

- Added `/help` command with basic instructions and source link.
- Added `/donate` command to support development.
- Added 10-minute rate limiting (cooldown) for `/bounce` command in group chats.
- Added 30-day Grace Period for new groups to avoid "never seen" false positives.

### Changed

- Opened `/bounce` command to all users (previously admin-only).
- Admins are now exempt from the `/bounce` rate limit.
- Improved background loop robustness with multiple RPC method fallbacks.
- Added a 10-second initialization delay for background tasks.

## [1.0.0] - 2026-05-07

### Added

- Initial release of Delta Chat Bouncer Bot.
- Automated daily inactivity reports for all group chats.
- Manual inactivity check via `/bounce` command (admin-only).
- Secure admin management with `/initadmin`.
- Dockerized deployment support.
