"""Optional local person-presence filter for backend download workflows.

It deliberately answers only whether a sampled image or video frame contains
the COCO ``person`` class.  It never compares faces or tries to identify the
profile owner.
"""

import asyncio
import hashlib
import logging
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersonFilterResult:
    decision: str
    reason: str
    model_version: str
    sample_count: int


class PersonPresenceFilter:
    """Keep media only when a person is a visually dominant subject."""

    MODEL_NAME = "yolov8n.onnx"
    # Bump this whenever the decision pipeline changes so old cached results
    # are not reused after an application update.
    MODEL_VERSION = "yolov8n-person-v5-dominant-subject-duration50s"
    DURATION_FILTER_VERSION = "video-duration-filter-v2-50s"
    DEFAULT_MAX_VIDEO_SECONDS = 50.0
    DEFAULT_CONFIDENCE = 0.5
    # A small detected arm, hand, background passer-by, or a cook beside food
    # is not the subject the gallery is meant to retain.  YOLO boxes are
    # measured against the padded 640x640 inference canvas, so 25% is a
    # deliberately strict, dominant-subject threshold.
    DEFAULT_MIN_PERSON_AREA = 0.25
    MODEL_URL = "https://huggingface.co/Kalray/yolov8/resolve/main/yolov8n.onnx?download=true"
    MODEL_SHA256 = "65158dad735be799c2466fa15e260c09558080bd530b42a8d0c3d1b419afd8b5"

    def __init__(
        self,
        *,
        confidence: float = DEFAULT_CONFIDENCE,
        min_person_area: float = DEFAULT_MIN_PERSON_AREA,
        model_name: str = MODEL_NAME,
        model_dir: Optional[Path] = None,
        infer: Optional[Callable[[Any], Any]] = None,
        image_loader: Optional[Callable[[Path], Any]] = None,
        video_sampler: Optional[Callable[[Path, int], Iterable[Any]]] = None,
        video_duration: Optional[Callable[[Path], Optional[float]]] = None,
    ) -> None:
        self.confidence = max(0.05, min(float(confidence), 0.99))
        self.min_person_area = max(0.001, min(float(min_person_area), 0.95))
        self.model_name = str(model_name or self.MODEL_NAME)
        self.model_dir = model_dir or Path(__file__).resolve().parents[1] / "data" / "models"
        self._infer_override = infer
        self._image_loader_override = image_loader
        self._video_sampler_override = video_sampler
        self._video_duration_override = video_duration
        self._model: Any = None
        self._model_lock = threading.Lock()

    @staticmethod
    def dependencies_available() -> bool:
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
            import onnxruntime  # noqa: F401
        except ImportError:
            return False
        return True

    async def inspect(
        self,
        path: Path,
        media_type: str,
        *,
        max_samples: int = 8,
        max_video_seconds: Optional[float] = DEFAULT_MAX_VIDEO_SECONDS,
    ) -> PersonFilterResult:
        """Inspect one temporary media file without blocking the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._inspect_sync,
            Path(path),
            str(media_type),
            max_samples,
            max_video_seconds,
        )

    def _inspect_sync(
        self,
        path: Path,
        media_type: str,
        max_samples: int,
        max_video_seconds: Optional[float],
    ) -> PersonFilterResult:
        try:
            sample_limit = max(1, int(max_samples))
            if media_type == "image":
                frames = [self._load_image(path)]
            else:
                duration, frames = self._sample_video_with_duration(
                    path, sample_limit, max_video_seconds
                )
                if (
                    max_video_seconds is not None
                    and max_video_seconds > 0
                    and duration is None
                ):
                    return PersonFilterResult(
                        "error",
                        "无法读取视频时长，未执行保存",
                        self.DURATION_FILTER_VERSION,
                        0,
                    )
                if (
                    max_video_seconds is not None
                    and max_video_seconds > 0
                    and duration is not None
                    and duration > float(max_video_seconds)
                ):
                    return PersonFilterResult(
                        "filtered",
                        "视频时长超过{0:g}秒，已过滤".format(float(max_video_seconds)),
                        self.DURATION_FILTER_VERSION,
                        0,
                    )
            if not frames:
                return PersonFilterResult("error", "视频无法提取可检测画面", self.MODEL_VERSION, 0)

            for frame in frames:
                if self._contains_person(frame):
                    return PersonFilterResult(
                        "keep", "检测到画面中出现人物", self.MODEL_VERSION, len(frames)
                    )
            return PersonFilterResult("filtered", "未检测到人物", self.MODEL_VERSION, len(frames))
        except FileNotFoundError:
            return PersonFilterResult("error", "媒体临时文件不存在", self.MODEL_VERSION, 0)
        except Exception as exc:
            logger.warning("Person-presence detection failed for %s: %s", path, exc)
            return PersonFilterResult(
                "error", "人像检测失败：{0}".format(self._friendly_error(exc)), self.MODEL_VERSION, 0
            )

    @staticmethod
    def _friendly_error(error: Exception) -> str:
        message = str(error).strip()
        if not message:
            return "检测器返回未知错误"
        if "No module named" in message:
            return "缺少检测依赖"
        if "cannot open" in message or "Could not open" in message:
            return "无法打开媒体"
        if not any("\u4e00" <= char <= "\u9fff" for char in message):
            if "DLL" in message or "load" in message.lower():
                return "检测组件加载失败，请重启应用后重试"
            return "检测器处理异常，请重试"
        return message[:240]

    def _load_image(self, path: Path) -> Any:
        if self._image_loader_override:
            return self._image_loader_override(path)
        import cv2
        import numpy as np

        # OpenCV's imread does not reliably accept Windows paths containing
        # Chinese characters. Reading bytes through NumPy keeps the path
        # handling in Python, then lets OpenCV decode the image data.
        encoded = np.fromfile(str(path), dtype=np.uint8)
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("图片无法解码")
        return frame

    def _sample_video_with_duration(
        self, path: Path, max_samples: int, max_video_seconds: Optional[float]
    ) -> Tuple[Optional[float], list]:
        if self._video_sampler_override:
            duration = self._video_duration_override(path) if self._video_duration_override else None
            if self._too_long(duration, max_video_seconds):
                return duration, []
            return duration, list(self._video_sampler_override(path, max_samples))

        import cv2

        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            raise ValueError("视频无法打开")
        try:
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if frame_count <= 0:
                raise ValueError("视频没有可读取的画面")
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
            duration = frame_count / fps if fps > 0 else None
            if self._too_long(duration, max_video_seconds):
                return duration, []
            sample_count = min(max_samples, frame_count)
            if sample_count == 1:
                indices = [0]
            else:
                indices = sorted(
                    set(
                        round(index * (frame_count - 1) / (sample_count - 1))
                        for index in range(sample_count)
                    )
                )
            frames = []
            for frame_index in indices:
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ok, frame = capture.read()
                if ok and frame is not None:
                    frames.append(frame)
            return duration, frames
        finally:
            capture.release()

    @staticmethod
    def _too_long(duration: Optional[float], maximum: Optional[float]) -> bool:
        return bool(duration is not None and maximum is not None and maximum > 0 and duration > maximum)

    def _contains_person(self, frame: Any) -> bool:
        if self._infer_override:
            result = self._infer_override(frame)
            if isinstance(result, bool):
                return result
            if isinstance(result, (list, tuple, set)):
                return any(value is True or str(value).lower() == "person" for value in result)
            return bool(result)

        session = self._get_model()
        import cv2
        import numpy as np

        height, width = frame.shape[:2]
        scale = min(640.0 / width, 640.0 / height)
        resized = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))))
        canvas = np.full((640, 640, 3), 114, dtype=np.uint8)
        offset_x = (640 - resized.shape[1]) // 2
        offset_y = (640 - resized.shape[0]) // 2
        canvas[offset_y:offset_y + resized.shape[0], offset_x:offset_x + resized.shape[1]] = resized
        tensor = canvas[:, :, ::-1].transpose(2, 0, 1).astype(np.float32) / 255.0
        output = session.run(None, {session.get_inputs()[0].name: tensor[None]})[0]
        predictions = np.asarray(output)
        if predictions.ndim == 3:
            predictions = predictions[0]
        if predictions.shape[0] < predictions.shape[1]:
            predictions = predictions.transpose(1, 0)
        return self._predictions_contain_person(predictions)

    def _predictions_contain_person(self, predictions: Any) -> bool:
        """Require a confident person detection that is meaningful in the frame.

        A hand, an arm, a background passer-by, or a cook beside food must not
        make an otherwise non-person image eligible.  The detected person has
        to occupy a substantial part of the inference canvas.
        """
        import numpy as np

        rows = np.asarray(predictions)
        if rows.ndim != 2 or rows.shape[1] <= 4:
            return False
        scores = rows[:, 4]
        left = np.clip(rows[:, 0] - rows[:, 2] / 2, 0, 640)
        right = np.clip(rows[:, 0] + rows[:, 2] / 2, 0, 640)
        top = np.clip(rows[:, 1] - rows[:, 3] / 2, 0, 640)
        bottom = np.clip(rows[:, 1] + rows[:, 3] / 2, 0, 640)
        areas = (right - left) * (bottom - top) / float(640 * 640)
        return bool(np.any((scores >= self.confidence) & (areas >= self.min_person_area)))

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                import onnxruntime as ort
            except ImportError as exc:
                raise RuntimeError("缺少人像检测依赖，请安装 ONNX Runtime 和 OpenCV") from exc
            model_path = Path(self.model_name)
            if model_path.suffix.lower() != ".onnx" or not model_path.exists():
                model_path = self.model_dir / self.model_name
            if not model_path.exists():
                model_path.parent.mkdir(parents=True, exist_ok=True)
                partial_path = model_path.with_suffix(model_path.suffix + ".part")
                try:
                    logger.info("正在下载人像检测模型：%s", self.model_name)
                    urllib.request.urlretrieve(self.MODEL_URL, str(partial_path))
                    if partial_path.stat().st_size < 1000000:
                        raise RuntimeError("人像检测模型文件不完整")
                    if self.model_name == self.MODEL_NAME and self._sha256(partial_path) != self.MODEL_SHA256:
                        raise RuntimeError("人像检测模型校验失败")
                    partial_path.replace(model_path)
                except Exception as exc:
                    try:
                        partial_path.unlink()
                    except OSError:
                        pass
                    if isinstance(exc, RuntimeError):
                        raise
                    raise RuntimeError("人像检测模型下载失败，请检查网络后重试") from exc
            if model_path.stat().st_size < 1000000:
                raise RuntimeError("人像检测模型文件不完整")
            if self.model_name == self.MODEL_NAME and self._sha256(model_path) != self.MODEL_SHA256:
                raise RuntimeError("人像检测模型校验失败，请删除模型缓存后重试")
            self._model = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
            return self._model

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
