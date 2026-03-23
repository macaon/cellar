"""Disc image handling for DOS game import.

Supports ISO 9660 images, CUE/BIN pairs (with CDDA conversion), and
floppy images (.ima/.img/.vfd).  All functions are pure Python with no
GTK dependency.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DISC_IMAGE_EXTS = {".iso", ".cue", ".img", ".ima", ".vfd"}

# Standard floppy image sizes in bytes.
_FLOPPY_SIZES = {
    163_840,   # 5.25" 160 KB single-sided
    184_320,   # 5.25" 180 KB single-sided
    327_680,   # 5.25" 320 KB double-sided
    368_640,   # 5.25" 360 KB double-sided
    737_280,   # 3.5"  720 KB DD
    1_228_800, # 5.25" 1.2 MB HD
    1_474_560, # 3.5"  1.44 MB HD
    2_949_120, # 3.5"  2.88 MB ED
}

# CD-ROM sector size (raw mode).
_SECTOR_SIZE = 2352


# Disc ordering regex patterns (case-insensitive).
_DISC_NUM_RE = re.compile(
    r"(?:disc|disk|cd)\s*[-_]?\s*(\d+)", re.IGNORECASE,
)
_DISC_ALPHA_RE = re.compile(
    r"(?:disc|disk|cd)\s*[-_]?\s*([a-z])\b", re.IGNORECASE,
)
_DISC_ROMAN_RE = re.compile(
    r"(?:disc|disk|cd)\s*[-_]?\s*(i{1,3}|iv|v|vi{0,3})\b", re.IGNORECASE,
)
_DISC_SEMANTIC_RE = re.compile(
    r"^(install|program|setup)", re.IGNORECASE,
)
_ROMAN_MAP = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8}

# CUE sheet line patterns.
_CUE_FILE_RE = re.compile(r'^FILE\s+"?([^"]+)"?\s+(\w+)', re.IGNORECASE)
_CUE_TRACK_RE = re.compile(r"^\s*TRACK\s+(\d+)\s+(.+)", re.IGNORECASE)
_CUE_INDEX_RE = re.compile(r"^\s*INDEX\s+(\d+)\s+(\d+:\d+:\d+)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CueTrack:
    """A single track from a CUE sheet."""

    number: int
    track_type: str  # e.g. "MODE1/2352", "AUDIO"
    filename: str    # BIN filename referenced by FILE directive
    is_audio: bool
    index0_offset: int = -1  # byte offset of INDEX 00 in BIN (-1 if absent)
    index1_offset: int = -1  # byte offset of INDEX 01 in BIN


@dataclass(frozen=True, slots=True)
class CueSheet:
    """Parsed CUE sheet with track and file information."""

    cue_path: Path
    tracks: tuple[CueTrack, ...]
    bin_files: tuple[Path, ...]  # resolved absolute paths to BIN files
    has_audio: bool              # True if any track is AUDIO


@dataclass(slots=True)
class DiscSet:
    """Grouped and ordered collection of disc images."""

    isos: list[Path] = field(default_factory=list)
    cue_bins: list[CueSheet] = field(default_factory=list)
    floppies: list[Path] = field(default_factory=list)
    unknown: list[Path] = field(default_factory=list)


DiscType = Literal["iso", "cue", "floppy", "unknown"]


# ---------------------------------------------------------------------------
# Classification & grouping
# ---------------------------------------------------------------------------


def classify_disc_image(path: Path) -> DiscType:
    """Classify a file as an ISO, CUE, floppy image, or unknown."""
    suffix = path.suffix.lower()
    if suffix == ".iso":
        return "iso"
    if suffix == ".cue":
        return "cue"
    if suffix in {".img", ".ima", ".vfd"}:
        return "floppy"
    return "unknown"


def validate_floppy_size(path: Path) -> bool:
    """Return True if *path* has a standard floppy image size."""
    try:
        return path.stat().st_size in _FLOPPY_SIZES
    except OSError:
        return False


def group_disc_files(paths: list[Path]) -> DiscSet:
    """Classify and group a list of paths into a :class:`DiscSet`.

    CUE files are parsed and matched with their BIN files.  BIN files that
    are referenced by a CUE are consumed and not placed in ``unknown``.
    """
    result = DiscSet()
    consumed_bins: set[Path] = set()

    # First pass: classify and collect CUE sheets (which consume BINs).
    cue_paths: list[Path] = []
    non_cue: list[Path] = []

    for p in paths:
        dtype = classify_disc_image(p)
        if dtype == "cue":
            cue_paths.append(p)
        else:
            non_cue.append(p)

    for cue_path in cue_paths:
        try:
            sheet = parse_cue(cue_path)
            problems = validate_cue(sheet)
            if problems:
                for w in problems:
                    log.warning("CUE %s: %s", cue_path.name, w)
                # Missing files are fatal — can't mount an incomplete disc
                if any(w.startswith("Missing file:") for w in problems):
                    result.unknown.append(cue_path)
                    continue
            result.cue_bins.append(sheet)
            consumed_bins.update(sheet.bin_files)
        except (OSError, ValueError) as exc:
            log.warning("Failed to parse CUE %s: %s", cue_path, exc)
            result.unknown.append(cue_path)

    # Second pass: classify remaining files.
    for p in non_cue:
        if p in consumed_bins:
            continue  # already consumed by a CUE
        dtype = classify_disc_image(p)
        if dtype == "iso":
            result.isos.append(p)
        elif dtype == "floppy":
            result.floppies.append(p)
        else:
            # Standalone .bin files without a matching CUE
            if p.suffix.lower() == ".bin":
                log.info("Standalone BIN without CUE: %s", p)
            result.unknown.append(p)

    # Auto-order each group.
    result.isos = detect_disc_order(result.isos)
    result.cue_bins = [
        result.cue_bins[i]
        for i in _order_indices([cs.cue_path for cs in result.cue_bins])
    ] if len(result.cue_bins) > 1 else result.cue_bins
    result.floppies = detect_disc_order(result.floppies)

    return result


# ---------------------------------------------------------------------------
# CUE sheet parsing
# ---------------------------------------------------------------------------


def parse_cue(path: Path) -> CueSheet:
    """Parse a CUE sheet and return a :class:`CueSheet`.

    Resolves BIN file references relative to the CUE file's directory.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    cue_dir = path.parent

    current_file: str = ""
    tracks: list[CueTrack] = []
    bin_files_seen: dict[str, Path] = {}
    current_track_num = 0
    current_track_type = ""
    current_index0 = -1
    current_index1 = -1

    def _flush_track() -> None:
        if current_track_num > 0:
            is_audio = current_track_type.upper() == "AUDIO"
            tracks.append(CueTrack(
                number=current_track_num,
                track_type=current_track_type,
                filename=current_file,
                is_audio=is_audio,
                index0_offset=current_index0,
                index1_offset=current_index1,
            ))

    for raw_line in text.splitlines():
        line = raw_line.strip()

        file_m = _CUE_FILE_RE.match(line)
        if file_m:
            _flush_track()
            current_track_num = 0
            current_file = file_m.group(1)
            if current_file not in bin_files_seen:
                resolved = (cue_dir / current_file).resolve()
                bin_files_seen[current_file] = resolved
            continue

        track_m = _CUE_TRACK_RE.match(line)
        if track_m:
            _flush_track()
            current_track_num = int(track_m.group(1))
            current_track_type = track_m.group(2).strip()
            current_index0 = -1
            current_index1 = -1
            continue

        index_m = _CUE_INDEX_RE.match(line)
        if index_m:
            idx_num = int(index_m.group(1))
            offset = _msf_to_bytes(index_m.group(2))
            if idx_num == 0:
                current_index0 = offset
            elif idx_num == 1:
                current_index1 = offset

    _flush_track()

    if not tracks:
        raise ValueError(f"No tracks found in CUE sheet: {path}")

    has_audio = any(t.is_audio for t in tracks)

    return CueSheet(
        cue_path=path,
        tracks=tuple(tracks),
        bin_files=tuple(bin_files_seen.values()),
        has_audio=has_audio,
    )


def validate_cue(sheet: CueSheet) -> list[str]:
    """Check a parsed CUE sheet for problems.

    Returns a list of human-readable warning strings.  An empty list means
    the CUE is valid and all referenced files exist.
    """
    warnings: list[str] = []
    for ref_path in sheet.bin_files:
        if not ref_path.is_file():
            warnings.append(f"Missing file: {ref_path.name}")
    if not sheet.tracks:
        warnings.append("CUE sheet contains no tracks")
    data_tracks = [t for t in sheet.tracks if not t.is_audio]
    if not data_tracks:
        warnings.append("CUE sheet has no data track (audio-only disc)")
    return warnings


def _msf_to_bytes(msf: str) -> int:
    """Convert MM:SS:FF (minutes:seconds:frames) to byte offset.

    Each frame is one sector (2352 bytes).  75 frames per second.
    """
    parts = msf.split(":")
    if len(parts) != 3:
        return 0
    minutes, seconds, frames = int(parts[0]), int(parts[1]), int(parts[2])
    total_frames = minutes * 60 * 75 + seconds * 75 + frames
    return total_frames * _SECTOR_SIZE


# ---------------------------------------------------------------------------
# ISO scanning
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Disc ordering
# ---------------------------------------------------------------------------


def detect_disc_order(paths: list[Path]) -> list[Path]:
    """Sort disc images into the correct mounting order.

    Detects numbering from filenames (numeric, alphabetic, roman numeral)
    and semantic prefixes (Install/Program first).  Falls back to
    alphabetical sort.
    """
    if len(paths) <= 1:
        return list(paths)

    indices = _order_indices(paths)
    return [paths[i] for i in indices]


def _order_indices(paths: list[Path]) -> list[int]:
    """Return indices that sort *paths* into disc order."""
    if len(paths) <= 1:
        return list(range(len(paths)))

    # Try each detection strategy in order.
    for strategy in (_extract_numeric, _extract_alpha, _extract_roman):
        keys = [strategy(p) for p in paths]
        if all(k is not None for k in keys):
            if len(set(keys)) == len(keys):
                return sorted(range(len(paths)), key=lambda i: keys[i])
            log.debug("Disc order strategy %s produced duplicate keys, skipping",
                       strategy.__name__)

    # Semantic: "install"/"program"/"setup" prefixed discs sort first.
    semantic = [(0 if _DISC_SEMANTIC_RE.search(p.stem) else 1, p.name.lower())
                for p in paths]
    if any(s[0] == 0 for s in semantic):
        return sorted(range(len(paths)), key=lambda i: semantic[i])

    # Fallback: alphabetical.
    return sorted(range(len(paths)), key=lambda i: paths[i].name.lower())


def _extract_numeric(path: Path) -> int | None:
    m = _DISC_NUM_RE.search(path.stem)
    return int(m.group(1)) if m else None


def _extract_alpha(path: Path) -> int | None:
    m = _DISC_ALPHA_RE.search(path.stem)
    return ord(m.group(1).lower()) - ord("a") if m else None


def _extract_roman(path: Path) -> int | None:
    m = _DISC_ROMAN_RE.search(path.stem)
    if m:
        return _ROMAN_MAP.get(m.group(1).lower())
    return None


# ---------------------------------------------------------------------------
# CDDA audio track conversion
# ---------------------------------------------------------------------------


def has_cdda_tools() -> bool:
    """Return True if oggenc or ffmpeg is available for CDDA conversion."""
    return bool(shutil.which("oggenc") or shutil.which("ffmpeg"))


def convert_cdda_tracks(
    cue_path: Path,
    dest_dir: Path,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
) -> Path:
    """Convert audio tracks from a CUE/BIN to .ogg files.

    Copies the data track(s) and converts audio tracks to Ogg Vorbis.
    Writes a new CUE file in *dest_dir* referencing the converted tracks.

    Returns the path to the new CUE file.
    """
    sheet = parse_cue(cue_path)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not sheet.has_audio:
        # No audio tracks — just copy files as-is.
        for bin_path in sheet.bin_files:
            if bin_path.is_file():
                shutil.copy2(bin_path, dest_dir / bin_path.name)
        new_cue = dest_dir / cue_path.name
        shutil.copy2(cue_path, new_cue)
        return new_cue

    # Audio tracks already converted (CUE references .ogg/.mp3/.wav/.flac
    # instead of raw BINARY) — copy everything as-is.
    _COMPRESSED_EXTS = {".ogg", ".mp3", ".wav", ".flac"}
    audio_already_converted = all(
        Path(t.filename).suffix.lower() in _COMPRESSED_EXTS
        for t in sheet.tracks if t.is_audio
    )
    if audio_already_converted:
        log.info("CUE audio tracks already converted — copying as-is")
        for ref_path in sheet.bin_files:
            if ref_path.is_file():
                shutil.copy2(ref_path, dest_dir / ref_path.name)
        new_cue = dest_dir / cue_path.name
        shutil.copy2(cue_path, new_cue)
        return new_cue

    # We need the BIN file to extract tracks from.
    if not sheet.bin_files:
        raise ValueError("CUE references no BIN files")

    bin_path = sheet.bin_files[0]
    if not bin_path.is_file():
        raise FileNotFoundError(f"BIN file not found: {bin_path}")

    bin_size = bin_path.stat().st_size
    audio_tracks = [t for t in sheet.tracks if t.is_audio]
    total_audio = len(audio_tracks)
    encoder = shutil.which("oggenc") or shutil.which("ffmpeg")

    if not encoder:
        log.warning("No CDDA encoder available — copying BIN as-is")
        shutil.copy2(bin_path, dest_dir / bin_path.name)
        new_cue = dest_dir / cue_path.name
        shutil.copy2(cue_path, new_cue)
        return new_cue

    use_oggenc = "oggenc" in Path(encoder).name

    # Build a map of track start/end byte offsets.
    track_offsets: list[tuple[CueTrack, int, int]] = []
    for i, track in enumerate(sheet.tracks):
        start = track.index1_offset if track.index1_offset >= 0 else 0
        # End is the start of the next track, or end of file.
        if i + 1 < len(sheet.tracks):
            next_track = sheet.tracks[i + 1]
            end = next_track.index0_offset if next_track.index0_offset >= 0 else (
                next_track.index1_offset if next_track.index1_offset >= 0 else bin_size
            )
        else:
            end = bin_size
        track_offsets.append((track, start, end))

    # New CUE lines.
    cue_lines: list[str] = []
    converted_count = 0

    with bin_path.open("rb") as bin_fh:
        for track, start, end in track_offsets:
            if track.is_audio:
                # Extract and convert audio track.
                ogg_name = f"Track {track.number:02d}.ogg"
                ogg_path = dest_dir / ogg_name
                chunk_size = end - start

                bin_fh.seek(start)
                raw_pcm = bin_fh.read(chunk_size)

                _encode_pcm_to_ogg(raw_pcm, ogg_path, encoder, use_oggenc)

                cue_lines.append(f'FILE "{ogg_name}" OGG')
                cue_lines.append(f"  TRACK {track.number:02d} AUDIO")
                cue_lines.append("    INDEX 01 00:00:00")

                converted_count += 1
                if progress_cb:
                    progress_cb(converted_count, total_audio)
            else:
                # Data track — copy the relevant portion of the BIN.
                data_name = f"{cue_path.stem}_data.bin"
                data_path = dest_dir / data_name

                bin_fh.seek(start)
                with data_path.open("wb") as out:
                    remaining = end - start
                    while remaining > 0:
                        chunk = bin_fh.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)

                cue_lines.append(f'FILE "{data_name}" BINARY')
                cue_lines.append(f"  TRACK {track.number:02d} {track.track_type}")
                if track.index0_offset >= 0:
                    cue_lines.append("    INDEX 00 00:00:00")
                cue_lines.append("    INDEX 01 00:00:00")

    new_cue = dest_dir / cue_path.name
    new_cue.write_text("\n".join(cue_lines) + "\n", encoding="utf-8")
    return new_cue


def _encode_pcm_to_ogg(
    raw_pcm: bytes,
    ogg_path: Path,
    encoder: str,
    use_oggenc: bool,
) -> None:
    """Encode raw 16-bit 44.1 kHz stereo PCM to Ogg Vorbis."""
    if use_oggenc:
        cmd = [
            encoder,
            "--raw",
            "--raw-rate=44100",
            "--raw-chan=2",
            "--raw-bits=16",
            "--raw-endianness=0",  # little-endian
            "-q", "6",
            "-o", str(ogg_path),
            "-",
        ]
    else:
        # ffmpeg fallback
        cmd = [
            encoder,
            "-f", "s16le",
            "-ar", "44100",
            "-ac", "2",
            "-i", "pipe:0",
            "-c:a", "libvorbis",
            "-q:a", "6",
            "-y",
            str(ogg_path),
        ]

    proc = subprocess.run(
        cmd,
        input=raw_pcm,
        capture_output=True,
        timeout=300,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Audio encoding failed: {stderr}")
