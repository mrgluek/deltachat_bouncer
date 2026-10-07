"""/test-turn: a browser self-test of the TURN servers Delta Chat calls use.

Everything runs in the visitor's browser with RTCPeerConnection; results and
addresses are never sent back to the server, and the page never shows the
visitor's IP address.

- turn.delta.chat (Delta Chat core's fallback TURN server, public
  credentials): a real relay allocation (iceTransportPolicy "relay"), over
  UDP - what the apps use - and over TCP for comparison.
- The TURN servers of the bot's relays: a STUN binding to their port 3478
  (UDP), i.e. reachability without handing out the relays' TURN credentials.
"""
import html
import json

from . import theme

_STRINGS = {
    "en": {
        "title": "TURN test",
        "lead": "Checks whether your network lets Delta Chat calls through to the TURN servers that relay them.",
        "how": "Runs in your browser; nothing is sent to this site. Turn off your VPN or proxy to test your real network, "
               "and run it on the network you call from (e.g. mobile data and Wi-Fi separately).",
        "again": "Run again",
        "running": "testing…",
        "ok": "works",
        "fail": "no answer",
        "timeout": "timed out",
        "unsupported": "Your browser does not support WebRTC, so it cannot run this test.",
        "fallback_udp": "turn.delta.chat · UDP",
        "fallback_udp_hint": "Used by Delta Chat apps whose relays have no TURN server of their own.",
        "fallback_tcp": "turn.delta.chat · TCP",
        "fallback_tcp_hint": "For comparison only: the apps use UDP.",
        "relay": "{name} · UDP port {port}",
        "relay_hint": "TURN server of the {name} relay (reachability check).",
        "v_all_ok": "✅ Calls through turn.delta.chat should work on this network.",
        "v_udp_blocked": "❌ UDP to turn.delta.chat is blocked here, TCP gets through. Delta Chat apps use UDP, so calls "
                         "that depend on turn.delta.chat will fail on this network.",
        "v_blocked": "❌ turn.delta.chat is unreachable from this network. Calls only work if your relay has its own "
                     "TURN server, or when both sides can connect directly.",
        "v_relays": "Relays whose TURN answered: {names}. Profiles on these relays use their relay's TURN server "
                    "instead of turn.delta.chat.",
        "v_relays_none": "None of the listed relays' TURN servers answered either: UDP may be blocked in general.",
        "foot": "Delta Chat chooses TURN servers itself: those announced by your profile's relays, otherwise "
                "turn.delta.chat. After a call to the bot, its report says which TURN server your app used.",
    },
    "ru": {
        "title": "Проверка TURN",
        "lead": "Проверяет, пропускает ли ваша сеть звонки Delta Chat к TURN-серверам, через которые они идут.",
        "how": "Работает в вашем браузере, на этот сайт ничего не отправляется. Выключите VPN или прокси, чтобы проверить "
               "свою настоящую сеть, и запускайте в той сети, откуда звоните (мобильный интернет и Wi-Fi — отдельно).",
        "again": "Проверить ещё раз",
        "running": "проверяю…",
        "ok": "работает",
        "fail": "нет ответа",
        "timeout": "время вышло",
        "unsupported": "Ваш браузер не поддерживает WebRTC, проверка невозможна.",
        "fallback_udp": "turn.delta.chat · UDP",
        "fallback_udp_hint": "Его используют приложения Delta Chat, если у их релеев нет своего TURN-сервера.",
        "fallback_tcp": "turn.delta.chat · TCP",
        "fallback_tcp_hint": "Только для сравнения: приложения используют UDP.",
        "relay": "{name} · UDP-порт {port}",
        "relay_hint": "TURN-сервер релея {name} (проверка доступности).",
        "v_all_ok": "✅ Звонки через turn.delta.chat в этой сети должны работать.",
        "v_udp_blocked": "❌ UDP до turn.delta.chat здесь заблокирован, TCP проходит. Приложения Delta Chat используют UDP, "
                         "поэтому звонки, которым нужен turn.delta.chat, в этой сети не пройдут.",
        "v_blocked": "❌ turn.delta.chat недоступен из этой сети. Звонки пройдут, только если у вашего релея есть свой "
                     "TURN-сервер или если стороны смогут соединиться напрямую.",
        "v_relays": "TURN ответил у релеев: {names}. Профили на этих релеях используют TURN своего релея, "
                    "а не turn.delta.chat.",
        "v_relays_none": "TURN-серверы перечисленных релеев тоже не ответили: возможно, UDP заблокирован вообще.",
        "foot": "Delta Chat сам выбирает TURN-серверы: те, что объявляют релеи вашего профиля, иначе turn.delta.chat. "
                "После звонка боту в его отчёте написано, какой TURN-сервер использовало ваше приложение.",
    },
}


def _json_for_script(data) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def get_turn_test_html(ingress_path: str, fallback: dict, relays: list[dict], instance_domain: str) -> str:
    """``fallback``: {host, port, user, password}; ``relays``: [{name, url}] (STUN URLs)."""
    base_path = ingress_path.rstrip("/")
    home_url = f"{base_path}/" if base_path else "/"
    cfg = {"fallback": fallback, "relays": relays, "strings": _STRINGS}
    domain = html.escape(instance_domain)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TURN test · {domain}</title>
    <meta name="description" content="Check whether your network lets Delta Chat calls reach their TURN servers." />
    <meta name="robots" content="noindex" />
    <link rel="icon" type="image/svg+xml" href="{base_path}/icon.svg" />
    <link rel="alternate icon" type="image/png" href="{base_path}/icon.png" />
    {theme._THEME_PRELOAD_SCRIPT}
    <style>
        :root {{
            --bg-color: #19232b; --card-bg: #232d36; --card-border: rgba(255,255,255,0.08);
            --text-main: #e9edef; --text-muted: #aebac1; --accent: #53bdeb;
            --ok: #3ccf91; --bad: #f2716b; --wait: #aebac1; --row-bg: rgba(0,0,0,0.18); --btn-text: #0b141a;
        }}
        @media (prefers-color-scheme: light) {{
            :root:not([data-theme="dark"]) {{
                --bg-color: #efeae2; --card-bg: #ffffff; --card-border: rgba(0,0,0,0.08);
                --text-main: #111b21; --text-muted: #54656f; --accent: #0070e0;
                --ok: #0b8a57; --bad: #c62f28; --wait: #54656f; --row-bg: rgba(0,0,0,0.035); --btn-text: #ffffff;
            }}
        }}
        :root[data-theme="light"] {{
            --bg-color: #efeae2; --card-bg: #ffffff; --card-border: rgba(0,0,0,0.08);
            --text-main: #111b21; --text-muted: #54656f; --accent: #0070e0;
            --ok: #0b8a57; --bad: #c62f28; --wait: #54656f; --row-bg: rgba(0,0,0,0.035); --btn-text: #ffffff;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0; background: var(--bg-color); color: var(--text-main);
            font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }}
        .wrap {{ max-width: 680px; margin: 0 auto; padding: 24px 16px 48px; }}
        header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 20px; }}
        header a {{ color: var(--text-main); text-decoration: none; font-weight: 600; }}
        .lang a {{ color: var(--text-muted); text-decoration: none; margin-left: 8px; font-size: 14px; }}
        .lang a.active {{ color: var(--accent); }}
        .card {{ background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 16px; padding: 22px; }}
        h1 {{ font-size: 24px; margin: 0 0 8px; }}
        p {{ margin: 0 0 12px; }}
        .muted {{ color: var(--text-muted); font-size: 14px; }}
        .rows {{ margin: 18px 0; display: grid; gap: 8px; }}
        .row {{ display: grid; grid-template-columns: 28px 1fr auto; gap: 10px; align-items: start;
               background: var(--row-bg); border-radius: 10px; padding: 10px 12px; }}
        .icon {{ font-size: 18px; line-height: 24px; text-align: center; }}
        .name {{ font-weight: 600; overflow-wrap: anywhere; }}
        .hint {{ color: var(--text-muted); font-size: 13px; }}
        .res {{ font-size: 14px; white-space: nowrap; line-height: 24px; }}
        .res.ok {{ color: var(--ok); }} .res.bad {{ color: var(--bad); }} .res.wait {{ color: var(--wait); }}
        .verdict {{ display: grid; gap: 8px; margin: 4px 0 16px; }}
        .verdict p {{ margin: 0; }}
        button.run {{ background: var(--accent); color: var(--btn-text); border: 0; border-radius: 10px; padding: 10px 18px;
                      font-size: 15px; cursor: pointer; }}
        button.run:disabled {{ opacity: 0.55; cursor: default; }}
        .foot {{ margin-top: 18px; }}
        .theme-switcher {{ display: inline-flex; gap: 2px; padding: 2px; border-radius: 10px;
                           border: 1px solid var(--card-border); }}
        .theme-btn {{ background: none; border: 0; color: var(--text-muted); padding: 5px 7px; border-radius: 8px; cursor: pointer; }}
        .theme-btn.active {{ background: var(--row-bg); color: var(--text-main); }}
        @media (max-width: 420px) {{ .row {{ grid-template-columns: 24px 1fr; }} .res {{ grid-column: 2; }} }}
    </style>
</head>
<body>
<div class="wrap">
    <header>
        <a href="{home_url}">{domain}</a>
        <div>
            <span class="lang"><a href="?lang=en" data-lang="en">EN</a><a href="?lang=ru" data-lang="ru">RU</a></span>
            {theme._THEME_SWITCHER_HTML}
        </div>
    </header>
    <main class="card">
        <h1 id="t-title"></h1>
        <p id="t-lead"></p>
        <p class="muted" id="t-how"></p>
        <div class="rows" id="rows"></div>
        <div class="verdict" id="verdict"></div>
        <button class="run" id="run"></button>
        <p class="muted foot" id="t-foot"></p>
    </main>
</div>
{theme._THEME_CONTROLLER_SCRIPT}
<script>
(function () {{
    var CFG = {_json_for_script(cfg)};
    var params = new URLSearchParams(location.search);
    var lang = params.get("lang") || ((navigator.language || "en").toLowerCase().indexOf("ru") === 0 ? "ru" : "en");
    if (!CFG.strings[lang]) lang = "en";
    var S = CFG.strings[lang];
    document.documentElement.lang = lang;
    document.querySelectorAll(".lang a").forEach(function (a) {{
        if (a.dataset.lang === lang) a.classList.add("active");
    }});
    function fmt(s, v) {{ return s.replace(/\\{{(\\w+)\\}}/g, function (_, k) {{ return v[k]; }}); }}
    function text(id, s) {{ document.getElementById(id).textContent = s; }}
    text("t-title", S.title); text("t-lead", S.lead); text("t-how", S.how); text("t-foot", S.foot);
    document.title = S.title + " · " + {_json_for_script(instance_domain)};
    var runBtn = document.getElementById("run");
    runBtn.textContent = S.again;

    var fb = CFG.fallback;
    var tests = [
        {{ id: "fb-udp", name: S.fallback_udp, hint: S.fallback_udp_hint, want: "relay", policy: "relay",
          servers: [{{ urls: "turn:" + fb.host + ":" + fb.port + "?transport=udp", username: fb.user, credential: fb.password }}] }},
        {{ id: "fb-tcp", name: S.fallback_tcp, hint: S.fallback_tcp_hint, want: "relay", policy: "relay",
          servers: [{{ urls: "turn:" + fb.host + ":" + fb.port + "?transport=tcp", username: fb.user, credential: fb.password }}] }}
    ];
    CFG.relays.forEach(function (r, i) {{
        tests.push({{ id: "relay-" + i, relay: r.name, name: fmt(S.relay, {{ name: r.name, port: (r.url.match(/:(\\d+)$/) || [0, "3478"])[1] }}),
                     hint: fmt(S.relay_hint, {{ name: r.name }}), want: "srflx", policy: "all",
                     servers: [{{ urls: r.url }}] }});
    }});

    var rows = document.getElementById("rows");
    tests.forEach(function (t) {{
        var row = document.createElement("div");
        row.className = "row";
        row.innerHTML = '<div class="icon">⏳</div><div><div class="name"></div><div class="hint"></div></div><div class="res wait"></div>';
        row.querySelector(".name").textContent = t.name;
        row.querySelector(".hint").textContent = t.hint;
        rows.appendChild(row);
        t.row = row;
    }});

    function setRow(t, state, label) {{
        t.row.querySelector(".icon").textContent = state === "ok" ? "✅" : state === "bad" ? "❌" : "⏳";
        var res = t.row.querySelector(".res");
        res.className = "res " + state;
        res.textContent = label;
    }}

    // Gather candidates and wait for one of type t.want. Only the type is
    // looked at; candidate addresses (the visitor's IP) are never shown.
    function probe(t, timeoutMs) {{
        return new Promise(function (resolve) {{
            var pc, done = false, t0 = performance.now(), timer;
            function finish(ok, why) {{
                if (done) return;
                done = true;
                clearTimeout(timer);
                try {{ pc.close(); }} catch (e) {{}}
                resolve({{ ok: ok, ms: Math.round(performance.now() - t0), why: why }});
            }}
            try {{
                pc = new RTCPeerConnection({{ iceServers: t.servers, iceTransportPolicy: t.policy }});
            }} catch (e) {{
                resolve({{ ok: false, why: "fail" }});
                return;
            }}
            pc.createDataChannel("probe");
            pc.onicecandidate = function (e) {{
                if (!e.candidate) {{ finish(false, "fail"); return; }}
                if ((" " + e.candidate.candidate + " ").indexOf(" typ " + t.want + " ") >= 0) finish(true);
            }};
            timer = setTimeout(function () {{ finish(false, "timeout"); }}, timeoutMs);
            pc.createOffer().then(function (o) {{ return pc.setLocalDescription(o); }})
              .catch(function () {{ finish(false, "fail"); }});
        }});
    }}

    function verdict(results) {{
        var v = document.getElementById("verdict");
        v.innerHTML = "";
        function say(s) {{ var p = document.createElement("p"); p.textContent = s; v.appendChild(p); }}
        var udp = results["fb-udp"], tcp = results["fb-tcp"];
        if (udp.ok) say(S.v_all_ok);
        else if (tcp.ok) say(S.v_udp_blocked);
        else say(S.v_blocked);
        var relayTests = tests.filter(function (t) {{ return t.relay; }});
        if (relayTests.length) {{
            var okNames = relayTests.filter(function (t) {{ return results[t.id].ok; }}).map(function (t) {{ return t.relay; }});
            if (okNames.length) say(fmt(S.v_relays, {{ names: okNames.join(", ") }}));
            else if (!udp.ok) say(S.v_relays_none);
        }}
    }}

    function run() {{
        if (typeof RTCPeerConnection === "undefined") {{
            document.getElementById("verdict").textContent = S.unsupported;
            runBtn.disabled = true;
            return;
        }}
        runBtn.disabled = true;
        document.getElementById("verdict").innerHTML = "";
        tests.forEach(function (t) {{ setRow(t, "wait", S.running); }});
        var results = {{}};
        Promise.all(tests.map(function (t) {{
            return probe(t, 10000).then(function (r) {{
                results[t.id] = r;
                if (r.ok) setRow(t, "ok", S.ok + " · " + r.ms + " ms");
                else setRow(t, "bad", r.why === "timeout" ? S.timeout : S.fail);
            }});
        }})).then(function () {{
            verdict(results);
            runBtn.disabled = false;
        }});
    }}
    runBtn.addEventListener("click", run);
    run();
}})();
</script>
</body>
</html>"""
