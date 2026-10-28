"""Icono de la app (se dibuja con código, así no hay archivos de imagen que empaquetar)."""
from PIL import Image, ImageDraw


def make_icon(color: str = "#44bd50", size: int = 64) -> Image.Image:
    """Monitor con un punto de estado: verde = listo, naranja = en uso, rojo = bloqueado."""
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    light = (232, 236, 240, 255)
    d.rounded_rectangle((4 * s, 6 * s, 60 * s, 42 * s), radius=6 * s, fill=(30, 36, 44, 255),
                        outline=light, width=max(1, int(3 * s)))
    d.rectangle((28 * s, 42 * s, 36 * s, 50 * s), fill=light)
    d.rounded_rectangle((18 * s, 50 * s, 46 * s, 56 * s), radius=3 * s, fill=light)
    d.ellipse((38 * s, 38 * s, 62 * s, 62 * s), fill=color, outline=(16, 20, 24, 255), width=max(1, int(2 * s)))
    return img
