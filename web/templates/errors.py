"""Tombstone (removed channel) and 404 page HTML."""
import html

from web.templates import theme

def get_tombstone_html(channel_name: str, ingress_path: str = "") -> str:
    base_path = ingress_path.rstrip("/")
    home_url = f"{base_path}/" if base_path else "/"
    ch_esc = html.escape(channel_name)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Channel Removed — Delta Chat</title>
    <link rel="icon" type="image/svg+xml" href="{base_path}/icon.svg" />
    <link rel="alternate icon" type="image/png" href="{base_path}/icon.png" />
    {theme._THEME_PRELOAD_SCRIPT}
    <style>
        :root {{
            --bg-color: #19232b;
            --bubble-bg: #232d36;
            --border-subtle: rgba(255, 255, 255, 0.08);
            --text-main: #e9edef;
            --text-muted: #aebac1;
            --color-primary: #2090ea;
        }}
        @media (prefers-color-scheme: light) {{
            :root:not([data-theme="dark"]) {{
                --bg-color: #efeae2;
                --bubble-bg: #ffffff;
                --border-subtle: rgba(0, 0, 0, 0.08);
                --text-main: #111b21;
                --text-muted: #54656f;
                --color-primary: #415e6b;
            }}
            :root:not([data-theme="dark"]) body {{
                background-image: url('{base_path}/background-light.jpg') !important;
            }}
            :root:not([data-theme="dark"]) .card {{
                box-shadow: 0 2px 10px rgba(0, 0, 0, 0.08) !important;
            }}
            :root:not([data-theme="dark"]) .btn:hover {{
                background: #354e59 !important;
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
            --text-main: #111b21;
            --text-muted: #54656f;
            --color-primary: #415e6b;
        }}
        :root[data-theme="light"] body {{
            background-image: url('{base_path}/background-light.jpg') !important;
        }}
        :root[data-theme="light"] .card {{
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.08) !important;
        }}
        :root[data-theme="light"] .btn:hover {{
            background: #354e59 !important;
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
            --text-main: #e9edef;
            --text-muted: #aebac1;
            --color-primary: #2090ea;
        }}
        :root[data-theme="dark"] body {{
            background-image: url('{base_path}/background.jpg') !important;
        }}
        :root[data-theme="dark"] .btn:hover {{
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
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            background-image: url('{base_path}/background.jpg');
            background-repeat: repeat;
            background-size: 430px auto;
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            align-items: center;
            padding: 1.5rem;
            text-align: center;
            -webkit-font-smoothing: antialiased;
        }}
        .card {{
            background: var(--bubble-bg);
            border: 1px solid var(--border-subtle);
            border-radius: 14px;
            padding: 2.5rem 2rem;
            max-width: 480px;
            width: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 1.25rem;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
            margin: auto;
        }}
        .icon {{ font-size: 3rem; }}
        h1 {{ font-size: 1.5rem; font-weight: 600; color: var(--text-main); }}
        p {{ color: var(--text-muted); font-size: 0.95rem; line-height: 1.5; }}
        .btn {{
            display: inline-block;
            background: var(--color-primary);
            color: #ffffff;
            text-decoration: none;
            padding: 0.65rem 1.4rem;
            border-radius: 8px;
            font-weight: 500;
            font-size: 0.92rem;
            margin-top: 0.5rem;
            transition: background 0.15s;
        }}
        .btn:hover {{ background: #1b7ed3; }}
        footer {{
            padding: 1rem;
            color: var(--text-muted);
            font-size: 0.85rem;
        }}
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
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">🔒</div>
        <h1>Channel Removed</h1>
        <p>The channel <strong>{ch_esc}</strong> has been removed from the public catalog and is no longer available for preview.</p>
        <a href="{home_url}" class="btn">← Return to Home</a>
    </div>
    <footer>
        {theme._THEME_SWITCHER_HTML}
    </footer>
    {theme._THEME_CONTROLLER_SCRIPT}
</body>
</html>
"""


def get_404_html(ingress_path: str = "") -> str:
    base_path = ingress_path.rstrip("/")
    home_url = f"{base_path}/" if base_path else "/"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Channel Not Found — Delta Chat</title>
    <link rel="icon" type="image/svg+xml" href="{base_path}/icon.svg" />
    <link rel="alternate icon" type="image/png" href="{base_path}/icon.png" />
    {theme._THEME_PRELOAD_SCRIPT}
    <style>
        :root {{
            --bg-color: #19232b;
            --bubble-bg: #232d36;
            --border-subtle: rgba(255, 255, 255, 0.08);
            --text-main: #e9edef;
            --text-muted: #aebac1;
            --color-primary: #2090ea;
        }}
        @media (prefers-color-scheme: light) {{
            :root:not([data-theme="dark"]) {{
                --bg-color: #efeae2;
                --bubble-bg: #ffffff;
                --border-subtle: rgba(0, 0, 0, 0.08);
                --text-main: #111b21;
                --text-muted: #54656f;
                --color-primary: #415e6b;
            }}
            :root:not([data-theme="dark"]) body {{
                background-image: url('{base_path}/background-light.jpg') !important;
            }}
            :root:not([data-theme="dark"]) .card {{
                box-shadow: 0 2px 10px rgba(0, 0, 0, 0.08) !important;
            }}
            :root:not([data-theme="dark"]) .btn:hover {{
                background: #354e59 !important;
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
            --text-main: #111b21;
            --text-muted: #54656f;
            --color-primary: #415e6b;
        }}
        :root[data-theme="light"] body {{
            background-image: url('{base_path}/background-light.jpg') !important;
        }}
        :root[data-theme="light"] .card {{
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.08) !important;
        }}
        :root[data-theme="light"] .btn:hover {{
            background: #354e59 !important;
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
            --text-main: #e9edef;
            --text-muted: #aebac1;
            --color-primary: #2090ea;
        }}
        :root[data-theme="dark"] body {{
            background-image: url('{base_path}/background.jpg') !important;
        }}
        :root[data-theme="dark"] .btn:hover {{
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
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            background-image: url('{base_path}/background.jpg');
            background-repeat: repeat;
            background-size: 430px auto;
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            align-items: center;
            padding: 1.5rem;
            text-align: center;
            -webkit-font-smoothing: antialiased;
        }}
        .card {{
            background: var(--bubble-bg);
            border: 1px solid var(--border-subtle);
            border-radius: 14px;
            padding: 2.5rem 2rem;
            max-width: 480px;
            width: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 1.25rem;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
            margin: auto;
        }}
        .icon {{ font-size: 3rem; }}
        h1 {{ font-size: 1.5rem; font-weight: 600; color: var(--text-main); }}
        p {{ color: var(--text-muted); font-size: 0.95rem; line-height: 1.5; }}
        .btn {{
            display: inline-block;
            background: var(--color-primary);
            color: #ffffff;
            text-decoration: none;
            padding: 0.65rem 1.4rem;
            border-radius: 8px;
            font-weight: 500;
            font-size: 0.92rem;
            margin-top: 0.5rem;
            transition: background 0.15s;
        }}
        .btn:hover {{ background: #1b7ed3; }}
        footer {{
            padding: 1rem;
            color: var(--text-muted);
            font-size: 0.85rem;
        }}
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
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">🔍</div>
        <h1>Channel Not Found</h1>
        <p>The requested channel preview could not be found. Please check that the URL is correct.</p>
        <a href="{home_url}" class="btn">← Return to Home</a>
    </div>
    <footer>
        {theme._THEME_SWITCHER_HTML}
    </footer>
    {theme._THEME_CONTROLLER_SCRIPT}
</body>
</html>
"""


