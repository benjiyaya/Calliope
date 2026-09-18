/**
 * Shot composition store — Svelte 5 runes port of open-media's composerStore
 * (MIT). The data model is identical: objects with SceneTransform + posture v7
 * + keyframes, shot params, playback, 50-step snapshot undo with drag
 * bracketing.
 *
 * Differences from the reference:
 * - No `objectInstances` Map — the Three.js viewport keeps a *view* registry
 *   (meshes/handles), not a second scene store. Adapters read SceneData via
 *   toSceneData() only.
 * - `scene_json` on the server is the source of truth: local edits autosave
 *   (debounced), and `shot.updated` SSE events reload the composition unless
 *   a drag/gesture is in progress (`dragInProgress` defers, matching the
 *   canvas graph-sync rule).
 */
import * as THREE from 'three';
import {
	clonePosture,
	describePostureError,
	type Posture,
} from './helpers/posture';
import { sampleTrack } from './motion/sampleTrack';
import { normalizeShotParams, type ShotParams } from './calibration/shotSolver';

export type ComposerTool = 'select' | 'move' | 'rotate' | 'scale' | 'pose';
export type CharacterType = 'male' | 'female' | 'child';
export type PrimitiveType = 'cube' | 'plane' | 'cylinder' | 'sphere' | 'capsule' | 'cone' | 'torus';
export type ObjectType = CharacterType | PrimitiveType | 'camera';

export interface SceneTransform {
	position: [number, number, number];
	rotation: [number, number, number];
	scale: [number, number, number];
}

export interface Keyframe {
	id: string;
	time: number;
	transform: SceneTransform;
	posture?: Posture;
	fov?: number;
}

export interface SceneObject {
	id: string;
	name: string;
	type: ObjectType;
	visible: boolean;
	locked: boolean;
	transform: SceneTransform;
	posture?: Posture;
	defaultPosture?: Posture;
	keyframes: Keyframe[];
	liveEditTime: number | null;
	fov?: number;
}

export interface PlaybackState {
	playing: boolean;
	elapsed: number;
	speed: number;
	duration: number;
}

export interface ShotComposition {
	id: number;
	title: string;
	scene: SceneData;
	/** Server clock — used for optimistic concurrency on PATCH. */
	updated_at?: string;
}

export interface CameraKeyframe {
	id: string;
	time: number;
	position: [number, number, number];
	target: [number, number, number];
	fov: number;
}

/** Brief/Cut human gates — live on scene_json.gates. */
export interface BuildSceneGateApproval {
	approved_at: string;
	source?: string;
	note?: string | null;
}

export interface BuildSceneGates {
	brief?: BuildSceneGateApproval | null;
	cut?: BuildSceneGateApproval | null;
}

export interface SceneData {
	objects: SceneObject[];
	selectedObjectId?: string | null;
	shotParams?: Partial<Omit<ShotParams, 'composition'>> & { composition?: string };
	playback?: PlaybackState;
	cameraTrack?: CameraKeyframe[];
	/** Brief/Cut approvals — must round-trip so client autosave does not wipe them. */
	gates?: BuildSceneGates;
}

/** Hard cap: exported blockout videos stay under a minute. */
export const MAX_TIMELINE_DURATION = 60;

const HISTORY_LIMIT = 50;
const KEYFRAME_EPSILON = 1 / 24;

const PRIMITIVE_TYPES: PrimitiveType[] = ['cube', 'plane', 'cylinder', 'sphere', 'capsule', 'cone', 'torus'];

export function isPrimitiveType(type: ObjectType): type is PrimitiveType {
	return (PRIMITIVE_TYPES as string[]).includes(type);
}

export function isCharacterType(type: ObjectType): type is CharacterType {
	return ['male', 'female', 'child'].includes(type);
}

function uid(): string {
	return typeof crypto !== 'undefined' && 'randomUUID' in crypto
		? crypto.randomUUID()
		: `id-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

function findNearKeyframeIndex(keyframes: Keyframe[], time: number): number {
	return keyframes.findIndex((k) => Math.abs(k.time - time) <= KEYFRAME_EPSILON);
}

function upsertKeyframe(
	keyframes: Keyframe[],
	time: number,
	patch: { transform?: Partial<SceneTransform>; posture?: Posture; fov?: number },
	fallback: { transform: SceneTransform; posture?: Posture; fov?: number },
): Keyframe[] {
	const index = findNearKeyframeIndex(keyframes, time);
	if (index >= 0) {
		const existing = keyframes[index];
		const updated: Keyframe = {
			...existing,
			transform: patch.transform ? { ...existing.transform, ...patch.transform } : existing.transform,
			posture: patch.posture ? clonePosture(patch.posture) : existing.posture,
			fov: patch.fov ?? existing.fov,
		};
		const next = [...keyframes];
		next[index] = updated;
		return next;
	}
	const sampled = keyframes.length > 0 ? sampleTrack(keyframes, time) : fallback;
	const seeded: Keyframe = {
		id: uid(),
		time,
		transform: patch.transform ? { ...sampled.transform, ...patch.transform } : sampled.transform,
		posture: patch.posture ? clonePosture(patch.posture) : sampled.posture,
		fov: patch.fov ?? sampled.fov,
	};
	return [...keyframes, seeded].sort((a, b) => a.time - b.time);
}

/** The transform/posture/fov a live edit should build on: staged edit or sampled track. */
export function stageBase(
	o: SceneObject,
	elapsed: number,
): { transform: SceneTransform; posture?: Posture; fov?: number } {
	const isLive = o.liveEditTime !== null && Math.abs(o.liveEditTime - elapsed) < 1e-6;
	if (isLive || o.keyframes.length === 0) {
		return { transform: o.transform, posture: o.posture, fov: o.fov };
	}
	return sampleTrack(o.keyframes, elapsed);
}

function objectName(type: ObjectType, index: number): string {
	return `${type.charAt(0).toUpperCase()}${type.slice(1)} ${String(index).padStart(2, '0')}`;
}

function createInitialObject(): SceneObject {
	const transform: SceneTransform = { position: [0, 0, 0], rotation: [0, 0, 0], scale: [1, 1, 1] };
	return {
		id: uid(),
		name: 'Male 01',
		type: 'male',
		visible: true,
		locked: false,
		transform,
		keyframes: [{ id: uid(), time: 0, transform }],
		liveEditTime: null,
	};
}

function canPoseSelection(objects: SceneObject[], id: string | null): boolean {
	const target = objects.find((o) => o.id === id);
	return !!target && !isPrimitiveType(target.type) && target.type !== 'camera';
}

// ── Svelte 5 runes store ─────────────────────────────────────────────────

function createStore() {
	let objects = $state<SceneObject[]>([]);
	let compositionId = $state<number | null>(null);
	let title = $state('Untitled composition');
	let selectedObjectId = $state<string | null>(null);
	let shotParams = $state<SceneData['shotParams']>({});
	let playback = $state<PlaybackState>({ playing: false, elapsed: 0, speed: 1, duration: 6 });
	let cameraTrack = $state<CameraKeyframe[]>([]);
	let gates = $state<BuildSceneGates>({});
	let loaded = $state(false);

	let history = $state<SceneObject[][]>([]);
	let future = $state<SceneObject[][]>([]);
	let suppressHistory = false;
	let groupDepth = 0;
	let groupRecorded = false;

	let dragInProgress = $state(false);
	let saveTimer: ReturnType<typeof setTimeout> | null = null;
	let lastServerVersion = 0;
	/** Last known server updated_at — sent as base_updated_at on PATCH. */
	let serverUpdatedAt: string | null = null;

	function withoutHistory<T>(fn: () => T): T {
		suppressHistory = true;
		try {
			return fn();
		} finally {
			suppressHistory = false;
		}
	}

	function recordHistory(before: SceneObject[]) {
		if (suppressHistory) return;
		if (groupDepth > 0) {
			if (groupRecorded) return;
			groupRecorded = true;
		}
		history = [...history, before].slice(-HISTORY_LIMIT);
		future = [];
	}

	function commitObjects(next: SceneObject[]) {
		const before = objects;
		objects = next;
		recordHistory(before);
		scheduleSave();
	}

	// ── server sync ─────────────────────────────────────────────────

	async function loadComposition(sessionId: number): Promise<ShotComposition | null> {
		const resp = await fetch(`/api/shots/by-session/${sessionId}`);
		if (!resp.ok) return null;
		const comp = (await resp.json()) as ShotComposition;
		applyServerScene(comp);
		return comp;
	}

	function applyServerScene(comp: ShotComposition) {
		// Cancel pending autosave so a stale local payload cannot clobber agent writes.
		if (saveTimer) {
			clearTimeout(saveTimer);
			saveTimer = null;
		}
		compositionId = comp.id;
		title = comp.title;
		serverUpdatedAt = comp.updated_at ?? serverUpdatedAt;
		const scene = comp.scene ?? {};
		objects = (scene.objects ?? []).map((o) => ({
			...o,
			keyframes: o.keyframes ?? [],
		}));
		selectedObjectId = scene.selectedObjectId ?? objects[0]?.id ?? null;
		// Legacy composition JSON can carry unknown/aliased param ids; restoring
		// those verbatim NaN'd the camera (see normalizeShotParams).
		shotParams = normalizeShotParams(scene.shotParams);
		if (scene.playback) playback = scene.playback;
		cameraTrack = (scene.cameraTrack ?? []).map((k) => ({ ...k }));
		gates = scene.gates ? { ...scene.gates } : {};
		loaded = true;
		history = [];
		future = [];
		lastServerVersion = Date.now();
	}

	function scheduleSave() {
		if (compositionId === null) return;
		if (saveTimer) clearTimeout(saveTimer);
		saveTimer = setTimeout(() => void saveScene(), 600);
	}

	async function saveScene() {
		if (compositionId === null) return;
		const payload = JSON.stringify(toSceneData());
		lastServerVersion = Date.now();
		try {
			const resp = await fetch(`/api/shots/${compositionId}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					scene_json: payload,
					...(serverUpdatedAt ? { base_updated_at: serverUpdatedAt } : {}),
				}),
			});
			if (resp.status === 409) {
				// Stale vs agent write — reload authoritative scene.
				const fresh = await fetch(`/api/shots/${compositionId}`);
				if (fresh.ok) applyServerScene((await fresh.json()) as ShotComposition);
				return;
			}
			if (resp.ok) {
				const body = (await resp.json()) as ShotComposition;
				if (body.updated_at) serverUpdatedAt = body.updated_at;
			}
		} catch {
			/* offline — next edit retries */
		}
	}

	function toSceneData(): SceneData {
		return {
			objects: objects.map((o) => ({ ...o })),
			selectedObjectId,
			shotParams,
			playback,
			cameraTrack,
			gates,
		} as SceneData;
	}

	/** SSE: the agent mutated the scene server-side. Ignore our own PATCH echo. */
	function handleShotUpdated(data: { shot_id?: number; reason?: string }) {
		if (data.shot_id !== compositionId) return;
		if (dragInProgress) return; // a gizmo drag wins; the refetch happens on dragstop
		if (compositionId === null) return;
		const reason = data.reason ?? '';
		// Our own PATCH publishes reason=patched — skip that echo. Always apply
		// agent/tool reasons (object_*, gate_*, …) so remote edits land even
		// when an autosave just ran.
		if (reason === 'patched' || reason === 'created') return;
		if (!reason && Date.now() - lastServerVersion < 1500) return;
		void (async () => {
			const resp = await fetch(`/api/shots/${compositionId}`);
			if (!resp.ok) return;
			applyServerScene((await resp.json()) as ShotComposition);
		})();
	}

	// ── actions ─────────────────────────────────────────────────────

	function addObject(type: ObjectType) {
		const position: [number, number, number] = [objects.length * 1.5, 0, 0];
		const rotation: [number, number, number] = [0, 0, 0];
		const transform: SceneTransform = { position, rotation, scale: [1, 1, 1] };
		const object: SceneObject = {
			id: uid(),
			name: objectName(type, objects.length + 1),
			type,
			visible: true,
			locked: false,
			transform,
			fov: type === 'camera' ? 10 : undefined,
			keyframes: type === 'camera' ? [] : [{ id: uid(), time: 0, transform }],
			liveEditTime: null,
		};
		commitObjects([...objects, object]);
		selectedObjectId = object.id;
		selectedKeyframeId = null;
		activeTool = 'move';
	}

	let selectedKeyframeId = $state<string | null>(null);
	let activeTool = $state<ComposerTool>('move');

	function deleteObject(id: string) {
		const remaining = objects.filter((o) => o.id !== id);
		const nextSelected =
			selectedObjectId === id ? (remaining.length > 0 ? remaining[0].id : null) : selectedObjectId;
		const poseStillValid = activeTool !== 'pose' || canPoseSelection(remaining, nextSelected);
		commitObjects(remaining);
		selectedObjectId = nextSelected;
		if (selectedObjectId === id) selectedKeyframeId = null;
		if (!poseStillValid) {
			activeTool = 'move';
		}
	}

	function clearScene() {
		commitObjects([]);
		selectedObjectId = null;
		selectedKeyframeId = null;
		activeTool = 'move';
		playback = { ...playback, playing: false, elapsed: 0 };
		cameraTrack = [];
		// Keep Brief/Cut gates — approvals survive wipe unless operator revises.
		scheduleSave();
	}

	function selectObject(id: string) {
		if (selectedObjectId === id && selectedKeyframeId === null) return;
		if (activeTool === 'pose' && !canPoseSelection(objects, id)) {
			selectedObjectId = id;
			selectedKeyframeId = null;
			activeTool = 'move';
			return;
		}
		selectedObjectId = id;
		selectedKeyframeId = null;
	}

	function clearSelection() {
		selectedObjectId = null;
		selectedKeyframeId = null;
	}

	function renameObject(id: string, name: string) {
		commitObjects(objects.map((o) => (o.id === id ? { ...o, name } : o)));
	}

	function toggleObjectVisibility(id: string) {
		commitObjects(objects.map((o) => (o.id === id ? { ...o, visible: !o.visible } : o)));
	}

	function toggleObjectLock(id: string) {
		commitObjects(objects.map((o) => (o.id === id ? { ...o, locked: !o.locked } : o)));
	}

	function updateObjectTransform(id: string, transform: Partial<SceneTransform>) {
		const time = playback.elapsed;
		commitObjects(
			objects.map((o) => {
				if (o.id !== id) return o;
				const base = stageBase(o, time);
				return {
					...o,
					transform: { ...base.transform, ...transform },
					posture: base.posture,
					fov: base.fov,
					liveEditTime: time,
				};
			}),
		);
	}

	function updateObjectPosture(id: string, posture: Posture) {
		const error = describePostureError(posture);
		if (error) {
			console.error(`[shotStore] rejected posture for ${id}: ${error}`);
			return;
		}
		const time = playback.elapsed;
		commitObjects(
			objects.map((o) => {
				if (o.id !== id) return o;
				const base = stageBase(o, time);
				return { ...o, transform: base.transform, posture: clonePosture(posture), liveEditTime: time };
			}),
		);
	}

	function updateObjectDefaultPosture(id: string, posture: Posture) {
		withoutHistory(() => {
			objects = objects.map((o) => (o.id === id ? { ...o, defaultPosture: clonePosture(posture) } : o));
		});
	}

	function updateObjectFov(id: string, fov: number) {
		const time = playback.elapsed;
		commitObjects(
			objects.map((o) => {
				if (o.id !== id) return o;
				const base = stageBase(o, time);
				return { ...o, transform: base.transform, posture: base.posture, fov, liveEditTime: time };
			}),
		);
	}

	function setActiveTool(tool: ComposerTool) {
		if (tool === 'pose' && !canPoseSelection(objects, selectedObjectId)) return;
		activeTool = tool;
	}

	function setShotParams(next: SceneData['shotParams']) {
		shotParams = { ...shotParams, ...next };
		scheduleSave();
	}

	function setPlaying(playing: boolean) {
		playback = { ...playback, playing };
	}
	function setElapsed(elapsed: number) {
		playback = { ...playback, elapsed };
	}
	function setSpeed(speed: number) {
		playback = { ...playback, speed };
	}
	function setDuration(duration: number) {
		const clamped = Math.min(Math.max(duration, 1), MAX_TIMELINE_DURATION);
		playback = { ...playback, duration: clamped, elapsed: Math.min(playback.elapsed, clamped) };
		scheduleSave();
	}

	// ── object keyframes (motion) ───────────────────────────────────
	// Port of composerStore's addKeyframe/commitLiveKeyframe/deleteKeyframe/
	// duplicateKeyframe/moveKeyframeTime/updateKeyframeTransform/selectKeyframe.
	// A keyframe pins {transform, posture, fov} at a time; playback samples the
	// track per frame (see ShotViewport's applyTrackSampling).

	/** Upserts a keyframe at `time`, seeding unsampled fields from the object's current state. */
	function addKeyframe(objectId: string, time: number) {
		commitObjects(
			objects.map((o) =>
				o.id === objectId
					? {
							...o,
							keyframes: upsertKeyframe(o.keyframes, time, {}, {
								transform: o.transform,
								posture: o.posture,
								fov: o.fov,
							}),
						}
					: o,
			),
		);
		playback = { ...playback, duration: Math.max(playback.duration, time) };
		scheduleSave();
	}

	/** Commits the object's current staged (live) state as a keyframe after the last one. */
	function commitLiveKeyframe(objectId: string) {
		const target = objects.find((o) => o.id === objectId);
		if (!target) return;
		const time = playback.elapsed;
		const base = stageBase(target, time);
		// The first keyframe ever committed lands where the user is parked, not +1s.
		const newTime = target.keyframes.length
			? Math.max(target.keyframes[target.keyframes.length - 1].time + 1, time)
			: time;
		const keyframe: Keyframe = {
			id: uid(),
			time: newTime,
			transform: { ...base.transform },
			posture: base.posture ? clonePosture(base.posture) : undefined,
			fov: base.fov,
		};
		commitObjects(
			objects.map((o) =>
				o.id === objectId ? { ...o, keyframes: [...o.keyframes, keyframe].sort((a, b) => a.time - b.time) } : o,
			),
		);
		selectedKeyframeId = null;
		playback = {
			...playback,
			elapsed: newTime,
			playing: false,
			duration: Math.max(playback.duration, newTime),
		};
	}

	function deleteKeyframe(objectId: string, keyframeId: string) {
		const target = objects.find((o) => o.id === objectId);
		if (!target || target.keyframes.length <= 1) return; // a track keeps ≥1 anchor
		commitObjects(
			objects.map((o) =>
				o.id === objectId ? { ...o, keyframes: o.keyframes.filter((k) => k.id !== keyframeId) } : o,
			),
		);
		if (selectedKeyframeId === keyframeId) selectedKeyframeId = null;
	}

	function duplicateKeyframe(objectId: string, keyframeId: string) {
		const object = objects.find((o) => o.id === objectId);
		const source = object?.keyframes.find((k) => k.id === keyframeId);
		if (!object || !source) return;
		const duplicate: Keyframe = {
			id: uid(),
			time: Math.min(source.time + 0.25, playback.duration),
			transform: {
				position: [...source.transform.position] as [number, number, number],
				rotation: [...source.transform.rotation] as [number, number, number],
				scale: [...source.transform.scale] as [number, number, number],
			},
			posture: source.posture ? clonePosture(source.posture) : undefined,
		};
		commitObjects(
			objects.map((o) =>
				o.id === objectId ? { ...o, keyframes: [...o.keyframes, duplicate].sort((a, b) => a.time - b.time) } : o,
			),
		);
		selectedKeyframeId = duplicate.id;
	}

	function moveKeyframeTime(objectId: string, keyframeId: string, newTime: number) {
		const clamped = Math.min(Math.max(newTime, 0), MAX_TIMELINE_DURATION);
		commitObjects(
			objects.map((o) =>
				o.id !== objectId
					? o
					: {
							...o,
							keyframes: o.keyframes
								.map((k) => (k.id === keyframeId ? { ...k, time: clamped } : k))
								.sort((a, b) => a.time - b.time),
						},
			),
		);
	}

	function updateKeyframeTransform(objectId: string, keyframeId: string, transform: Partial<SceneTransform>) {
		commitObjects(
			objects.map((o) => {
				if (o.id !== objectId) return o;
				return {
					...o,
					keyframes: o.keyframes.map((k) =>
						k.id === keyframeId ? { ...k, transform: { ...k.transform, ...transform } } : k,
					),
				};
			}),
		);
	}

	/** Selecting a keyframe parks the playhead at its time (reference parity). */
	function selectKeyframe(id: string | null) {
		const object = objects.find((o) => o.id === selectedObjectId);
		const keyframe = object?.keyframes.find((k) => k.id === id);
		selectedKeyframeId = id;
		if (keyframe) playback = { ...playback, elapsed: keyframe.time, playing: false };
	}

	/** Keyframe the given camera view (or drop/replace at ~same time). */
	function setCameraKeyframe(
		time: number,
		view: { position: [number, number, number]; target: [number, number, number]; fov: number },
	) {
		const t = Math.min(Math.max(time, 0), playback.duration);
		const idx = cameraTrack.findIndex((k) => Math.abs(k.time - t) < 1 / 24);
		if (idx >= 0) {
			cameraTrack[idx] = { ...cameraTrack[idx], ...view, time: t };
		} else {
			cameraTrack = [...cameraTrack, { id: uid(), time: t, ...view }].sort((a, b) => a.time - b.time);
		}
		scheduleSave();
	}

	function removeCameraKeyframe(id: string) {
		cameraTrack = cameraTrack.filter((k) => k.id !== id);
		scheduleSave();
	}

	function removeCameraKeyframeAt(time: number) {
		const idx = cameraTrack.findIndex((k) => Math.abs(k.time - time) < 1 / 12);
		if (idx >= 0) removeCameraKeyframe(cameraTrack[idx].id);
	}

	/**
	 * Timeline asks to keyframe the live camera view at `time`. The viewport
	 * owns the camera, so it watches `cameraKeyframeRequest` and fulfills via
	 * setCameraKeyframe with the current pose. Nonce makes back-to-back
	 * requests at the same time observable.
	 */
	let cameraKeyframeRequest = $state<{ time: number; nonce: number } | null>(null);
	function requestCameraKeyframeAt(time: number) {
		cameraKeyframeRequest = { time: Math.min(Math.max(time, 0), playback.duration), nonce: Date.now() };
	}

	function setGates(next: BuildSceneGates) {
		gates = {
			...(next.brief ? { brief: { ...next.brief } } : gates.brief ? { brief: gates.brief } : {}),
			...(next.cut ? { cut: { ...next.cut } } : gates.cut ? { cut: gates.cut } : {}),
		};
		scheduleSave();
	}

	function applyGatesSnapshot(next: BuildSceneGates) {
		gates = {
			...(next.brief ? { brief: { ...next.brief } } : {}),
			...(next.cut ? { cut: { ...next.cut } } : {}),
		};
	}

	function undo() {
		if (history.length === 0) return;
		const previous = history[history.length - 1];
		const selectionSurvives = previous.some((o) => o.id === selectedObjectId);
		withoutHistory(() => {
			future = [objects, ...future].slice(0, HISTORY_LIMIT);
			objects = previous;
			history = history.slice(0, -1);
			if (!selectionSurvives) selectedObjectId = previous[0]?.id ?? null;
		});
		scheduleSave();
	}

	function redo() {
		if (future.length === 0) return;
		const next = future[0];
		const selectionSurvives = next.some((o) => o.id === selectedObjectId);
		withoutHistory(() => {
			history = [...history, objects].slice(-HISTORY_LIMIT);
			objects = next;
			future = future.slice(1);
			if (!selectionSurvives) selectedObjectId = next[0]?.id ?? null;
		});
		scheduleSave();
	}

	function beginHistoryGroup() {
		groupDepth += 1;
		groupRecorded = false;
	}

	function endHistoryGroup() {
		groupDepth = Math.max(0, groupDepth - 1);
	}

	return {
		// state (getters)
		get objects() {
			return objects;
		},
		get compositionId() {
			return compositionId;
		},
		get title() {
			return title;
		},
		get selectedObjectId() {
			return selectedObjectId;
		},
		get selectedKeyframeId() {
			return selectedKeyframeId;
		},
		get activeTool() {
			return activeTool;
		},
		get shotParams() {
			return shotParams;
		},
		get playback() {
			return playback;
		},
		get cameraTrack() {
			return cameraTrack;
		},
		get gates() {
			return gates;
		},
		get cameraKeyframeRequest() {
			return cameraKeyframeRequest;
		},
		get loaded() {
			return loaded;
		},
		get canUndo() {
			return history.length > 0;
		},
		get canRedo() {
			return future.length > 0;
		},
		get dragInProgress() {
			return dragInProgress;
		},
		set dragInProgress(v: boolean) {
			dragInProgress = v;
		},
		get selectedObject(): SceneObject | null {
			return objects.find((o) => o.id === selectedObjectId) ?? null;
		},
		// sync
		loadComposition,
		applyServerScene,
		handleShotUpdated,
		flushSave: saveScene,
		/** Sole mutable graph snapshot for adapters (viewport sampling, export). */
		toSceneData,
		// actions
		addObject,
		deleteObject,
		clearScene,
		selectObject,
		clearSelection,
		renameObject,
		toggleObjectVisibility,
		toggleObjectLock,
		updateObjectTransform,
		updateObjectPosture,
		updateObjectDefaultPosture,
		updateObjectFov,
		setActiveTool,
		setShotParams,
		setPlaying,
		setElapsed,
		setSpeed,
		setDuration,
		setCameraKeyframe,
		removeCameraKeyframe,
		removeCameraKeyframeAt,
		requestCameraKeyframeAt,
		setGates,
		applyGatesSnapshot,
		addKeyframe,
		commitLiveKeyframe,
		deleteKeyframe,
		duplicateKeyframe,
		moveKeyframeTime,
		updateKeyframeTransform,
		selectKeyframe,
		undo,
		redo,
		beginHistoryGroup,
		endHistoryGroup,
	};
}

export const shotStore = createStore();
export type ShotStore = typeof shotStore;
export { createInitialObject, THREE };
