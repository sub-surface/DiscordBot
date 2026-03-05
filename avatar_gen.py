import hashlib
import os
from pathlib import Path
from PIL import Image, ImageDraw

AVATAR_DIR = Path(__file__).parent / "personas" / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)

def generate_avatar(name: str) -> str:
    """Generates a unique abstract avatar for a persona and returns the path."""
    filename = f"{name.lower().replace(' ', '_')}.png"
    filepath = AVATAR_DIR / filename
    
    if filepath.exists():
        return str(filepath)

    # Use hash of name for deterministic generation
    hash_obj = hashlib.md5(name.lower().encode())
    hash_hex = hash_obj.hexdigest()
    
    # Extract colors from hash
    bg_color = (
        int(hash_hex[0:2], 16),
        int(hash_hex[2:4], 16),
        int(hash_hex[4:6], 16)
    )
    fg_color = (
        int(hash_hex[6:8], 16),
        int(hash_hex[8:10], 16),
        int(hash_hex[10:12], 16)
    )
    
    # Ensure colors aren't too similar (crude check)
    if sum(abs(a - b) for a, b in zip(bg_color, fg_color)) < 100:
        fg_color = (255 - fg_color[0], 255 - fg_color[1], 255 - fg_color[2])

    img = Image.new("RGB", (256, 256), color=bg_color)
    draw = ImageDraw.Draw(img)
    
    # Draw some abstract shapes based on the hash
    for i in range(5):
        x1 = int(hash_hex[i*2:i*2+2], 16)
        y1 = int(hash_hex[i*3:i*3+2], 16)
        x2 = int(hash_hex[i*4:i*4+2], 16)
        y2 = int(hash_hex[i*5:i*5+2], 16)
        
        # Ensure x1, y1 is top-left and x2, y2 is bottom-right for PIL
        coords = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        
        shape_type = int(hash_hex[i], 16) % 3
        if shape_type == 0:
            draw.ellipse(coords, outline=fg_color, width=4)
        elif shape_type == 1:
            draw.rectangle(coords, outline=fg_color, width=4)
        else:
            draw.line([x1, y1, x2, y2], fill=fg_color, width=6)

    # Add a little "sparkle" or detail
    for i in range(10):
        px = int(hash_hex[i:i+2], 16)
        py = int(hash_hex[i+1:i+3], 16)
        draw.point([px, py], fill=(255, 255, 255))

    img.save(filepath)
    return str(filepath)

if __name__ == "__main__":
    # Test generation for a few names
    for test_name in ["Mochi", "The Merchant", "Sigint Ghost", "Cassandra"]:
        print(f"Generated avatar for {test_name}: {generate_avatar(test_name)}")
