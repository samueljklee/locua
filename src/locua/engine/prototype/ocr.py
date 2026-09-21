"""Single-image local Apple Vision OCR and conservative observation binding.

Compilation is explicit; no package download, capture, UI input, model selection
or online fallback occurs here. OCR boxes never create executable handles or
establish editability. Capture/recognition stamps are Unix time.time_ns().
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Mapping

from .perception import ObservationError, validate_observation

DEFAULT_BINARY = Path.home() / "Library/Caches/locua/vision_ocr"
# A narrow raster-boundary accommodation, not a claim about OCR accuracy. A
# partially clipped detection is retained for diagnosis/display but is never
# eligible for grounding. Larger overhangs require a fresh usable capture.
MAX_PARTIAL_BOX_OVERHANG_PX = 2.0
NUMERICAL_PIXEL_TOLERANCE = 1e-6


class OCRBindingError(ObservationError):
    pass


# A single-image adaptation of the successful v3 two-image smoke. Kept here so
# compile_vision_helper writes only generated/ignored artifacts, not upstream.
VISION_HELPER_SOURCE = r'''
import Foundation
import Vision
import ImageIO
import CryptoKit

enum OCRFailure: Error { case usage, decode }
do {
    guard CommandLine.arguments.count == 2 else { throw OCRFailure.usage }
    let start = DispatchTime.now().uptimeNanoseconds
    let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
    guard let source = CGImageSourceCreateWithData(data as CFData, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { throw OCRFailure.decode }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US"]
    request.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: image, orientation: .up, options: [:])
    let recognitionStart = DispatchTime.now().uptimeNanoseconds
    try handler.perform([request])
    let recognitionMS = Double(DispatchTime.now().uptimeNanoseconds - recognitionStart) / 1_000_000
    let observations: [[String: Any]] = (request.results ?? []).compactMap { observation in
        guard let text = observation.topCandidates(1).first else { return nil }
        let b = observation.boundingBox
        return ["text": text.string, "confidence": Double(text.confidence),
                "box_normalized_lower_left": ["x": b.minX, "y": b.minY, "width": b.width, "height": b.height]]
    }
    let result: [String: Any] = [
        "schema": "locua.vision_single_image.v1",
        "input_sha256": SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined(),
        "pixel_width": image.width, "pixel_height": image.height,
        "observations": observations,
        "configuration": ["api": "VNRecognizeTextRequest", "revision": request.revision,
                          "recognition_level": "accurate", "recognition_languages": ["en-US"],
                          "uses_language_correction": false, "custom_words": [],
                          "compute_device_selection": "Vision default; no override"],
        "timing": ["recognition_wall_ms": recognitionMS,
                   "process_body_wall_ms": Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000]
    ]
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]))
} catch {
    FileHandle.standardError.write(Data("Vision OCR failed: \(error)\n".utf8))
    exit(1)
}
'''


def _finite(value, label: str, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise OCRBindingError(f"{label} must be finite numeric data")
    if minimum is not None and value < minimum:
        raise OCRBindingError(f"{label} must be >= {minimum}")
    return value


def _stamp(value, label: str):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OCRBindingError(f"{label} must be nonnegative Unix nanoseconds")
    return value


def _sha(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise OCRBindingError("A lowercase SHA-256 image digest is required")
    return value


def _target(value: Mapping) -> dict:
    if not isinstance(value, Mapping):
        raise OCRBindingError("OCR target must be an object")
    native = all(key in value for key in ("pid", "window_id"))
    browser = all(key in value for key in ("target_id", "tab_id"))
    if not (native or browser):
        raise OCRBindingError("OCR needs an exact native or browser target")
    if native and any(type(value[k]) is not int or value[k] <= 0 for k in ("pid", "window_id")):
        raise OCRBindingError("Invalid native OCR target")
    if browser and any(not isinstance(value[k], str) or not value[k] for k in ("target_id", "tab_id")):
        raise OCRBindingError("Invalid browser OCR target")
    return deepcopy(dict(value))


def compile_vision_helper(binary_path: str | Path = DEFAULT_BINARY, *,
                          swiftc: str = "/usr/bin/swiftc", timeout_s: float = 60) -> Path:
    """Explicit, bounded local build. Does not execute OCR or download anything."""
    if sys.platform != "darwin":
        raise OCRBindingError("Apple Vision helper is available only on macOS")
    _finite(timeout_s, "timeout_s", minimum=0.001)
    if timeout_s > 60:
        raise OCRBindingError("Compilation timeout must not exceed 60 seconds")
    path = Path(binary_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    source = path.with_suffix(".swift")
    source.write_text(VISION_HELPER_SOURCE)
    cache = path.parent / "swift-module-cache"
    result = subprocess.run([swiftc, "-O", "-module-cache-path", str(cache), str(source), "-o", str(path)],
                            capture_output=True, text=True, timeout=timeout_s)
    if result.returncode:
        raise OCRBindingError(f"Local Vision compile failed: {result.stderr[:2000]}")
    return path


def bind_ocr_result(raw: dict, *, target: Mapping, snapshot_id: str,
                     image_sha256: str, captured_at_ns: int, recognized_at_ns: int) -> dict:
    """Bind a saved smoke/helper result to explicit caller capture provenance.

    Importing a historical result does not make its capture fresh. Callers must
    supply the original capture stamp, not the import time.
    """
    target = _target(target)
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise OCRBindingError("snapshot_id is required")
    _sha(image_sha256)
    _stamp(captured_at_ns, "captured_at_ns")
    _stamp(recognized_at_ns, "recognized_at_ns")
    if recognized_at_ns < captured_at_ns:
        raise OCRBindingError("Recognition precedes capture")
    if not isinstance(raw, dict) or raw.get("input_sha256") != image_sha256:
        raise OCRBindingError("OCR input image digest disagrees with capture")
    width, height = raw.get("pixel_width"), raw.get("pixel_height")
    if any(type(v) is not int or v <= 0 for v in (width, height)):
        raise OCRBindingError("Positive image pixel dimensions are required")
    observations = raw.get("observations")
    if not isinstance(observations, list):
        raise OCRBindingError("OCR observations must be an array")
    boxes = []
    for i, row in enumerate(observations):
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            raise OCRBindingError("Invalid recognized text observation")
        confidence = _finite(row.get("confidence"), "confidence", minimum=0)
        if confidence > 1:
            raise OCRBindingError("Confidence must be <= 1")
        normalized = row.get("box_normalized_lower_left")
        if not isinstance(normalized, dict):
            raise OCRBindingError("Missing normalized text box")
        values = {k: _finite(normalized.get(k), f"box.{k}")
                  for k in ("x", "y", "width", "height")}
        if values["width"] <= 0 or values["height"] <= 0:
            raise OCRBindingError("OCR text boxes must have positive size")
        original = {"x": values["x"] * width,
                    "y": (1 - values["y"] - values["height"]) * height,
                    "width": values["width"] * width, "height": values["height"] * height,
                    "coordinate_space": "screenshot_pixels"}
        left, top = original["x"], original["y"]
        right, bottom = left + original["width"], top + original["height"]
        overhang = max(0, -left, -top, right - width, bottom - height)
        if overhang > MAX_PARTIAL_BOX_OVERHANG_PX:
            raise OCRBindingError("OCR box exceeds the bounded image-edge tolerance")
        clipped_left, clipped_top = max(0, left), max(0, top)
        clipped_right, clipped_bottom = min(width, right), min(height, bottom)
        if clipped_right <= clipped_left or clipped_bottom <= clipped_top:
            raise OCRBindingError("OCR box has no positive intersection with the image")
        clipped = overhang > NUMERICAL_PIXEL_TOLERANCE
        bounds = {"x": clipped_left, "y": clipped_top,
                  "width": clipped_right - clipped_left, "height": clipped_bottom - clipped_top,
                  "coordinate_space": "screenshot_pixels"}
        boxes.append({"id": f"ocr:{i}", "text": row["text"], "confidence": confidence,
                      "bounds": bounds, "original_bounds": original, "clipped": clipped,
                      "roundoff_adjusted": 0 < overhang <= NUMERICAL_PIXEL_TOLERANCE,
                      "grounding_eligible": not clipped, "source": deepcopy(row)})
    result = {"schema": "locua.bound_ocr.v1", "image_sha256": image_sha256,
              "target": target, "snapshot_id": snapshot_id, "captured_at_ns": captured_at_ns,
              "recognized_at_ns": recognized_at_ns, "dimensions": {"width": width, "height": height},
              "boxes": boxes,
              "provenance": {"provider": "local_apple_vision", "clock": "unix_time_ns",
                             "configuration": deepcopy(raw.get("configuration", {})),
                             "timing": deepcopy(raw.get("timing", {})),
                             "raw_result": deepcopy(raw), "editability_inferred": False}}
    return result


def validate_ocr(ocr: dict, *, expected_target: Mapping, snapshot_id: str,
                 image_sha256: str, now_ns: int | None = None, max_age_s: float = 10) -> None:
    """Reject stale, mismatched, malformed or tampered binding/geometry."""
    if not isinstance(ocr, dict) or ocr.get("schema") != "locua.bound_ocr.v1":
        raise OCRBindingError("Not a bound OCR result")
    if ocr.get("target") != dict(expected_target) or ocr.get("snapshot_id") != snapshot_id:
        raise OCRBindingError("OCR target or snapshot does not match observation")
    if ocr.get("image_sha256") != _sha(image_sha256):
        raise OCRBindingError("OCR belongs to another screenshot")
    now_ns = time.time_ns() if now_ns is None else _stamp(now_ns, "now_ns")
    captured = _stamp(ocr.get("captured_at_ns"), "captured_at_ns")
    recognized = _stamp(ocr.get("recognized_at_ns"), "recognized_at_ns")
    _finite(max_age_s, "max_age_s", minimum=0)
    if recognized < captured or now_ns < recognized or now_ns - captured > max_age_s * 1_000_000_000:
        raise OCRBindingError("OCR capture is stale or timestamps are inconsistent")
    # Re-derive public boxes from retained raw observations to prevent a caller
    # accidentally reusing/changing geometry under an old binding.
    derived = bind_ocr_result(ocr.get("provenance", {}).get("raw_result"), target=expected_target,
                              snapshot_id=snapshot_id, image_sha256=image_sha256,
                              captured_at_ns=captured, recognized_at_ns=recognized)
    if derived["boxes"] != ocr.get("boxes") or derived["dimensions"] != ocr.get("dimensions"):
        raise OCRBindingError("OCR boxes or dimensions contradict the retained result")


def run_local_ocr(image_path: str | Path, *, target: Mapping, snapshot_id: str,
                  captured_at_ns: int, image_sha256: str | None = None,
                  binary_path: str | Path = DEFAULT_BINARY, output_path: str | Path | None = None,
                  timeout_s: float = 30, max_age_s: float = 10) -> dict:
    """Run exactly one local recognition request; never compile/fallback implicitly."""
    _target(target)
    _stamp(captured_at_ns, "captured_at_ns")
    _finite(timeout_s, "timeout_s", minimum=0.001)
    if timeout_s > 60:
        raise OCRBindingError("OCR timeout must not exceed 60 seconds")
    _finite(max_age_s, "max_age_s", minimum=0)
    now = time.time_ns()
    if now < captured_at_ns or now - captured_at_ns > max_age_s * 1_000_000_000:
        raise OCRBindingError("Screenshot is stale before OCR starts")
    path = Path(image_path).resolve()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    if image_sha256 is not None and before != _sha(image_sha256):
        raise OCRBindingError("Screenshot file does not match expected image hash")
    binary = Path(binary_path).resolve()
    if not binary.is_file():
        raise OCRBindingError("Vision helper is missing; call compile_vision_helper explicitly")
    start = time.monotonic_ns()
    execution = subprocess.run([str(binary), str(path)], capture_output=True, text=True, timeout=timeout_s)
    duration_ms = (time.monotonic_ns() - start) / 1_000_000
    if execution.returncode:
        raise OCRBindingError(f"Local Vision OCR failed; no fallback: {execution.stderr[:2000]}")
    try:
        raw = json.loads(execution.stdout)
    except ValueError as exc:
        raise OCRBindingError("Vision helper returned invalid JSON") from exc
    if hashlib.sha256(path.read_bytes()).hexdigest() != before:
        raise OCRBindingError("Screenshot file changed while OCR was running")
    recognized = time.time_ns()
    result = bind_ocr_result(raw, target=target, snapshot_id=snapshot_id, image_sha256=before,
                             captured_at_ns=captured_at_ns, recognized_at_ns=recognized)
    result["provenance"].update({"image_path": str(path), "binary_path": str(binary),
                                 "subprocess_wall_ms": duration_ms})
    validate_ocr(result, expected_target=target, snapshot_id=snapshot_id,
                 image_sha256=before, now_ns=recognized, max_age_s=max_age_s)
    if output_path is not None:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return result


def screenshot_transform_from_observation(observation: dict, *, image_sha256: str) -> dict | None:
    """Use only Cua's validated frame from this exact screenshot response.

    In particular, a requested resize origin is not the observed window origin.
    Missing/invalid capture-frame evidence yields no screen-point transform.
    """
    metadata = observation.get("provenance", {}).get("raw_metadata", {})
    if metadata.get("screenshot_frame_valid") is not True:
        return None
    dimensions = {"width": metadata.get("screenshot_width"), "height": metadata.get("screenshot_height")}
    if any(type(v) is not int or v <= 0 for v in dimensions.values()):
        raise OCRBindingError("Validated screenshot is missing pixel dimensions")
    bounds = metadata.get("window_bounds")
    if not isinstance(bounds, dict):
        raise OCRBindingError("Validated screenshot is missing its observed window bounds")
    return {"target": deepcopy(observation["target"]), "snapshot_id": observation["snapshot_id"],
            "image_sha256": _sha(image_sha256), "dimensions": dimensions,
            "window_frame_screen_points": deepcopy(bounds), "source": "cua_validated_capture_frame"}


def _screen_transform(transform: dict | None, observation: dict, ocr: dict) -> dict | None:
    if transform is None:
        return None
    if not isinstance(transform, dict) or any(transform.get(k) != ocr.get(k)
            for k in ("target", "snapshot_id", "image_sha256")):
        raise OCRBindingError("Screen transform is not bound to this target/snapshot/image")
    if transform.get("dimensions") != ocr["dimensions"]:
        raise OCRBindingError("Screen transform uses different screenshot dimensions")
    frame = transform.get("window_frame_screen_points")
    if not isinstance(frame, dict):
        raise OCRBindingError("Transform requires observed window frame in screen points")
    frame = {k: _finite(frame.get(k), f"window_frame.{k}") for k in ("x", "y", "width", "height")}
    if frame["width"] <= 0 or frame["height"] <= 0:
        raise OCRBindingError("Window frame must have positive size")
    observed = screenshot_transform_from_observation(observation, image_sha256=ocr["image_sha256"])
    if observed is not None and (observed["window_frame_screen_points"] != frame or
                                  observed["dimensions"] != transform["dimensions"]):
        raise OCRBindingError("Transform contradicts Cua's observed capture geometry")
    # Caller must supply this from the exact capture's geometry, not a guessed
    # window origin or retained geometry from a different observation.
    return frame


def _control_pixels(control: dict, frame: dict | None, dimensions: dict) -> dict | None:
    bounds = control.get("bounds")
    if not isinstance(bounds, dict):
        return None
    space = bounds.get("coordinate_space")
    if space == "screenshot_pixels":
        result = {k: _finite(bounds.get(k), f"control.bounds.{k}") for k in ("x", "y", "width", "height")}
    elif space == "screen_points" and frame is not None:
        sx, sy = dimensions["width"] / frame["width"], dimensions["height"] / frame["height"]
        result = {"x": (bounds["x"] - frame["x"]) * sx, "y": (bounds["y"] - frame["y"]) * sy,
                  "width": bounds["width"] * sx, "height": bounds["height"] * sy}
    else:
        return None
    if result["width"] <= 0 or result["height"] <= 0:
        return None
    return result


def _contains_text(control_bounds: dict, text_bounds: dict) -> bool:
    """Require the entire recognized text box, not just a nearby label."""
    epsilon = 1e-5
    return (control_bounds["x"] - epsilon <= text_bounds["x"] and
            control_bounds["y"] - epsilon <= text_bounds["y"] and
            text_bounds["x"] + text_bounds["width"] <= control_bounds["x"] + control_bounds["width"] + epsilon and
            text_bounds["y"] + text_bounds["height"] <= control_bounds["y"] + control_bounds["height"] + epsilon)


def ground_ocr(observation: dict, ocr: dict, *, image_sha256: str,
                now_ns: int | None = None, max_age_s: float = 10,
                screenshot_transform: dict | None = None) -> dict:
    """Associate OCR text only with exact observed semantics and unique geometry.

    No substring/fuzzy matches, guessed axes, fixture labels or writable-state
    inference. Missing geometry and unresolved duplicate text remain explicit.
    The return value is evidence only; it never adds or changes action handles.
    """
    now_ns = time.time_ns() if now_ns is None else now_ns
    validate_observation(observation, expected_target=observation["target"],
                         current_snapshot_id=ocr.get("snapshot_id"), now_ns=now_ns, max_age_s=max_age_s)
    validate_ocr(ocr, expected_target=observation["target"], snapshot_id=observation["snapshot_id"],
                 image_sha256=image_sha256, now_ns=now_ns, max_age_s=max_age_s)
    if screenshot_transform is None:
        screenshot_transform = screenshot_transform_from_observation(observation, image_sha256=image_sha256)
    frame = _screen_transform(screenshot_transform, observation, ocr)
    bindings = []
    for box in ocr["boxes"]:
        text = box["text"]
        if not box["grounding_eligible"]:
            bindings.append({"ocr_id": box["id"], "status": "unmatched",
                             "reason": "clipped_detection_not_groundable", "control_id": None,
                             "candidate_ids": [], "spatial_candidate_ids": [],
                             "bounds": deepcopy(box["bounds"]), "text": text,
                             "confidence": box["confidence"], "editability_inferred": False,
                             "new_action_handle": None})
            continue
        candidates = [c for c in observation["controls"] if text and any(
            isinstance(c.get(field), str) and c[field] == text for field in ("name", "value"))]
        pixels = {c["id"]: _control_pixels(c, frame, ocr["dimensions"]) for c in candidates}
        matching = [c for c in candidates if pixels[c["id"]] is not None and
                    _contains_text(pixels[c["id"]], box["bounds"])]
        unknown = [c for c in candidates if pixels[c["id"]] is None]
        status, reason, control_id = "unmatched", "no_exact_semantic_label_or_value", None
        if candidates:
            status, reason = "ambiguous", "semantic_match_without_unique_spatial_evidence"
            if len(matching) == 1 and not unknown:
                chosen = matching[0]
                overlapping_duplicates = [b for b in ocr["boxes"] if b["text"] == text and
                                           _contains_text(pixels[chosen["id"]], b["bounds"])]
                if len(overlapping_duplicates) == 1:
                    status, reason, control_id = "grounded", "exact_semantics_and_unique_containment", chosen["id"]
                else:
                    reason = "duplicate_ocr_text_inside_one_control"
            elif not matching and not unknown:
                status, reason = "unmatched", "semantic_text_is_outside_observed_control_bounds"
        bindings.append({"ocr_id": box["id"], "status": status, "reason": reason,
                         "control_id": control_id, "candidate_ids": [c["id"] for c in candidates],
                         "spatial_candidate_ids": [c["id"] for c in matching],
                         "bounds": deepcopy(box["bounds"]), "text": text,
                         "confidence": box["confidence"], "editability_inferred": False,
                         "new_action_handle": None})
    return {"schema": "locua.ocr_grounding.v1", "target": deepcopy(ocr["target"]),
            "snapshot_id": ocr["snapshot_id"], "image_sha256": image_sha256,
            "bindings": bindings, "grounded_count": sum(b["status"] == "grounded" for b in bindings),
            "ambiguous_count": sum(b["status"] == "ambiguous" for b in bindings),
            "creates_action_capabilities": False}
