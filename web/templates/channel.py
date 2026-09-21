"""Channel web-preview page HTML (the /c/<token> route)."""
import html
import time
import urllib.parse

import formatting
from web.templates import theme
from config import VERSION

def get_channel_preview_html(channel: dict, posts: list[dict], base_url: str, ingress_path: str = "") -> str:
    token = channel.get("token", "")
    ch_name = channel.get("name") or "Channel"
    ch_desc = channel.get("description") or ""
    member_count = channel.get("member_count") or 0
    if member_count > 1:
        subscribers_pill = f'<div class="subscribers-pill">👥 {member_count} subscribers</div>'
    else:
        subscribers_pill = '<div class="subscribers-pill">📢 Channel</div>'
    invite_link = channel.get("invite_link") or ""

    join_link = invite_link
    if invite_link.startswith("OPEN-CHAT:"):
        join_link = "https://i.delta.chat/#" + invite_link[10:]
    elif invite_link.startswith("OPEN:"):
        join_link = "https://i.delta.chat/#" + invite_link[5:]
    elif invite_link.startswith("dcqr://"):
        join_link = "https://i.delta.chat/#" + invite_link[7:]

    base_path = ingress_path.rstrip("/")
    ch_name_esc = html.escape(ch_name)
    ch_desc_summary = html.escape(ch_desc[:160]) if ch_desc else f"Preview posts from {ch_name_esc} on Delta Chat."
    has_full_base = bool(base_url and base_url.startswith(("http://", "https://")))
    ch_url = f"{base_url.rstrip('/')}/c/{token}" if has_full_base else f"{base_path}/c/{token}"
    avatar_url = f"{base_path}/c/{token}/avatar.png"
    qr_img_url = f"{base_path}/c/{token}/qr.png"
    rss_url = f"{base_url.rstrip('/')}/c/{token}/rss.xml" if has_full_base else f"{base_path}/c/{token}/rss.xml"
    home_url = f"{base_path}/" if base_path else "/"

    if join_link:
        actions_buttons_html = f"""<a href="{join_link}" class="btn btn-primary"><span>🗨️</span> Open in Delta Chat</a>
                <button onclick="openQrModal()" class="btn btn-secondary"><span>📱</span> Show QR Code</button>"""
        qr_modal_html = f"""
    <div id="qr-modal" class="modal" role="dialog" aria-modal="true" aria-labelledby="qr-modal-title" onclick="if(event.target === this) closeQrModal()">
        <div class="modal-content">
            <h3 id="qr-modal-title">Scan with Delta Chat</h3>
            <p style="font-size: 0.9rem; color: var(--text-muted);">Scan this QR code with the Delta Chat camera to join <strong>{ch_name_esc}</strong>.</p>
            <img src="{qr_img_url}" alt="Channel Join QR Code" />
            <div style="display: flex; gap: 0.75rem; justify-content: center; flex-wrap: wrap;">
                <button class="close-btn" id="qr-copy-btn" onclick="copyJoinLink(this, '{join_link}')">Copy Link</button>
                <button class="close-btn" id="qr-close-btn" onclick="closeQrModal()">Close</button>
            </div>
        </div>
    </div>"""
    else:
        actions_buttons_html = """<button class="btn btn-primary" disabled style="opacity: 0.55; cursor: not-allowed;" title="Invite link not available"><span>🗨️</span> Invite Link Unavailable</button>"""
        qr_modal_html = ""

    fedi_domain = ""
    if has_full_base:
        try:
            fedi_domain = urllib.parse.urlparse(base_url).netloc
        except Exception:
            pass
    if fedi_domain:
        fedi_handle = f"@{token}@{fedi_domain}"
        fedi_btn_html = f"""<div class="top-nav">
            <button class="fedi-tag-btn" onclick="copyFediHandle(this, '{fedi_handle}')" title="Click to copy Fediverse handle for Mastodon/Fediverse">
                <span class="fedi-icon">🪐</span>
                <span class="fedi-handle">{fedi_handle}</span>
                <span class="fedi-copy-icon">📋</span>
            </button>
        </div>"""
    else:
        fedi_handle = ""
        fedi_btn_html = ""

    # Build posts HTML
    posts_html_parts = []
    if not posts:
        posts_html_parts.append('<div class="empty-feed">ℹ️ No messages posted in this channel yet. New broadcasts will appear here.</div>')
    else:
        for p in posts:
            p_ts = p.get("timestamp") or time.time()
            time_str = formatting._format_post_time(p_ts)
            from_name = html.escape(p.get("from_name") or ch_name)
            p_text = formatting.format_markdown_html(p.get("text") or "")

            media_type = p.get("media_type")
            media_fn = p.get("media_filename")
            msg_id = p.get("msg_id")

            media_html = ""
            if media_fn and msg_id:
                media_url = f"{base_path}/media/{token}/{msg_id}/{media_fn}"
                if media_type == "image":
                    media_html = f'<div class="post-media"><a href="{media_url}" target="_blank"><img src="{media_url}" alt="Post image" class="post-media-img" loading="lazy" /></a></div>'
                elif media_type == "video":
                    media_html = f'<div class="post-media"><video controls preload="metadata" class="post-media-video"><source src="{media_url}"></video></div>'
                elif media_type == "audio":
                    media_html = f'<div class="post-media"><audio controls class="post-media-audio"><source src="{media_url}"></audio></div>'
                elif media_type == "file":
                    media_html = f'<div class="post-media"><a href="{media_url}" download class="post-media-file">📎 Download {html.escape(media_fn)}</a></div>'

            msg_anchor_attr = f'id="post-{p.get("msg_id")}" ' if p.get("msg_id") else ''
            post_card = f"""
            <article {msg_anchor_attr}class="post-bubble post-card">
                <div class="post-bubble-header post-header">
                    <span class="post-author">{from_name}</span>
                </div>
                {f'<div class="post-body">{p_text}</div>' if p_text else ''}
                {media_html}
                <div class="post-meta">
                    <time class="post-time post-date" title="{time_str}">{time_str}</time>
                    <span class="post-status-check">✓</span>
                </div>
            </article>
            """
            posts_html_parts.append(post_card)

    feed_html = "\n".join(posts_html_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{ch_name_esc} — Delta Chat Channel</title>
    <meta name="description" content="{ch_desc_summary}">
    <link rel="icon" type="image/png" href="{avatar_url}" />
    <link rel="alternate" type="application/rss+xml" title="{ch_name_esc} RSS Feed" href="{rss_url}">

    <!-- Open Graph / Social Sharing Cards -->
    <meta property="og:title" content="{ch_name_esc}">
    <meta property="og:description" content="{ch_desc_summary}">
    <meta property="og:image" content="{avatar_url}">
    <meta property="og:url" content="{ch_url}">
    <meta property="og:type" content="website">
    <meta name="twitter:card" content="summary">
    <meta name="twitter:title" content="{ch_name_esc}">
    <meta name="twitter:description" content="{ch_desc_summary}">
    <meta name="twitter:image" content="{avatar_url}">
    {theme._THEME_PRELOAD_SCRIPT}

    <style>
        :root {{
            --bg-color: #19232b;
            --bubble-bg: #232d36;
            --border-subtle: rgba(255, 255, 255, 0.08);
            --border-bubble: rgba(255, 255, 255, 0.05);
            --text-main: #e9edef;
            --text-muted: #aebac1;
            --text-secondary: #aebac1;
            --color-primary: #2090ea;
            --color-author: #53bdeb;
            --color-success: #00a884;
            --bg-overlay: 1;
        }}
        @media (prefers-color-scheme: light) {{
            :root:not([data-theme="dark"]) {{
                --bg-color: #efeae2;
                --bubble-bg: #ffffff;
                --border-subtle: rgba(0, 0, 0, 0.08);
                --border-bubble: rgba(0, 0, 0, 0.05);
                --text-main: #111b21;
                --text-muted: #54656f;
                --text-secondary: #54656f;
                --color-primary: #415e6b;
                --color-author: #0070e0;
                --color-success: #008069;
                --bg-overlay: 1;
            }}
            :root:not([data-theme="dark"]) body::before {{
                background-image: url('{base_path}/background-light.jpg') !important;
                opacity: 1 !important;
            }}
            :root:not([data-theme="dark"]) .btn-primary:hover {{
                background: #354e59 !important;
            }}
            :root:not([data-theme="dark"]) .btn-secondary {{
                background: rgba(0, 0, 0, 0.05) !important;
                border-color: rgba(0, 0, 0, 0.1) !important;
                color: #111b21 !important;
            }}
            :root:not([data-theme="dark"]) .btn-secondary:hover {{
                background: rgba(0, 0, 0, 0.08) !important;
                border-color: rgba(0, 0, 0, 0.15) !important;
            }}
            :root:not([data-theme="dark"]) .close-btn {{
                background: #f0f2f5 !important;
                border-color: rgba(0, 0, 0, 0.1) !important;
                color: #111b21 !important;
            }}
            :root:not([data-theme="dark"]) .close-btn:hover {{
                background: #e4e6eb !important;
            }}
            :root:not([data-theme="dark"]) .close-btn.copied {{
                background: rgba(0, 128, 105, 0.12) !important;
                border-color: #008069 !important;
                color: #008069 !important;
            }}
            :root:not([data-theme="dark"]) .channel-header-card {{
                background: #ffffff !important;
                box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08) !important;
            }}
            :root:not([data-theme="dark"]) .channel-title {{
                color: #111b21 !important;
            }}
            :root:not([data-theme="dark"]) .channel-desc {{
                color: #3b4a54 !important;
            }}
            :root:not([data-theme="dark"]) .post-bubble {{
                background: #ffffff !important;
                box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08) !important;
            }}
            :root:not([data-theme="dark"]) .post-author {{
                color: #0070e0 !important;
            }}
            :root:not([data-theme="dark"]) .fedi-tag-btn {{
                background: rgba(0, 0, 0, 0.05) !important;
                color: #111b21 !important;
                border-color: rgba(0, 0, 0, 0.1) !important;
            }}
            :root:not([data-theme="dark"]) .modal {{
                background: rgba(17, 27, 33, 0.55) !important;
                backdrop-filter: blur(6px) !important;
                -webkit-backdrop-filter: blur(6px) !important;
            }}
            :root:not([data-theme="dark"]) .modal-content {{
                background: #ffffff !important;
                border: 1px solid rgba(0, 0, 0, 0.08) !important;
                box-shadow: 0 20px 48px rgba(0, 0, 0, 0.22), 0 4px 12px rgba(0, 0, 0, 0.08) !important;
                color: #111b21 !important;
            }}
            :root:not([data-theme="dark"]) .modal-content h3 {{
                color: #111b21 !important;
            }}
            :root:not([data-theme="dark"]) .modal-content p {{
                color: #54656f !important;
            }}
            :root:not([data-theme="dark"]) .modal-content img {{
                background: #ffffff !important;
                border: 1px solid rgba(0, 0, 0, 0.08) !important;
                border-radius: 12px !important;
                box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06) !important;
            }}
            :root:not([data-theme="dark"]) .theme-switcher {{
                background: rgba(0, 0, 0, 0.05) !important;
                border-color: rgba(0, 0, 0, 0.1) !important;
            }}
            :root:not([data-theme="dark"]) .theme-btn {{
                color: #54656f !important;
            }}
            :root:not([data-theme="dark"]) .theme-btn:hover {{
                background: rgba(0, 0, 0, 0.06) !important;
                color: #111b21 !important;
            }}
            :root:not([data-theme="dark"]) .theme-btn.active {{
                background: rgba(0, 0, 0, 0.1) !important;
                color: #111b21 !important;
            }}
        }}
        :root[data-theme="light"] {{
            --bg-color: #efeae2;
            --bubble-bg: #ffffff;
            --border-subtle: rgba(0, 0, 0, 0.08);
            --border-bubble: rgba(0, 0, 0, 0.05);
            --text-main: #111b21;
            --text-muted: #54656f;
            --text-secondary: #54656f;
            --color-primary: #415e6b;
            --color-author: #0070e0;
            --color-success: #008069;
            --bg-overlay: 1;
        }}
        :root[data-theme="light"] body::before {{
            background-image: url('{base_path}/background-light.jpg') !important;
            opacity: 1 !important;
        }}
        :root[data-theme="light"] .btn-primary:hover {{
            background: #354e59 !important;
        }}
        :root[data-theme="light"] .btn-secondary {{
            background: rgba(0, 0, 0, 0.05) !important;
            border-color: rgba(0, 0, 0, 0.1) !important;
            color: #111b21 !important;
        }}
        :root[data-theme="light"] .btn-secondary:hover {{
            background: rgba(0, 0, 0, 0.08) !important;
            border-color: rgba(0, 0, 0, 0.15) !important;
        }}
        :root[data-theme="light"] .close-btn {{
            background: #f0f2f5 !important;
            border-color: rgba(0, 0, 0, 0.1) !important;
            color: #111b21 !important;
        }}
        :root[data-theme="light"] .close-btn:hover {{
            background: #e4e6eb !important;
        }}
        :root[data-theme="light"] .close-btn.copied {{
            background: rgba(0, 128, 105, 0.12) !important;
            border-color: #008069 !important;
            color: #008069 !important;
        }}
        :root[data-theme="light"] .channel-header-card {{
            background: #ffffff !important;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08) !important;
        }}
        :root[data-theme="light"] .channel-title {{
            color: #111b21 !important;
        }}
        :root[data-theme="light"] .channel-desc {{
            color: #3b4a54 !important;
        }}
        :root[data-theme="light"] .post-bubble {{
            background: #ffffff !important;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08) !important;
        }}
        :root[data-theme="light"] .post-author {{
            color: #0070e0 !important;
        }}
        :root[data-theme="light"] .fedi-tag-btn {{
            background: rgba(0, 0, 0, 0.05) !important;
            color: #111b21 !important;
            border-color: rgba(0, 0, 0, 0.1) !important;
        }}
        :root[data-theme="light"] .modal {{
            background: rgba(17, 27, 33, 0.55) !important;
            backdrop-filter: blur(6px) !important;
            -webkit-backdrop-filter: blur(6px) !important;
        }}
        :root[data-theme="light"] .modal-content {{
            background: #ffffff !important;
            border: 1px solid rgba(0, 0, 0, 0.08) !important;
            box-shadow: 0 20px 48px rgba(0, 0, 0, 0.22), 0 4px 12px rgba(0, 0, 0, 0.08) !important;
            color: #111b21 !important;
        }}
        :root[data-theme="light"] .modal-content h3 {{
            color: #111b21 !important;
        }}
        :root[data-theme="light"] .modal-content p {{
            color: #54656f !important;
        }}
        :root[data-theme="light"] .modal-content img {{
            background: #ffffff !important;
            border: 1px solid rgba(0, 0, 0, 0.08) !important;
            border-radius: 12px !important;
            box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06) !important;
        }}
        :root[data-theme="light"] .theme-switcher {{
            background: rgba(0, 0, 0, 0.05) !important;
            border-color: rgba(0, 0, 0, 0.1) !important;
        }}
        :root[data-theme="light"] .theme-btn {{
            color: #54656f !important;
        }}
        :root[data-theme="light"] .theme-btn:hover {{
            background: rgba(0, 0, 0, 0.06) !important;
            color: #111b21 !important;
        }}
        :root[data-theme="light"] .theme-btn.active {{
            background: rgba(0, 0, 0, 0.1) !important;
            color: #111b21 !important;
        }}

        :root[data-theme="dark"] {{
            --bg-color: #19232b;
            --bubble-bg: #232d36;
            --border-subtle: rgba(255, 255, 255, 0.08);
            --border-bubble: rgba(255, 255, 255, 0.05);
            --text-main: #e9edef;
            --text-muted: #aebac1;
            --text-secondary: #aebac1;
            --color-primary: #2090ea;
            --color-author: #53bdeb;
            --color-success: #00a884;
            --bg-overlay: 1;
        }}
        :root[data-theme="dark"] body::before {{
            background-image: url('{base_path}/background.jpg') !important;
            opacity: 1 !important;
        }}
        :root[data-theme="dark"] .btn-primary:hover {{
            background: #1b7ed3 !important;
        }}
        :root[data-theme="dark"] .theme-switcher {{
            background: rgba(255, 255, 255, 0.06) !important;
            border-color: rgba(255, 255, 255, 0.08) !important;
        }}
        :root[data-theme="dark"] .theme-btn {{
            color: var(--text-muted) !important;
        }}
        :root[data-theme="dark"] .theme-btn:hover {{
            background: rgba(255, 255, 255, 0.08) !important;
            color: var(--text-main) !important;
        }}
        :root[data-theme="dark"] .theme-btn.active {{
            background: rgba(255, 255, 255, 0.14) !important;
            color: var(--text-main) !important;
        }}
        :root[data-theme="dark"] .modal {{
            background: rgba(0, 0, 0, 0.75) !important;
            backdrop-filter: blur(6px) !important;
            -webkit-backdrop-filter: blur(6px) !important;
        }}
        :root[data-theme="dark"] .modal-content {{
            background: #232d36 !important;
            border: 1px solid rgba(255, 255, 255, 0.08) !important;
            box-shadow: 0 20px 48px rgba(0, 0, 0, 0.6) !important;
            color: #e9edef !important;
        }}
        :root[data-theme="dark"] .modal-content h3 {{
            color: #e9edef !important;
        }}
        :root[data-theme="dark"] .modal-content p {{
            color: #aebac1 !important;
        }}
        :root[data-theme="dark"] .modal-content img {{
            background: #ffffff !important;
            border-radius: 12px !important;
        }}
        :root[data-theme="dark"] .close-btn {{
            background: rgba(255, 255, 255, 0.08) !important;
            border: 1px solid rgba(255, 255, 255, 0.1) !important;
            color: var(--text-main) !important;
        }}
        :root[data-theme="dark"] .close-btn:hover {{
            background: rgba(255, 255, 255, 0.14) !important;
        }}
        :root[data-theme="dark"] .close-btn.copied {{
            background: rgba(0, 168, 132, 0.2) !important;
            border-color: #00a884 !important;
            color: #25d366 !important;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            -webkit-font-smoothing: antialiased;
            position: relative;
        }}
        body::before {{
            content: "";
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background-image: url('{base_path}/background.jpg');
            background-repeat: repeat;
            background-size: 430px auto;
            opacity: var(--bg-overlay);
            z-index: -1;
            pointer-events: none;
        }}
        header {{
            max-width: 680px;
            width: 100%;
            margin: 0 auto;
            padding: 1.25rem 1rem 0.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .top-nav a {{
            color: var(--text-muted);
            text-decoration: none;
            font-size: 0.9rem;
            font-weight: 500;
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            transition: color 0.15s;
        }}
        .top-nav a:hover {{ color: var(--text-main); }}
        .fedi-tag-btn {{
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 0.3rem 0.65rem;
            border-radius: 9999px;
            font-size: 0.85rem;
            font-weight: 500;
            font-family: inherit;
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        .fedi-tag-btn:hover {{
            background: rgba(255, 255, 255, 0.12);
            color: var(--text-main);
            border-color: rgba(255, 255, 255, 0.18);
        }}
        .fedi-tag-btn:active {{
            transform: scale(0.97);
        }}
        .fedi-tag-btn.copied {{
            background: rgba(0, 168, 132, 0.2);
            border-color: var(--color-success);
            color: #25d366;
        }}
        .fedi-handle {{
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.82rem;
            letter-spacing: -0.01em;
        }}
        .fedi-copy-icon {{
            font-size: 0.75rem;
            opacity: 0.75;
        }}
        @media (max-width: 480px) {{
            .fedi-handle {{
                max-width: 170px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }}
        }}
        main {{
            flex-grow: 1;
            max-width: 680px;
            width: 100%;
            margin: 0 auto;
            padding: 0.75rem 1rem 3rem;
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
        }}
        .channel-card {{
            background: var(--bubble-bg);
            border: 1px solid var(--border-subtle);
            border-radius: 14px;
            padding: 1.75rem 1.5rem;
            display: flex;
            flex-direction: column;
            align-items: center;
            text-align: center;
            gap: 1rem;
            position: relative;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
        }}
        .channel-avatar {{
            width: 88px;
            height: 88px;
            border-radius: 50%;
            object-fit: cover;
            border: 2px solid rgba(255, 255, 255, 0.12);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
        }}
        .channel-info {{
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 0.4rem;
        }}
        .channel-info h1 {{
            font-size: 1.5rem;
            font-weight: 600;
            color: var(--text-main);
            word-break: break-word;
        }}
        .subscribers-pill {{
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            background: rgba(83, 189, 235, 0.12);
            color: var(--color-author);
            border: 1px solid rgba(83, 189, 235, 0.25);
            padding: 0.2rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 500;
        }}
        .channel-desc {{
            font-size: 0.95rem;
            color: var(--text-secondary);
            line-height: 1.55;
            max-width: 580px;
            word-break: break-word;
            margin-top: 0.25rem;
        }}
        .channel-desc a {{
            color: var(--color-author);
            text-decoration: none;
        }}
        .channel-desc a:hover {{ text-decoration: underline; }}
        .actions-row {{
            display: flex;
            gap: 0.65rem;
            flex-wrap: wrap;
            justify-content: center;
            margin-top: 0.25rem;
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 0.45rem;
            padding: 0.65rem 1.25rem;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 500;
            font-size: 0.92rem;
            cursor: pointer;
            border: none;
            font-family: inherit;
            transition: background 0.15s, transform 0.15s;
        }}
        .btn-primary {{
            background: var(--color-primary);
            color: #ffffff;
        }}
        .btn-primary:hover {{
            background: #1b7ed3;
        }}
        .btn-secondary {{
            background: rgba(255, 255, 255, 0.07);
            color: var(--text-main);
            border: 1px solid var(--border-subtle);
        }}
        .btn-secondary:hover {{
            background: rgba(255, 255, 255, 0.12);
        }}
        .feed-section h2 {{
            font-size: 1.05rem;
            font-weight: 600;
            margin-bottom: 0.85rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 0.45rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}
        .post-bubble, .post-card {{
            background: var(--bubble-bg);
            border: 1px solid var(--border-bubble);
            border-radius: 12px;
            padding: 10px 14px 8px;
            margin-bottom: 0.75rem;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25);
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
        }}
        .post-bubble-header, .post-header {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 0.75rem;
        }}
        .post-author {{
            font-weight: 600;
            font-size: 0.92rem;
            color: var(--color-author);
        }}
        .post-body {{
            font-size: 0.96rem;
            line-height: 1.52;
            color: var(--text-main);
            word-break: break-word;
        }}
        .post-body a {{ color: var(--color-author); text-decoration: none; }}
        .post-body a:hover {{ text-decoration: underline; }}
        .post-body blockquote {{
            border-left: 3px solid var(--color-author);
            padding: 4px 10px;
            margin: 6px 0;
            color: var(--text-secondary);
            background: rgba(83, 189, 235, 0.08);
            border-radius: 0 6px 6px 0;
            font-size: 0.92rem;
        }}
        .post-body code {{
            font-family: ui-monospace, "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 0.88em;
            background: rgba(255, 255, 255, 0.08);
            padding: 0.15em 0.35em;
            border-radius: 4px;
            color: #79c0ff;
        }}
        .post-body pre {{
            background: #18222d;
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            padding: 10px 12px;
            overflow-x: auto;
            margin: 6px 0;
        }}
        .post-body pre code {{
            background: none;
            padding: 0;
            color: var(--text-main);
            font-size: 0.86rem;
            display: block;
        }}
        .post-body del {{
            opacity: 0.7;
            text-decoration: line-through;
        }}
        .spoiler {{
            background: rgba(255, 255, 255, 0.16);
            color: transparent;
            border-radius: 4px;
            padding: 0.1em 0.35em;
            cursor: pointer;
            user-select: none;
            transition: background 0.2s, color 0.2s;
        }}
        .spoiler:hover, .spoiler.revealed {{
            background: rgba(255, 255, 255, 0.06);
            color: inherit;
            user-select: text;
        }}
        .post-media {{
            margin-top: 4px;
            border-radius: 8px;
            overflow: hidden;
        }}
        .post-media-img {{
            max-width: 100%;
            max-height: 520px;
            object-fit: contain;
            border-radius: 8px;
            display: block;
        }}
        .post-media-video {{
            max-width: 100%;
            border-radius: 8px;
            background: #000;
            display: block;
        }}
        .post-media-audio {{
            width: 100%;
            margin-top: 4px;
        }}
        .post-media-file {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--border-subtle);
            padding: 8px 14px;
            border-radius: 8px;
            color: var(--text-main);
            text-decoration: none;
            font-size: 0.9rem;
            transition: background 0.15s;
        }}
        .post-media-file:hover {{ background: rgba(255, 255, 255, 0.1); }}
        .post-meta {{
            display: flex;
            justify-content: flex-end;
            align-items: center;
            gap: 4px;
            font-size: 11px;
            color: var(--text-muted);
            margin-top: 4px;
            user-select: none;
        }}
        .post-time {{
            color: var(--text-muted);
            font-size: 11px;
        }}
        .post-status-check {{
            color: var(--color-author);
            font-size: 11px;
            font-weight: bold;
        }}
        .empty-feed {{
            text-align: center;
            padding: 2.5rem 1rem;
            color: var(--text-muted);
            font-size: 0.95rem;
            background: var(--bubble-bg);
            border: 1px solid var(--border-bubble);
            border-radius: 12px;
        }}
        footer {{
            border-top: 1px solid var(--border-subtle);
            padding: 1.75rem 1rem;
            color: var(--text-muted);
            font-size: 0.85rem;
        }}
        .footer-content {{
            max-width: 680px;
            margin: 0 auto;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            flex-wrap: wrap;
        }}
        @media (max-width: 600px) {{
            .footer-content {{
                flex-direction: column;
                text-align: center;
                gap: 0.75rem;
            }}
        }}
        footer a {{ color: var(--color-author); text-decoration: none; }}
        footer a:hover {{ text-decoration: underline; }}
        .theme-switcher {{
            display: inline-flex;
            align-items: center;
            gap: 2px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--border-subtle);
            border-radius: 9999px;
            padding: 2px;
        }}
        .theme-btn {{
            background: transparent;
            border: none;
            color: var(--text-muted);
            width: 30px;
            height: 30px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            padding: 0;
            transition: color 0.15s, background 0.15s;
        }}
        .theme-btn:hover {{
            color: var(--text-main);
            background: rgba(255, 255, 255, 0.08);
        }}
        .theme-btn.active {{
            color: var(--text-main);
            background: rgba(255, 255, 255, 0.14);
        }}
        .modal {{
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(6px);
            -webkit-backdrop-filter: blur(6px);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }}
        .modal-content {{
            background: var(--bubble-bg);
            border: 1px solid var(--border-subtle);
            border-radius: 16px;
            padding: 1.75rem;
            text-align: center;
            max-width: 380px;
            width: 90%;
            display: flex;
            flex-direction: column;
            gap: 1rem;
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.5);
        }}
        .modal-content img {{
            width: 200px;
            height: 200px;
            margin: 0 auto;
            border-radius: 12px;
            background: #ffffff;
            padding: 8px;
        }}
        .close-btn {{
            background: rgba(255, 255, 255, 0.08);
            color: var(--text-main);
            border: 1px solid var(--border-subtle);
            padding: 0.5rem 1rem;
            border-radius: 8px;
            cursor: pointer;
            font-family: inherit;
            font-size: 0.88rem;
            font-weight: 500;
            transition: all 0.15s ease;
        }}
        .close-btn:hover {{ background: rgba(255, 255, 255, 0.14); }}
        .close-btn.copied {{
            background: rgba(0, 168, 132, 0.2) !important;
            border-color: #00a884 !important;
            color: #25d366 !important;
        }}
    </style>
</head>
<body>
    <header>
        <div class="top-nav">
            <a href="{home_url}">← Home</a>
        </div>
        {fedi_btn_html}
    </header>

    <main>
        <section class="channel-card">
            <img src="{avatar_url}" alt="{ch_name_esc} Avatar" class="channel-avatar" onerror="this.src='{base_path}/channel-default.svg'" />
            <div class="channel-info">
                <h1>{ch_name_esc}</h1>
                {subscribers_pill}
                {f'<p class="channel-desc">{formatting._autolink(ch_desc)}</p>' if ch_desc else ''}
            </div>
            <div class="actions-row">
                {actions_buttons_html}
                <a href="{rss_url}" class="btn btn-secondary"><span>📡</span> RSS Feed</a>
            </div>
        </section>

        <section class="feed-section">
            <h2>📜 Recent Posts</h2>
            {feed_html}
        </section>
    </main>

    {qr_modal_html}

    <footer>
        <div class="footer-content">
            <p>Powered by <a href="https://github.com/mrgluek/deltachat_bouncer" target="_blank">Delta Chat Bouncer Bot</a> (v{VERSION}) · <a href="https://git.gluek.info/gluek/deltachat_bouncer" target="_blank">Forgejo Mirror</a></p>
            {theme._THEME_SWITCHER_HTML}
        </div>
    </footer>
    {theme._THEME_CONTROLLER_SCRIPT}

    <script>
    var _qrOpenerBtn = null;
    function openQrModal() {{
        var m = document.getElementById('qr-modal');
        if (m) {{
            _qrOpenerBtn = document.activeElement;
            m.style.display = 'flex';
            var first = m.querySelector('button, [href], input, [tabindex]:not([tabindex="-1"])');
            if (first) first.focus();
        }}
    }}
    function closeQrModal() {{
        var m = document.getElementById('qr-modal');
        if (m) m.style.display = 'none';
        if (_qrOpenerBtn) {{ _qrOpenerBtn.focus(); _qrOpenerBtn = null; }}
    }}
    function copyJoinLink(btn, link) {{
        var orig = btn.innerHTML;
        if (navigator.clipboard && navigator.clipboard.writeText) {{
            navigator.clipboard.writeText(link).then(function() {{
                btn.innerHTML = '✓ Copied!';
                btn.classList.add('copied');
                setTimeout(function() {{ btn.innerHTML = orig; btn.classList.remove('copied'); }}, 2000);
            }}).catch(function() {{ fallbackCopy(btn, link, orig); }});
        }} else {{
            fallbackCopy(btn, link, orig);
        }}
    }}
    function fallbackCopy(btn, link, orig) {{
        var ta = document.createElement('textarea');
        ta.value = link; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        try {{ document.execCommand('copy'); btn.innerHTML = '✓ Copied!'; btn.classList.add('copied'); setTimeout(function() {{ btn.innerHTML = orig; btn.classList.remove('copied'); }}, 2000); }} catch(e) {{}}
        document.body.removeChild(ta);
    }}
    function copyFediHandle(btn, text) {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
            navigator.clipboard.writeText(text).then(function() {{
                showFediCopied(btn);
            }}).catch(function() {{
                fallbackFediCopy(btn, text);
            }});
        }} else {{
            fallbackFediCopy(btn, text);
        }}
    }}
    function fallbackFediCopy(btn, text) {{
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try {{
            document.execCommand('copy');
            showFediCopied(btn);
        }} catch (e) {{}}
        document.body.removeChild(ta);
    }}
    function showFediCopied(btn) {{
        var orig = btn.innerHTML;
        btn.classList.add('copied');
        btn.innerHTML = '<span>✓</span> Copied!';
        setTimeout(function() {{
            btn.classList.remove('copied');
            btn.innerHTML = orig;
        }}, 2000);
    }}
    document.addEventListener('keydown', function(e) {{
        var m = document.getElementById('qr-modal');
        if (!m || m.style.display === 'none' || m.style.display === '') return;
        if (e.key === 'Escape') {{
            e.preventDefault();
            closeQrModal();
            return;
        }}
        if (e.key === 'Tab') {{
            var focusable = Array.from(m.querySelectorAll('button, [href], input, [tabindex]:not([tabindex="-1"])'));
            if (!focusable.length) return;
            var first = focusable[0], last = focusable[focusable.length - 1];
            if (e.shiftKey) {{
                if (document.activeElement === first) {{ e.preventDefault(); last.focus(); }}
            }} else {{
                if (document.activeElement === last) {{ e.preventDefault(); first.focus(); }}
            }}
        }}
    }});
    </script>
</body>
</html>
"""


