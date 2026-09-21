"""Channel RSS/Atom-ish XML feed generation (the /c/<token>/rss.xml route)."""
import html
import os
import time
from datetime import datetime, timezone
from email.utils import format_datetime

import formatting
import state

def _escape_cdata(text: str) -> str:
    """Escape ]]> to prevent CDATA section breakout in XML generation."""
    if not text:
        return ""
    return str(text).replace("]]>", "]]]]><![CDATA[>")


def get_channel_rss_xml(channel: dict, posts: list[dict], base_url: str) -> str:
    token = channel.get("token", "")
    ch_name = channel.get("name") or "Channel"
    ch_desc = channel.get("description") or "Delta Chat Channel"
    channel_url = f"{base_url.rstrip('/')}/c/{token}"
    rss_url = f"{channel_url}/rss.xml"
    safe_ch_name = _escape_cdata(ch_name)
    safe_ch_desc = _escape_cdata(ch_desc)

    last_build = format_datetime(datetime.now(timezone.utc))
    if posts and posts[0].get("timestamp"):
        try:
            last_build = format_datetime(datetime.fromtimestamp(posts[0]["timestamp"], tz=timezone.utc))
        except Exception:
            pass

    items_xml = []
    for p in posts:
        post_ts = p.get("timestamp") or time.time()
        try:
            pub_date = format_datetime(datetime.fromtimestamp(post_ts, tz=timezone.utc))
        except Exception:
            pub_date = format_datetime(datetime.now(timezone.utc))

        guid = f"{token}-{p.get('id', p.get('msg_id', int(post_ts)))}"
        post_text = p.get("text") or ""
        post_title = (post_text.split("\n")[0][:80] if post_text else f"Post {p.get('id')}")

        desc_parts = []
        if post_text:
            desc_parts.append(formatting.format_markdown_html(post_text))

        media_type = p.get("media_type")
        media_fn = p.get("media_filename")
        msg_id = p.get("msg_id")
        enclosure_tag = ""
        if media_fn and msg_id:
            media_url = f"{base_url.rstrip('/')}/media/{token}/{msg_id}/{media_fn}"
            file_length = 0
            expected_dir = os.path.join(state.CHANNEL_MEDIA_DIR, token)
            for cand in [
                os.path.join(expected_dir, f"{msg_id}_{media_fn}"),
                os.path.join(expected_dir, f"{msg_id}_{os.path.splitext(media_fn)[0]}.webp"),
                os.path.join(expected_dir, media_fn),
            ]:
                if os.path.exists(cand):
                    try:
                        file_length = os.path.getsize(cand)
                        break
                    except Exception:
                        pass
            if file_length <= 0 and p.get("file_bytes"):
                file_length = p.get("file_bytes", 0)
            length_attr = str(file_length if file_length > 0 else 1)

            if media_type == "image":
                mime_type = "image/webp" if (media_fn and media_fn.lower().endswith(".webp")) else "image/jpeg"
                desc_parts.append(f'<p><img src="{media_url}" alt="image" /></p>')
                enclosure_tag = f'<enclosure url="{media_url}" length="{length_attr}" type="{mime_type}" />'
            elif media_type == "video":
                desc_parts.append(f'<p><video src="{media_url}" controls></video></p>')
                enclosure_tag = f'<enclosure url="{media_url}" length="{length_attr}" type="video/mp4" />'
            elif media_type == "audio":
                desc_parts.append(f'<p><audio src="{media_url}" controls></audio></p>')
                enclosure_tag = f'<enclosure url="{media_url}" length="{length_attr}" type="audio/mpeg" />'
            else:
                desc_parts.append(f'<p><a href="{media_url}">📎 Download {html.escape(media_fn)}</a></p>')
                enclosure_tag = f'<enclosure url="{media_url}" length="{length_attr}" type="application/octet-stream" />'

        full_desc = "\n".join(desc_parts)
        safe_post_title = _escape_cdata(post_title)
        safe_full_desc = _escape_cdata(full_desc)
        post_link = f"{channel_url}#post-{msg_id}" if msg_id else f"{channel_url}#{guid}"

        item = f"""    <item>
      <title><![CDATA[{safe_post_title}]]></title>
      <description><![CDATA[{safe_full_desc}]]></description>
      <link>{post_link}</link>
      <guid isPermaLink="true">{post_link}</guid>
      <pubDate>{pub_date}</pubDate>
      {enclosure_tag}
    </item>"""
        items_xml.append(item)

    items_str = "\n".join(items_xml)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title><![CDATA[{safe_ch_name}]]></title>
    <link>{channel_url}</link>
    <description><![CDATA[{safe_ch_desc}]]></description>
    <atom:link href="{rss_url}" rel="self" type="application/rss+xml" />
    <lastBuildDate>{last_build}</lastBuildDate>
{items_str}
  </channel>
</rss>"""


# ── Web Request Handlers ──

