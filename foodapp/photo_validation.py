"""Check image container structure and dimensions without decoding pixels."""

from struct import unpack_from
from zlib import crc32

MAX_PHOTO_PIXELS = 20_000_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def valid_dimensions(width, height):
    return width > 0 and height > 0 and width * height <= MAX_PHOTO_PIXELS


def valid_png(data):
    offset = len(PNG_SIGNATURE)
    image_data = False
    while offset + 12 <= len(data):
        size, tag = unpack_from(">I4s", data, offset)
        end = offset + 8 + size
        if end + 4 > len(data):
            return False
        checksum, = unpack_from(">I", data, end)
        if crc32(memoryview(data)[offset + 4:end]) != checksum:
            return False
        if offset == len(PNG_SIGNATURE):
            if tag != b"IHDR" or size != 13:
                return False
            width, height, depth, color, compression, filtering, interlace = unpack_from(">IIBBBBB", data, offset + 8)
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if (not valid_dimensions(width, height) or depth not in depths.get(color, ())
                    or compression or filtering or interlace not in (0, 1)):
                return False
        elif tag == b"IHDR":
            return False
        elif tag == b"IDAT":
            image_data |= size > 0
        elif tag == b"IEND":
            return image_data and size == 0 and end + 4 == len(data)
        offset = end + 4
    return False


def valid_jpeg(data):
    offset = 2
    frame = scanning = pixels = False
    while offset < len(data):
        marker_start = data.find(b"\xff", offset) if scanning else offset
        if marker_start < 0 or data[marker_start] != 0xFF:
            return False
        pixels |= scanning and marker_start > offset
        offset = marker_start + 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset == len(data):
            return False
        marker = data[offset]
        offset += 1
        if scanning and (marker == 0 or 0xD0 <= marker <= 0xD7):
            pixels |= marker == 0  # An escaped 0xFF is entropy data.
            continue
        scanning = False
        if marker == 0xD9:
            return frame and pixels and offset == len(data)
        if marker < 0xC0 or 0xD0 <= marker <= 0xD8 or offset + 2 > len(data):
            return False
        size, = unpack_from(">H", data, offset)
        if size < 2 or offset + size > len(data):
            return False
        if marker in JPEG_SOF:
            if size < 11:
                return False
            height, width, components = unpack_from(">HHB", data, offset + 3)
            if size != 8 + 3 * components or not valid_dimensions(width, height):
                return False
            frame = True
        elif marker == 0xDA:
            if not frame or size < 8 or size != 6 + 2 * data[offset + 2]:
                return False
            scanning = True
        offset += size
    return False


def webp_chunks(data, offset, end):
    while offset < end:
        if end - offset < 8:
            raise ValueError
        tag, size = unpack_from("<4sI", data, offset)
        start = offset + 8
        offset = start + size + (size & 1)
        if offset > end:
            raise ValueError
        yield tag, start, size


def webp_image_dimensions(data, tag, start, size):
    if tag == b"VP8 " and size > 10 and data[start + 3:start + 6] == b"\x9d\x01\x2a":
        frame_tag = int.from_bytes(data[start:start + 3], "little")
        if frame_tag & 1 or 10 + (frame_tag >> 5) > size:
            raise ValueError
        width, height = unpack_from("<HH", data, start + 6)
        return width & 0x3FFF, height & 0x3FFF
    if tag == b"VP8L" and size > 5 and data[start] == 0x2F:
        bits, = unpack_from("<I", data, start + 1)
        if bits >> 29:
            raise ValueError
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise ValueError


def webp_u24(data, offset):
    return int.from_bytes(data[offset:offset + 3], "little")


def valid_webp(data):
    if len(data) < 12 or unpack_from("<I", data, 4)[0] + 8 != len(data):
        return False
    canvas = None
    image = animated = False
    try:
        for tag, start, size in webp_chunks(data, 12, len(data)):
            if tag == b"VP8X":
                if start != 20 or size != 10:
                    return False
                canvas = webp_u24(data, start + 4) + 1, webp_u24(data, start + 7) + 1
                if not valid_dimensions(*canvas):
                    return False
            elif tag in (b"VP8 ", b"VP8L"):
                dims = webp_image_dimensions(data, tag, start, size)
                if image or animated or not valid_dimensions(*dims) or (canvas is not None and dims != canvas):
                    return False
                image = True
            elif tag == b"ANIM":
                if canvas is None or size != 6 or image or animated:
                    return False
                animated = True
            elif tag == b"ANMF":
                if not animated or size < 16:
                    return False
                x, y = 2 * webp_u24(data, start), 2 * webp_u24(data, start + 3)
                width, height = webp_u24(data, start + 6) + 1, webp_u24(data, start + 9) + 1
                if x + width > canvas[0] or y + height > canvas[1]:
                    return False
                frames = [webp_image_dimensions(data, inner_tag, pos, amount)
                          for inner_tag, pos, amount in webp_chunks(data, start + 16, start + size)
                          if inner_tag in (b"VP8 ", b"VP8L")]
                if frames != [(width, height)]:
                    return False
                image = True
    except ValueError:
        return False
    return image


def photo_type(data):
    if data.startswith(PNG_SIGNATURE) and valid_png(data):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff") and valid_jpeg(data):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and valid_webp(data):
        return "image/webp"
    return None
