"""Brand assets: crisp inline SVG logo and a generated favicon (no binary files to ship)."""

LOGO_SVG = (
    '<svg width="34" height="34" viewBox="0 0 34 34" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<defs><linearGradient id="cg" x1="0" y1="0" x2="34" y2="34" gradientUnits="userSpaceOnUse">'
    '<stop stop-color="#8b5cf6"/><stop offset="1" stop-color="#06b6d4"/></linearGradient></defs>'
    '<rect width="34" height="34" rx="10" fill="url(#cg)"/>'
    '<path d="M17 7.5l2.1 6.4 6.4 2.1-6.4 2.1L17 24.5l-2.1-6.4-6.4-2.1 6.4-2.1L17 7.5z" fill="#fff"/>'
    '</svg>'
)


def favicon(size=64):
    """Gradient rounded square with a white sparkle. Returns a PIL image, or an emoji if Pillow is unavailable."""
    try:
        from PIL import Image, ImageDraw
    except Exception:  # pragma: no cover
        return "✦"
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size - 2)
            px[x, y] = (int(139 + (6 - 139) * t), int(92 + (182 - 92) * t), int(246 + (212 - 246) * t), 255)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.3), fill=255)
    img.putalpha(mask)
    s = size / 34
    star = [(17, 7.5), (19.1, 13.9), (25.5, 16), (19.1, 18.1), (17, 24.5), (14.9, 18.1), (8.5, 16), (14.9, 13.9)]
    ImageDraw.Draw(img).polygon([(x * s, y * s) for x, y in star], fill=(255, 255, 255, 255))
    return img
