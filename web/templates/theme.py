"""Shared theme-toggle <script>/<div> snippets embedded in every rendered page."""

_THEME_PRELOAD_SCRIPT = """<script>
(function() {
    try {
        var t = localStorage.getItem('theme');
        if (t === 'light' || t === 'dark') {
            document.documentElement.setAttribute('data-theme', t);
        } else {
            document.documentElement.setAttribute('data-theme', 'system');
        }
    } catch (e) {}
})();
</script>"""

_THEME_SWITCHER_HTML = """<div class="theme-switcher" role="radiogroup" aria-label="Theme selection">
    <button id="light-theme-button" class="theme-btn" aria-label="Light theme" title="Light theme" onclick="setTheme('light')">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="5"></circle>
            <line x1="12" y1="1" x2="12" y2="3"></line>
            <line x1="12" y1="21" x2="12" y2="23"></line>
            <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
            <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
            <line x1="1" y1="12" x2="3" y2="12"></line>
            <line x1="21" y1="12" x2="23" y2="12"></line>
            <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
            <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
        </svg>
    </button>
    <button id="dark-theme-button" class="theme-btn" aria-label="Dark theme" title="Dark theme" onclick="setTheme('dark')">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
        </svg>
    </button>
    <button id="system-theme-button" class="theme-btn" aria-label="System theme" title="System theme" onclick="setTheme('system')">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
            <rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect>
            <line x1="8" y1="21" x2="16" y2="21"></line>
            <line x1="12" y1="17" x2="12" y2="21"></line>
        </svg>
    </button>
</div>"""

_THEME_CONTROLLER_SCRIPT = """<script>
function updateThemeButtons(theme) {
    var btns = {
        'light': document.getElementById('light-theme-button'),
        'dark': document.getElementById('dark-theme-button'),
        'system': document.getElementById('system-theme-button')
    };
    for (var k in btns) {
        if (btns[k]) {
            if (k === theme) {
                btns[k].classList.add('active');
                btns[k].setAttribute('aria-pressed', 'true');
            } else {
                btns[k].classList.remove('active');
                btns[k].setAttribute('aria-pressed', 'false');
            }
        }
    }
}
function setTheme(theme) {
    var css = document.createElement('style');
    css.appendChild(document.createTextNode('* { transition: none !important; }'));
    document.head.appendChild(css);
    try {
        if (theme === 'light' || theme === 'dark') {
            localStorage.setItem('theme', theme);
            document.documentElement.setAttribute('data-theme', theme);
        } else {
            localStorage.setItem('theme', 'system');
            document.documentElement.setAttribute('data-theme', 'system');
        }
    } catch (e) {}
    window.getComputedStyle(css).opacity;
    document.head.removeChild(css);
    updateThemeButtons(theme);
}
(function initTheme() {
    var current = 'system';
    try { current = localStorage.getItem('theme') || 'system'; } catch (e) {}
    updateThemeButtons(current);
    if (window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function() {
            var t = 'system';
            try { t = localStorage.getItem('theme') || 'system'; } catch (e) {}
            if (t === 'system') updateThemeButtons('system');
        });
    }
})();
</script>"""
