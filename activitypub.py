from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from urllib.parse import urlparse

try:
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import serialization, hashes
except ImportError:
    rsa = padding = serialization = hashes = None

try:
    import aiohttp as _aiohttp
except ImportError:
    _aiohttp = None

import database

logger = logging.getLogger("bouncer_bot.activitypub")

# ==============================================================================
# 1. RSA Key Management
# ==============================================================================

def generate_actor_keypair() -> tuple[str, str]:
    """Generate RSA-2048 keypair. Returns (private_pem, public_pem)."""
    if rsa is None:
        raise ImportError("cryptography package is not available.")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')
    pub_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode('utf-8')
    return priv_pem, pub_pem

def get_or_create_actor_keys(token: str) -> tuple[str, str]:
    """Get existing keys from DB or generate new ones. Returns (private_pem, public_pem)."""
    existing = database.get_ap_actor_keys(token)
    if existing:
        return existing['private_key_pem'], existing['public_key_pem']
    priv_pem, pub_pem = generate_actor_keypair()
    database.save_ap_actor_keys(token, priv_pem, pub_pem)
    return priv_pem, pub_pem

# ==============================================================================
# 2. HTTP Signature Signing
# ==============================================================================

def sign_headers(method: str, url: str, body: bytes | None,
                 private_key_pem: str, key_id: str) -> dict[str, str]:
    """Build HTTP Signature headers for an outgoing request.
    
    For POST: signs (request-target) host date digest content-type
    For GET: signs (request-target) host date accept
    
    Returns dict of headers to add to the request.
    """
    if serialization is None:
        raise ImportError("cryptography package is not available.")
        
    parsed_url = urlparse(url)
    host = parsed_url.netloc
    path = parsed_url.path
    if parsed_url.query:
        path += "?" + parsed_url.query
    if not path:
        path = "/"
        
    date_str = formatdate(usegmt=True)
    headers_dict = {
        'host': host,
        'date': date_str,
    }
    
    if method.upper() == 'POST':
        digest_bytes = hashlib.sha256(body or b'').digest()
        digest_b64 = base64.b64encode(digest_bytes).decode('ascii')
        headers_dict['digest'] = f"SHA-256={digest_b64}"
        headers_dict['content-type'] = 'application/activity+json'
        headers_to_sign = ['(request-target)', 'host', 'date', 'digest', 'content-type']
    else:
        headers_dict['accept'] = 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"'
        headers_to_sign = ['(request-target)', 'host', 'date', 'accept']

    signing_lines = []
    for h in headers_to_sign:
        if h == '(request-target)':
            signing_lines.append(f"(request-target): {method.lower()} {path}")
        else:
            signing_lines.append(f"{h}: {headers_dict[h]}")
            
    signing_string = "\n".join(signing_lines).encode('utf-8')
    
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode('utf-8'),
        password=None
    )
    signature_bytes = private_key.sign(
        signing_string,
        padding.PKCS1v15(),
        hashes.SHA256()
    )
    signature_b64 = base64.b64encode(signature_bytes).decode('ascii')
    
    headers_str = " ".join(headers_to_sign)
    sig_header = f'keyId="{key_id}",algorithm="rsa-sha256",headers="{headers_str}",signature="{signature_b64}"'
    
    final_headers = {
        'Host': headers_dict['host'],
        'Date': headers_dict['date'],
        'Signature': sig_header
    }
    if 'digest' in headers_dict:
        final_headers['Digest'] = headers_dict['digest']
        final_headers['Content-Type'] = headers_dict['content-type']
    if 'accept' in headers_dict:
        final_headers['Accept'] = headers_dict['accept']
        
    return final_headers

# ==============================================================================
# 3. HTTP Signature Verification
# ==============================================================================

def parse_signature_header(sig_header: str) -> dict:
    """Parse HTTP Signature header into {keyId, algorithm, headers, signature}."""
    parsed = {}
    parts = sig_header.split(',')
    for part in parts:
        if '=' in part:
            k, v = part.split('=', 1)
            parsed[k.strip()] = v.strip().strip('"')
    return parsed

def verify_http_signature(method: str, path: str, headers: dict, body: bytes,
                          public_key_pem: str) -> bool:
    """Verify HTTP Signature on an incoming request."""
    if serialization is None:
        raise ImportError("cryptography package is not available.")
        
    headers_lower = {k.lower(): str(v) for k, v in headers.items()}
    
    sig_header = headers_lower.get('signature')
    if not sig_header:
        return False
        
    sig_dict = parse_signature_header(sig_header)
    if 'signature' not in sig_dict or 'headers' not in sig_dict:
        return False
        
    if 'date' in headers_lower:
        try:
            date_dt = parsedate_to_datetime(headers_lower['date'])
            now = datetime.now(timezone.utc)
            if abs((now - date_dt).total_seconds()) > 300:
                return False
        except Exception:
            return False
            
    if method.upper() == 'POST' and 'digest' in headers_lower:
        digest_bytes = hashlib.sha256(body or b'').digest()
        digest_b64 = base64.b64encode(digest_bytes).decode('ascii')
        expected_digest = f"SHA-256={digest_b64}"
        if headers_lower['digest'].lower() != expected_digest.lower():
            return False

    headers_to_sign = sig_dict['headers'].split(' ')
    signing_lines = []
    for h in headers_to_sign:
        if h == '(request-target)':
            signing_lines.append(f"(request-target): {method.lower()} {path}")
        else:
            val = headers_lower.get(h, '')
            signing_lines.append(f"{h}: {val}")
            
    signing_string = "\n".join(signing_lines).encode('utf-8')
    
    try:
        signature_bytes = base64.b64decode(sig_dict['signature'])
        public_key = serialization.load_pem_public_key(public_key_pem.encode('utf-8'))
        hash_algo = hashes.SHA512() if 'sha512' in sig_dict.get('algorithm', '').lower() else hashes.SHA256()
        public_key.verify(
            signature_bytes,
            signing_string,
            padding.PKCS1v15(),
            hash_algo
        )
        return True
    except Exception as e:
        logger.debug(f"HTTP signature verification failed: {e}")
        return False

# ==============================================================================
# 4. Actor / Activity JSON Builders
# ==============================================================================

def build_actor_json(channel: dict, base_url: str, public_key_pem: str) -> dict:
    """Build ActivityStreams Actor object from channel data."""
    token = channel['token']
    actor_url = f"{base_url}/c/{token}"
    return {
        "@context": [
            "https://www.w3.org/ns/activitystreams",
            "https://w3id.org/security/v1"
        ],
        "id": actor_url,
        "type": "Service",
        "preferredUsername": token,
        "name": channel.get('name') or 'Channel',
        "summary": f"<p>{html.escape(channel.get('description') or '')}</p>",
        "url": actor_url,
        "inbox": f"{actor_url}/inbox",
        "outbox": f"{actor_url}/outbox",
        "followers": f"{actor_url}/followers",
        "following": f"{actor_url}/following",
        "manuallyApprovesFollowers": False,
        "discoverable": True,
        "published": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "icon": {
            "type": "Image",
            "mediaType": "image/png",
            "url": f"{actor_url}/avatar.png"
        },
        "publicKey": {
            "id": f"{actor_url}#main-key",
            "owner": actor_url,
            "publicKeyPem": public_key_pem
        },
        "endpoints": {
            "sharedInbox": f"{base_url}/inbox"
        }
    }

def build_note(channel: dict, post: dict, base_url: str) -> dict:
    """Build ActivityStreams Note object from a channel post."""
    token = channel['token']
    actor_url = f"{base_url}/c/{token}"
    msg_id = post.get('msg_id') or post.get('id')
    post_url = f"{actor_url}/posts/{msg_id}"
    
    content_html = format_post_html(post.get('text', ''))
    
    note = {
        "id": post_url,
        "type": "Note",
        "attributedTo": actor_url,
        "published": datetime.fromtimestamp(post.get('timestamp', time.time()), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [f"{actor_url}/followers"],
        "content": content_html,
        "url": post_url,
        "sensitive": False,
        "attachment": [],
        "tag": [],
    }
    
    media_fn = post.get('media_filename')
    media_type = post.get('media_type')
    if media_fn and msg_id:
        media_url = f"{base_url}/media/{token}/{msg_id}/{media_fn}"
        mime_map = {
            'image': 'image/webp' if media_fn.endswith('.webp') else 'image/jpeg',
            'video': 'video/mp4',
            'audio': 'audio/mpeg',
        }
        note['attachment'].append({
            "type": "Document",
            "mediaType": mime_map.get(media_type, 'application/octet-stream'),
            "url": media_url,
            "name": media_fn,
        })
    
    return note

def build_create_activity(actor_url: str, note: dict) -> dict:
    """Wrap a Note in a Create activity."""
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{note['id']}/activity",
        "type": "Create",
        "actor": actor_url,
        "published": note['published'],
        "to": note['to'],
        "cc": note['cc'],
        "object": note,
    }

def build_accept_follow(actor_url: str, follow_activity: dict) -> dict:
    """Build Accept activity in response to a Follow."""
    accept = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{actor_url}/activities/accept-{uuid.uuid4().hex[:12]}",
        "type": "Accept",
        "actor": actor_url,
        "object": follow_activity,
    }
    recipient = follow_activity.get("actor")
    if recipient:
        accept["to"] = [recipient]
    return accept

def build_ordered_collection(collection_id: str, total_items: int) -> dict:
    """Build ActivityStreams OrderedCollection object."""
    return {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": collection_id,
        "type": "OrderedCollection",
        "totalItems": total_items,
    }

def format_post_html(text: str) -> str:
    """Convert post text to simple HTML for ActivityPub content field.
    Lighter than the full format_markdown_html — just escape, linkify, and wrap in <p>.
    """
    if not text:
        return ""
    text = html.escape(text)
    # Autolink URLs
    text = re.sub(r'(https?://[^\s<>"]+)', r'<a href="\1" rel="nofollow noopener noreferrer" target="_blank">\1</a>', text)
    # Convert newlines to <br>
    paragraphs = text.split('\n\n')
    return ''.join(f'<p>{p.replace(chr(10), "<br>")}</p>' for p in paragraphs if p.strip())

# ==============================================================================
# 5. Delivery Worker
# ==============================================================================

_delivery_queue = None
_http_session = None
_web_loop = None

async def init_delivery_worker(loop: asyncio.AbstractEventLoop):
    """Initialize delivery queue and HTTP session. Called from _run_web_server."""
    global _delivery_queue, _http_session, _web_loop
    if _aiohttp is None:
        logger.warning("aiohttp not available, delivery worker cannot be started.")
        return
        
    _web_loop = loop
    _delivery_queue = asyncio.Queue()
    _http_session = _aiohttp.ClientSession(
        timeout=_aiohttp.ClientTimeout(total=15),
        headers={"User-Agent": "BouncerBot/2.12.3 (+https://dc.gluek.info)"}
    )
    asyncio.create_task(_delivery_loop())
    logger.info("ActivityPub delivery worker started.")

async def _delivery_loop():
    """Background loop processing post delivery to follower inboxes."""
    while True:
        try:
            token, post_data, base_url = await _delivery_queue.get()
            await _deliver_post(token, post_data, base_url)
        except Exception as e:
            logger.error(f"AP delivery loop error: {e}")
        finally:
            _delivery_queue.task_done()

async def _deliver_post(token: str, post_data: dict, base_url: str):
    """Deliver a Create(Note) to all follower inboxes for a channel."""
    channel = database.get_catalog_channel_by_token(token)
    if not channel:
        return
    
    inboxes = database.get_ap_follower_inboxes(token)
    if not inboxes:
        return
    
    priv_pem, pub_pem = get_or_create_actor_keys(token)
    actor_url = f"{base_url}/c/{token}"
    key_id = f"{actor_url}#main-key"
    
    note = build_note(channel, post_data, base_url)
    activity = build_create_activity(actor_url, note)
    body = json.dumps(activity, ensure_ascii=False).encode('utf-8')
    
    # Deduplicate: prefer shared_inbox
    seen_inboxes = set()
    for inbox_info in inboxes:
        inbox_url = inbox_info.get('follower_shared_inbox') or inbox_info['follower_inbox']
        if inbox_url in seen_inboxes:
            continue
        seen_inboxes.add(inbox_url)
        try:
            await deliver_to_inbox(inbox_url, body, priv_pem, key_id)
        except Exception as e:
            logger.warning(f"AP delivery failed to {inbox_url}: {e}")

async def deliver_to_inbox(inbox_url: str, body: bytes, private_key_pem: str, key_id: str):
    """Send signed HTTP POST to a single inbox."""
    if _http_session is None:
        return
        
    headers = sign_headers('POST', inbox_url, body, private_key_pem, key_id)
    headers['Content-Type'] = 'application/activity+json'
    try:
        async with _http_session.post(inbox_url, data=body, headers=headers) as resp:
            if resp.status >= 400:
                resp_text = await resp.text()
                logger.warning(f"AP POST {inbox_url} returned {resp.status}: {resp_text[:200]}")
            else:
                logger.info(f"AP delivered to {inbox_url}: {resp.status}")
    except Exception as e:
        logger.warning(f"AP POST {inbox_url} exception: {e}")

def queue_post_delivery(token: str, post_data: dict, base_url: str):
    """Thread-safe: queue a post for AP delivery. Called from _ingest_channel_post (sync context)."""
    if _web_loop is None or _delivery_queue is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _delivery_queue.put((token, post_data, base_url)),
            _web_loop
        )
    except Exception as e:
        logger.warning(f"Failed to queue AP delivery: {e}")

# ==============================================================================
# 6. Remote Actor Fetching
# ==============================================================================

_remote_actor_cache: dict[str, tuple[float, dict]] = {}  # actor_id -> (expires_at, actor_json)
REMOTE_ACTOR_CACHE_TTL = 3600  # 1 hour

def _get_default_signing_info() -> tuple[str, str] | None:
    try:
        channels = database.get_all_catalog_channels(include_deleted=False)
        if not channels:
            return None
        token = channels[0]['token']
        base_url = database.get_config("base_url") or os.getenv("BASE_URL") or "https://dc.gluek.info"
        return token, base_url.rstrip('/')
    except Exception:
        return None

async def fetch_remote_actor(actor_id: str, use_cache: bool = True,
                             sign_as_token: str | None = None,
                             base_url: str | None = None) -> dict | None:
    """Fetch and cache a remote actor JSON for Follow verification and key retrieval.
    Supports Authorized Fetch (HTTP Signatures on GET) for instances like GoToSocial/Akkoma."""
    if _http_session is None:
        return None

    if use_cache:
        cached = _remote_actor_cache.get(actor_id)
        if cached and time.time() < cached[0]:
            return cached[1]

    headers = {
        'Accept': 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
        'User-Agent': 'BouncerBot/2.12.3 (+https://dc.gluek.info)',
    }

    if sign_as_token and base_url and serialization is not None:
        try:
            priv_pem, _ = get_or_create_actor_keys(sign_as_token)
            key_id = f"{base_url.rstrip('/')}/c/{sign_as_token}#main-key"
            headers.update(sign_headers('GET', actor_id, None, priv_pem, key_id))
        except Exception as e:
            logger.warning(f"Error signing GET request for {actor_id}: {e}")

    try:
        async with _http_session.get(actor_id, headers=headers, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
            # Handle Authorized Fetch (401/403) by retrying with signature from default channel if not signed yet
            if resp.status in (401, 403) and not (sign_as_token and base_url):
                signing = _get_default_signing_info()
                if signing and serialization is not None:
                    s_tok, s_base = signing
                    try:
                        priv_pem, _ = get_or_create_actor_keys(s_tok)
                        key_id = f"{s_base}/c/{s_tok}#main-key"
                        auth_headers = {
                            'Accept': 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
                            'User-Agent': 'BouncerBot/2.12.3 (+https://dc.gluek.info)',
                        }
                        auth_headers.update(sign_headers('GET', actor_id, None, priv_pem, key_id))
                        async with _http_session.get(actor_id, headers=auth_headers, timeout=_aiohttp.ClientTimeout(total=10)) as resp2:
                            if resp2.status == 200:
                                actor_data = await resp2.json(content_type=None)
                                _remote_actor_cache[actor_id] = (time.time() + REMOTE_ACTOR_CACHE_TTL, actor_data)
                                return actor_data
                            else:
                                logger.warning(f"Signed GET for {actor_id} returned {resp2.status}")
                                return None
                    except Exception as e:
                        logger.warning(f"Error retrying signed GET for {actor_id}: {e}")
                        return None

            if resp.status != 200:
                logger.warning(f"Failed to fetch actor {actor_id}: {resp.status}")
                return None
            actor_data = await resp.json(content_type=None)
            _remote_actor_cache[actor_id] = (time.time() + REMOTE_ACTOR_CACHE_TTL, actor_data)
            return actor_data
    except Exception as e:
        logger.warning(f"Error fetching remote actor {actor_id}: {e}")
        return None


async def resolve_public_key(key_id: str, sign_as_token: str | None = None,
                             base_url: str | None = None) -> tuple[str | None, dict | None]:
    """Resolve public key PEM from a keyId URL.
    Returns (public_key_pem, actor_or_key_doc).
    Handles:
    - Key endpoints returning publicKeyPem directly (e.g. GoToSocial /main-key)
    - Actor endpoints returning publicKey dict (e.g. Mastodon /users/alice#main-key)
    - Remote servers enforcing Authorized Fetch
    """
    if not key_id:
        return None, None

    # Try fetching key_id directly (without fragment)
    fetch_url = key_id.split('#')[0]
    data = await fetch_remote_actor(fetch_url, sign_as_token=sign_as_token, base_url=base_url)
    if not data and fetch_url != key_id:
        data = await fetch_remote_actor(key_id, sign_as_token=sign_as_token, base_url=base_url)

    if not data or not isinstance(data, dict):
        return None, None

    # Case 1: Standalone key object (GoToSocial, etc.)
    if "publicKeyPem" in data and isinstance(data["publicKeyPem"], str):
        return data["publicKeyPem"], data

    # Case 2: Actor object with embedded publicKey dict
    pk = data.get("publicKey")
    if isinstance(pk, dict) and "publicKeyPem" in pk:
        return pk["publicKeyPem"], data

    # Case 3: Actor object with publicKey list
    if isinstance(pk, list):
        for item in pk:
            if isinstance(item, dict) and item.get("publicKeyPem"):
                if item.get("id") == key_id or len(pk) == 1:
                    return item["publicKeyPem"], data

    # Case 4: If key_id has a fragment and data didn't have the key, try fetching key_id directly if different
    if fetch_url != key_id:
        key_data = await fetch_remote_actor(key_id, sign_as_token=sign_as_token, base_url=base_url)
        if isinstance(key_data, dict):
            if "publicKeyPem" in key_data:
                return key_data["publicKeyPem"], key_data
            pk2 = key_data.get("publicKey")
            if isinstance(pk2, dict) and "publicKeyPem" in pk2:
                return pk2["publicKeyPem"], key_data

    return None, data
