import asyncio

import cv2
import numpy as np

from core.person_filter import PersonPresenceFilter


def test_image_without_person_is_filtered_without_loading_model(tmp_path):
    detector = PersonPresenceFilter(
        infer=lambda _frame: False,
        image_loader=lambda _path: object(),
    )

    result = asyncio.run(detector.inspect(tmp_path / "image.jpg", "image"))

    assert result.decision == "filtered"
    assert result.reason == "未检测到人物"
    assert result.sample_count == 1


def test_video_with_person_is_kept_after_sampled_frame(tmp_path):
    detector = PersonPresenceFilter(
        infer=lambda frame: frame == "person",
        video_sampler=lambda _path, _count: ["background", "person"],
        video_duration=lambda _path: 12.0,
    )

    result = asyncio.run(detector.inspect(tmp_path / "video.mp4", "video", max_samples=8))

    assert result.decision == "keep"
    assert result.sample_count == 2


def test_long_video_is_filtered_before_inference(tmp_path):
    detector = PersonPresenceFilter(
        infer=lambda _frame: True,
        video_sampler=lambda _path, _count: ["person"],
        video_duration=lambda _path: 50.1,
    )

    result = asyncio.run(detector.inspect(tmp_path / "long.mp4", "video", max_video_seconds=50))

    assert result.decision == "filtered"
    assert result.model_version == detector.DURATION_FILTER_VERSION


def test_image_in_chinese_path_is_decoded_before_filtering(tmp_path):
    image_path = tmp_path / "抖音收藏" / "风景.jpg"
    image_path.parent.mkdir()
    ok, encoded = cv2.imencode(".jpg", np.zeros((12, 12, 3), dtype=np.uint8))
    assert ok
    encoded.tofile(str(image_path))

    detector = PersonPresenceFilter(infer=lambda _frame: False)
    result = asyncio.run(detector.inspect(image_path, "image"))

    assert result.decision == "filtered"


def test_unknown_video_duration_is_not_saved(tmp_path):
    detector = PersonPresenceFilter(
        infer=lambda _frame: True,
        video_sampler=lambda _path, _count: ["person"],
        video_duration=lambda _path: None,
    )

    result = asyncio.run(detector.inspect(tmp_path / "unknown-duration.mp4", "video"))

    assert result.decision == "error"
    assert "视频时长" in result.reason


def test_person_detection_requires_confident_prominent_box():
    detector = PersonPresenceFilter(confidence=0.5, min_person_area=0.25)

    assert detector._predictions_contain_person([[320, 320, 327, 537, 0.65]]) is True
    assert detector._predictions_contain_person([[320, 320, 250, 250, 0.90]]) is False
    assert detector._predictions_contain_person([[526, 497, 26, 57, 0.80]]) is False
    assert detector._predictions_contain_person([[328, 319, 630, 475, 0.31]]) is False
