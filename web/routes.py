"""Public web routes for the channel preview / landing-page aiohttp server:
static assets, robots.txt, health, the landing page, QR codes, channel
preview + RSS, media files, and the server bootstrap that also wires in the
ActivityPub routes from ap_routes.py.
"""
import asyncio
import hashlib
import io
import os
import shutil
import time

import qrcode

try:
    from aiohttp import web
except ImportError:
    web = None

import config
import database
import security
import state
from web import ap_routes
from web.templates import channel as tpl_channel
from web.templates import errors as tpl_errors
from web.templates import feed as tpl_feed
from web.templates import landing as tpl_landing

def invalidate_channel_cache(chat_id: int = None, token: str = None) -> None:
    """Invalidate in-memory preview and RSS caches for a specific channel or all channels."""
    state.index_page_html_cache = None
    if chat_id is None and token is None:
        state._channel_preview_cache.clear()
        state._channel_rss_cache.clear()
        return

    target_tokens = []
    if token:
        target_tokens.append(token)
    elif chat_id:
        try:
            ch = database.get_catalog_channel_by_chat_id(chat_id)
            if ch and ch.get("token"):
                target_tokens.append(ch["token"])
        except Exception:
            pass

    for t in target_tokens:
        prefix = f"{t}:"
        for k in list(state._channel_preview_cache.keys()):
            if k.startswith(prefix):
                state._channel_preview_cache.pop(k, None)
        for k in list(state._channel_rss_cache.keys()):
            if k.startswith(prefix):
                state._channel_rss_cache.pop(k, None)


def _generate_qr_bytes(link: str, fmt: str = "png", box_size: int = 6) -> tuple[bytes, str]:
    """Generate and cache QR code image bytes in memory."""
    cache_key = f"{link}:{fmt}:{box_size}"
    if cache_key in state._qr_cache:
        return state._qr_cache[cache_key]

    buf = io.BytesIO()
    if fmt == "svg":
        try:
            import qrcode.image.svg as qrcode_svg
            svg_factory = qrcode_svg.SvgPathImage
        except (ImportError, AttributeError):
            svg_factory = None
        qr = qrcode.QRCode(image_factory=svg_factory, border=2)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image()
        img.save(buf)
        res = (buf.getvalue(), "image/svg+xml")
    else:
        qr = qrcode.QRCode(box_size=box_size, border=2)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(buf, format="PNG")
        res = (buf.getvalue(), "image/png")

    state._qr_cache[cache_key] = res
    while len(state._qr_cache) > 200:
        state._qr_cache.pop(next(iter(state._qr_cache)), None)
    return res


def get_bot_avatar_file_path() -> str | None:
    base_dir = os.path.abspath(os.path.dirname(__file__) if "__file__" in globals() else ".")
    avatar_env = os.environ.get("AVATAR_PATH") or database.get_config("bot_avatar_path")
    candidates = []
    if avatar_env:
        if os.path.isabs(avatar_env):
            candidates.append(avatar_env)
        else:
            candidates.append(os.path.join(base_dir, avatar_env))
            candidates.append(os.path.join(base_dir, "static", avatar_env))
            candidates.append(os.path.join(base_dir, "data", avatar_env))
            candidates.append(os.path.abspath(avatar_env))
            candidates.append(avatar_env)

    if state.dc_bot_instance and state.dc_accid:
        try:
            selfavatar = state.dc_bot_instance.rpc.get_config(state.dc_accid, "selfavatar")
            if selfavatar and os.path.exists(selfavatar):
                candidates.append(selfavatar)
        except Exception:
            pass

    candidates.extend([
        os.path.join(base_dir, "static", "icon.png"),
        os.path.join(base_dir, "icon.png"),
        os.path.join(base_dir, "data", "icon.png"),
        "static/icon.png",
        "icon.png",
    ])

    for c in candidates:
        if c and os.path.exists(c) and os.path.isfile(c):
            return c
    return None

def _get_allowed_icon_filenames() -> set[str]:
    names = {"icon.png", "favicon.ico"}
    avatar_env = os.environ.get("AVATAR_PATH") or database.get_config("bot_avatar_path")
    if avatar_env:
        names.add(os.path.basename(avatar_env))
    return names

_ALLOWED_BG_FILENAMES = {"background.jpg", "background-light.jpg", "background-light.jpg"}

_DEFAULT_CHANNEL_AVATAR_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:svg="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" id="svg2985" width="48" height="48" version="1.1">'
    '<defs id="defs2987">'
    '<linearGradient id="linearGradient4409">'
    '<stop style="stop-color:#f9f9f9;stop-opacity:1" id="stop4411" offset="0"/>'
    '<stop style="stop-color:#ccc;stop-opacity:0" id="stop4413" offset="1"/>'
    '</linearGradient>'
    '<linearGradient id="linearGradient4399">'
    '<stop style="stop-color:#f9f9f9;stop-opacity:1" id="stop4401" offset="0"/>'
    '<stop style="stop-color:#f9f9f9;stop-opacity:0" id="stop4403" offset="1"/>'
    '</linearGradient>'
    '<linearGradient id="linearGradient4375">'
    '<stop style="stop-color:#364e59;stop-opacity:1" id="stop4377" offset="0"/>'
    '<stop style="stop-color:#364e59;stop-opacity:0" id="stop4379" offset="1"/>'
    '</linearGradient>'
    '<linearGradient id="linearGradient4367">'
    '<stop style="stop-color:#dc000f;stop-opacity:1" id="stop4369" offset="0"/>'
    '<stop style="stop-color:#0f0;stop-opacity:0" id="stop4371" offset="1"/>'
    '</linearGradient>'
    '<linearGradient id="linearGradient4359">'
    '<stop style="stop-color:#dc000f;stop-opacity:1" id="stop4361" offset="0"/>'
    '<stop style="stop-color:#000;stop-opacity:0" id="stop4363" offset="1"/>'
    '</linearGradient>'
    '<linearGradient id="linearGradient4381" x1="31.957" x2="-45.041" y1="29.751" y2="-18.592" '
    'gradientTransform="matrix(0.93766393,0,0,0.93766393,1.542566,1.7199693)" '
    'gradientUnits="userSpaceOnUse" xlink:href="#linearGradient4375"/>'
    '<linearGradient id="linearGradient4415" x1="16.345" x2="36.002" y1="3.839" y2="24.359" '
    'gradientUnits="userSpaceOnUse" xlink:href="#linearGradient4409"/>'
    '</defs>'
    '<metadata id="metadata2990"/>'
    '<g id="layer1">'
    '<path style="fill:#fff;fill-opacity:1;stroke:#000;stroke-width:.57405078;stroke-linejoin:round;'
    'stroke-miterlimit:4;stroke-dasharray:none;stroke-opacity:.43921569" id="path3769" '
    'd="m 24.015419,1.2870249 c -12.549421,0 -22.7283936,10.1789711 -22.7283936,22.7283931 '
    '0,12.549422 10.1789726,22.728395 22.7283936,22.728395 14.337742,-0.342877 9.614352,-4.702705 '
    '23.697556,0.969161 -7.545453,-13.001555 -1.082973,-13.32964 -0.969161,-23.697556 '
    '0,-12.549422 -10.178973,-22.7283931 -22.728395,-22.7283931 z"/>'
    '<path id="path3799" '
    'd="M 23.982249,5.3106163 C 13.645822,5.4364005 5.2618355,13.92999 5.2618355,24.275753 '
    'c 0,10.345764 8.3839865,18.635301 18.7204135,18.509516 9.827724,-0.03951 7.516769,-5.489695 '
    '18.380082,-0.443187 -5.950849,-9.296115 0.201753,-10.533667 0.340336,-18.521947 '
    '0,-10.345766 -8.383989,-18.6353031 -18.720418,-18.5095187 z" '
    'style="fill:url(#linearGradient4381);fill-opacity:1;stroke:none"/>'
    '<g style="font-style:normal;font-weight:400;font-size:42.10587311px;line-height:125%;'
    'font-family:Sans;letter-spacing:0;word-spacing:0;fill:#fff;fill-opacity:1;stroke:none" '
    'id="text3797" transform="scale(1.1122373,0.89908874)">'
    '<path style="font-family:\'Times New Roman\';-inkscape-font-specification:\'Times New Roman\';'
    'fill:#fff;fill-opacity:1" id="path4161" '
    'd="m 21.688854,23.636251 q -1.027975,-1.151333 -2.857771,-2.754974 -2.014832,-1.768118 '
    '-2.713855,-2.775534 -0.699024,-1.027975 -0.699024,-2.240986 0,-1.809237 1.68588,-2.837212 '
    '1.68588,-1.048535 4.399735,-1.048535 2.713855,0 4.728687,0.925178 2.035391,0.925177 '
    '2.035391,2.549379 0,0.781261 -0.493428,1.295249 -0.493428,0.513987 -1.151333,0.513987 '
    '-0.945737,0 -2.220426,-1.418606 -1.295249,-1.439165 -2.199868,-2.014832 '
    '-0.884059,-0.596225 -2.07651,-0.596225 -1.521404,0 -2.50826,0.678463 '
    '-0.966297,0.678464 -0.966297,1.726999 0,0.986857 0.801821,1.850356 '
    '0.801821,0.863499 4.132461,3.145605 3.556795,2.446581 5.01652,3.824068 '
    '1.480285,1.377487 2.405462,3.3512 0.925178,1.973713 0.925178,4.17358 '
    '0,3.865188 -2.734414,6.825757 -2.713855,2.94001 -6.352888,2.94001 '
    '-3.310081,0 -5.592187,-2.364344 -2.282105,-2.364343 -2.282105,-6.311769 '
    '0,-3.803509 2.50826,-6.352888 2.528819,-2.549379 6.208971,-3.083926 z '
    'm 0.904619,0.945737 q -5.900579,0.966297 -5.900579,8.100447 0,3.680152 1.459725,5.715543 '
    '1.480285,2.035391 3.433438,2.035391 2.035391,0 3.3512,-1.953153 '
    '1.315808,-1.973713 1.315808,-5.324913 0,-4.852044 -3.659592,-8.573315 z"/>'
    '</g>'
    '</g>'
    '</svg>'
)

def get_default_channel_avatar_response():
    return web.Response(
        text=_DEFAULT_CHANNEL_AVATAR_SVG,
        content_type="image/svg+xml",
        headers={'Cache-Control': 'public, max-age=3600'}
    )

async def handle_channel_default_avatar(request):
    return get_default_channel_avatar_response()

@security.rate_limited("assets", max_requests=120, window_seconds=60)
async def handle_icon(request):
    raw_filename = os.path.basename(getattr(request, "path", "")) if isinstance(getattr(request, "path", None), str) else "icon.png"
    if raw_filename not in _get_allowed_icon_filenames():
        return web.Response(status=404)
    target = get_bot_avatar_file_path()
    if target and os.path.exists(target) and os.path.isfile(target):
        content_type = security._guess_media_content_type(target)
        headers = {
            'Cache-Control': 'public, max-age=3600',
            'Content-Type': content_type,
        }
        return web.FileResponse(target, headers=headers)
    return web.Response(status=404)


@security.rate_limited("assets", max_requests=120, window_seconds=60)
async def handle_background(request):
    raw_filename = os.path.basename(getattr(request, "path", "")) if isinstance(getattr(request, "path", None), str) else "background.jpg"
    if raw_filename not in _ALLOWED_BG_FILENAMES:
        return web.Response(status=404)
    bg_file = raw_filename if raw_filename in _ALLOWED_BG_FILENAMES else "background.jpg"
    base_dir = os.path.abspath(os.path.dirname(__file__) if "__file__" in globals() else ".")
    for candidate in (
        os.path.join(base_dir, "static", bg_file),
        os.path.join(base_dir, bg_file),
        f"static/{bg_file}",
        bg_file,
    ):
        if os.path.exists(candidate) and os.path.isfile(candidate):
            content_type = security._guess_media_content_type(candidate)
            headers = {
                'Cache-Control': 'public, max-age=31536000, immutable',
                'Content-Type': content_type,
            }
            return web.FileResponse(candidate, headers=headers)
    return web.Response(status=404)


async def handle_robots_txt(request):
    headers = {"Cache-Control": "public, max-age=86400, immutable"}
    content = """# Bouncer Bot robots.txt — modeled after GoToSocial
# AI scrapers and the like.
# https://github.com/ai-robots-txt/ai.robots.txt/
User-agent: AI2Bot
User-agent: Ai2Bot-Dolma
User-agent: Amazonbot
User-agent: Applebot-Extended
User-agent: Bytespider
User-agent: CCBot
User-agent: ChatGPT-User
User-agent: ClaudeBot
User-agent: Claude-Web
User-agent: Diffbot
User-agent: FacebookBot
User-agent: facebookexternalhit
User-agent: Google-Extended
User-agent: GoogleOther
User-agent: GPTBot
User-agent: ImagesiftBot
User-agent: img2dataset
User-agent: Meta-ExternalAgent
User-agent: Meta-ExternalFetcher
User-agent: OAI-SearchBot
User-agent: PetalBot
User-agent: Scrapy
User-agent: anthropic-ai
User-agent: cohere-ai
User-agent: DeepSeekBot
User-agent: PerplexityBot
User-agent: TikTokSpider
Disallow: /

# Marketing/SEO data scrapers
User-agent: DataForSeoBot
User-agent: Meltwater
User-agent: SemrushBot-OCOB
User-agent: SemrushBot-SWA
Disallow: /

# Rules for everything else.
User-agent: *
Crawl-delay: 500

# Allow channel pages and media for legitimate indexing.
Allow: /c/
Allow: /media/

# Disallow internal endpoints.
Disallow: /health
Disallow: /qr.svg
Disallow: /qr.png

# Disallow WebFinger (Fediverse handles it via direct HTTP, not crawlers).
Disallow: /.well-known/webfinger
Disallow: /.well-known/nodeinfo
Disallow: /nodeinfo/
"""
    return web.Response(text=content, content_type="text/plain", headers=headers)


async def handle_health(request):
    return web.json_response({"status": "ok", "service": "bouncer_bot", "version": config.VERSION})


@security.rate_limited("index", max_requests=60, window_seconds=60)
async def handle_index(request):
    ingress_path = request.headers.get("X-Ingress-Path", "")
    content = tpl_landing.get_landing_page_html(ingress_path=ingress_path)
    return web.Response(text=content, content_type="text/html", headers={"Cache-Control": "public, max-age=300"})


@security.rate_limited("qr", max_requests=60, window_seconds=60)
async def handle_qr_svg(request):
    link = state.get_bot_invite_link()
    if not link:
        return web.Response(status=404)
    try:
        body, content_type = await asyncio.to_thread(_generate_qr_bytes, link, fmt="svg")
        headers = {"Cache-Control": "public, max-age=86400, immutable", "Content-Type": content_type}
        return web.Response(body=body, headers=headers)
    except Exception as e:
        config.logger.error(f"Error generating qr.svg: {e}")
        return web.Response(status=500)


@security.rate_limited("qr", max_requests=60, window_seconds=60)
async def handle_qr_png(request):
    link = state.get_bot_invite_link()
    if not link:
        return web.Response(status=404)
    try:
        body, content_type = await asyncio.to_thread(_generate_qr_bytes, link, fmt="png", box_size=6)
        headers = {"Cache-Control": "public, max-age=86400, immutable", "Content-Type": content_type}
        return web.Response(body=body, headers=headers)
    except Exception as e:
        config.logger.error(f"Error generating qr.png: {e}")
        return web.Response(status=500)


@security.rate_limited("channel_preview", max_requests=120, window_seconds=60)
async def handle_channel_preview(request):
    # ── ActivityPub Content Negotiation ──
    accept = request.headers.get("Accept", "")
    if "application/activity+json" in accept or 'profile="https://www.w3.org/ns/activitystreams"' in accept:
        return await ap_routes.handle_ap_actor(request)

    ingress_path = request.headers.get("X-Ingress-Path", "")
    token = request.match_info.get('token')

    cache_key = f"{token}:{ingress_path}"
    cached = state._channel_preview_cache.get(cache_key)
    now = time.time()
    if cached and now < cached[0]:
        _, etag, html_content = cached
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag, "Cache-Control": "public, max-age=60"})
        return web.Response(text=html_content, content_type="text/html", headers={"ETag": etag, "Cache-Control": "public, max-age=60"})

    channel = database.get_catalog_channel_by_token(token)
    if not channel:
        return web.Response(text=tpl_errors.get_404_html(ingress_path=ingress_path), status=404, content_type="text/html")

    if channel.get('is_deleted'):
        return web.Response(text=tpl_errors.get_tombstone_html(channel.get("name") or "Channel", ingress_path=ingress_path), status=200, content_type="text/html")

    base_url = security._get_base_url(request)

    posts = database.get_channel_posts(channel['chat_id'], limit=50)
    html_content = tpl_channel.get_channel_preview_html(channel, posts, base_url, ingress_path=ingress_path)
    etag = f'"{hashlib.md5(html_content.encode("utf-8")).hexdigest()}"'
    state._channel_preview_cache[cache_key] = (now + 60.0, etag, html_content)

    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers={"ETag": etag, "Cache-Control": "public, max-age=60"})

    return web.Response(text=html_content, content_type="text/html", headers={"ETag": etag, "Cache-Control": "public, max-age=60"})


@security.rate_limited("qr", max_requests=60, window_seconds=60)
async def handle_channel_qr_png(request):
    token = request.match_info.get('token')
    channel = database.get_catalog_channel_by_token(token)
    if not channel or not channel.get('invite_link'):
        return web.Response(status=404)

    raw_link = channel.get('invite_link')
    if raw_link.startswith("OPEN-CHAT:"):
        link = "https://i.delta.chat/#" + raw_link[10:]
    elif raw_link.startswith("OPEN:"):
        link = "https://i.delta.chat/#" + raw_link[5:]
    elif raw_link.startswith("dcqr://"):
        link = "https://i.delta.chat/#" + raw_link[7:]
    else:
        link = raw_link

    try:
        body, content_type = await asyncio.to_thread(_generate_qr_bytes, link, fmt="png", box_size=6)
        headers = {"Cache-Control": "public, max-age=86400, immutable", "Content-Type": content_type}
        return web.Response(body=body, headers=headers)
    except Exception as e:
        config.logger.error(f"Error generating channel qr.png: {e}")
        return web.Response(status=500)


@security.rate_limited("qr", max_requests=60, window_seconds=60)
async def handle_channel_qr_svg(request):
    token = request.match_info.get('token')
    channel = database.get_catalog_channel_by_token(token)
    if not channel or not channel.get('invite_link'):
        return web.Response(status=404)

    raw_link = channel.get('invite_link')
    if raw_link.startswith("OPEN-CHAT:"):
        link = "https://i.delta.chat/#" + raw_link[10:]
    elif raw_link.startswith("OPEN:"):
        link = "https://i.delta.chat/#" + raw_link[5:]
    elif raw_link.startswith("dcqr://"):
        link = "https://i.delta.chat/#" + raw_link[7:]
    else:
        link = raw_link

    try:
        body, content_type = await asyncio.to_thread(_generate_qr_bytes, link, fmt="svg")
        headers = {"Cache-Control": "public, max-age=86400, immutable", "Content-Type": content_type}
        return web.Response(body=body, headers=headers)
    except Exception as e:
        config.logger.error(f"Error generating channel qr.svg: {e}")
        return web.Response(status=500)


@security.rate_limited("assets", max_requests=120, window_seconds=60)
async def handle_channel_avatar(request):
    token = request.match_info.get('token')
    channel = database.get_catalog_channel_by_token(token)
    if not channel:
        return web.Response(status=404)

    avatar_cache_path = os.path.join(state.CHANNEL_MEDIA_DIR, token, "avatar.png")
    if os.path.exists(avatar_cache_path):
        content_type = security._guess_media_content_type(avatar_cache_path)
        return web.FileResponse(avatar_cache_path, headers={'Cache-Control': 'public, max-age=3600', 'Content-Type': content_type})

    if state.dc_bot_instance and state.dc_accid:
        chat_id = channel.get('chat_id')
        try:
            prof_img = None
            # 1. Check get_basic_chat_info (supporting camelCase profileImage and snake_case profile_image)
            try:
                chat_info = state.dc_bot_instance.rpc.get_basic_chat_info(state.dc_accid, chat_id)
                if chat_info:
                    prof_img = (
                        chat_info.get("profileImage") or chat_info.get("profile_image")
                        if isinstance(chat_info, dict)
                        else (getattr(chat_info, "profile_image", None) or getattr(chat_info, "profileImage", None))
                    )
            except Exception:
                pass

            # 2. Check get_full_chat_by_id
            if not prof_img:
                try:
                    full_chat = state.dc_bot_instance.rpc.get_full_chat_by_id(state.dc_accid, chat_id)
                    if full_chat:
                        prof_img = (
                            full_chat.get("profileImage") or full_chat.get("profile_image")
                            if isinstance(full_chat, dict)
                            else (getattr(full_chat, "profile_image", None) or getattr(full_chat, "profileImage", None))
                        )
                except Exception:
                    pass

            # 3. Check contacts in chat
            if not prof_img:
                try:
                    contacts = state.dc_bot_instance.rpc.get_chat_contacts(state.dc_accid, chat_id)
                    other_contacts = [c for c in contacts if c != 1]
                    if other_contacts:
                        contact = state.dc_bot_instance.rpc.get_contact(state.dc_accid, other_contacts[0])
                        if contact:
                            prof_img = (
                                contact.get("profileImage") or contact.get("profile_image")
                                if isinstance(contact, dict)
                                else (getattr(contact, "profile_image", None) or getattr(contact, "profileImage", None))
                            )
                except Exception:
                    pass

            if prof_img and os.path.exists(prof_img):
                os.makedirs(os.path.join(state.CHANNEL_MEDIA_DIR, token), exist_ok=True)
                shutil.copy2(prof_img, avatar_cache_path)
                content_type = security._guess_media_content_type(avatar_cache_path)
                return web.FileResponse(avatar_cache_path, headers={'Cache-Control': 'public, max-age=3600', 'Content-Type': content_type})
        except Exception as e:
            config.logger.debug(f"Failed to fetch avatar for channel {token}: {e}")

    # For channels without an avatar, serve the default channel avatar SVG (NOT the bot's icon!)
    return get_default_channel_avatar_response()


@security.rate_limited("rss", max_requests=60, window_seconds=60)
async def handle_channel_rss(request):
    token = request.match_info.get('token')
    base_url = security._get_base_url(request)

    cache_key = f"{token}:{base_url}"
    cached = state._channel_rss_cache.get(cache_key)
    now = time.time()
    if cached and now < cached[0]:
        _, etag, rss_xml = cached
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag, "Cache-Control": "public, max-age=120"})
        return web.Response(text=rss_xml, content_type="application/rss+xml", charset="utf-8", headers={"ETag": etag, "Cache-Control": "public, max-age=120"})

    channel = database.get_catalog_channel_by_token(token)
    if not channel or channel.get('is_deleted'):
        return web.Response(status=404, text="Channel not found")

    try:
        posts = database.get_channel_posts(channel['chat_id'], limit=50)
        rss_xml = tpl_feed.get_channel_rss_xml(channel, posts, base_url)
        etag = f'"{hashlib.md5(rss_xml.encode("utf-8")).hexdigest()}"'
        state._channel_rss_cache[cache_key] = (now + 120.0, etag, rss_xml)

        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag, "Cache-Control": "public, max-age=120"})

        return web.Response(text=rss_xml, content_type="application/rss+xml", charset="utf-8", headers={"ETag": etag, "Cache-Control": "public, max-age=120"})
    except Exception as e:
        config.logger.exception(f"Error generating RSS XML for channel {token}: {e}")
        return web.Response(status=500, text="Internal Server Error generating RSS feed")


@security.rate_limited("rss", max_requests=60, window_seconds=60)
async def handle_channel_rss_redirect(request):
    token = request.match_info.get('token')
    ingress_path = request.headers.get("X-Ingress-Path", "")
    base_path = ingress_path.rstrip("/")
    raise web.HTTPFound(f"{base_path}/c/{token}/rss.xml")


@security.rate_limited("media", max_requests=120, window_seconds=60)
async def handle_media_file(request):
    token = request.match_info.get('token')
    msg_id = request.match_info.get('msg_id')
    filename = request.match_info.get('filename')

    channel = database.get_catalog_channel_by_token(token)
    if not channel:
        return web.Response(status=404, text="Channel not found")

    expected_dir = os.path.abspath(os.path.join(state.CHANNEL_MEDIA_DIR, token))
    target_path = os.path.abspath(os.path.join(expected_dir, f"{msg_id}_{filename}"))
    if not target_path.startswith(expected_dir):
        return web.Response(status=403, text="Forbidden")

    if not os.path.exists(target_path):
        stem, _ = os.path.splitext(filename)
        webp_target = os.path.abspath(os.path.join(expected_dir, f"{msg_id}_{stem}.webp"))
        if webp_target.startswith(expected_dir) and os.path.exists(webp_target):
            target_path = webp_target
        else:
            alt_path = os.path.abspath(os.path.join(expected_dir, filename))
            if alt_path.startswith(expected_dir) and os.path.exists(alt_path):
                target_path = alt_path
            else:
                return web.Response(status=404, text="Media not found")

    content_type = security._guess_media_content_type(target_path)
    return web.FileResponse(
        target_path,
        headers={
            'Cache-Control': 'public, max-age=86400',
            'Content-Type': content_type,
        }
    )


# ── ActivityPub Handlers ──


async def _run_web_server():
    app = web.Application(client_max_size=256 * 1024)
    app.router.add_get('/icon.png', handle_icon)
    app.router.add_get('/icon.svg', handle_channel_default_avatar)
    app.router.add_get('/favicon.ico', handle_icon)
    app.router.add_get('/background.jpg', handle_background)
    app.router.add_get('/static/background.jpg', handle_background)
    app.router.add_get('/background-light.jpg', handle_background)
    app.router.add_get('/static/background-light.jpg', handle_background)
    app.router.add_get('/robots.txt', handle_robots_txt)
    app.router.add_get('/health', handle_health)
    app.router.add_get('/qr.svg', handle_qr_svg)
    app.router.add_get('/qr.png', handle_qr_png)
    app.router.add_get('/', handle_index)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}', handle_channel_preview)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/qr.png', handle_channel_qr_png)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/qr.svg', handle_channel_qr_svg)
    app.router.add_get('/channel-default.svg', handle_channel_default_avatar)
    app.router.add_get(r'/{slash:/*}channel-default.svg', handle_channel_default_avatar)
    avatar_env = os.environ.get("AVATAR_PATH") or database.get_config("bot_avatar_path")
    if avatar_env:
        fn = os.path.basename(avatar_env)
        if fn and fn not in ('icon.png', 'favicon.ico'):
            app.router.add_get(f'/{fn}', handle_icon)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/avatar.png', handle_channel_avatar)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/rss.xml', handle_channel_rss)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/rss', handle_channel_rss_redirect)
    app.router.add_get(r'/{slash:/*}media/{token:[a-zA-Z0-9]{12}}/{msg_id:[0-9]+}/{filename}', handle_media_file)

    # ActivityPub routes
    app.router.add_get('/.well-known/webfinger', ap_routes.handle_webfinger)
    app.router.add_get('/.well-known/nodeinfo', ap_routes.handle_nodeinfo_discovery)
    app.router.add_get('/nodeinfo/2.0', ap_routes.handle_nodeinfo)
    app.router.add_get('/api/v1/instance', ap_routes.handle_api_v1_instance)
    app.router.add_get(r'/{slash:/*}api/v1/instance', ap_routes.handle_api_v1_instance)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/actor', ap_routes.handle_ap_actor)
    app.router.add_post(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/inbox', ap_routes.handle_ap_inbox)
    app.router.add_post(r'/{slash:/*}inbox', ap_routes.handle_ap_inbox)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/outbox', ap_routes.handle_ap_outbox)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/followers', ap_routes.handle_ap_followers)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/following', ap_routes.handle_ap_following)
    app.router.add_get(r'/{slash:/*}c/{token:[a-zA-Z0-9]{12}}/posts/{msg_id:[0-9]+}', ap_routes.handle_ap_post)

    access_log_format = '%{X-Forwarded-For}i %t "%r" %s %b "%{Referer}i" "%{User-Agent}i"'
    runner = web.AppRunner(app, access_log_format=access_log_format)
    await runner.setup()

    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, '0.0.0.0', port)
    config.logger.info(f"Starting Bouncer channel preview web server on 0.0.0.0:{port}...")
    await site.start()
    config.logger.info("Channel preview web server is running.")

    # Initialize ActivityPub delivery worker in this web loop
    try:
        await activitypub.init_delivery_worker(asyncio.get_running_loop())
    except Exception as e:
        config.logger.warning(f"Could not initialize ActivityPub delivery worker: {e}")

    while True:
        await asyncio.sleep(3600)


def start_web_server_thread():
    if web is None:
        config.logger.warning("aiohttp is not installed, web server disabled.")
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_web_server())
    except Exception as e:
        config.logger.error(f"Web server error: {e}")
