"""Watch a fixed screen region for a Bambu Studio print completion status."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.json"
SUPPORTED_AUDIO_SUFFIXES = {".wav", ".mp3"}


def normalize_text(value: str) -> str:
    """Normalize OCR output while preserving useful word boundaries."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def phrase_score(text: str, phrase: str) -> float:
    """Return a 0-100 fuzzy score for a phrase within OCR text."""
    from rapidfuzz.fuzz import partial_ratio

    normalized_text = normalize_text(text)
    normalized_phrase = normalize_text(phrase)
    if not normalized_text or not normalized_phrase:
        return 0.0
    if normalized_phrase in normalized_text:
        return 100.0
    return float(partial_ratio(normalized_phrase, normalized_text))


@dataclass
class Detection:
    text: str
    confidence: float
    readable: bool


@dataclass
class PersistentState:
    state: str = "waiting"
    finished_streak: int = 0


class PrintStateMachine:
    """Arm on printing and alert once after repeated finished readings."""

    VALID_STATES = {"waiting", "printing", "finished"}

    def __init__(
        self,
        state: PersistentState,
        required_matches: int,
        alert: Callable[[], None],
        save: Callable[[PersistentState], None],
        logger: logging.Logger,
    ) -> None:
        self.data = state
        self.required_matches = max(1, required_matches)
        self.alert = alert
        self.save = save
        self.logger = logger

    def observe(self, result: str) -> None:
        if result == "unreadable":
            if self.data.finished_streak:
                self.data.finished_streak = 0
                self.save(self.data)
            return

        if result == "printing":
            changed = self.data.state != "printing" or self.data.finished_streak != 0
            if self.data.state != "printing":
                self.logger.info("State changed: %s -> printing", self.data.state)
            self.data.state = "printing"
            self.data.finished_streak = 0
            if changed:
                self.save(self.data)
            return

        if result == "finished" and self.data.state == "printing":
            self.data.finished_streak += 1
            self.logger.info(
                "Finished match %d/%d",
                self.data.finished_streak,
                self.required_matches,
            )
            if self.data.finished_streak >= self.required_matches:
                self.alert()
                self.data.state = "finished"
                self.data.finished_streak = 0
                self.logger.info("State changed: printing -> finished; alert played")
            self.save(self.data)
            return

        # Readable unrelated text breaks a completion streak but does not disarm.
        if self.data.finished_streak:
            self.data.finished_streak = 0
            self.save(self.data)


def load_json(path: Path) -> dict[str, Any]:
    try:
        # utf-8-sig also accepts the BOM emitted by Windows PowerShell 5.1.
        with path.open("r", encoding="utf-8-sig") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def resolve_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def audio_path_from_config(config: dict[str, Any], config_path: Path) -> Path:
    """Resolve the preferred audio_path key or the legacy wav_path key."""
    value = str(config.get("audio_path", config.get("wav_path", ""))).strip()
    if not value:
        raise ValueError("audio_path is empty; set it to an absolute .wav or .mp3 file path")
    return resolve_path(value, config_path)


def validate_audio_file(path: Path) -> None:
    if path.suffix.casefold() not in SUPPORTED_AUDIO_SUFFIXES:
        raise ValueError(f"Alert sound must be a .wav or .mp3 file: {path}")
    if not path.is_file():
        raise ValueError(f"Alert sound file does not exist: {path}")


def validate_config(config: dict[str, Any], config_path: Path, require_region: bool = True) -> None:
    required = [
        "screen_number",
        "capture_region",
        "poll_interval_seconds",
        "active_phrase",
        "completion_phrase",
        "similarity_threshold",
        "required_completion_matches",
        "log_path",
        "state_path",
    ]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError("Missing configuration keys: " + ", ".join(missing))
    if int(config["screen_number"]) < 1:
        raise ValueError("screen_number must be 1 or greater")
    if float(config["poll_interval_seconds"]) <= 0:
        raise ValueError("poll_interval_seconds must be greater than zero")
    threshold = float(config["similarity_threshold"])
    if not 0 <= threshold <= 100:
        raise ValueError("similarity_threshold must be between 0 and 100")
    if int(config["required_completion_matches"]) < 1:
        raise ValueError("required_completion_matches must be 1 or greater")
    if require_region:
        region = config["capture_region"]
        if not isinstance(region, dict) or any(int(region.get(k, 0)) <= 0 for k in ("width", "height")):
            raise ValueError("capture_region is not calibrated; run the calibrate command")
        if any(k not in region for k in ("left", "top")):
            raise ValueError("capture_region must contain left, top, width, and height")
    validate_audio_file(audio_path_from_config(config, config_path))


def setup_logging(config: dict[str, Any], config_path: Path, console: bool) -> logging.Logger:
    log_path = resolve_path(str(config["log_path"]), config_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bambu_watcher")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger


def load_state(path: Path, logger: logging.Logger) -> PersistentState:
    if not path.exists():
        return PersistentState()
    try:
        raw = load_json(path)
        state = PersistentState(str(raw.get("state", "waiting")), int(raw.get("finished_streak", 0)))
        if state.state not in PrintStateMachine.VALID_STATES or state.finished_streak < 0:
            raise ValueError("invalid state values")
        return state
    except (ValueError, TypeError) as exc:
        logger.warning("Ignoring invalid state file %s: %s", path, exc)
        return PersistentState()


def save_state(path: Path, state: PersistentState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(asdict(state), handle, indent=2)
        handle.write("\n")
    temporary.replace(path)


def discover_tesseract(config: dict[str, Any]) -> None:
    import pytesseract

    configured = str(config.get("tesseract_path", "")).strip()
    candidates = [
        Path(configured) if configured else None,
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            break
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        raise RuntimeError(
            "Tesseract OCR was not found. Install it or set tesseract_path in config.json."
        ) from exc


def monitor_definition(screen_number: int) -> dict[str, int]:
    from mss import MSS

    with MSS() as capture:
        if screen_number >= len(capture.monitors):
            raise ValueError(
                f"screen_number {screen_number} is unavailable; found {len(capture.monitors) - 1} monitor(s)"
            )
        return dict(capture.monitors[screen_number])


def capture_region(config: dict[str, Any]):
    import cv2
    import numpy as np
    from mss import MSS

    monitor = monitor_definition(int(config["screen_number"]))
    region = config["capture_region"]
    absolute = {
        "left": monitor["left"] + int(region["left"]),
        "top": monitor["top"] + int(region["top"]),
        "width": int(region["width"]),
        "height": int(region["height"]),
    }
    with MSS() as capture:
        frame = np.asarray(capture.grab(absolute))
    return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)


def preprocess(frame):
    import cv2

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)


def recognize(frame, config: dict[str, Any]) -> Detection:
    import cv2
    import numpy as np
    import pytesseract
    from pytesseract import Output

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if float(np.std(gray)) < float(config.get("minimum_image_stddev", 3.0)):
        return Detection("", 0.0, False)
    processed = preprocess(frame)
    data = pytesseract.image_to_data(processed, output_type=Output.DICT, config="--psm 6")
    words: list[str] = []
    confidences: list[float] = []
    for word, confidence in zip(data["text"], data["conf"]):
        word = str(word).strip()
        try:
            numeric_confidence = float(confidence)
        except (TypeError, ValueError):
            continue
        if word and numeric_confidence >= 0:
            words.append(word)
            confidences.append(numeric_confidence)
    if not words:
        return Detection("", 0.0, False)
    average = sum(confidences) / len(confidences)
    minimum = float(config.get("minimum_ocr_confidence", 25.0))
    return Detection(" ".join(words), average, average >= minimum)


def classify(detection: Detection, config: dict[str, Any]) -> tuple[str, float, float]:
    if not detection.readable:
        return "unreadable", 0.0, 0.0
    active_score = phrase_score(detection.text, str(config["active_phrase"]))
    finished_score = phrase_score(detection.text, str(config["completion_phrase"]))
    threshold = float(config["similarity_threshold"])
    if finished_score >= threshold and finished_score >= active_score:
        return "finished", active_score, finished_score
    if active_score >= threshold:
        return "printing", active_score, finished_score
    return "other", active_score, finished_score


def play_audio(path: Path) -> None:
    """Play WAV through winsound or MP3 through Windows Media Control Interface."""
    if path.suffix.casefold() == ".wav":
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return

    import ctypes
    import uuid

    alias = "bambu_alert_" + uuid.uuid4().hex
    winmm = ctypes.windll.winmm

    def mci(command: str) -> None:
        result = winmm.mciSendStringW(command, None, 0, None)
        if result:
            message = ctypes.create_unicode_buffer(256)
            winmm.mciGetErrorStringW(result, message, len(message))
            raise RuntimeError(f"Windows could not play {path.name}: {message.value or result}")

    opened = False
    try:
        mci(f'open "{path}" type mpegvideo alias {alias}')
        opened = True
        mci(f"play {alias} wait")
    finally:
        if opened:
            winmm.mciSendStringW(f"close {alias}", None, 0, None)


@contextmanager
def minimized_terminal():
    """Minimize the visible terminal around screen capture and UI display."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]

    # Windows Terminal uses a pseudoconsole, so GetConsoleWindow may not be the
    # visible window. The foreground HWND is the terminal when a CLI command
    # starts and is therefore the reliable first choice.
    window = user32.GetForegroundWindow() or kernel32.GetConsoleWindow()
    if window:
        user32.ShowWindow(window, 6)  # SW_MINIMIZE
        time.sleep(1.0)
    try:
        yield
    finally:
        if window:
            user32.ShowWindow(window, 9)  # SW_RESTORE


def calibrate(config: dict[str, Any], config_path: Path) -> None:
    import cv2
    import numpy as np
    from mss import MSS

    screen_number = int(config["screen_number"])
    monitor = monitor_definition(screen_number)
    with minimized_terminal():
        with MSS() as capture:
            screenshot = np.asarray(capture.grab(monitor))
        frame = cv2.cvtColor(screenshot, cv2.COLOR_BGRA2BGR)
        height, width = frame.shape[:2]
        scale = min(1.0, 1600 / width, 900 / height)
        preview = frame if scale == 1.0 else cv2.resize(frame, None, fx=scale, fy=scale)
        print("Draw a box around the Bambu Studio status text, then press ENTER or SPACE.")
        x, y, w, h = cv2.selectROI("Bambu Watcher Calibration", preview, False, False)
        cv2.destroyAllWindows()
    if w == 0 or h == 0:
        raise RuntimeError("Calibration cancelled; configuration was not changed")
    config["capture_region"] = {
        "left": round(x / scale),
        "top": round(y / scale),
        "width": round(w / scale),
        "height": round(h / scale),
    }
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    print(f"Saved capture region to {config_path}")


def diagnostic(config: dict[str, Any], config_path: Path, show: bool) -> int:
    import cv2

    validate_config(config, config_path)
    discover_tesseract(config)
    with minimized_terminal():
        frame = capture_region(config)
        result = recognize(frame, config)
        category, active_score, finished_score = classify(result, config)
        output_path = resolve_path(str(config.get("diagnostic_image_path", "diagnostic-capture.png")), config_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_path), frame)
        if show:
            cv2.imshow("Bambu Watcher Diagnostic (press any key to close)", frame)
            cv2.waitKey(0)
            cv2.destroyAllWindows()
    print(f"OCR text: {result.text!r}")
    print(f"OCR confidence: {result.confidence:.1f}")
    print(f"Classification: {category}")
    print(f"Printing score: {active_score:.1f}; Finished score: {finished_score:.1f}")
    print(f"Capture saved to: {output_path}")
    return 0


def watch(config: dict[str, Any], config_path: Path, once: bool = False) -> int:
    validate_config(config, config_path)
    discover_tesseract(config)
    # pythonw.exe has no stdout; regular Python should remain observable.
    logger = setup_logging(config, config_path, console=sys.stdout is not None)
    state_path = resolve_path(str(config["state_path"]), config_path)
    audio_path = audio_path_from_config(config, config_path)
    machine = PrintStateMachine(
        load_state(state_path, logger),
        int(config["required_completion_matches"]),
        lambda: play_audio(audio_path),
        lambda state: save_state(state_path, state),
        logger,
    )
    logger.info("Watcher started in state %s", machine.data.state)
    interval = float(config["poll_interval_seconds"])
    while True:
        started = time.monotonic()
        try:
            detection = recognize(capture_region(config), config)
            category, active_score, finished_score = classify(detection, config)
            logger.info(
                "OCR=%r confidence=%.1f result=%s scores(printing=%.1f finished=%.1f)",
                detection.text,
                detection.confidence,
                category,
                active_score,
                finished_score,
            )
            machine.observe(category)
        except Exception:
            logger.exception("Capture/OCR cycle failed; state left unchanged")
        if once:
            return 0
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to config JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("calibrate", help="select the status area on screen")
    diagnostic_parser = subparsers.add_parser("diagnostic", help="capture and OCR once")
    diagnostic_parser.add_argument("--show", action="store_true", help="display the captured region")
    watch_parser = subparsers.add_parser("watch", help="run continuously")
    watch_parser.add_argument("--once", action="store_true", help="run one capture cycle and exit")
    subparsers.add_parser("validate", help="validate configuration and dependencies")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = args.config.resolve()
    config: dict[str, Any] | None = None
    try:
        config = load_json(config_path)
        if args.command == "calibrate":
            validate_config(config, config_path, require_region=False)
            calibrate(config, config_path)
            return 0
        if args.command == "diagnostic":
            return diagnostic(config, config_path, args.show)
        if args.command == "watch":
            return watch(config, config_path, args.once)
        validate_config(config, config_path)
        discover_tesseract(config)
        monitor_definition(int(config["screen_number"]))
        print("Configuration, Tesseract, monitor, and alert sound are valid.")
        return 0
    except Exception as exc:
        # Startup errors must remain visible when launched by pythonw.exe, which
        # has no console. Fall back to stderr if logging itself cannot start.
        if config is not None:
            try:
                error_logger = setup_logging(
                    {"log_path": config.get("log_path", "logs/bambu-watcher.log")},
                    config_path,
                    console=sys.stderr is not None,
                )
                error_logger.exception("Command %s failed: %s", args.command, exc)
            except Exception:
                pass
        if sys.stderr is not None:
            print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
