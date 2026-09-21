"""Text formatting helpers: Delta Chat markdown -> HTML, post timestamps."""
import html
import re
from datetime import datetime, timezone

from config import DC_FALLBACK_PATTERN


def format_markdown_html(text: str) -> str:
    """Safely escape HTML and render CommonMark/Delta Chat markdown formatting."""
    if not text:
        return ""

    # Strip Delta Chat attachment fallback tags (e.g. [Image – 304.26 KiB])
    text = DC_FALLBACK_PATTERN.sub('', text).strip()
    if not text:
        return ""

    code_blocks = []
    inline_codes = []

    # 1. Extract fenced code blocks
    def save_code_block(match):
        lang = match.group(1) or ""
        code = match.group(2)
        code_escaped = html.escape(code.strip("\r\n"))
        lang_class = f' class="language-{html.escape(lang)}"' if lang else ""
        placeholder = f"\x00CB_{len(code_blocks)}\x00"
        code_blocks.append(f'<pre><code{lang_class}>{code_escaped}</code></pre>')
        return placeholder

    text = re.sub(r'```([a-zA-Z0-9_-]*)\r?\n?(.*?)\r?\n?```', save_code_block, text, flags=re.DOTALL)

    # 2. Extract inline code
    def save_inline_code(match):
        code = match.group(1)
        placeholder = f"\x00IC_{len(inline_codes)}\x00"
        inline_codes.append(f'<code>{html.escape(code)}</code>')
        return placeholder

    text = re.sub(r'`([^`\r\n]+)`', save_inline_code, text)

    # 3. HTML escape remaining text
    text = html.escape(text)

    # 4. Spoilers: ||spoiler||
    text = re.sub(r'\|\|(.+?)\|\|', r'<span class="spoiler" onclick="this.classList.toggle(\'revealed\')">\1</span>', text)

    # 5. Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)

    # 6. Strikethrough: ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<del>\1</del>', text)

    # 7. Italic: *text* or _text_
    text = re.sub(r'(?<![a-zA-Z0-9])\*([^*\r\n]+?)\*(?![a-zA-Z0-9])', r'<em>\1</em>', text)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_\r\n]+?)_(?![a-zA-Z0-9])', r'<em>\1</em>', text)

    # 8. Markdown links: [label](url)
    def replace_md_link(match):
        label = match.group(1)
        url = html.unescape(match.group(2)).strip()
        if re.match(r'^(https?://|mailto:)', url, re.IGNORECASE):
            safe_url = html.escape(url, quote=True)
            return f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{label}</a>'
        return match.group(0)

    text = re.sub(r'\[([^\]]+)\]\(((?:https?://|mailto:)[^\s\)]+)\)', replace_md_link, text)

    # 9. Autolink bare URLs
    def autolink_url(match):
        raw_url = html.unescape(match.group(1)).strip()
        safe_url = html.escape(raw_url, quote=True)
        return f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{match.group(1)}</a>'

    parts = re.split(r'(<a\s+[^>]*>.*?</a>)', text, flags=re.DOTALL)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r'(?<!href=")(?<!href=\')(?<!">)(https?://[^\s<>"\'\)]+)', autolink_url, parts[i])
    text = "".join(parts)

    # 10. Blockquotes
    lines = text.split("\n")
    new_lines = []
    in_quote = False
    quote_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("&gt; ") or stripped == "&gt;":
            q_content = line[line.find("&gt;") + 4:].lstrip()
            quote_lines.append(q_content)
            in_quote = True
        else:
            if in_quote:
                new_lines.append(f'<blockquote>{"<br>".join(quote_lines)}</blockquote>')
                quote_lines = []
                in_quote = False
            new_lines.append(line)
    if in_quote:
        new_lines.append(f'<blockquote>{"<br>".join(quote_lines)}</blockquote>')

    text = "<br>".join(new_lines)

    # 11. Restore placeholders
    for idx, cb in enumerate(code_blocks):
        text = text.replace(f"<br>\x00CB_{idx}\x00<br>", f"\n{cb}\n")
        text = text.replace(f"<br>\x00CB_{idx}\x00", f"\n{cb}")
        text = text.replace(f"\x00CB_{idx}\x00<br>", f"{cb}\n")
        text = text.replace(f"\x00CB_{idx}\x00", cb)

    for idx, ic in enumerate(inline_codes):
        text = text.replace(f"\x00IC_{idx}\x00", ic)

    text = re.sub(r'(<br>){3,}', '<br><br>', text)
    return text.strip()


def _autolink(text: str) -> str:
    """Backward-compatible alias for format_markdown_html."""
    return format_markdown_html(text)


def _format_post_time(ts: float) -> str:
    """Format post timestamp in a human-friendly format."""
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%d %b %Y, %H:%M UTC")
    except Exception:
        return ""
