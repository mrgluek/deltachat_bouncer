"""ActivityPub federation routes: actor, inbox, outbox, followers/following,
individual posts, WebFinger, NodeInfo, and the Mastodon-compatible instance stub.
"""
import asyncio
import json
import re
import time
import urllib.parse

try:
    from aiohttp import web
except ImportError:
    web = None

import activitypub
import config
import database
import security

_ap_backfill_semaphore: asyncio.Semaphore | None = None
_channel_last_backfill: dict[str, float] = {}

def _get_ap_backfill_semaphore() -> asyncio.Semaphore:
    global _ap_backfill_semaphore
    if _ap_backfill_semaphore is None:
        _ap_backfill_semaphore = asyncio.Semaphore(2)
    return _ap_backfill_semaphore


@security.rate_limited("ap_read", max_requests=120, window_seconds=60)
async def handle_ap_actor(request):
    """GET /c/{token} with Accept: application/activity+json -> Actor JSON."""
    token = request.match_info.get('token')
    channel = database.get_catalog_channel_by_token(token)
    if not channel or channel.get('is_deleted'):
        return web.Response(
            status=404,
            text=json.dumps({"error": "Channel not found"}),
            content_type="application/activity+json",
            charset="utf-8"
        )
    base_url = security._get_base_url(request)
    try:
        priv_pem, pub_pem = activitypub.get_or_create_actor_keys(token)
        actor_json = activitypub.build_actor_json(channel, base_url, pub_pem)
        return web.Response(
            text=json.dumps(actor_json, ensure_ascii=False),
            content_type="application/activity+json",
            charset="utf-8",
            headers={"Cache-Control": "public, max-age=300"}
        )
    except Exception as e:
        config.logger.error(f"Error building actor JSON for {token}: {e}")
        return web.Response(status=500, text="Internal server error")


def _extract_channel_token_from_activity(activity: dict) -> str | None:
    """Extract channel token from ActivityStreams activity object/target field."""
    if not isinstance(activity, dict):
        return None

    obj = activity.get("object")
    target_str = None
    if isinstance(obj, str):
        target_str = obj
    elif isinstance(obj, dict):
        # In Undo(Follow), obj is the Follow activity whose object is the channel actor
        inner_obj = obj.get("object")
        if isinstance(inner_obj, str):
            target_str = inner_obj
        elif isinstance(inner_obj, dict):
            target_str = inner_obj.get("id") or inner_obj.get("url")
        else:
            target_str = obj.get("id") or obj.get("url")

    if not target_str and isinstance(activity.get("target"), str):
        target_str = activity.get("target")

    if target_str:
        m = re.search(r'/c/([a-zA-Z0-9]{12})', target_str)
        if m:
            return m.group(1)
    return None


@security.rate_limited("inbox", max_requests=60, window_seconds=60)
async def handle_ap_inbox(request):
    """POST /c/{token}/inbox or POST /inbox (sharedInbox) — receive Follow/Undo/Delete activities."""
    body = await request.read()
    if not body:
        return web.Response(status=400, text="Empty request body")

    if len(body) > 65536:
        return web.Response(status=413, text="Payload Too Large")

    try:
        activity = json.loads(body.decode("utf-8"))
    except Exception:
        return web.Response(status=400, text="Invalid JSON body")

    base_url = security._get_base_url(request)

    # Determine channel token from route match_info or from activity object
    token = request.match_info.get('token')
    if not token:
        token = _extract_channel_token_from_activity(activity)

    act_type = activity.get("type")
    if token:
        channel = database.get_catalog_channel_by_token(token)
        if not channel or channel.get('is_deleted'):
            return web.Response(status=404, text="Channel not found")
    elif act_type not in ("Delete",):
        config.logger.warning(f"Could not resolve target channel token from AP activity: {activity}")
        return web.Response(status=404, text="Target channel not found")

    # Fast preliminary verification (Date freshness, Digest match, KeyId URL safety) before remote fetch
    req_headers = dict(request.headers)
    is_valid, reason = activitypub.validate_signature_cheap(request.method, req_headers, body)
    if not is_valid:
        config.logger.warning(f"AP inbox signature preliminary check failed: {reason}")
        return web.Response(status=401, text=reason)

    sig_info = activitypub.extract_signature_info(req_headers)
    key_id = sig_info.get("key_id") if sig_info else None

    # Verify KeyId <-> Actor origin binding
    activity_actor = activity.get("actor")
    if isinstance(activity_actor, dict):
        activity_actor = activity_actor.get("id") or activity_actor.get("url")
    if isinstance(activity_actor, str) and key_id:
        actor_host = urllib.parse.urlparse(activity_actor).netloc.lower()
        key_host = urllib.parse.urlparse(key_id).netloc.lower()
        if actor_host and key_host and actor_host != key_host:
            config.logger.warning(f"AP signature KeyId/Actor origin mismatch: key_id={key_id}, actor={activity_actor}")
            return web.Response(status=401, text="KeyId and actor origin mismatch")

    # Fetch remote public key with Authorized Fetch support
    pub_pem, key_doc = await activitypub.resolve_public_key(key_id, sign_as_token=token, base_url=base_url)
    if not pub_pem:
        fetch_status = activitypub.get_last_fetch_status(key_id)
        if act_type == "Delete" and fetch_status in (403, 404, 410):
            deleted_actor = activity.get("actor") or activity.get("object")
            if isinstance(deleted_actor, dict):
                deleted_actor = deleted_actor.get("id") or deleted_actor.get("url")
            if isinstance(deleted_actor, str) and key_id:
                actor_host = urllib.parse.urlparse(deleted_actor).netloc.lower()
                key_host = urllib.parse.urlparse(key_id).netloc.lower()
                if actor_host and actor_host == key_host:
                    removed = database.remove_ap_followers_by_actor(deleted_actor)
                    config.logger.info(
                        f"Processed Delete for gone/suspended AP actor: {deleted_actor} "
                        f"(remote status {fetch_status}, removed {removed} follower record(s))"
                    )
                    return web.Response(status=202, text="Accepted")

        config.logger.warning(f"Could not resolve remote actor public key for {key_id}")
        return web.Response(status=401, text="Could not resolve remote actor public key")

    # Verify key ownership by actor (prevent cross-actor signature forgery on same host)
    if activity_actor and not activitypub.is_key_owned_by_actor(key_id, key_doc, activity_actor):
        config.logger.warning(
            f"AP signature key ownership mismatch: key_id={key_id}, "
            f"key_owner={key_doc.get('owner') if isinstance(key_doc, dict) else None}, "
            f"actor={activity_actor}"
        )
        return web.Response(status=401, text="Key owner does not match activity actor")

    req_path = getattr(request, 'raw_path', None) or getattr(request, 'path_qs', None) or request.path
    target_uri = f"{base_url.rstrip('/')}{req_path}" if base_url else str(request.url)
    if not activitypub.verify_http_signature(request.method, req_path, req_headers, body, pub_pem, target_uri=target_uri):
        config.logger.warning(f"AP signature verification failed for {key_id} on {req_path}")
        return web.Response(status=401, text="Invalid signature")

    actor_url = f"{base_url}/c/{token}" if token else ""

    if act_type == "Follow":
        follower_id = activity.get("actor")
        if follower_id and token:
            follower_actor = await activitypub.fetch_remote_actor(follower_id, sign_as_token=token, base_url=base_url)
            if follower_actor:
                inbox = follower_actor.get("inbox")
                shared_inbox = follower_actor.get("endpoints", {}).get("sharedInbox") if isinstance(follower_actor.get("endpoints"), dict) else None
                if inbox or shared_inbox:
                    follower_pk = None
                    pk_obj = follower_actor.get("publicKey")
                    if isinstance(pk_obj, dict):
                        follower_pk = pk_obj.get("publicKeyPem")
                    elif isinstance(pk_obj, list):
                        for item in pk_obj:
                            if isinstance(item, dict) and item.get("publicKeyPem"):
                                follower_pk = item["publicKeyPem"]
                                break
                    database.add_ap_follower(
                        token, follower_id, inbox or shared_inbox, shared_inbox,
                        follower_public_key=follower_pk
                    )
                    config.logger.info(f"New AP follower for channel {token}: {follower_id}")
                    # Build and send Accept activity, followed by backfilling recent posts
                    priv_pem, _ = activitypub.get_or_create_actor_keys(token)
                    accept_act = activitypub.build_accept_follow(actor_url, activity)
                    accept_body = json.dumps(accept_act, ensure_ascii=False).encode("utf-8")
                    target_inbox = inbox or shared_inbox

                    # Security check: validate that target_inbox belongs to the same host as follower_id
                    follower_host = urllib.parse.urlparse(follower_id).netloc.lower()
                    inbox_host = urllib.parse.urlparse(target_inbox).netloc.lower()
                    if not follower_host or not inbox_host or follower_host != inbox_host:
                        config.logger.warning(
                            f"AP target inbox host mismatch: follower={follower_id} ({follower_host}) vs "
                            f"target_inbox={target_inbox} ({inbox_host}). Rejecting delivery to foreign inbox."
                        )
                        return web.Response(status=202, text="Accepted")

                    async def _accept_and_backfill(t_inbox, a_body, p_pem, k_id, t_token, b_url):
                        await activitypub.deliver_to_inbox(t_inbox, a_body, p_pem, k_id)
                        now = time.time()
                        last_bf = _channel_last_backfill.get(t_token, 0)
                        if now - last_bf < 10.0:
                            config.logger.info(f"Skipping AP backfill for channel {t_token}: backfill cooldown active")
                            return
                        _channel_last_backfill[t_token] = now
                        sem = _get_ap_backfill_semaphore()
                        async with sem:
                            await activitypub.deliver_backfill_posts(t_token, t_inbox, b_url, limit=10)

                    asyncio.create_task(_accept_and_backfill(
                        target_inbox, accept_body, priv_pem, f"{actor_url}#main-key", token, base_url
                    ))
                else:
                    config.logger.warning(f"Follower {follower_id} has no inbox or sharedInbox")
            else:
                config.logger.warning(f"Could not fetch follower actor {follower_id}")
        return web.Response(status=202, text="Accepted")

    elif act_type == "Undo":
        obj = activity.get("object")
        is_undo_follow = False
        if isinstance(obj, dict) and obj.get("type") == "Follow":
            is_undo_follow = True
        elif isinstance(obj, str):
            is_undo_follow = True
        if is_undo_follow:
            follower_id = activity.get("actor")
            if follower_id and token:
                database.remove_ap_follower(token, follower_id)
                config.logger.info(f"Removed AP follower for channel {token}: {follower_id}")
        return web.Response(status=202, text="Accepted")

    elif act_type == "Delete":
        deleted_actor = activity.get("actor") or activity.get("object")
        if isinstance(deleted_actor, dict):
            deleted_actor = deleted_actor.get("id") or deleted_actor.get("url")
        if isinstance(deleted_actor, str):
            database.remove_ap_followers_by_actor(deleted_actor)
            config.logger.info(f"Removed deleted AP actor: {deleted_actor}")
        return web.Response(status=202, text="Accepted")

    return web.Response(status=202, text="Accepted")


@security.rate_limited("ap_read", max_requests=120, window_seconds=60)
async def handle_ap_outbox(request):
    """GET /c/{token}/outbox -> OrderedCollection or OrderedCollectionPage."""
    token = request.match_info.get('token')
    channel = database.get_catalog_channel_by_token(token)
    if not channel or channel.get('is_deleted'):
        return web.Response(status=404, text="Channel not found")

    base_url = security._get_base_url(request)
    outbox_url = f"{base_url}/c/{token}/outbox"
    actor_url = f"{base_url}/c/{token}"

    posts = database.get_channel_posts(channel['chat_id'], limit=50)
    ordered_items = []
    for p in posts:
        note = activitypub.build_note(channel, p, base_url)
        ordered_items.append(activitypub.build_create_activity(actor_url, note))

    is_page = "page" in request.query or request.query.get("page") in ("true", "1")

    if is_page:
        page_url = f"{outbox_url}?page=true"
        collection_page = activitypub.build_ordered_collection_page(
            page_id=page_url,
            part_of=outbox_url,
            total_items=len(ordered_items),
            ordered_items=ordered_items
        )
        return web.Response(
            text=json.dumps(collection_page, ensure_ascii=False),
            content_type="application/activity+json",
            charset="utf-8",
            headers={"Cache-Control": "public, max-age=60"}
        )

    # Root OrderedCollection with pointer to first page
    first_url = f"{outbox_url}?page=true" if ordered_items else None
    collection = activitypub.build_ordered_collection(outbox_url, len(ordered_items), first=first_url, last=first_url)
    collection["orderedItems"] = ordered_items

    return web.Response(
        text=json.dumps(collection, ensure_ascii=False),
        content_type="application/activity+json",
        charset="utf-8",
        headers={"Cache-Control": "public, max-age=60"}
    )


@security.rate_limited("ap_read", max_requests=120, window_seconds=60)
async def handle_ap_followers(request):
    """GET /c/{token}/followers -> OrderedCollection (count only)."""
    token = request.match_info.get('token')
    channel = database.get_catalog_channel_by_token(token)
    if not channel or channel.get('is_deleted'):
        return web.Response(status=404, text="Channel not found")

    base_url = security._get_base_url(request)
    followers_url = f"{base_url}/c/{token}/followers"
    count = database.get_ap_followers_count(token)

    collection = activitypub.build_ordered_collection(followers_url, count)
    return web.Response(
        text=json.dumps(collection, ensure_ascii=False),
        content_type="application/activity+json",
        charset="utf-8",
        headers={"Cache-Control": "public, max-age=60"}
    )


@security.rate_limited("ap_read", max_requests=120, window_seconds=60)
async def handle_ap_following(request):
    """GET /c/{token}/following -> OrderedCollection (empty, count 0)."""
    token = request.match_info.get('token')
    channel = database.get_catalog_channel_by_token(token)
    if not channel or channel.get('is_deleted'):
        return web.Response(status=404, text="Channel not found")

    base_url = security._get_base_url(request)
    following_url = f"{base_url}/c/{token}/following"

    collection = activitypub.build_ordered_collection(following_url, 0)
    return web.Response(
        text=json.dumps(collection, ensure_ascii=False),
        content_type="application/activity+json",
        charset="utf-8",
        headers={"Cache-Control": "public, max-age=300"}
    )


@security.rate_limited("ap_read", max_requests=120, window_seconds=60)
async def handle_ap_post(request):
    """GET /c/{token}/posts/{msg_id} -> Note object."""
    token = request.match_info.get('token')
    msg_id_str = request.match_info.get('msg_id')
    try:
        msg_id = int(msg_id_str)
    except (ValueError, TypeError):
        return web.Response(status=400, text="Invalid message ID")

    channel = database.get_catalog_channel_by_token(token)
    if not channel or channel.get('is_deleted'):
        return web.Response(status=404, text="Channel not found")

    posts = database.get_channel_posts(channel['chat_id'], limit=100)
    found_post = None
    for p in posts:
        if p.get('msg_id') == msg_id or p.get('id') == msg_id:
            found_post = p
            break

    if not found_post:
        return web.Response(status=404, text="Post not found")

    base_url = security._get_base_url(request)
    note = activitypub.build_note(channel, found_post, base_url)
    return web.Response(
        text=json.dumps(note, ensure_ascii=False),
        content_type="application/activity+json",
        charset="utf-8",
        headers={"Cache-Control": "public, max-age=300"}
    )


@security.rate_limited("ap_meta", max_requests=120, window_seconds=60)
async def handle_webfinger(request):
    """GET /.well-known/webfinger?resource=acct:{token}@{domain} -> JRD JSON."""
    resource = request.query.get("resource", "").strip()
    if not resource.startswith("acct:"):
        return web.Response(status=400, text="Bad Request: Missing or invalid resource query param")

    user_host = resource[5:]
    username = user_host.split("@")[0]

    channel = database.get_catalog_channel_by_token(username)
    if not channel or channel.get("is_deleted"):
        return web.Response(status=404, text="User not found")

    base_url = security._get_base_url(request)
    actor_url = f"{base_url}/c/{username}"

    data = {
        "subject": resource,
        "aliases": [actor_url],
        "links": [
            {
                "rel": "self",
                "type": "application/activity+json",
                "href": actor_url
            },
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": actor_url
            }
        ]
    }
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/jrd+json",
        charset="utf-8",
        headers={"Cache-Control": "public, max-age=3600"}
    )


@security.rate_limited("ap_meta", max_requests=120, window_seconds=60)
async def handle_nodeinfo_discovery(request):
    """GET /.well-known/nodeinfo -> nodeinfo discovery document."""
    base_url = security._get_base_url(request)
    data = {
        "links": [
            {
                "rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                "href": f"{base_url}/nodeinfo/2.0"
            }
        ]
    }
    return web.json_response(data)


@security.rate_limited("ap_meta", max_requests=120, window_seconds=60)
async def handle_nodeinfo(request):
    """GET /nodeinfo/2.0 -> NodeInfo 2.0 metadata."""
    channels = database.get_all_catalog_channels(include_deleted=False)
    data = {
        "version": "2.0",
        "software": {
            "name": "deltachat-bouncer",
            "version": config.VERSION
        },
        "protocols": ["activitypub"],
        "services": {
            "inbound": [],
            "outbound": []
        },
        "openRegistrations": False,
        "usage": {
            "users": {
                "total": len(channels)
            }
        },
        "metadata": {
            "nodeName": "Delta Chat Bouncer Channel Relay",
            "nodeDescription": "Delta Chat channel broadcast to ActivityPub"
        }
    }
    return web.json_response(
        data,
        content_type="application/json; profile=\"http://nodeinfo.diaspora.software/ns/schema/2.0#\""
    )


@security.rate_limited("ap_meta", max_requests=120, window_seconds=60)
async def handle_api_v1_instance(request):
    """GET /api/v1/instance -> Mastodon-compatible v1 instance metadata."""
    base_url = security._get_base_url(request)
    parsed = urllib.parse.urlparse(base_url)
    domain = parsed.netloc or getattr(request, "host", "") or "localhost"
    channels = database.get_all_catalog_channels(include_deleted=False)
    posts_count = database.get_total_channel_posts_count()
    admin_email = database.get_config("admin_dc_email") or database.get_config("admin_email") or ""

    data = {
        "uri": domain,
        "title": "Delta Chat Bouncer Channel Relay",
        "short_description": "Delta Chat channel broadcast to the Fediverse (ActivityPub)",
        "description": f"Delta Chat channel broadcast to the Fediverse (ActivityPub). Follow channels as @<token>@{domain}",
        "email": admin_email,
        "version": f"{config.VERSION} (compatible; DeltaChatBouncer)",
        "urls": {
            "streaming_api": ""
        },
        "stats": {
            "user_count": len(channels),
            "status_count": posts_count,
            "domain_count": 0
        },
        "thumbnail": f"{base_url}/background.jpg",
        "languages": ["en"],
        "registrations": False,
        "approval_required": False,
        "invites_enabled": False,
        "configuration": {
            "statuses": {
                "max_characters": 5000,
                "max_media_attachments": 4
            }
        },
        "contact_account": None,
        "rules": []
    }
    return web.json_response(data, headers={"Cache-Control": "public, max-age=300"})


