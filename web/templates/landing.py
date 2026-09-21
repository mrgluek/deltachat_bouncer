"""Landing page HTML: the public / route content."""
import html
import os
import urllib.parse

import database
import state
from web.templates import theme
from config import VERSION

def get_landing_page_html(ingress_path: str = "") -> str:
    if not ingress_path and state.index_page_html_cache is not None:
        return state.index_page_html_cache

    invite_link = state.get_bot_invite_link()
    deep_link = invite_link or ""
    if invite_link and invite_link.startswith("OPEN-CHAT:"):
        deep_link = "https://i.delta.chat/#" + invite_link[10:]

    base_path = ingress_path.rstrip("/")
    bg_url = f"{base_path}/background.jpg"
    bg_light_url = f"{base_path}/background-light.jpg"
    home_url = f"{base_path}/" if base_path else "/"
    channels = database.get_all_catalog_channels(public_only=True)

    base_url = database.get_config("base_url") or os.getenv("BASE_URL") or ""
    instance_domain = ""
    if base_url:
        try:
            parsed = urllib.parse.urlparse(base_url)
            instance_domain = parsed.netloc or parsed.path
        except Exception:
            pass
    if not instance_domain:
        instance_domain = "dc.gluek.info"

    if invite_link:
        hero_btn_html = f"""<a href="{deep_link}" class="btn btn-primary">
                <span>🗨️</span> Add Bot to Delta Chat
            </a>"""
        qr_modal_html = f"""<div id="qr-modal" class="modal" role="dialog" aria-modal="true" aria-labelledby="qr-modal-title" onclick="if(event.target === this) closeQrModal()">
        <div class="modal-content">
            <h3 id="qr-modal-title">Add Bouncer Bot</h3>
            <p style="font-size: 0.9rem; color: var(--text-muted);">Scan this QR code with your Delta Chat mobile app or click the link below.</p>
            <img src="{base_path}/qr.png" alt="Bot QR Code" />
            <div style="display: flex; gap: 0.75rem; justify-content: center; flex-wrap: wrap;">
                <a href="{deep_link}" class="btn btn-primary" style="padding: 0.5rem 1rem; font-size: 0.9rem;"><span>🗨️</span> Open in Delta Chat</a>
                <button class="close-btn" id="qr-copy-btn" onclick="copyJoinLink(this, '{deep_link}')">Copy Link</button>
                <button class="close-btn" id="qr-close-btn" onclick="closeQrModal()">Close</button>
            </div>
        </div>
    </div>"""
    else:
        hero_btn_html = """<button class="btn btn-primary" disabled style="opacity: 0.55; cursor: not-allowed;" title="Bot invite link not yet configured">
                <span>🗨️</span> Bot Link Unavailable
            </button>"""
        qr_modal_html = """<div id="qr-modal" class="modal" role="dialog" aria-modal="true" aria-labelledby="qr-modal-title" onclick="if(event.target === this) closeQrModal()">
        <div class="modal-content">
            <h3 id="qr-modal-title">Add Bouncer Bot</h3>
            <p style="font-size: 0.95rem; color: var(--text-muted); margin: 1.5rem 0;">ℹ️ Bot invite link is not configured yet. Please check back later.</p>
            <button class="close-btn" id="qr-close-btn" onclick="closeQrModal()">Close</button>
        </div>
    </div>"""

    channel_items = []
    for ch in channels:
        t = ch.get("token")
        if not t:
            continue
        c_name = html.escape(ch.get("name") or "Channel")
        c_desc = html.escape(ch.get("description") or "")
        c_desc_short = (c_desc[:120] + "…") if len(c_desc) > 120 else c_desc
        c_members = ch.get("member_count") or 0
        c_mem_str = f"👥 {c_members} subscribers" if c_members > 1 else "📢 Channel"
        c_url = f"{base_path}/c/{t}"
        c_avatar = f"{base_path}/c/{t}/avatar.png"
        channel_items.append(f"""
            <a href="{c_url}" class="channel-card-item">
                <img src="{c_avatar}" alt="{c_name}" class="channel-card-avatar" onerror="this.src='{base_path}/channel-default.svg'" />
                <div class="channel-card-content">
                    <div class="channel-card-title">{c_name}</div>
                    <div class="channel-card-meta">{c_mem_str}</div>
                    {f'<div class="channel-card-desc">{c_desc_short}</div>' if c_desc_short else ''}
                </div>
            </a>
        """)

    channels_section = ""
    if channel_items:
        channels_section = f"""
        <section class="card">
            <h2>📢 Public Channels ({len(channel_items)})</h2>
            <div class="channels-grid">
                {''.join(channel_items)}
            </div>
        </section>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Delta Chat Bouncer Bot</title>
    <meta name="description" content="Bouncer bot maintains group quality with inactivity reports, auto-kick management, VirusTotal inspection, and public channel web previews with RSS feeds." />
    <meta property="og:title" content="Delta Chat Bouncer Bot" />
    <meta property="og:description" content="Maintain group quality and public channel web previews for Delta Chat." />
    <meta property="og:image" content="{base_path}/icon.png" />
    <meta property="og:type" content="website" />
    <meta name="twitter:card" content="summary" />
    <link rel="icon" type="image/svg+xml" href="{base_path}/icon.svg" />
    <link rel="alternate icon" type="image/png" href="{base_path}/icon.png" />
    <link rel="shortcut icon" href="{base_path}/favicon.ico" />
    {theme._THEME_PRELOAD_SCRIPT}
    <style>
        :root {{
            --bg-color: #19232b;
            --card-bg: #232d36;
            --card-border: rgba(255, 255, 255, 0.08);
            --text-main: #e9edef;
            --text-muted: #aebac1;
            --color-primary: #2090ea;
            --accent-blue: #53bdeb;
            --code-bg: rgba(0, 0, 0, 0.25);
            --code-border: rgba(255, 255, 255, 0.06);
            --bg-overlay: 1;
        }}
        @media (prefers-color-scheme: light) {{
            :root:not([data-theme="dark"]) {{
                --bg-color: #efeae2;
                --card-bg: #ffffff;
                --card-border: rgba(0, 0, 0, 0.08);
                --text-main: #111b21;
                --text-muted: #54656f;
                --color-primary: #415e6b;
                --accent-blue: #0070e0;
                --code-bg: rgba(0, 0, 0, 0.05);
                --code-border: rgba(0, 0, 0, 0.08);
                --bg-overlay: 1;
            }}
            :root:not([data-theme="dark"]) body::before {{
                background-image: url('{bg_light_url}') !important;
                opacity: 1 !important;
            }}
            :root:not([data-theme="dark"]) .btn-primary:hover {{
                background: #354e59 !important;
            }}
            :root:not([data-theme="dark"]) .top-qr-btn {{
                background: rgba(0, 0, 0, 0.05) !important;
                color: #111b21 !important;
                border-color: rgba(0, 0, 0, 0.1) !important;
            }}
            :root:not([data-theme="dark"]) .top-qr-btn:hover {{
                background: rgba(0, 0, 0, 0.08) !important;
                border-color: rgba(0, 0, 0, 0.16) !important;
            }}
            :root:not([data-theme="dark"]) .card {{
                box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08) !important;
            }}
            :root:not([data-theme="dark"]) .channel-card-item {{
                background: rgba(0, 0, 0, 0.03) !important;
                border-color: rgba(0, 0, 0, 0.06) !important;
            }}
            :root:not([data-theme="dark"]) .channel-card-item:hover {{
                background: rgba(0, 0, 0, 0.06) !important;
                border-color: rgba(0, 0, 0, 0.12) !important;
            }}
            :root:not([data-theme="dark"]) .logo-title,
            :root:not([data-theme="dark"]) .hero h1,
            :root:not([data-theme="dark"]) .card h2,
            :root:not([data-theme="dark"]) .channel-card-title,
            :root:not([data-theme="dark"]) .feature-text h3 {{
                color: #111b21 !important;
            }}
            :root:not([data-theme="dark"]) .hero p {{
                color: #3b4a54 !important;
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
            --card-bg: #ffffff;
            --card-border: rgba(0, 0, 0, 0.08);
            --text-main: #111b21;
            --text-muted: #54656f;
            --color-primary: #415e6b;
            --accent-blue: #0070e0;
            --code-bg: rgba(0, 0, 0, 0.05);
            --code-border: rgba(0, 0, 0, 0.08);
            --bg-overlay: 1;
        }}
        :root[data-theme="light"] body::before {{
            background-image: url('{bg_light_url}') !important;
            opacity: 1 !important;
        }}
        :root[data-theme="light"] .btn-primary:hover {{
            background: #354e59 !important;
        }}
        :root[data-theme="light"] .top-qr-btn {{
            background: rgba(0, 0, 0, 0.05) !important;
            color: #111b21 !important;
            border-color: rgba(0, 0, 0, 0.1) !important;
        }}
        :root[data-theme="light"] .top-qr-btn:hover {{
            background: rgba(0, 0, 0, 0.08) !important;
            border-color: rgba(0, 0, 0, 0.16) !important;
        }}
        :root[data-theme="light"] .card {{
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08) !important;
        }}
        :root[data-theme="light"] .channel-card-item {{
            background: rgba(0, 0, 0, 0.03) !important;
            border-color: rgba(0, 0, 0, 0.06) !important;
        }}
        :root[data-theme="light"] .channel-card-item:hover {{
            background: rgba(0, 0, 0, 0.06) !important;
            border-color: rgba(0, 0, 0, 0.12) !important;
        }}
        :root[data-theme="light"] .logo-title,
        :root[data-theme="light"] .hero h1,
        :root[data-theme="light"] .card h2,
        :root[data-theme="light"] .channel-card-title,
        :root[data-theme="light"] .feature-text h3 {{
            color: #111b21 !important;
        }}
        :root[data-theme="light"] .hero p {{
            color: #3b4a54 !important;
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
            --card-bg: #232d36;
            --card-border: rgba(255, 255, 255, 0.08);
            --text-main: #e9edef;
            --text-muted: #aebac1;
            --color-primary: #2090ea;
            --accent-blue: #53bdeb;
            --code-bg: rgba(0, 0, 0, 0.25);
            --code-border: rgba(255, 255, 255, 0.06);
            --bg-overlay: 1;
        }}
        :root[data-theme="dark"] body::before {{
            background-image: url('{bg_url}') !important;
            opacity: 1 !important;
        }}
        :root[data-theme="dark"] .btn-primary:hover {{
            background: #1a7ec9 !important;
        }}
        :root[data-theme="dark"] .card {{
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.25) !important;
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
            background-image: url('{bg_url}');
            background-repeat: repeat;
            background-size: 430px auto;
            opacity: var(--bg-overlay);
            z-index: -1;
            pointer-events: none;
        }}
        header {{
            padding: 1.5rem 1rem;
            max-width: 760px;
            width: 100%;
            margin: 0 auto;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .logo-container {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            text-decoration: none;
            color: var(--text-main);
        }}
        .logo-title {{
            font-size: 1.25rem;
            font-weight: 700;
            color: var(--text-main);
        }}
        .top-qr-btn {{
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--card-border);
            color: var(--text-muted);
            padding: 0.35rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.85rem;
            font-weight: 500;
            font-family: inherit;
            cursor: pointer;
            transition: all 0.15s ease;
            text-decoration: none;
        }}
        .top-qr-btn:hover {{
            background: rgba(255, 255, 255, 0.12);
            color: var(--text-main);
            border-color: rgba(255, 255, 255, 0.18);
        }}
        .header-links a {{
            color: var(--text-muted);
            text-decoration: none;
            font-size: 0.9rem;
            transition: color 0.15s;
        }}
        .header-links a:hover {{ color: var(--text-main); }}
        main {{
            flex-grow: 1;
            max-width: 760px;
            width: 100%;
            margin: 0 auto;
            padding: 0 1rem 2.5rem;
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
        }}
        .hero {{
            text-align: center;
            padding: 1.5rem 0 0.5rem;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 1rem;
        }}
        .hero-badge {{
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            background: rgba(32, 144, 234, 0.12);
            border: 1px solid rgba(32, 144, 234, 0.25);
            padding: 0.3rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.82rem;
            color: var(--accent-blue);
            font-weight: 500;
        }}
        .hero h1 {{
            font-size: 2rem;
            font-weight: 700;
            line-height: 1.25;
            color: var(--text-main);
        }}
        .hero p {{
            font-size: 1rem;
            color: var(--text-muted);
            max-width: 640px;
            line-height: 1.6;
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            padding: 0.65rem 1.35rem;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 500;
            font-size: 0.95rem;
            cursor: pointer;
            border: none;
            font-family: inherit;
            transition: background 0.15s, opacity 0.15s;
        }}
        .btn-primary {{
            background: var(--color-primary);
            color: #ffffff;
        }}
        .btn-primary:hover {{ background: #1a7ec9; }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 1.5rem;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.25);
        }}
        .card h2 {{
            font-size: 1.25rem;
            font-weight: 600;
            color: var(--text-main);
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        .channels-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr));
            gap: 0.75rem;
        }}
        .channel-card-item {{
            display: flex;
            align-items: center;
            gap: 0.85rem;
            padding: 0.75rem;
            background: rgba(0, 0, 0, 0.18);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 10px;
            text-decoration: none;
            color: var(--text-main);
            transition: background 0.15s, border-color 0.15s;
        }}
        .channel-card-item:hover {{
            background: rgba(0, 0, 0, 0.32);
            border-color: rgba(255, 255, 255, 0.12);
        }}
        .channel-card-avatar {{
            width: 46px;
            height: 46px;
            border-radius: 50%;
            object-fit: cover;
            flex-shrink: 0;
            background: #19232b;
        }}
        .channel-card-content {{
            overflow: hidden;
            flex-grow: 1;
        }}
        .channel-card-title {{
            font-weight: 600;
            font-size: 0.95rem;
            color: var(--text-main);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .channel-card-meta {{
            font-size: 0.78rem;
            color: var(--text-muted);
            margin-top: 1px;
        }}
        .channel-card-desc {{
            font-size: 0.82rem;
            color: var(--text-muted);
            margin-top: 2px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .features-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 1.25rem;
        }}
        .feature-item {{
            display: flex;
            gap: 0.75rem;
        }}
        .feature-icon {{ font-size: 1.35rem; flex-shrink: 0; line-height: 1.2; }}
        .feature-text h3 {{ font-size: 0.98rem; font-weight: 600; color: var(--text-main); margin-bottom: 0.25rem; }}
        .feature-text p {{ font-size: 0.88rem; color: var(--text-muted); line-height: 1.5; }}
        code {{
            background: var(--code-bg);
            border: 1px solid var(--code-border);
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
            color: var(--accent-blue);
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
            font-size: 0.85rem;
        }}
        .commands-table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 0.25rem;
        }}
        .commands-table th, .commands-table td {{
            text-align: left;
            padding: 0.65rem 0.75rem;
            border-bottom: 1px solid var(--card-border);
            font-size: 0.9rem;
        }}
        .commands-table th {{
            color: var(--text-muted);
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}
        .commands-table td {{
            color: var(--text-main);
        }}
        .commands-table code {{
            background: var(--code-bg);
            border: 1px solid var(--code-border);
            padding: 0.15rem 0.4rem;
            border-radius: 4px;
            color: var(--accent-blue);
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
            font-size: 0.85rem;
        }}
        footer {{
            border-top: 1px solid var(--card-border);
            padding: 1.5rem 1rem;
            color: var(--text-muted);
            font-size: 0.85rem;
        }}
        .footer-content {{
            max-width: 760px;
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
        footer a {{ color: var(--text-muted); text-decoration: underline; text-underline-offset: 2px; }}
        footer a:hover {{ color: var(--text-main); }}
        .theme-switcher {{
            display: inline-flex;
            align-items: center;
            gap: 2px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--card-border);
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
            background: var(--card-bg);
            border: 1px solid var(--card-border);
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
            width: 220px;
            height: 220px;
            margin: 0 auto;
            border-radius: 12px;
            background: #ffffff;
            padding: 8px;
        }}
        .close-btn {{
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: var(--text-main);
            padding: 0.5rem 1rem;
            border-radius: 6px;
            cursor: pointer;
            font-family: inherit;
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
        <a href="{home_url}" class="logo-container">
            <span class="logo-title">{instance_domain}</span>
        </a>
        <div class="header-links">
            <button class="top-qr-btn" onclick="openQrModal()" title="Show Delta Chat QR Code">📱 QR Code</button>
        </div>
    </header>

    <main>
        <section class="hero">
            <div class="hero-badge">🛡️ Delta Chat Bouncer Bot</div>
            <h1>Maintain Group Quality & Channel Web Previews</h1>
            <p>Bouncer bot maintains group quality by monitoring inactivity and saving server resources by pruning stale users. It features inactivity reports, automatic two-stage warnings & kicks, VirusTotal security inspection, CMPing server monitoring, and clean web previews & RSS feeds for public channels.</p>
            {hero_btn_html}
        </section>

        {channels_section}

        <section class="card">
            <h2>✨ Core Capabilities</h2>
            <div class="features-grid">
                <div class="feature-item">
                    <span class="feature-icon">🧹</span>
                    <div class="feature-text">
                        <h3>Inactivity Reports & Auto-Kick</h3>
                        <p>Monitors member activity, provides inactivity reports (<code>/bounce</code>), and automatically purges stale members with two-stage warnings and cryptographic fingerprint exemptions.</p>
                    </div>
                </div>
                <div class="feature-item">
                    <span class="feature-icon">📢</span>
                    <div class="feature-text">
                        <h3>Public Channel Previews & RSS</h3>
                        <p>Web previews for registered Delta Chat broadcast channels with live feeds, media attachments, QR join modals, and standard RSS 2.0 feeds.</p>
                    </div>
                </div>
                <div class="feature-item">
                    <span class="feature-icon">📖</span>
                    <div class="feature-text">
                        <h3>Group & Channel Catalogs</h3>
                        <p>Browse public and private group chats (<code>/chats</code>) and public broadcast channels (<code>/dchannels</code>) with real-time membership counts and join workflows.</p>
                    </div>
                </div>
                <div class="feature-item">
                    <span class="feature-icon">🌐</span>
                    <div class="feature-text">
                        <h3>CMPing Connectivity Monitor</h3>
                        <p>Continuous network health checks across mail relays and Delta Chat servers, with incident-based dynamic alerting and root-cause fault isolation.</p>
                    </div>
                </div>
                <div class="feature-item">
                    <span class="feature-icon">🦠</span>
                    <div class="feature-text">
                        <h3>VirusTotal Security Inspection</h3>
                        <p>Inspect links, files, and attachments against 70+ antivirus engines with FIFO rate limiting and live in-place message updates.</p>
                    </div>
                </div>
                <div class="feature-item">
                    <span class="feature-icon">🔄</span>
                    <div class="feature-text">
                        <h3>Resilient Multi-Relay Failover</h3>
                        <p>Automatic mail server failover with round-robin relay switching and exponential backoff retry to guarantee deliverability.</p>
                    </div>
                </div>
            </div>
        </section>

        <section class="card">
            <h2>⌨️ Popular Commands</h2>
            <table class="commands-table">
                <thead>
                    <tr>
                        <th>Command</th>
                        <th>Description</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td><code>/bounce</code></td>
                        <td>Check user activity status or list group members near the inactivity threshold</td>
                    </tr>
                    <tr>
                        <td><code>/autokick</code></td>
                        <td>Configure automatic inactivity warnings and kicks (Admin only)</td>
                    </tr>
                    <tr>
                        <td><code>/dchannels</code></td>
                        <td>Browse the catalog of public Delta Chat channels with web preview links</td>
                    </tr>
                    <tr>
                        <td><code>/chats</code></td>
                        <td>Browse the catalog of registered group chats available to join</td>
                    </tr>
                    <tr>
                        <td><code>/virus &lt;url/file&gt;</code></td>
                        <td>Scan a link or attached file for security threats with VirusTotal</td>
                    </tr>
                    <tr>
                        <td><code>/cmping &lt;server&gt;</code></td>
                        <td>Ping mail relays to/from specified target servers</td>
                    </tr>
                    <tr>
                        <td><code>/top</code></td>
                        <td>Display the 10 most active group members over the last 24 hours</td>
                    </tr>
                    <tr>
                        <td><code>/away [text]</code></td>
                        <td>Set vacation/away status (auto-notifies users who mention or quote you)</td>
                    </tr>
                </tbody>
            </table>
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
            var first = m.querySelector('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])');
            if (first) first.focus();
        }}
    }}
    function closeQrModal() {{
        var m = document.getElementById('qr-modal');
        if (m) m.style.display = 'none';
        if (_qrOpenerBtn) {{ _qrOpenerBtn.focus(); _qrOpenerBtn = null; }}
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
            var focusable = Array.from(m.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'));
            if (!focusable.length) return;
            var first = focusable[0], last = focusable[focusable.length - 1];
            if (e.shiftKey) {{
                if (document.activeElement === first) {{ e.preventDefault(); last.focus(); }}
            }} else {{
                if (document.activeElement === last) {{ e.preventDefault(); first.focus(); }}
            }}
        }}
    }});
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
    </script>
</body>
</html>
"""
    if not ingress_path:
        state.index_page_html_cache = html_content
    return html_content


