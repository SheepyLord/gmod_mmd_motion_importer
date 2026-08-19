"""Qt/OpenGL preview widget for the importer."""

from __future__ import annotations

import ctypes
import math
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtOpenGL import QOpenGLWindow
except Exception as exc:  # pragma: no cover - GUI dependency guard
    raise RuntimeError("PySide6 is required for the importer preview UI") from exc

try:
    from OpenGL import GL
except Exception:  # pragma: no cover - widget reports unavailable backend
    GL = None

try:
    from tools import import_vmd
    from tools.preview.data import PreviewScene, load_preview_scene, sample_bone_motion, sample_ik_enabled
except ModuleNotFoundError:
    import import_vmd  # type: ignore[no-redef]
    from preview.data import PreviewScene, load_preview_scene, sample_bone_motion, sample_ik_enabled  # type: ignore[no-redef]


DEFAULT_FRONT_VIEW_YAW = math.pi
PREVIEW_GMOD_MODEL_LABEL = r"models\sheepylord\arknight_endfield\li_zhiyan.mdl"


def _translation_matrix(v: np.ndarray) -> np.ndarray:
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, 3] = v[:3]
    return matrix


def _rotation_matrix(q: tuple[float, float, float, float]) -> np.ndarray:
    x, y, z, w = q
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length <= 1e-8:
        return np.eye(4, dtype=np.float64)
    x, y, z, w = x / length, y / length, z / length, w / length
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )
    return matrix


def _axis_angle_matrix(axis: np.ndarray, angle: float) -> np.ndarray:
    axis_len = float(np.linalg.norm(axis))
    if axis_len <= 1e-8 or abs(angle) <= 1e-8:
        return np.eye(4, dtype=np.float64)
    x, y, z = axis / axis_len
    c = math.cos(angle)
    s = math.sin(angle)
    t = 1.0 - c
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = np.array(
        [
            [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
            [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
            [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
        ],
        dtype=np.float64,
    )
    return matrix


def _orthonormalize_rotation(rotation: np.ndarray) -> np.ndarray:
    u, _, vh = np.linalg.svd(rotation)
    out = u @ vh
    if np.linalg.det(out) < 0:
        u[:, -1] *= -1.0
        out = u @ vh
    return out


def _euler_xyz_matrix(x: float, y: float, z: float) -> np.ndarray:
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]], dtype=np.float64)
    ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]], dtype=np.float64)
    rz = np.array([[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    return rz @ ry @ rx


def _matrix_to_euler_xyz(rotation: np.ndarray) -> tuple[float, float, float]:
    matrix = _orthonormalize_rotation(rotation[:3, :3])
    sy = float(np.clip(-matrix[2, 0], -1.0, 1.0))
    y = math.asin(sy)
    if abs(sy) < 0.999999:
        x = math.atan2(matrix[2, 1], matrix[2, 2])
        z = math.atan2(matrix[1, 0], matrix[0, 0])
    else:
        x = 0.0
        z = math.atan2(-matrix[0, 1], matrix[1, 1])
    return x, y, z


def _clamp_angle(angle: float, minimum: float, maximum: float) -> float:
    low = min(minimum, maximum)
    high = max(minimum, maximum)
    candidates = [angle + math.tau * offset for offset in (-2, -1, 0, 1, 2)]

    def distance_to_range(value: float) -> float:
        if low <= value <= high:
            return 0.0
        return min(abs(value - low), abs(value - high))

    best = min(candidates, key=distance_to_range)
    return max(low, min(high, best))


def _rotation_between(a: np.ndarray, b: np.ndarray, max_angle: float) -> np.ndarray:
    a_len = float(np.linalg.norm(a))
    b_len = float(np.linalg.norm(b))
    if a_len <= 1e-8 or b_len <= 1e-8:
        return np.eye(4, dtype=np.float64)
    av = a / a_len
    bv = b / b_len
    dot = float(np.clip(np.dot(av, bv), -1.0, 1.0))
    if dot > 0.99995:
        return np.eye(4, dtype=np.float64)
    axis = np.cross(av, bv)
    if float(np.linalg.norm(axis)) <= 1e-8:
        axis = np.array([1.0, 0.0, 0.0], dtype=np.float64)
    angle = math.acos(dot)
    if max_angle > 0:
        angle = min(angle, max_angle)
    return _axis_angle_matrix(axis, angle)


# A QOpenGLWindow embedded via QWidget.createWindowContainer, NOT a
# QOpenGLWidget: the mere presence of a QOpenGLWidget in the widget tree —
# even on a never-shown tab — switches the ENTIRE top-level window from raster
# to GPU-composited flushing, which made every text-field repaint (drag
# selection, typing) several times slower app-wide (and far worse when Qt
# falls back to software OpenGL in the packaged exe). A native GL child window
# confines the OpenGL pipeline to the preview viewport; the rest of the UI
# keeps the fast raster backing store.
class PreviewWidget(QOpenGLWindow):
    frameChanged = QtCore.Signal(int, float)
    statsChanged = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__()
        self.scene_data: PreviewScene | None = None
        self.playing = False
        self.loop = True
        self.speed = 1.0
        self.current_frame = 0.0
        self.started_at = time.monotonic()
        self.started_frame = 0.0
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.75)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)
        # QWindow wants a QSize (no (w, h) overload); the widget container in
        # the GUI mirrors this minimum. QWindow needs no setMouseTracking —
        # move events are always delivered, and only drags are used anyway.
        self.setMinimumSize(QtCore.QSize(480, 300))

        self._positions: np.ndarray | None = None
        self._normals: np.ndarray | None = None
        self._uvs: np.ndarray | None = None
        self._bone_indices: np.ndarray | None = None
        self._bone_weights: np.ndarray | None = None
        self._indices: np.ndarray | None = None
        self._material_ranges: list[tuple[int, int, int]] = []
        self._hidden_material_count = 0
        self._rest_inverse: np.ndarray | None = None
        self._rest_local: np.ndarray | None = None
        self._children: list[list[int]] = []
        self._deform_mirror_pairs: list[tuple[int, int]] = []
        self._scene_center = np.zeros(3, dtype=np.float64)
        self._scene_mins = np.zeros(3, dtype=np.float64)
        self._scene_maxs = np.ones(3, dtype=np.float64)
        self._scene_extent = 1.0
        self._view_yaw = DEFAULT_FRONT_VIEW_YAW
        self._view_pitch = 0.0
        self._view_zoom = 1.0
        self._view_pan = QtCore.QPointF(0.0, 0.0)
        self._follow_camera = True
        self._last_mouse_pos: QtCore.QPoint | None = None
        self._show_bone_overlay = False
        self._show_bone_names = False
        self._audio_offset_seconds = 0.0
        self._audio_waiting_for_offset = False
        self._frame_cache: OrderedDict[float, tuple[np.ndarray | None, np.ndarray | None, int, int, int, str]] = OrderedDict()

        self._vao = 0
        self._vbo = 0
        self._ibo = 0
        self._texture_ids: dict[Path, int] = {}
        self._gl_ready = False
        self._gl_error = ""
        self._vertex_data: np.ndarray | None = None
        self._use_client_arrays = False

        self._fps_started = time.monotonic()
        self._fps_counter = 0
        self._preview_fps = 0.0
        self._active_morph_count = 0
        self._active_ik_count = 0
        self._enabled_ik_count = 0
        self._first_ik_warning = ""
        self._show_debug_center_marker = False

    def rect(self) -> QtCore.QRect:
        # QWindow has no rect(); the painter overlays use it like QWidget's.
        return QtCore.QRect(0, 0, self.width(), self.height())

    def load_scene(
        self,
        model_path: Path,
        body_vmd_path: Path,
        flex_vmd_paths: list[Path] | None = None,
        music_path: Path | None = None,
        camera_vmd_path: Path | None = None,
    ) -> PreviewScene:
        self.scene_data = load_preview_scene(model_path, body_vmd_path, flex_vmd_paths, music_path, camera_vmd_path)
        self._prepare_scene_cache()
        self.reset_front_view()
        self.current_frame = float(self.scene_data.frame_start)
        self.started_frame = self.current_frame
        self.started_at = time.monotonic()
        self.playing = False
        self._gl_ready = False
        self._frame_cache.clear()
        if self.scene_data.music_path:
            self.player.setSource(QtCore.QUrl.fromLocalFile(str(self.scene_data.music_path)))
        else:
            self.player.setSource(QtCore.QUrl())
        self.update()
        self.frameChanged.emit(int(self.current_frame), self.current_frame / max(1, self.scene_data.fps))
        return self.scene_data

    def set_loop(self, enabled: bool) -> None:
        self.loop = enabled

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.05, float(speed or 1.0))

    def set_audio_enabled(self, enabled: bool) -> None:
        self.audio_output.setMuted(not enabled)

    def set_audio_offset_seconds(self, seconds: float) -> None:
        self._audio_offset_seconds = float(seconds or 0.0)
        if self.scene_data and self.scene_data.music_path:
            self._sync_audio_to_frame(force=True, play_if_ready=self.playing)

    def set_follow_camera(self, enabled: bool) -> None:
        self._follow_camera = bool(enabled)
        self.update()

    def has_camera_motion(self) -> bool:
        return bool(self.scene_data and self.scene_data.camera_frames)

    def set_bone_overlay_enabled(self, enabled: bool) -> None:
        self._show_bone_overlay = bool(enabled)
        self.update()

    def set_bone_names_enabled(self, enabled: bool) -> None:
        self._show_bone_names = bool(enabled)
        self.update()

    def reset_front_view(self) -> None:
        self._view_yaw = DEFAULT_FRONT_VIEW_YAW
        self._view_pitch = 0.0
        self._view_zoom = 1.0
        self._view_pan = QtCore.QPointF(0.0, 0.0)
        self.update()

    def play(self) -> None:
        if not self.scene_data:
            return
        self.playing = True
        self.started_frame = self.current_frame
        self.started_at = time.monotonic()
        self._sync_audio_to_frame(force=True, play_if_ready=True)

    def pause(self) -> None:
        self.playing = False
        self.player.pause()

    def stop(self) -> None:
        self.playing = False
        self.current_frame = float(self.scene_data.frame_start if self.scene_data else 0)
        self.player.stop()
        self._audio_waiting_for_offset = False
        self.update()

    def scrub_to_fraction(self, fraction: float) -> None:
        if not self.scene_data:
            return
        fraction = max(0.0, min(1.0, float(fraction)))
        self.current_frame = self.scene_data.frame_start + (self.scene_data.frame_end - self.scene_data.frame_start) * fraction
        self.started_frame = self.current_frame
        self.started_at = time.monotonic()
        self._sync_audio_to_frame(force=True, play_if_ready=self.playing)
        self.update()
        self.frameChanged.emit(int(self.current_frame), self.current_frame / max(1, self.scene_data.fps))

    def _audio_time_seconds_for_frame(self) -> float:
        if not self.scene_data:
            return 0.0
        animation_seconds = self.current_frame / max(1, self.scene_data.fps)
        # Positive offset starts audio later; negative offset starts it advanced.
        return animation_seconds - self._audio_offset_seconds

    def _sync_audio_to_frame(self, force: bool = False, play_if_ready: bool = False) -> None:
        if not self.scene_data or not self.scene_data.music_path:
            self._audio_waiting_for_offset = False
            return

        audio_seconds = self._audio_time_seconds_for_frame()
        if audio_seconds < 0:
            self._audio_waiting_for_offset = True
            self.player.stop()
            return

        position_ms = max(0, int(audio_seconds * 1000))
        if force or self._audio_waiting_for_offset:
            self.player.setPosition(position_ms)
        self._audio_waiting_for_offset = False
        if play_if_ready and self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.player.play()

    def _tick(self) -> None:
        if not self.playing or not self.scene_data:
            return
        elapsed = (time.monotonic() - self.started_at) * self.speed
        self.current_frame = self.started_frame + elapsed * self.scene_data.fps
        if self.current_frame >= self.scene_data.frame_end:
            if self.loop:
                span = max(1, self.scene_data.frame_end - self.scene_data.frame_start)
                self.current_frame = self.scene_data.frame_start + ((self.current_frame - self.scene_data.frame_start) % span)
                self.started_frame = self.current_frame
                self.started_at = time.monotonic()
                if self.scene_data.music_path:
                    self._sync_audio_to_frame(force=True, play_if_ready=True)
            else:
                self.current_frame = float(self.scene_data.frame_end)
                self.pause()
        # Only keep audio following the animation while still playing; after the
        # non-loop end pause() above, re-syncing with play_if_ready would restart
        # the music over a frozen frame.
        if self.playing:
            self._sync_audio_to_frame(play_if_ready=True)
        self.frameChanged.emit(int(self.current_frame), self.current_frame / max(1, self.scene_data.fps))
        self.update()

    def _prepare_scene_cache(self) -> None:
        scene = self.scene_data
        if not scene or not scene.mesh:
            return

        self._positions = np.array([vertex.position for vertex in scene.mesh.vertices], dtype=np.float64)
        self._normals = np.array([vertex.normal for vertex in scene.mesh.vertices], dtype=np.float64)
        self._uvs = np.array([vertex.uv for vertex in scene.mesh.vertices], dtype=np.float64)
        self._bone_indices = np.array([vertex.bone_indices for vertex in scene.mesh.vertices], dtype=np.int32)
        self._bone_weights = np.array([vertex.bone_weights for vertex in scene.mesh.vertices], dtype=np.float64)
        self._indices = np.array(scene.mesh.indices, dtype=np.uint32)
        self._material_ranges = []
        self._hidden_material_count = 0
        for index, material in enumerate(scene.mesh.materials):
            if material.index_count <= 0 or material.index_start >= len(scene.mesh.indices):
                continue
            if self._is_hidden_preview_material(material.name):
                self._hidden_material_count += 1
                continue
            self._material_ranges.append((index, material.index_start, min(material.index_count, len(scene.mesh.indices) - material.index_start)))

        self._children = [[] for _ in scene.bones]
        for index, bone in enumerate(scene.bones):
            if 0 <= bone.parent < len(scene.bones):
                self._children[bone.parent].append(index)
        self._deform_mirror_pairs = self._build_deform_mirror_pairs()

        # Per-bone rest offset relative to its parent is frame-invariant; cache
        # it once instead of rebuilding np.arrays for every bone every frame in
        # the skinning hot path.
        self._rest_local = np.zeros((len(scene.bones), 3), dtype=np.float64)
        for index, bone in enumerate(scene.bones):
            parent = bone.parent if 0 <= bone.parent < len(scene.bones) else -1
            position = np.array(bone.position, dtype=np.float64)
            if parent >= 0:
                self._rest_local[index] = position - np.array(scene.bones[parent].position, dtype=np.float64)
            else:
                self._rest_local[index] = position

        self._rest_inverse = self._compute_rest_inverse()
        bounds_source = self._positions if len(self._positions) > 0 else np.array([bone.position for bone in scene.bones], dtype=np.float64)
        mins = bounds_source.min(axis=0)
        maxs = bounds_source.max(axis=0)
        self._scene_mins = mins
        self._scene_maxs = maxs
        self._scene_center = (mins + maxs) * 0.5
        self._scene_extent = max(1.0, float(np.max(maxs - mins)))
        self._vertex_data = np.zeros((len(self._positions), 8), dtype=np.float32)
        self._vertex_data[:, 3:6] = self._normals.astype(np.float32)
        self._vertex_data[:, 6:8] = self._uvs.astype(np.float32)

    def _compute_rest_inverse(self) -> np.ndarray | None:
        scene = self.scene_data
        if not scene or not scene.bones:
            return None

        rest = np.zeros((len(scene.bones), 4, 4), dtype=np.float64)
        for index, bone in enumerate(scene.bones):
            parent = bone.parent if 0 <= bone.parent < len(scene.bones) else -1
            position = np.array(bone.position, dtype=np.float64)
            if parent >= 0:
                parent_position = np.array(scene.bones[parent].position, dtype=np.float64)
                rest[index] = rest[parent] @ _translation_matrix(position - parent_position)
            else:
                rest[index] = _translation_matrix(position)
        return np.linalg.inv(rest)

    @staticmethod
    def _is_hidden_preview_material(name: str) -> bool:
        return "\u5f71" in name

    def _build_deform_mirror_pairs(self) -> list[tuple[int, int]]:
        scene = self.scene_data
        if not scene:
            return []

        lookup = {bone.name: index for index, bone in enumerate(scene.bones) if bone.name}
        pairs: list[tuple[int, int]] = []
        for deform_index, bone in enumerate(scene.bones):
            if not bone.name.endswith("D"):
                continue
            source_index = lookup.get(bone.name[:-1])
            if source_index is None or source_index == deform_index:
                continue

            source_position = np.array(scene.bones[source_index].position, dtype=np.float64)
            deform_position = np.array(bone.position, dtype=np.float64)
            if np.linalg.norm(source_position - deform_position) > 1e-4:
                continue
            pairs.append((source_index, deform_index))

        return sorted(pairs, key=lambda pair: self._bone_depth(pair[1]))

    def _bone_depth(self, index: int) -> int:
        scene = self.scene_data
        if not scene:
            return 0
        depth = 0
        seen: set[int] = set()
        parent = scene.bones[index].parent if 0 <= index < len(scene.bones) else -1
        while 0 <= parent < len(scene.bones) and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = scene.bones[parent].parent
        return depth

    def _morphed_positions(self, frame: float) -> tuple[np.ndarray | None, int]:
        scene = self.scene_data
        if not scene or self._positions is None:
            return None, 0

        positions = self._positions.copy()
        active_count = 0
        for track in scene.morph_tracks.values():
            weight = import_vmd.sample_morph(track.frames, frame)
            if abs(weight) <= 1e-5:
                continue
            active_count += 1
            self._apply_morph(track.morph_index, weight, positions, set())
        return positions, active_count

    def _apply_morph(self, morph_index: int, weight: float, positions: np.ndarray, guard: set[int]) -> None:
        scene = self.scene_data
        if not scene or morph_index in guard or morph_index < 0 or morph_index >= len(scene.morphs):
            return
        guard.add(morph_index)
        morph = scene.morphs[morph_index]
        for vertex_index, offset in morph.vertex_offsets:
            if 0 <= vertex_index < len(positions):
                positions[vertex_index] += np.array(offset, dtype=np.float64) * weight
        for child_index, child_weight in morph.group_offsets:
            self._apply_morph(child_index, weight * child_weight, positions, guard)
        guard.remove(morph_index)

    def _pose_matrices(self) -> tuple[np.ndarray | None, np.ndarray | None, int, int, str]:
        scene = self.scene_data
        if not scene or not scene.bones or self._rest_inverse is None:
            return None, None, 0, 0, ""

        local = np.zeros((len(scene.bones), 4, 4), dtype=np.float64)
        pose = np.zeros((len(scene.bones), 4, 4), dtype=np.float64)
        bone_world = np.zeros((len(scene.bones), 3), dtype=np.float64)
        for index, bone in enumerate(scene.bones):
            parent = bone.parent if 0 <= bone.parent < len(scene.bones) else -1
            rest_local = self._rest_local[index]

            loc, quat = sample_bone_motion(scene.bone_tracks.get(index), self.current_frame)
            local[index] = _translation_matrix(rest_local + np.array(loc, dtype=np.float64)) @ _rotation_matrix(quat)
            pose[index] = pose[parent] @ local[index] if parent >= 0 else local[index]
            bone_world[index] = pose[index][:3, 3]

        active_ik, enabled_ik, ik_warning = self._apply_ik(local, pose, bone_world)
        self._mirror_deform_bones(local, pose, bone_world)
        return pose @ self._rest_inverse, bone_world, active_ik, enabled_ik, ik_warning

    def _update_descendants(self, root: int, local: np.ndarray, pose: np.ndarray, bone_world: np.ndarray) -> None:
        for child in self._children[root]:
            pose[child] = pose[root] @ local[child]
            bone_world[child] = pose[child][:3, 3]
            self._update_descendants(child, local, pose, bone_world)

    def _update_subtree(self, root: int, local: np.ndarray, pose: np.ndarray, bone_world: np.ndarray) -> None:
        scene = self.scene_data
        if not scene:
            return
        parent = scene.bones[root].parent
        pose[root] = pose[parent] @ local[root] if 0 <= parent < len(scene.bones) else local[root]
        bone_world[root] = pose[root][:3, 3]
        self._update_descendants(root, local, pose, bone_world)

    def _clamp_link_rotation(self, link_index: int, link, local: np.ndarray) -> None:
        if not link.has_limits or not (0 <= link_index < len(local)):
            return

        angles = list(_matrix_to_euler_xyz(local[link_index][:3, :3]))
        for axis in range(3):
            angles[axis] = _clamp_angle(angles[axis], float(link.min_angle[axis]), float(link.max_angle[axis]))
        local[link_index][:3, :3] = _euler_xyz_matrix(angles[0], angles[1], angles[2])

    @staticmethod
    def _chain_deficit(ik, pose: np.ndarray, bone_world: np.ndarray, target_position: np.ndarray) -> float:
        """How far the target lies beyond the chain's maximum reach (0 when
        reachable). A miss up to this deficit is geometry, not solver failure:
        motions authored for longer-limbed models put IK targets out of reach,
        and the best any solver (MMD included) can do is a straight limb
        pointing at the target."""
        indices = [link.bone_index for link in ik.links]
        if not indices:
            return 0.0
        reach = float(np.linalg.norm(pose[ik.target_index][:3, 3] - bone_world[indices[0]]))
        for near, far in zip(indices, indices[1:]):
            reach += float(np.linalg.norm(bone_world[near] - bone_world[far]))
        span = float(np.linalg.norm(target_position - bone_world[indices[-1]]))
        if len(indices) == 1:
            # A single-link chain (toe IK: the ankle aiming the toe) sweeps a
            # fixed-radius sphere — a target nearer OR farther than the reach
            # is equally unhittable; only the aim direction is solvable.
            return abs(span - reach)
        return max(0.0, span - reach)

    def _solve_two_bone_chain(self, ik, local: np.ndarray, pose: np.ndarray, bone_world: np.ndarray, target_position: np.ndarray) -> bool:
        """Closed-form solve for the standard MMD leg chain [hinge, free]:
        law-of-cosines bend at the hinge (knee), then one minimal rotation at
        the free link (hip) that puts the effector exactly on the target.
        Returns False when the chain does not match, leaving CCD to handle it."""
        scene = self.scene_data
        if not scene or len(ik.links) != 2:
            return False
        hinge_link, free_link = ik.links[0], ik.links[1]
        hinge_axis = self._link_hinge_axis(hinge_link)
        if hinge_axis is None or self._link_hinge_axis(free_link) is not None or free_link.has_limits:
            return False
        knee = hinge_link.bone_index
        hip = free_link.bone_index
        effector = ik.target_index
        if not (0 <= knee < len(scene.bones) and 0 <= hip < len(scene.bones) and 0 <= effector < len(scene.bones)):
            return False

        upper_len = float(np.linalg.norm(bone_world[knee] - bone_world[hip]))
        lower_len = float(np.linalg.norm(pose[effector][:3, 3] - bone_world[knee]))
        span = float(np.linalg.norm(target_position - bone_world[hip]))
        if upper_len <= 1e-6 or lower_len <= 1e-6:
            return False

        cos_interior = (upper_len * upper_len + lower_len * lower_len - span * span) / (2.0 * upper_len * lower_len)
        bend = math.pi - math.acos(max(-1.0, min(1.0, cos_interior)))
        low = min(float(hinge_link.min_angle[hinge_axis]), float(hinge_link.max_angle[hinge_axis]))
        high = max(float(hinge_link.min_angle[hinge_axis]), float(hinge_link.max_angle[hinge_axis]))
        bend_angle = -bend if low < -1e-6 else bend
        bend_angle = max(low, min(high, bend_angle))
        axis_vec = np.zeros(3, dtype=np.float64)
        axis_vec[hinge_axis] = 1.0
        local[knee][:3, :3] = _axis_angle_matrix(axis_vec, bend_angle)[:3, :3]
        self._update_subtree(knee, local, pose, bone_world)

        try:
            inv_hip_pose = np.linalg.inv(pose[hip])
        except np.linalg.LinAlgError:
            return False
        to_effector = (inv_hip_pose @ np.append(pose[effector][:3, 3], 1.0))[:3]
        to_target = (inv_hip_pose @ np.append(target_position, 1.0))[:3]
        delta = _rotation_between(to_effector, to_target, 0.0)
        local[hip][:3, :3] = _orthonormalize_rotation(local[hip][:3, :3] @ delta[:3, :3])
        self._update_subtree(hip, local, pose, bone_world)
        return True

    @staticmethod
    def _link_hinge_axis(link) -> int | None:
        """The single free rotation axis of an angle-limited IK link (knees:
        X free in [-pi, 0], Y/Z locked at 0), or None when the link is not a
        pure hinge. MMD solves limited links as hinges; clamping a general
        CCD rotation through an euler decomposition instead (the previous
        behaviour) stalls the chain and leaves feet visibly short of their
        IK targets on motions with deep knee bends."""
        if not link.has_limits:
            return None
        free = [
            axis
            for axis in range(3)
            if abs(float(link.min_angle[axis])) > 1e-9 or abs(float(link.max_angle[axis])) > 1e-9
        ]
        return free[0] if len(free) == 1 else None

    def _mirror_deform_bones(self, local: np.ndarray, pose: np.ndarray, bone_world: np.ndarray) -> None:
        scene = self.scene_data
        if not scene or not self._deform_mirror_pairs:
            return

        for source_index, deform_index in self._deform_mirror_pairs:
            pose[deform_index] = pose[source_index].copy()
            parent = scene.bones[deform_index].parent
            if 0 <= parent < len(scene.bones):
                local[deform_index] = np.linalg.inv(pose[parent]) @ pose[deform_index]
            else:
                local[deform_index] = pose[deform_index]
            bone_world[deform_index] = pose[deform_index][:3, 3]
            self._update_descendants(deform_index, local, pose, bone_world)

    def _apply_ik(self, local: np.ndarray, pose: np.ndarray, bone_world: np.ndarray) -> tuple[int, int, str]:
        scene = self.scene_data
        if not scene:
            return 0, 0, ""

        ik_states = sample_ik_enabled(scene.property_frames, self.current_frame)
        active_count = 0
        enabled_count = 0
        first_warning = ""
        for ik_bone_index, bone in enumerate(scene.bones):
            ik = bone.ik
            if ik is None:
                continue
            enabled = ik_states.get(bone.name, ik_states.get(bone.english_name, True))
            if not enabled:
                continue
            enabled_count += 1
            if not (0 <= ik.target_index < len(scene.bones)):
                if not first_warning:
                    first_warning = f'IK "{bone.name}" has invalid effector {ik.target_index}'
                continue

            active_count += 1
            target_position = pose[ik_bone_index][:3, 3].copy()
            iteration_count = max(1, min(int(ik.iterations or 1), 64))
            max_angle = float(ik.limit_radian or 0.5)
            # Accumulated hinge angle per limited link (MMD plane solve). The
            # start value absorbs any FK rotation the motion keyed on the link.
            hinge_angles: dict[int, float] = {}

            # The standard MMD leg — exactly [knee(hinge), hip(free)] — has a
            # closed-form solution: triangle bend at the knee, one exact aim
            # at the hip. CCD on this chain is a tarpit: the straight-leg
            # configuration is a zero-gradient local minimum (the effector
            # overshoots THROUGH the target while every projected angle reads
            # ~0), and with large per-iteration angle limits (this model
            # ships 2.0 rad) the hip and knee oscillate and can end mid-swing
            # at the iteration cap. The analytic path is exact, stable, and
            # preserves the hip's FK twist (minimal-rotation aim), which
            # decides where the knee points.
            if self._solve_two_bone_chain(ik, local, pose, bone_world, target_position):
                final_distance = float(np.linalg.norm(pose[ik.target_index][:3, 3] - target_position))
                solver_miss = final_distance - self._chain_deficit(ik, pose, bone_world, target_position)
                if not first_warning and solver_miss > max(0.05, self._scene_extent * 0.015):
                    first_warning = f'IK "{bone.name}" ended {final_distance:.3f} units from target'
                continue

            # Analytic hinge seed (law of cosines) for other hinge-bearing
            # chains: puts them inside the CCD convergence basin instead of
            # the straight-chain local minimum.
            for seed_position, link in enumerate(ik.links):
                seed_axis = self._link_hinge_axis(link)
                if seed_axis is None or seed_position + 1 >= len(ik.links):
                    continue
                link_index = link.bone_index
                parent_index = ik.links[seed_position + 1].bone_index
                if not (0 <= link_index < len(scene.bones) and 0 <= parent_index < len(scene.bones)):
                    continue
                upper_len = float(np.linalg.norm(bone_world[link_index] - bone_world[parent_index]))
                lower_len = float(np.linalg.norm(pose[ik.target_index][:3, 3] - bone_world[link_index]))
                span = float(np.linalg.norm(target_position - bone_world[parent_index]))
                if upper_len <= 1e-6 or lower_len <= 1e-6:
                    continue
                cos_interior = (upper_len * upper_len + lower_len * lower_len - span * span) / (2.0 * upper_len * lower_len)
                bend = math.pi - math.acos(max(-1.0, min(1.0, cos_interior)))
                low = min(float(link.min_angle[seed_axis]), float(link.max_angle[seed_axis]))
                high = max(float(link.min_angle[seed_axis]), float(link.max_angle[seed_axis]))
                seed = -bend if low < -1e-6 else bend
                seed = max(low, min(high, seed))
                axis_vec = np.zeros(3, dtype=np.float64)
                axis_vec[seed_axis] = 1.0
                hinge_angles[link_index] = seed
                local[link_index][:3, :3] = _axis_angle_matrix(axis_vec, seed)[:3, :3]
                self._update_subtree(link_index, local, pose, bone_world)

            for _ in range(iteration_count):
                moved = False
                for link in ik.links:
                    link_index = link.bone_index
                    if not (0 <= link_index < len(scene.bones)):
                        if not first_warning:
                            first_warning = f'IK "{bone.name}" has invalid link {link_index}'
                        continue

                    effector_position = pose[ik.target_index][:3, 3]
                    try:
                        inv_link_pose = np.linalg.inv(pose[link_index])
                    except np.linalg.LinAlgError:
                        if not first_warning:
                            first_warning = f'IK "{bone.name}" has a singular link matrix on {scene.bones[link_index].name}'
                        continue

                    to_effector = (inv_link_pose @ np.append(effector_position, 1.0))[:3]
                    to_target = (inv_link_pose @ np.append(target_position, 1.0))[:3]

                    hinge_axis = self._link_hinge_axis(link)
                    if hinge_axis is not None:
                        # MMD-style plane solve: rotate only about the free
                        # axis, accumulating the clamped hinge angle.
                        axis_vec = np.zeros(3, dtype=np.float64)
                        axis_vec[hinge_axis] = 1.0
                        ve = to_effector - axis_vec * float(np.dot(to_effector, axis_vec))
                        vt = to_target - axis_vec * float(np.dot(to_target, axis_vec))
                        if float(np.linalg.norm(ve)) <= 1e-8 or float(np.linalg.norm(vt)) <= 1e-8:
                            continue
                        ve /= np.linalg.norm(ve)
                        vt /= np.linalg.norm(vt)
                        theta = math.atan2(
                            float(np.dot(np.cross(ve, vt), axis_vec)),
                            float(np.clip(np.dot(ve, vt), -1.0, 1.0)),
                        )
                        if max_angle > 0:
                            theta = max(-max_angle, min(max_angle, theta))
                        if abs(theta) <= 1e-7:
                            continue
                        phi = hinge_angles.get(link_index)
                        if phi is None:
                            phi = _matrix_to_euler_xyz(local[link_index][:3, :3])[hinge_axis]
                        # LINEAR clamp of the accumulated hinge angle — the
                        # wrap-aware _clamp_angle is for decomposed eulers; on
                        # an accumulated angle its ±2π candidates can "wrap" a
                        # large positive swing onto the far boundary and pin a
                        # knee fully folded at -180°.
                        low = min(float(link.min_angle[hinge_axis]), float(link.max_angle[hinge_axis]))
                        high = max(float(link.min_angle[hinge_axis]), float(link.max_angle[hinge_axis]))
                        new_phi = max(low, min(high, phi + theta))
                        if abs(new_phi - phi) <= 1e-9 and hinge_angles.get(link_index) is not None:
                            continue
                        hinge_angles[link_index] = new_phi
                        local[link_index][:3, :3] = _axis_angle_matrix(axis_vec, new_phi)[:3, :3]
                        self._update_subtree(link_index, local, pose, bone_world)
                        moved = True
                        continue

                    delta = _rotation_between(to_effector, to_target, max_angle)
                    if np.allclose(delta, np.eye(4), atol=1e-7):
                        continue

                    local[link_index][:3, :3] = _orthonormalize_rotation(local[link_index][:3, :3] @ delta[:3, :3])
                    self._clamp_link_rotation(link_index, link, local)
                    self._update_subtree(link_index, local, pose, bone_world)
                    moved = True

                if np.linalg.norm(pose[ik.target_index][:3, 3] - target_position) <= 1e-4:
                    break
                if not moved:
                    break

            final_distance = float(np.linalg.norm(pose[ik.target_index][:3, 3] - target_position))
            solver_miss = final_distance - self._chain_deficit(ik, pose, bone_world, target_position)
            if not first_warning and solver_miss > max(0.05, self._scene_extent * 0.015):
                first_warning = f'IK "{bone.name}" ended {final_distance:.3f} units from target'
        return active_count, enabled_count, first_warning

    def _skinned_vertices(self, skin_matrices: np.ndarray | None, morphed_positions: np.ndarray | None) -> np.ndarray | None:
        if (
            skin_matrices is None
            or morphed_positions is None
            or self._bone_indices is None
            or self._bone_weights is None
            or len(morphed_positions) == 0
        ):
            return None

        pos_h = np.concatenate([morphed_positions, np.ones((len(morphed_positions), 1), dtype=np.float64)], axis=1)
        skinned = np.zeros((len(morphed_positions), 3), dtype=np.float64)
        bone_count = skin_matrices.shape[0]
        for slot in range(4):
            indices = self._bone_indices[:, slot]
            weights = self._bone_weights[:, slot]
            valid = (indices >= 0) & (indices < bone_count) & (weights > 1e-6)
            if not np.any(valid):
                continue
            transformed = np.einsum("nij,nj->ni", skin_matrices[indices[valid]], pos_h[valid])
            skinned[valid] += transformed[:, :3] * weights[valid, None]
        return skinned

    def _evaluate_frame(self) -> tuple[np.ndarray | None, np.ndarray | None, int, int]:
        key = round(float(self.current_frame), 3)
        cached = self._frame_cache.get(key)
        if cached is not None:
            self._frame_cache.move_to_end(key)
            self._enabled_ik_count = cached[4]
            self._first_ik_warning = cached[5]
            return cached[0], cached[1], cached[2], cached[3]

        morphed_positions, active_morphs = self._morphed_positions(self.current_frame)
        skin_matrices, bone_world, active_ik, enabled_ik, ik_warning = self._pose_matrices()
        skinned = self._skinned_vertices(skin_matrices, morphed_positions)
        self._enabled_ik_count = enabled_ik
        self._first_ik_warning = ik_warning
        result = (skinned, bone_world, active_morphs, active_ik, enabled_ik, ik_warning)
        self._frame_cache[key] = result
        while len(self._frame_cache) > 16:
            self._frame_cache.popitem(last=False)
        return skinned, bone_world, active_morphs, active_ik

    def initializeGL(self) -> None:
        if GL:
            GL.glClearColor(0.08, 0.09, 0.10, 1.0)
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glDisable(GL.GL_CULL_FACE)

    def paintGL(self) -> None:
        self._update_fps()
        skinned, bone_world, self._active_morph_count, self._active_ik_count = self._evaluate_frame()

        if GL:
            try:
                viewport_width, viewport_height = self._gl_viewport_size()
                GL.glViewport(0, 0, viewport_width, viewport_height)
                GL.glClearColor(0.08, 0.09, 0.10, 1.0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
                if skinned is not None:
                    self._draw_mesh_gl(skinned)
            except Exception as exc:
                self._gl_error = str(exc)

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if not GL:
            painter.fillRect(self.rect(), QtGui.QColor(22, 24, 27))
            painter.setPen(QtGui.QColor(230, 230, 230))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "PyOpenGL is required for mesh preview rendering.")
        elif self._gl_error:
            painter.fillRect(self.rect(), QtGui.QColor(22, 24, 27))
            painter.setPen(QtGui.QColor(255, 210, 120))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "OpenGL preview is unavailable in this context.")
        if self._show_bone_overlay or self._show_bone_names:
            self._draw_skeleton(painter, bone_world)
        if self._show_debug_center_marker:
            self._draw_center_marker(painter)
        self._draw_overlay(painter)
        painter.end()
        self._emit_stats()

    def _gl_viewport_size(self) -> tuple[int, int]:
        dpr = max(1.0, float(self.devicePixelRatio()))
        return max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr)))

    def _release_gl_resources(self) -> None:
        """Delete previously allocated GL buffers/textures. Called before a
        realloc so reloading a scene does not leak the old GPU objects."""
        if not GL:
            self._vao, self._vbo, self._ibo, self._texture_ids = 0, 0, 0, {}
            return
        try:
            tex_list = [t for t in (self._texture_ids or {}).values() if t]
            if tex_list and hasattr(GL, "glDeleteTextures"):
                GL.glDeleteTextures(tex_list)
        except Exception:
            pass
        for handle, deleter in ((self._vbo, "glDeleteBuffers"), (self._ibo, "glDeleteBuffers")):
            if handle and hasattr(GL, deleter):
                try:
                    getattr(GL, deleter)(1, [handle])
                except Exception:
                    pass
        if self._vao and hasattr(GL, "glDeleteVertexArrays"):
            try:
                GL.glDeleteVertexArrays(1, [self._vao])
            except Exception:
                pass
        self._vao, self._vbo, self._ibo, self._texture_ids = 0, 0, 0, {}

    def _ensure_gl_resources(self) -> bool:
        if not GL or self._gl_ready or not self.scene_data or not self.scene_data.mesh or self._indices is None:
            return self._gl_ready

        # Free any resources left over from a previously loaded scene first.
        self._release_gl_resources()

        try:
            if hasattr(GL, "glGenVertexArrays"):
                try:
                    self._vao = GL.glGenVertexArrays(1)
                    GL.glBindVertexArray(self._vao)
                except Exception:
                    self._vao = 0
            self._use_client_arrays = False
            if hasattr(GL, "glGenBuffers") and bool(GL.glGenBuffers):
                self._vbo = GL.glGenBuffers(1)
                self._ibo = GL.glGenBuffers(1)
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
                GL.glBufferData(GL.GL_ARRAY_BUFFER, int(self._vertex_data.nbytes if self._vertex_data is not None else 0), None, GL.GL_DYNAMIC_DRAW)
                GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self._ibo)
                GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, self._indices.astype(np.uint32).nbytes, self._indices.astype(np.uint32), GL.GL_STATIC_DRAW)
            else:
                self._use_client_arrays = True

            self._texture_ids = {}
            for material in self.scene_data.mesh.materials:
                if material.texture_path and material.texture_path.exists() and material.texture_path not in self._texture_ids:
                    tex_id = self._create_texture(material.texture_path)
                    if tex_id:
                        self._texture_ids[material.texture_path] = tex_id

            if self._vao and hasattr(GL, "glBindVertexArray"):
                GL.glBindVertexArray(0)
            self._gl_ready = True
            self._gl_error = ""
        except Exception as exc:
            self._gl_error = str(exc)
            self._gl_ready = False
        return self._gl_ready

    def _create_texture(self, path: Path) -> int:
        if not hasattr(GL, "glGenTextures") or not bool(GL.glGenTextures):
            return 0
        image = QtGui.QImage(str(path))
        if image.isNull():
            return 0
        image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
        try:
            tex_id = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_REPEAT)
            bits = image.constBits()
            data = bits.tobytes() if hasattr(bits, "tobytes") else bytes(bits)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, image.width(), image.height(), 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            return int(tex_id)
        except Exception:
            return 0

    def _draw_mesh_gl(self, skinned: np.ndarray) -> None:
        scene = self.scene_data
        if not scene or not scene.mesh or self._vertex_data is None or self._indices is None:
            return
        if not self._ensure_gl_resources():
            return

        self._vertex_data[:, 0:3] = skinned.astype(np.float32)
        self._vertex_data = np.ascontiguousarray(self._vertex_data, dtype=np.float32)
        if not self._use_client_arrays:
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
            GL.glBufferSubData(GL.GL_ARRAY_BUFFER, 0, self._vertex_data.nbytes, self._vertex_data)

        if self._vao and hasattr(GL, "glBindVertexArray"):
            GL.glBindVertexArray(self._vao)
        if self._use_client_arrays:
            if hasattr(GL, "glBindBuffer") and bool(GL.glBindBuffer):
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)
                GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, 0)
        else:
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self._ibo)

        projection, view = self._projection_view_matrices()
        GL.glMatrixMode(GL.GL_PROJECTION)
        self._load_gl_matrix(projection)
        GL.glMatrixMode(GL.GL_MODELVIEW)
        self._load_gl_matrix(view)

        stride = 8 * 4
        GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
        base_ptr = int(self._vertex_data.ctypes.data) if self._use_client_arrays else 0
        GL.glVertexPointer(3, GL.GL_FLOAT, stride, ctypes.c_void_p(base_ptr))
        GL.glEnableClientState(GL.GL_NORMAL_ARRAY)
        GL.glNormalPointer(GL.GL_FLOAT, stride, ctypes.c_void_p(base_ptr + 12))
        GL.glEnableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glTexCoordPointer(2, GL.GL_FLOAT, stride, ctypes.c_void_p(base_ptr + 24))
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glEnable(GL.GL_TEXTURE_2D)
        GL.glTexEnvi(GL.GL_TEXTURE_ENV, GL.GL_TEXTURE_ENV_MODE, GL.GL_MODULATE)

        for material_index, start, count in self._material_ranges:
            material = scene.mesh.materials[material_index]
            color = material.diffuse
            GL.glColor4f(float(color[0]), float(color[1]), float(color[2]), 1.0)
            tex_id = self._texture_ids.get(material.texture_path) if material.texture_path else None
            if tex_id:
                if hasattr(GL, "glActiveTexture") and bool(GL.glActiveTexture):
                    GL.glActiveTexture(GL.GL_TEXTURE0)
                GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
            else:
                GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            if self._use_client_arrays:
                GL.glDrawElements(GL.GL_TRIANGLES, int(count), GL.GL_UNSIGNED_INT, self._indices[int(start) : int(start) + int(count)])
            else:
                GL.glDrawElements(GL.GL_TRIANGLES, int(count), GL.GL_UNSIGNED_INT, ctypes.c_void_p(int(start) * 4))

        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisableClientState(GL.GL_NORMAL_ARRAY)
        GL.glDisableClientState(GL.GL_VERTEX_ARRAY)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        if self._vao and hasattr(GL, "glBindVertexArray"):
            GL.glBindVertexArray(0)

    def _load_gl_matrix(self, matrix: np.ndarray) -> None:
        # PyOpenGL passes ndarray memory through to OpenGL's column-major
        # glLoadMatrixf. A contiguous transpose keeps the CPU MVP convention
        # aligned with GL without putting the orthographic translation in the
        # clip-space w row.
        GL.glLoadMatrixf(np.ascontiguousarray(matrix.T, dtype=np.float32))

    def _camera_projection_view(self) -> tuple[np.ndarray, np.ndarray] | None:
        """Perspective look-through matrices for the MMD camera at the current
        frame, or None when no camera motion is loaded.

        The right vector uses the left-handed cross product (up x forward)
        because PMX/VMD data is left-handed and rendered here as-is; this keeps
        the camera framing un-mirrored, matching MMD and the in-game camera.
        """
        scene = self.scene_data
        if not self._follow_camera or not scene or not scene.camera_frames:
            return None

        target, rotation, distance, fov = import_vmd.sample_camera_frames(scene.camera_frames, self.current_frame)
        eye, forward, up = import_vmd.mmd_camera_pose(target, rotation, distance)
        eye = np.array(eye, dtype=np.float64)
        f = np.array(forward, dtype=np.float64)
        f /= max(1e-9, np.linalg.norm(f))
        u = np.array(up, dtype=np.float64)
        s = np.cross(u, f)  # left-handed: screen-right
        s /= max(1e-9, np.linalg.norm(s))
        u = np.cross(f, s)
        u /= max(1e-9, np.linalg.norm(u))

        view = np.identity(4, dtype=np.float64)
        view[0, :3] = s
        view[1, :3] = u
        view[2, :3] = -f
        view[0, 3] = -np.dot(s, eye)
        view[1, 3] = -np.dot(u, eye)
        view[2, 3] = np.dot(f, eye)

        width = max(1, self.width())
        height = max(1, self.height())
        aspect = width / height
        near = max(0.05, self._scene_extent * 0.01)
        far = max(4000.0, self._scene_extent * 40.0)
        t = 1.0 / math.tan(math.radians(max(1.0, min(179.0, fov))) * 0.5)
        proj = np.array(
            [
                [t / aspect, 0.0, 0.0, 0.0],
                [0.0, t, 0.0, 0.0],
                [0.0, 0.0, -(far + near) / (far - near), -2.0 * far * near / (far - near)],
                [0.0, 0.0, -1.0, 0.0],
            ],
            dtype=np.float64,
        )
        return proj, view

    def _projection_view_matrices(self) -> tuple[np.ndarray, np.ndarray]:
        camera = self._camera_projection_view()
        if camera is not None:
            return camera

        width = max(1, self.width())
        height = max(1, self.height())
        aspect = width / height
        view_height = self._scene_extent / max(0.001, self._view_zoom * 0.78)
        view_width = view_height * aspect
        pixel_scale = min(width, height) * 0.78 * self._view_zoom / max(0.001, self._scene_extent)
        pan_x = -self._view_pan.x() / max(0.001, pixel_scale)
        pan_y = self._view_pan.y() / max(0.001, pixel_scale)
        left = -view_width * 0.5 + pan_x
        right = view_width * 0.5 + pan_x
        bottom = -view_height * 0.5 + pan_y
        top = view_height * 0.5 + pan_y
        near = -self._scene_extent * 4.0
        far = self._scene_extent * 4.0

        proj = np.array(
            [
                [2.0 / (right - left), 0.0, 0.0, -(right + left) / (right - left)],
                [0.0, 2.0 / (top - bottom), 0.0, -(top + bottom) / (top - bottom)],
                [0.0, 0.0, -2.0 / (far - near), -(far + near) / (far - near)],
                [0.0, 0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )

        cy, sy = math.cos(self._view_yaw), math.sin(self._view_yaw)
        cp, sp = math.cos(self._view_pitch), math.sin(self._view_pitch)
        yaw = np.array([[cy, 0.0, sy, 0.0], [0.0, 1.0, 0.0, 0.0], [-sy, 0.0, cy, 0.0], [0.0, 0.0, 0.0, 1.0]], dtype=np.float64)
        pitch = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, cp, -sp, 0.0], [0.0, sp, cp, 0.0], [0.0, 0.0, 0.0, 1.0]], dtype=np.float64)
        view = pitch @ yaw @ _translation_matrix(-self._scene_center)
        return proj, view

    def _project_np(self, positions: np.ndarray) -> tuple[list[QtCore.QPointF], np.ndarray]:
        if len(positions) == 0:
            return [], np.zeros(0, dtype=np.float64)

        projection, view = self._projection_view_matrices()
        mvp = projection @ view
        homogeneous = np.concatenate([positions[:, :3], np.ones((len(positions), 1), dtype=np.float64)], axis=1)
        clip = (mvp @ homogeneous.T).T
        w = clip[:, 3]
        safe_w = np.where(np.abs(w) <= 1e-8, 1.0, w)
        ndc = clip[:, :3] / safe_w[:, None]
        # Points behind a perspective camera have negative clip-space w; the
        # division mirrors them onto the screen. Push them far offscreen and
        # behind the depth range so overlays never draw ghost bones.
        behind = w <= 1e-8
        if behind.any():
            ndc[behind, 0] = -1e6
            ndc[behind, 1] = -1e6
            ndc[behind, 2] = 2.0
        x = self.width() * (0.5 + ndc[:, 0] * 0.5)
        y = self.height() * (0.5 - ndc[:, 1] * 0.5)
        return [QtCore.QPointF(float(px), float(py)) for px, py in zip(x, y)], ndc[:, 2]

    def _project_point(self, pos: np.ndarray) -> QtCore.QPointF:
        point, _ = self._project_np(pos.reshape(1, 3))
        return point[0]

    def _draw_skeleton(self, painter: QtGui.QPainter, bone_world: np.ndarray | None) -> None:
        scene = self.scene_data
        if not scene or not scene.bones:
            painter.setPen(QtGui.QColor(230, 230, 230))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "Load a PMX model and VMD motion, then press Preview.")
            return
        if bone_world is None:
            bone_world = np.array([bone.position for bone in scene.bones], dtype=np.float64)

        points, _ = self._project_np(np.asarray(bone_world[: len(scene.bones)], dtype=np.float64))
        if self._show_bone_overlay:
            painter.setPen(QtGui.QPen(QtGui.QColor(80, 170, 255, 170), 1))
            for index, bone in enumerate(scene.bones):
                if 0 <= bone.parent < len(points):
                    painter.drawLine(points[bone.parent], points[index])
            painter.setBrush(QtGui.QColor(245, 245, 245, 180))
            painter.setPen(QtGui.QPen(QtGui.QColor(20, 20, 20, 160), 1))
            for point in points[:: max(1, len(points) // 90)]:
                painter.drawEllipse(point, 2.0, 2.0)

        if self._show_bone_names:
            old_font = painter.font()
            font = QtGui.QFont(old_font)
            font.setPointSize(max(7, old_font.pointSize() - 1))
            painter.setFont(font)
            painter.setPen(QtGui.QColor(235, 235, 235, 220))
            visible_rect = self.rect().adjusted(-80, -20, 160, 40)
            for index, bone in enumerate(scene.bones):
                point = points[index]
                if not visible_rect.contains(point.toPoint()):
                    continue
                name = bone.name or bone.english_name or str(index)
                painter.drawText(QtCore.QPointF(point.x() + 5.0, point.y() - 3.0), name)
            painter.setFont(old_font)

    def _draw_center_marker(self, painter: QtGui.QPainter) -> None:
        center = self._project_point(self._scene_center)
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 90, 90, 210), 1))
        painter.drawLine(QtCore.QPointF(center.x() - 8.0, center.y()), QtCore.QPointF(center.x() + 8.0, center.y()))
        painter.drawLine(QtCore.QPointF(center.x(), center.y() - 8.0), QtCore.QPointF(center.x(), center.y() + 8.0))

    def _draw_overlay(self, painter: QtGui.QPainter) -> None:
        scene = self.scene_data
        painter.setPen(QtGui.QColor(235, 235, 235))
        if not scene:
            return
        mesh = scene.mesh
        vertex_count = len(mesh.vertices) if mesh else 0
        triangle_count = int(sum(count for _, _, count in self._material_ranges) / 3) if mesh else 0
        hidden_text = f" | hidden materials {self._hidden_material_count}" if self._hidden_material_count else ""
        backend = "OpenGL" if GL and self._gl_error == "" else ("OpenGL error" if self._gl_error else "No OpenGL")
        text = (
            f"{PREVIEW_GMOD_MODEL_LABEL} | frame {int(self.current_frame)} / {scene.frame_end} | "
            f"{len(scene.bones)} bones | {vertex_count} verts | {triangle_count} tris | "
            f"{scene.flex_count} morph tracks{hidden_text} | active morphs {self._active_morph_count} | "
            f"active IK {self._active_ik_count} | enabled IK {self._enabled_ik_count} | {backend}"
        )
        painter.drawText(12, 22, text)
        if self._first_ik_warning:
            painter.setPen(QtGui.QColor(255, 210, 120))
            painter.drawText(12, 44, self._first_ik_warning)
        elif scene.warnings:
            painter.setPen(QtGui.QColor(255, 210, 120))
            painter.drawText(12, 44, scene.warnings[0] + (f" (+{len(scene.warnings) - 1} more)" if len(scene.warnings) > 1 else ""))

    def _update_fps(self) -> None:
        self._fps_counter += 1
        now = time.monotonic()
        elapsed = now - self._fps_started
        if elapsed >= 0.5:
            self._preview_fps = self._fps_counter / elapsed
            self._fps_counter = 0
            self._fps_started = now

    def _emit_stats(self) -> None:
        scene = self.scene_data
        if not scene:
            self.statsChanged.emit("Preview idle")
            return
        backend = "OpenGL" if GL and self._gl_error == "" else ("OpenGL error: " + self._gl_error if self._gl_error else "PyOpenGL unavailable")
        self.statsChanged.emit(
            f"{backend} | {self._preview_fps:.1f} FPS | active morphs {self._active_morph_count} | "
            f"active IK {self._active_ik_count} | enabled IK {self._enabled_ik_count} | warnings {len(scene.warnings)}"
            + (f" | {self._first_ik_warning}" if self._first_ik_warning else "")
        )

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self._last_mouse_pos = event.position().toPoint()
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        elif event.button() in (QtCore.Qt.MouseButton.RightButton, QtCore.Qt.MouseButton.MiddleButton):
            self.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._last_mouse_pos is None:
            self._last_mouse_pos = event.position().toPoint()
            return

        current = event.position().toPoint()
        delta = current - self._last_mouse_pos
        self._last_mouse_pos = current

        buttons = event.buttons()
        if buttons & QtCore.Qt.MouseButton.LeftButton:
            self._view_yaw += delta.x() * 0.008
            self._view_pitch = max(-1.45, min(1.45, self._view_pitch + delta.y() * 0.008))
            self.update()
        elif buttons & (QtCore.Qt.MouseButton.RightButton | QtCore.Qt.MouseButton.MiddleButton):
            self._view_pan += QtCore.QPointF(delta.x(), delta.y())
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if not event.buttons():
            self._last_mouse_pos = None
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.reset_front_view()
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta:
            self._view_zoom = max(0.2, min(8.0, self._view_zoom * math.pow(1.0015, delta)))
            self.update()
        event.accept()
