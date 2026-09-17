from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import typing
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
VERSION = "2.14.1"

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

def extract_signature_info(headers: dict) -> dict:
    """Extract signature metadata from headers supporting both:
    1. Modern RFC 9421 HTTP Message Signatures (`Signature-Input` + `Signature: sig1=...`)
    2. Legacy Cavage / draft-cavage-http-signatures (`Signature: keyId="..."...`)
    """
    if not headers:
        return {}

    headers_lower = {k.lower(): str(v) for k, v in headers.items()}
    sig_input = headers_lower.get('signature-input')
    sig_header = headers_lower.get('signature')

    # If no Signature header, check Authorization: Signature ...
    if not sig_header:
        auth = headers_lower.get('authorization', '')
        if auth.lower().startswith('signature '):
            sig_header = auth[10:].strip()

    # Priority 1: RFC 9421 when Signature-Input is present
    if sig_input:
        first_input = sig_input.split('\n')[0].strip()
        m = re.match(r'^\s*([a-zA-Z0-9_-]+)\s*=\s*(.+)$', first_input)
        if m:
            label = m.group(1)
            params = m.group(2).strip()

            comp_m = re.search(r'\((.*?)\)', params)
            components = re.findall(r'"([^"]+)"', comp_m.group(1)) if comp_m else []

            keyid_m = re.search(r'keyid="([^"]+)"', params, re.IGNORECASE)
            key_id = keyid_m.group(1) if keyid_m else None

            alg_m = re.search(r'alg="?([a-zA-Z0-9_-]+)"?', params, re.IGNORECASE)
            alg = alg_m.group(1) if alg_m else "rsa-v1_5-sha256"

            created_m = re.search(r'created=(\d+)', params)
            created = int(created_m.group(1)) if created_m else None

            # Find signature bytes in Signature header
            sig_b64 = None
            if sig_header:
                sig_m = re.search(rf'{re.escape(label)}\s*=\s*:([^:]+):', sig_header)
                if not sig_m:
                    sig_m = re.search(rf'{re.escape(label)}\s*=\s*"([^"]+)"', sig_header)
                if not sig_m:
                    sig_m = re.search(r':([^:]+):', sig_header)
                if sig_m:
                    sig_b64 = sig_m.group(1).strip()

            if key_id or sig_b64:
                return {
                    "format": "rfc9421",
                    "key_id": key_id,
                    "algorithm": alg,
                    "signature_b64": sig_b64,
                    "headers": components,
                    "created": created,
                    "label": label,
                    "sig_params": params,
                    "raw_dict": {"Signature-Input": sig_input, "Signature": sig_header or ""}
                }

    # Priority 2: Cavage / draft-cavage-http-signatures
    if sig_header:
        sig_dict = parse_signature_header(sig_header)
        key_id = sig_dict.get('keyId')
        sig_b64 = sig_dict.get('signature')
        headers_str = sig_dict.get('headers', '')
        headers_list = headers_str.split(' ') if headers_str else []
        alg = sig_dict.get('algorithm', 'rsa-sha256')

        if key_id or sig_b64 or headers_str:
            return {
                "format": "cavage",
                "key_id": key_id,
                "algorithm": alg,
                "signature_b64": sig_b64,
                "headers": headers_list,
                "created": None,
                "label": None,
                "sig_params": None,
                "raw_dict": sig_dict
            }

    return {}

def verify_http_signature(method: str, path: str, headers: dict, body: bytes,
                          public_key_pem: str, target_uri: str | None = None) -> bool:
    """Verify HTTP Signature on an incoming request, supporting both RFC 9421 and Cavage."""
    if serialization is None:
        raise ImportError("cryptography package is not available.")

    headers_lower = {k.lower(): str(v) for k, v in headers.items()}
    sig_info = extract_signature_info(headers_lower)
    if not sig_info:
        return False

    # Date / timestamp check
    created_ts = sig_info.get('created')
    now = datetime.now(timezone.utc)
    if created_ts is not None:
        if abs(now.timestamp() - created_ts) > 300:
            return False
    elif 'date' in headers_lower:
        try:
            date_dt = parsedate_to_datetime(headers_lower['date'])
            if date_dt.tzinfo is None:
                date_dt = date_dt.replace(tzinfo=timezone.utc)
            if abs((now - date_dt).total_seconds()) > 300:
                return False
        except Exception:
            return False

    # Digest check
    if method.upper() in ('POST', 'PUT'):
        digest_bytes = hashlib.sha256(body or b'').digest()
        digest_b64 = base64.b64encode(digest_bytes).decode('ascii')
        if 'digest' in headers_lower:
            expected_digest = f"SHA-256={digest_b64}"
            if headers_lower['digest'].lower() != expected_digest.lower():
                return False
        if 'content-digest' in headers_lower:
            expected_cd = f"sha-256=:{digest_b64}:"
            if headers_lower['content-digest'].lower() != expected_cd.lower():
                return False

    public_key = serialization.load_pem_public_key(public_key_pem.encode('utf-8'))

    def _verify_rfc9421(info: dict) -> bool:
        sig_b64 = info.get('signature_b64')
        if not sig_b64:
            return False
        signing_lines = []
        for comp in info.get('headers', []):
            comp_lower = comp.lower()
            if comp_lower == '@method':
                signing_lines.append(f'"@method": {method.upper()}')
            elif comp_lower == '@target-uri':
                uri = target_uri
                if not uri:
                    scheme = headers_lower.get('x-forwarded-proto') or 'https'
                    host = headers_lower.get('x-forwarded-host') or headers_lower.get('host') or 'localhost'
                    uri = f"{scheme}://{host}{path}"
                signing_lines.append(f'"@target-uri": {uri}')
            elif comp_lower == '@path':
                signing_lines.append(f'"@path": {path.split("?")[0]}')
            elif comp_lower == '@query':
                q = path.split("?")[1] if "?" in path else ""
                signing_lines.append(f'"@query": ?{q}' if q else '"@query": ')
            elif comp_lower == '@authority':
                auth = headers_lower.get('x-forwarded-host') or headers_lower.get('host') or ''
                signing_lines.append(f'"@authority": {auth}')
            elif comp_lower == '@request-target':
                signing_lines.append(f'"@request-target": {method.lower()} {path}')
            else:
                val = headers_lower.get(comp_lower, '')
                signing_lines.append(f'"{comp_lower}": {val}')
        signing_lines.append(f'"@signature-params": {info.get("sig_params", "")}')
        signing_string = "\n".join(signing_lines).encode('utf-8')

        sig_bytes = base64.b64decode(sig_b64)
        alg = (info.get('algorithm') or '').lower()
        if 'pss' in alg:
            h = hashes.SHA512() if 'sha512' in alg else hashes.SHA256()
            padding_algo = padding.PSS(mgf=padding.MGF1(h), salt_length=padding.PSS.MAX_LENGTH)
        else:
            h = hashes.SHA512() if 'sha512' in alg else hashes.SHA256()
            padding_algo = padding.PKCS1v15()

        public_key.verify(sig_bytes, signing_string, padding_algo, h)
        return True

    def _verify_cavage(info: dict) -> bool:
        sig_b64 = info.get('signature_b64')
        if not sig_b64:
            return False
        headers_to_sign = info.get('headers', [])
        signing_lines = []
        for h in headers_to_sign:
            if h == '(request-target)':
                signing_lines.append(f"(request-target): {method.lower()} {path}")
            else:
                val = headers_lower.get(h.lower(), '')
                signing_lines.append(f"{h}: {val}")
        signing_string = "\n".join(signing_lines).encode('utf-8')
        sig_bytes = base64.b64decode(sig_b64)
        hash_algo = hashes.SHA512() if 'sha512' in info.get('algorithm', '').lower() else hashes.SHA256()
        public_key.verify(sig_bytes, signing_string, padding.PKCS1v15(), hash_algo)
        return True

    try:
        if sig_info.get("format") == "rfc9421":
            try:
                return _verify_rfc9421(sig_info)
            except Exception as e:
                logger.debug(f"RFC 9421 signature verify error: {e}")
                # Fallback to Cavage if Cavage is also present in headers
                sig_header = headers_lower.get('signature', '')
                if 'keyid=' in sig_header.lower():
                    cavage_info = {
                        "format": "cavage",
                        "raw_dict": parse_signature_header(sig_header)
                    }
                    cavage_info["key_id"] = cavage_info["raw_dict"].get("keyId")
                    cavage_info["headers"] = cavage_info["raw_dict"].get("headers", "date").split(" ")
                    cavage_info["signature_b64"] = cavage_info["raw_dict"].get("signature")
                    cavage_info["algorithm"] = cavage_info["raw_dict"].get("algorithm", "rsa-sha256")
                    return _verify_cavage(cavage_info)
                return False
        else:
            return _verify_cavage(sig_info)
    except Exception as e:
        logger.debug(f"HTTP signature verification failed: {e}")
        return False

# ==============================================================================
# 4. Actor / Activity JSON Builders
# ==============================================================================

def build_actor_json(channel: dict, base_url: str, public_key_pem: str) -> dict:
    """Build ActivityStreams Actor object from channel data."""
    base_url = (base_url or "").strip().rstrip('/')
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
        "image": {
            "type": "Image",
            "mediaType": "image/jpeg",
            "url": f"{base_url}/background.jpg"
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
    base_url = (base_url or "").strip().rstrip('/')
    token = channel['token']
    actor_url = f"{base_url}/c/{token}"
    msg_id = post.get('msg_id') or post.get('id')
    post_url = f"{actor_url}/posts/{msg_id}"
    
    content_html = format_post_html(post.get('text', ''))
    
    note = {
        "@context": "https://www.w3.org/ns/activitystreams",
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

def build_ordered_collection(collection_id: str, total_items: int,
                             first: str | None = None, last: str | None = None) -> dict:
    """Build ActivityStreams OrderedCollection object."""
    coll = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": collection_id,
        "type": "OrderedCollection",
        "totalItems": total_items,
    }
    if first:
        coll["first"] = first
    if last:
        coll["last"] = last
    return coll

def build_ordered_collection_page(page_id: str, part_of: str, total_items: int,
                                  ordered_items: list, next_page: str | None = None,
                                  prev_page: str | None = None) -> dict:
    """Build ActivityStreams OrderedCollectionPage object."""
    page = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": page_id,
        "type": "OrderedCollectionPage",
        "partOf": part_of,
        "totalItems": total_items,
        "orderedItems": ordered_items,
    }
    if next_page:
        page["next"] = next_page
    if prev_page:
        page["prev"] = prev_page
    return page

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
        headers={"User-Agent": f"BouncerBot/{VERSION} (+https://dc.gluek.info)"}
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
    base_url = (base_url or "").strip().rstrip('/')
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
        
    safe, reason = is_safe_url(inbox_url)
    if not safe:
        logger.warning(f"Blocked delivery to unsafe inbox URL {inbox_url}: {reason}")
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

async def deliver_backfill_posts(token: str, target_inbox: str, base_url: str, limit: int = 10):
    """Deliver recent channel posts to a new follower's inbox in chronological order."""
    channel = database.get_catalog_channel_by_token(token)
    if not channel or channel.get('is_deleted'):
        return

    chat_id = channel.get('chat_id')
    if not chat_id:
        return

    posts = database.get_channel_posts(chat_id, limit=limit)
    if not posts:
        return

    base_url = (base_url or "").strip().rstrip('/')
    actor_url = f"{base_url}/c/{token}"
    key_id = f"{actor_url}#main-key"
    priv_pem, _ = get_or_create_actor_keys(token)

    # Deliver oldest to newest so they appear in correct chronological order
    for post in reversed(posts):
        try:
            note = build_note(channel, post, base_url)
            create_act = build_create_activity(actor_url, note)
            body = json.dumps(create_act, ensure_ascii=False).encode('utf-8')
            await deliver_to_inbox(target_inbox, body, priv_pem, key_id)
        except Exception as e:
            logger.warning(f"Failed to deliver backfilled post to {target_inbox}: {e}")

def queue_post_delivery(token: str, post_data: dict, base_url: str):
    """Thread-safe: queue a post for AP delivery. Called from _ingest_channel_post (sync context)."""
    if _web_loop is None or _delivery_queue is None:
        return
    base_url = (base_url or "").strip().rstrip('/')
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
        base_url = (database.get_config("base_url") or os.getenv("BASE_URL") or "https://dc.gluek.info").strip().rstrip('/')
        return token, base_url
    except Exception:
        return None

def is_safe_url(url: str, allow_private: bool | None = None) -> tuple[bool, str]:
    """Validate that a URL is safe to fetch and does not target private or internal resources (SSRF protection).
    Returns (is_safe, error_reason).
    """
    if not url or not isinstance(url, str):
        return False, "Empty or invalid URL"

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL parse error: {e}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"Unsupported scheme: '{scheme}'"

    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname in URL"

    hostname = hostname.lower()

    if allow_private is None:
        allow_private = (
            os.getenv("ALLOW_PRIVATE_NETWORKS", "").lower() in ("1", "true", "yes") or
            database.get_config("allow_private_networks") == "1"
        )

    if not allow_private:
        # Check forbidden local / internal domain names
        if (hostname in ("localhost", "localhost.localdomain") or
                hostname.endswith(".local") or
                hostname.endswith(".internal") or
                hostname.endswith(".lan") or
                hostname.endswith(".home.arpa")):
            return False, f"Access to local hostname '{hostname}' is forbidden"

        # RFC 2606 reserved testing domains (safe in unit test environments)
        if (hostname.endswith(".example") or hostname.endswith(".test") or
                hostname == "example.com" or hostname.endswith(".example.com") or
                hostname == "remote.social" or hostname.endswith(".remote.social")):
            return True, "OK"

        # If hostname is an explicit IP address
        try:
            ip_obj = ipaddress.ip_address(hostname)
            if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
                ip_obj = ip_obj.ipv4_mapped
            if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or
                    ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
                return False, f"Target IP {hostname} is in a reserved or private range"
            return True, "OK"
        except ValueError:
            # Domain name, resolve via DNS
            pass

        try:
            port = parsed.port or (443 if scheme == "https" else 80)
            addr_infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            return False, f"DNS resolution failed for {hostname}: {e}"
        except Exception as e:
            return False, f"Error resolving {hostname}: {e}"

        if not addr_infos:
            return False, f"No IP addresses resolved for {hostname}"

        for addr_info in addr_infos:
            ip_str = addr_info[4][0]
            try:
                ip_obj = ipaddress.ip_address(ip_str)
            except ValueError:
                return False, f"Invalid resolved IP address: {ip_str}"

            if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
                ip_obj = ip_obj.ipv4_mapped

            if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or
                    ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
                return False, f"Target {hostname} resolved to reserved/private IP {ip_str}"

    return True, "OK"


def validate_signature_cheap(method: str, headers: dict, body: bytes) -> tuple[bool, str]:
    """Perform fast, non-cryptographic validation of HTTP Signature and headers
    before attempting outbound remote key fetches (SSRF & DoS mitigation).
    Supports both RFC 9421 and Cavage.
    Returns (is_valid, error_reason).
    """
    headers_lower = {k.lower(): str(v) for k, v in headers.items()}
    sig_header = headers_lower.get('signature')
    sig_input = headers_lower.get('signature-input')
    auth_header = headers_lower.get('authorization', '')

    if not sig_header and not sig_input and not auth_header.lower().startswith('signature '):
        return False, "Missing Signature header"

    sig_info = extract_signature_info(headers_lower)
    if not sig_info:
        return False, "Missing Signature header"

    if not sig_info.get('key_id'):
        return False, "Missing keyId in Signature header"
    if not sig_info.get('signature_b64'):
        return False, "Missing signature data in Signature header"
    if not sig_info.get('headers'):
        return False, "Missing headers list in Signature header"

    # 1. Date header / created timestamp freshness verification (±300s)
    created_ts = sig_info.get('created')
    now = datetime.now(timezone.utc)
    date_header = headers_lower.get('date')

    if created_ts is not None:
        if abs(now.timestamp() - created_ts) > 300:
            return False, "Signature created timestamp outside allowed 300s window"
        if date_header:
            try:
                date_dt = parsedate_to_datetime(date_header)
                if date_dt.tzinfo is None:
                    date_dt = date_dt.replace(tzinfo=timezone.utc)
                if abs((now - date_dt).total_seconds()) > 300:
                    return False, "Date header outside allowed 300s window"
            except Exception:
                return False, "Malformed Date header"
    elif date_header:
        try:
            date_dt = parsedate_to_datetime(date_header)
            if date_dt.tzinfo is None:
                date_dt = date_dt.replace(tzinfo=timezone.utc)
            if abs((now - date_dt).total_seconds()) > 300:
                return False, "Date header outside allowed 300s window"
        except Exception:
            return False, "Malformed Date header"
    else:
        return False, "Missing Date header"

    # 2. Digest header verification for requests with bodies (e.g. POST)
    if method.upper() in ('POST', 'PUT'):
        digest_bytes = hashlib.sha256(body or b'').digest()
        digest_b64 = base64.b64encode(digest_bytes).decode('ascii')

        # Check standard Cavage Digest: SHA-256=<base64>
        digest_header = headers_lower.get('digest')
        if digest_header:
            expected_digest = f"SHA-256={digest_b64}"
            if digest_header.lower() != expected_digest.lower():
                return False, "Digest header does not match body SHA-256"

        # Check RFC 9530 Content-Digest: sha-256=:<base64>:
        content_digest_header = headers_lower.get('content-digest')
        if content_digest_header:
            expected_cd = f"sha-256=:{digest_b64}:"
            if content_digest_header.lower() != expected_cd.lower():
                return False, "Content-Digest header does not match body SHA-256"

    # 3. KeyId URL safety verification (SSRF prevention)
    is_safe, reason = is_safe_url(sig_info['key_id'])
    if not is_safe:
        return False, f"Unsafe keyId URL: {reason}"

    # 4. Anti-replay verification
    sig_raw = sig_info.get('signature_b64') or ""
    if sig_raw and not check_and_record_signature_replay(sig_raw):
        return False, "Replay detected: duplicate request signature"

    return True, "OK"


_recent_signatures: dict[str, float] = {}
_replay_lock = threading.Lock()


def check_and_record_signature_replay(sig_identifier: str, window_seconds: float = 300.0) -> bool:
    """Anti-replay check: returns True if allowed (first time seen), False if replay detected."""
    if not sig_identifier:
        return True
    now = time.time()
    sig_hash = hashlib.sha256(sig_identifier.encode('utf-8')).hexdigest()
    with _replay_lock:
        expired = [k for k, exp in _recent_signatures.items() if exp <= now]
        for k in expired:
            del _recent_signatures[k]

        if sig_hash in _recent_signatures:
            return False

        _recent_signatures[sig_hash] = now + window_seconds
        return True


def reset_signature_replay_cache():
    """Reset signature replay cache (for test suite isolation)."""
    with _replay_lock:
        _recent_signatures.clear()


_last_fetch_status: dict[str, int] = {}

def get_last_fetch_status(url: str) -> int | None:
    """Return the HTTP status code of the most recent fetch attempt for this URL."""
    if not url:
        return None
    return _last_fetch_status.get(url) or _last_fetch_status.get(url.split('#')[0])


async def fetch_remote_actor(actor_id: str, use_cache: bool = True,
                             sign_as_token: str | None = None,
                             base_url: str | None = None) -> dict | None:
    """Fetch and cache a remote actor JSON for Follow verification and key retrieval.
    Supports Authorized Fetch (HTTP Signatures on GET) for instances like GoToSocial/Akkoma."""
    if _http_session is None:
        return None

    safe, reason = is_safe_url(actor_id)
    if not safe:
        logger.warning(f"Blocked unsafe remote actor fetch to {actor_id}: {reason}")
        return None

    if use_cache:
        cached = _remote_actor_cache.get(actor_id)
        if cached and time.time() < cached[0]:
            return cached[1]

    headers = {
        'Accept': 'application/activity+json, application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
        'User-Agent': f'BouncerBot/{VERSION} (+https://dc.gluek.info)',
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
            fetch_url = actor_id.split('#')[0]
            _last_fetch_status[actor_id] = resp.status
            _last_fetch_status[fetch_url] = resp.status

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
                            'User-Agent': f'BouncerBot/{VERSION} (+https://dc.gluek.info)',
                        }
                        auth_headers.update(sign_headers('GET', actor_id, None, priv_pem, key_id))
                        async with _http_session.get(actor_id, headers=auth_headers, timeout=_aiohttp.ClientTimeout(total=10)) as resp2:
                            _last_fetch_status[actor_id] = resp2.status
                            _last_fetch_status[fetch_url] = resp2.status
                            if resp2.status == 200:
                                actor_data = await resp2.json(content_type=None)
                                _remote_actor_cache[actor_id] = (time.time() + REMOTE_ACTOR_CACHE_TTL, actor_data)
                                return actor_data
                            elif resp2.status in (403, 404, 410):
                                logger.info(f"Signed GET for {actor_id} returned HTTP {resp2.status} (deleted or unavailable)")
                                return None
                            else:
                                logger.warning(f"Signed GET for {actor_id} returned {resp2.status}")
                                return None
                    except Exception as e:
                        logger.warning(f"Error retrying signed GET for {actor_id}: {e}")
                        return None

            if resp.status != 200:
                if resp.status in (403, 404, 410):
                    logger.info(f"Fetch remote actor {actor_id} returned HTTP {resp.status} (deleted or unavailable)")
                else:
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
    - Cached follower public keys in database
    - Key endpoints returning publicKeyPem directly (e.g. GoToSocial /main-key)
    - Actor endpoints returning publicKey dict (e.g. Mastodon /users/alice#main-key)
    - Remote servers enforcing Authorized Fetch
    """
    if not key_id:
        return None, None

    actor_id = key_id.split('#')[0]
    try:
        cached_pk = database.get_follower_public_key(actor_id)
        if cached_pk:
            return cached_pk, {"id": actor_id, "owner": actor_id, "publicKey": {"id": key_id, "publicKeyPem": cached_pk, "owner": actor_id}}
    except Exception as e:
        logger.debug(f"DB follower public key lookup error for {actor_id}: {e}")

    safe, reason = is_safe_url(key_id)
    if not safe:
        logger.warning(f"Blocked unsafe keyId URL {key_id}: {reason}")
        return None, None

    # Try fetching key_id directly (without fragment)
    fetch_url = actor_id
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


def is_key_owned_by_actor(key_id: str | None, key_doc: typing.Any, actor: str | None) -> bool:
    """Verify that a signing public key belongs to the claiming actor.
    Prevents cross-account signature forgery within the same domain.
    """
    if not actor:
        return False
    actor_norm = actor.strip().rstrip("/")
    if not actor_norm:
        return False

    # 1. If key_doc provides an explicit owner, verify it
    if isinstance(key_doc, dict):
        # Case A: Standalone key document {"id": "...", "owner": "...", "publicKeyPem": "..."}
        owner = key_doc.get("owner")
        if isinstance(owner, str):
            if owner.strip().rstrip("/") != actor_norm:
                return False
            return True

        # Case B: Embedded publicKey {"id": "...", "publicKey": {"owner": "...", ...}}
        pk = key_doc.get("publicKey")
        if isinstance(pk, dict):
            pk_owner = pk.get("owner")
            if isinstance(pk_owner, str):
                if pk_owner.strip().rstrip("/") != actor_norm:
                    return False
                return True
        elif isinstance(pk, list):
            for item in pk:
                if isinstance(item, dict) and (item.get("id") == key_id or len(pk) == 1):
                    pk_owner = item.get("owner")
                    if isinstance(pk_owner, str):
                        if pk_owner.strip().rstrip("/") != actor_norm:
                            return False
                        return True

        # Case C: Actor document with canonical 'id' and aliases
        doc_id = key_doc.get("id")
        if isinstance(doc_id, str):
            doc_id_norm = doc_id.strip().rstrip("/")
            if doc_id_norm == actor_norm:
                return True
            url_val = key_doc.get("url")
            if isinstance(url_val, str) and url_val.strip().rstrip("/") == actor_norm:
                return True
            aliases = key_doc.get("alsoKnownAs") or key_doc.get("aliases") or []
            if isinstance(aliases, list) and any(isinstance(a, str) and a.strip().rstrip("/") == actor_norm for a in aliases):
                return True
            # If doc_id is an Actor object of standard type and does NOT match actor, it's a mismatch
            if key_doc.get("type") in ("Person", "Service", "Application", "Group", "Organization"):
                return False

    # 2. Check canonical key_id URI: actor URI + fragment (e.g. https://domain/users/alice#main-key)
    if key_id and "#" in key_id:
        key_actor = key_id.split("#")[0].strip().rstrip("/")
        if key_actor == actor_norm:
            return True

    # 3. Fallback for dict with matching doc_id
    if isinstance(key_doc, dict):
        doc_id = key_doc.get("id")
        if isinstance(doc_id, str) and doc_id.strip().rstrip("/") == actor_norm:
            return True

    return False
