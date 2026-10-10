"""Упаковка готовых пакетов Opus в файл .ogg — без кодирования и перекодирования.

Зачем своё, а не PyOgg: сервер без sudo, системных libogg/libopus нет, а формат Ogg
простой и полностью описан в RFC 3533 (страницы) и RFC 7845 (Opus внутри Ogg).
Сто строк здесь дешевле, чем зависимость, которую нельзя поставить.

Что важно и неочевидно:
- **Длительность кадра считается из первого байта пакета (TOC)**, а не берётся «20 мс».
  Телемост может прислать 10 или 40 мс, и тогда счётчик позиций (granule) поедет,
  а проигрыватель покажет неверную длину.
- **Потерянный пакет нельзя просто пропустить** (RFC 7845): позиции считаются подряд,
  разрыв ломает перемотку. `write_packet(..., gap_samples=N)` добавляет пропуск к счётчику,
  а декодер на этом месте восстановит тишину.
- CRC у Ogg свой: полином 0x04c11db7, без отражения битов и без финального XOR —
  обычный zlib.crc32 сюда не подходит.
"""
from __future__ import annotations

import struct
from pathlib import Path

_CRC_POLY = 0x04C11DB7
_CRC_TABLE = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ _CRC_POLY) & 0xFFFFFFFF if _r & 0x80000000 else (_r << 1) & 0xFFFFFFFF
    _CRC_TABLE.append(_r)


def _crc32(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) & 0xFF) ^ b]
    return crc


# Длительность кадра Opus по конфигурации из байта TOC, в миллисекундах (RFC 6716, §3.1).
_CONFIG_MS = (
    10, 20, 40, 60,      # SILK NB
    10, 20, 40, 60,      # SILK MB
    10, 20, 40, 60,      # SILK WB
    10, 20,              # Hybrid SWB
    10, 20,              # Hybrid FB
    2.5, 5, 10, 20,      # CELT NB
    2.5, 5, 10, 20,      # CELT WB
    2.5, 5, 10, 20,      # CELT SWB
    2.5, 5, 10, 20,      # CELT FB
)


def packet_samples(packet: bytes, rate: int = 48000) -> int:
    """Сколько отсчётов декодируется из пакета. 0 — пакет битый или пустой."""
    if not packet:
        return 0
    toc = packet[0]
    ms = _CONFIG_MS[toc >> 3]
    code = toc & 0b11
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    else:                                  # произвольное число кадров
        if len(packet) < 2:
            return 0
        frames = packet[1] & 0b00111111
    return int(ms * rate / 1000) * frames


class OggOpusWriter:
    """Пишет поток Ogg Opus из готовых пакетов. Один экземпляр — один говорящий."""

    def __init__(self, path: Path | str, *, channels: int = 1, rate: int = 48000,
                 serial: int | None = None, pre_skip: int = 0):
        self.path = Path(path)
        self.channels = channels
        self.rate = rate
        self.serial = serial if serial is not None else (id(self) & 0x7FFFFFFF)
        self._f = self.path.open("wb")
        self._seq = 0
        self._granule = 0
        self._closed = False
        self._write_page(self._opus_head(pre_skip), granule=0, bos=True)
        self._write_page(self._opus_tags(), granule=0)

    # ── публичное ────────────────────────────────────────────────────
    def write_packet(self, packet: bytes, *, gap_samples: int = 0) -> None:
        """Добавить пакет. gap_samples — пропуск ПЕРЕД ним (потерянные пакеты)."""
        if self._closed:
            raise RuntimeError("писатель уже закрыт")
        self._granule += gap_samples + packet_samples(packet, self.rate)
        self._write_page(packet, granule=self._granule)

    def close(self) -> None:
        if self._closed:
            return
        self._write_page(b"", granule=self._granule, eos=True)
        self._f.close()
        self._closed = True

    @property
    def seconds(self) -> float:
        return self._granule / self.rate

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── внутреннее ───────────────────────────────────────────────────
    def _opus_head(self, pre_skip: int) -> bytes:
        return (b"OpusHead" + bytes([1, self.channels])
                + struct.pack("<H", pre_skip) + struct.pack("<I", self.rate)
                + struct.pack("<h", 0) + bytes([0]))

    def _opus_tags(self) -> bytes:
        vendor = b"telemost-rec-bot"
        return b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)

    def _write_page(self, packet: bytes, *, granule: int, bos: bool = False,
                    eos: bool = False) -> None:
        # Пакет длиннее 255*255 байт в одну страницу не влезает; речевые пакеты Opus
        # это сотни байт, поэтому такого не бывает — но проверяем, чтобы не молчать.
        segments = [255] * (len(packet) // 255) + [len(packet) % 255]
        if len(segments) > 255:
            raise ValueError("пакет слишком длинный для одной страницы Ogg")
        header_type = (0x02 if bos else 0) | (0x04 if eos else 0)
        head = (b"OggS" + bytes([0, header_type])
                + struct.pack("<q", granule)
                + struct.pack("<I", self.serial)
                + struct.pack("<I", self._seq)
                + struct.pack("<I", 0)               # место под контрольную сумму
                + bytes([len(segments)]) + bytes(segments))
        page = head + packet
        crc = _crc32(page)
        page = page[:22] + struct.pack("<I", crc) + page[26:]
        self._f.write(page)
        self._seq += 1
