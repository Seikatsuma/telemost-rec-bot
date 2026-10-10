"""Запуск Chromium: фейковые «микрофон» (тишина) и «камера» (картинка из assets/camera.png).

Камера бота показывает участникам картинку «Запись встречи» — видно, что идёт запись; микрофон бота выключен.
BROWSER_SYSLIBS — папка с системными библиотеками для Chromium, если их нельзя поставить через sudo
(на обычном сервере не нужно: `playwright install --with-deps chromium`).
"""
import os
import pathlib
import shutil
import subprocess

import config  # noqa: F401 — загружает .env (BROWSER_SYSLIBS и др.) для любого, кто запускает браузер

# Окно и перерисовка бота: Телемост рисует плитки видео программно (SwiftShader) и крутит анимации —
# замер 17.09: браузер на экране подключения ~50 % ядра, в звонке больше. Звуку перерисовка не нужна
# (WebRTC и WebAudio работают в своих потоках), поэтому бот рисует раз в секунду и в маленьком окне.
BOT_VIEWPORT = {"width": 640, "height": 360}
BOT_PAGE_INIT = ("(() => { window.requestAnimationFrame = (cb) => setTimeout(() => cb(performance.now()), 1000); })();")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/139.0.0.0 Safari/537.36")


def browser_env() -> dict:
    env = dict(os.environ)
    syslibs = os.environ.get("BROWSER_SYSLIBS", "")
    if syslibs and os.path.isdir(syslibs):
        env["LD_LIBRARY_PATH"] = syslibs + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def low_priority(cmd: list[str]) -> list[str]:
    """Сжатие звука — с пониженным приоритетом, но не самым низким: замер 17.09 при занятых обоих ядрах —
    минутная запись 43.5 с при nice 19 против 5.3 с при nice 5 (аудио приходило через 2.5 мин после встречи).
    Приоритет можно поменять: FFMPEG_NICE в .env (0 — без понижения)."""
    prefix = []
    nice = os.environ.get("FFMPEG_NICE", "5")
    if shutil.which("nice") and nice != "0":
        prefix += ["nice", "-n", nice]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", "2", "-n", "7"]
    return prefix + cmd


def _silence(root: pathlib.Path) -> str:
    # Фейковый микрофон Chromium по умолчанию пищит — подменяем на тишину.
    path = root / "data" / "silence.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(low_priority(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                                     "anullsrc=r=48000:cl=mono", "-t", "2", str(path)]), check=True)
    return str(path)


def _camera(root: pathlib.Path) -> str | None:
    """Картинка для камеры бота в формате y4m (его понимает фейковая камера Chromium). 5 кадров/с — почти без CPU."""
    src = root / "assets" / "camera.png"
    path = root / "data" / "camera.y4m"
    if not src.exists():
        return None
    if not path.exists() or path.stat().st_mtime < src.stat().st_mtime:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(low_priority(["ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-i", str(src), "-t", "1",
                                     "-r", "5", "-pix_fmt", "yuv420p", str(path)]), check=True)
    return str(path)


def chromium_args(root: pathlib.Path) -> list[str]:
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={_silence(root)}",
        "--autoplay-policy=no-user-gesture-required",
        "--mute-audio",
    ]
    camera = _camera(root)
    if camera:
        args.append(f"--use-file-for-fake-video-capture={camera}")
    return args
