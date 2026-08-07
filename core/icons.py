"""Unified icon registry for FlashShare.

All SVG icon paths live here. Every part of the codebase (views.py,
app.js, detail pages) pulls from this single source — no more scattered
hand-drawn paths.

Style: 24x24 viewBox, stroke-based, multi-path (each semantic part is a
separate <path> for clean rendering at small sizes).

Usage in Python:
    from icons import icon_svg
    html = icon_svg("download")

Usage in JS (injected via window.__ICONS__):
    const path = window.__ICONS__.download;
"""

_ICONS = {
    # ── Transfer ──
    "upload": '<path d="M12 13v8"></path><path d="M8 17l4-4 4 4"></path><path d="M20 16.5A4.5 4.5 0 0 0 17.5 8h-1.3A7 7 0 1 0 4 14.9"></path>',
    "download": '<path d="M12 4v11"></path><path d="M7 10l5 5 5-5"></path><path d="M5 20h14"></path>',
    "download_count": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><path d="M7 10l5 5 5-5"></path><path d="M12 15V3"></path>',
    "upload_count": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><path d="M7 8l5-5 5 5"></path><path d="M12 3v12"></path>',

    # ── Share / link ──
    "share": '<circle cx="18" cy="5" r="3"></circle><circle cx="6" cy="12" r="3"></circle><circle cx="18" cy="19" r="3"></circle><path d="M8.6 13.5l6.8 4"></path><path d="M15.4 6.5l-6.8 4"></path>',
    "link": '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"></path><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"></path>',
    "copy": '<rect x="8" y="8" width="11" height="11" rx="2"></rect><path d="M5 15H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v1"></path>',

    # ── QR ──
    "qr": '<path d="M4 4h6v6H4z"></path><path d="M14 4h6v6h-6z"></path><path d="M4 14h6v6H4z"></path><path d="M15 15h2"></path><path d="M19 14v3"></path><path d="M14 19h3"></path><path d="M20 20h-1"></path>',

    # ── Actions ──
    "check": '<path d="M20 6L9 17l-5-5"></path>',
    "close": '<path d="M6 6l12 12"></path><path d="M18 6L6 18"></path>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.2-4.9L4 8"></path><path d="M4 4v4h4"></path><path d="M4 13a8 8 0 0 0 14.2 4.9L20 16"></path><path d="M20 20v-4h-4"></path>',
    "edit": '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>',
    "trash": '<path d="M4 7h16"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M6 7l1 13h10l1-13"></path><path d="M9 7V4h6v3"></path>',
    "open": '<path d="M14 4h6v6"></path><path d="M10 14L20 4"></path><path d="M20 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h5"></path>',
    "logout": '<path d="M10 6H6a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h4"></path><path d="M14 8l4 4-4 4"></path><path d="M18 12H9"></path>',
    "settings": '<circle cx="12" cy="8" r="4"></circle><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"></path>',

    # ── File types ──
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><path d="M21 15l-5-5L5 21"></path>',
    "archive": '<path d="M21 8l-5-5H7a2 2 0 0 0-2 2v3"></path><rect x="3" y="8" width="18" height="4" rx="1"></rect><path d="M5 12v8a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-8"></path>',
    "doc": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><path d="M14 2v6h6"></path><path d="M8 13h6M8 16h6M8 19h4"></path>',
    "pdf": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><path d="M14 2v6h6"></path>',
    "sheet": '<rect x="3" y="3" width="18" height="18" rx="2"></rect><path d="M3 9h18M3 15h18M9 3v18M15 3v18"></path>',
    "slides": '<rect x="3" y="4" width="18" height="12" rx="2"></rect><path d="M12 16v4M8 20h8"></path>',
    "text": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><path d="M14 2v6h6"></path><path d="M8 13h8M8 17h6"></path>',
    "file_size": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"></path><path d="M14 3v5h5"></path>',

    # ── Meta / info ──
    "info": '<circle cx="12" cy="12" r="10"></circle><path d="M12 16v-4"></path><path d="M12 8h.01"></path>',
    "expiry": '<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path>',
    "bandwidth": '<path d="M7 4v12"></path><path d="M3 8l4-4 4 4"></path><path d="M17 20V8"></path><path d="M13 16l4 4 4-4"></path>',

    # ── Tools ──
    "wand": '<rect x="3" y="3" width="18" height="18" rx="2"></rect><path d="M3 9h18"></path><circle cx="6.5" cy="6" r="0.5" fill="currentColor"></circle><circle cx="9.5" cy="6" r="0.5" fill="currentColor"></circle><circle cx="12.5" cy="6" r="0.5" fill="currentColor"></circle>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"></path><path d="M9 12l2 2 4-4"></path>',
    "crop": '<path d="M6 2v14a2 2 0 0 0 2 2h14"></path><path d="M18 22V8a2 2 0 0 0-2-2H2"></path>',
    "compress": '<path d="M4 9V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v3"></path><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"></path><path d="M9 9l3-3 3 3"></path><path d="M9 15l3 3 3-3"></path>',
    "convert": '<path d="M16 3l4 4-4 4"></path><path d="M20 7H4"></path><path d="M8 21l-4-4 4-4"></path><path d="M4 17h16"></path>',
    "favicon": '<rect x="4" y="4" width="16" height="16" rx="3"></rect><path d="M12 8l1.5 3 3 .5-2 2 .5 3-3-1.5-3 1.5.5-3-2-2 3-.5z"></path>',
    "merge": '<path d="M8 3v5l4 3 4-3V3"></path><path d="M12 11v10"></path>',
    "hash": '<path d="M4 9h16"></path><path d="M4 15h16"></path><path d="M10 3L8 21"></path><path d="M16 3l-2 18"></path>',

    # ── Brand ──
    "brand": '',
}


def icon_svg(name, cls=""):
    """Return a complete <svg> element for the given icon name."""
    path = _ICONS.get(name)
    if not path:
        return ""
    class_attr = ' class="%s"' % cls if cls else ""
    return (
        '<svg viewBox="0 0 24 24" aria-hidden="true"%s>%s</svg>' % (class_attr, path)
    )


def icon_path(name):
    """Return just the inner SVG path(s) for JS usage."""
    return _ICONS.get(name, "")


def all_icon_names():
    return set(_ICONS.keys())
