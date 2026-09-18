<script lang="ts">
	/**
	 * The 3D blockout viewport — plain three.js (no React; Svelte port of
	 * open-media's ComposerViewport). Renders the store's composition:
	 * mannequin figures + primitives on a ground grid, OrbitControls camera,
	 * TransformControls gizmo mapped to the active tool, and a capture
	 * renderer (PNG dataURL) used by the Capture button and the agent's
	 * request_capture SSE. This canvas is also the source for video export —
	 * one deterministic MediaRecorder pass of the camera track + object
	 * keyframes (Timeline → Export video).
	 */
	import { onMount, onDestroy, untrack } from 'svelte';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { TransformControls } from 'three/examples/jsm/controls/TransformControls.js';
import { shotStore, isPrimitiveType, isCharacterType, type CharacterType } from '../shotStore.svelte';
import type { SceneObject } from '../shotStore.svelte';
import ViewportToolbar from './ViewportToolbar.svelte';
import { createPrimitiveMesh } from '../helpers/primitiveFactory';
import { createMannequin, groundFigure } from '../helpers/mannequinFactory';
import { writePosture, readPosture } from '../helpers/posture';
import { JOINT_CONFIGS, getJoint, setDOF, getDOF } from '../helpers/jointConfig';
import { getCharacterAnchors, solveShot } from '../calibration/shotSolver';
import { getCompositionPreset } from '../calibration/compositionPresets';
import { sampleTrack } from '../motion/sampleTrack';
import { t } from '$lib/i18n.svelte';
import '../types/mannequin-js.d';

	let {
		oncapture,
		onselectionchange,
	}: {
		oncapture?: (dataUrl: string) => void;
		onselectionchange?: () => void;
	} = $props();

let container: HTMLDivElement;
let renderer: THREE.WebGLRenderer;
let scene: THREE.Scene;
let camera: THREE.PerspectiveCamera;
let orbit: OrbitControls;
let gizmo: TransformControls;
let gizmoHelper: THREE.Object3D;
let raf = 0;
let resizeObserver: ResizeObserver;
let cleanupPointerUp: (() => void) | null = null;

// Video export frame rate — 24 fps (film standard) for the blockout clip.
const EXPORT_FPS = 24;

// toolbar-owned view state (the grid mesh + a CSS rule-of-thirds overlay)
let showGrid = $state(true);
let showComposition = $state(false);
let grid: THREE.GridHelper;

// Bumped when an async mannequin finishes building, so the change-driven
// shot solve can re-run once anchors actually exist (it early-returns
// while the figure is still loading).
let figureRevision = $state(0);

// true while a TransformControls gesture is live — tick() must not flip
// orbit.enabled mid-drag (it would let the camera rotate with the gizmo)
let gizmoDragging = false;

	/** instance registry: object id → three root + async-build state */
	interface Instance {
		root: THREE.Group;
		figure?: THREE.Object3D | null;
		ready: boolean;
		type: SceneObject['type'];
	}
	const instances = new Map<string, Instance>();

	// capture-request guard: SSE may ask while a previous capture renders
	let captureRequestedAt = 0;

	function ensureInstance(o: SceneObject): Instance {
		let inst = instances.get(o.id);
		if (inst && inst.type !== o.type) {
			scene.remove(inst.root);
			instances.delete(o.id);
			inst = undefined;
		}
		if (!inst) {
			const root = new THREE.Group();
			root.name = o.id;
			scene.add(root);
			inst = { root, ready: false, type: o.type };
			instances.set(o.id, inst);
			if (isPrimitiveType(o.type)) {
				const mesh = createPrimitiveMesh(o.type);
				root.add(mesh);
				inst.ready = true;
			} else if (isCharacterType(o.type)) {
				const charType: CharacterType = o.type;
				void createMannequin({ type: charType }).then((figure) => {
					if (!instances.has(o.id)) return; // deleted while loading
					inst!.figure = figure;
					inst!.root.add(figure);
					inst!.ready = true;
				});
			} else {
				inst.ready = true; // camera object: rendered as a gizmo helper
				const helper = new THREE.CameraHelper(new THREE.PerspectiveCamera(10, 16 / 9, 0.1, 10));
				helper.name = 'camhelper';
				root.add(helper);
			}
		}
		return inst;
	}

	function applyTransform(o: SceneObject) {
		const inst = ensureInstance(o);
		const t = o.transform;
		inst.root.position.set(t.position[0], t.position[1], t.position[2]);
		inst.root.rotation.set(
			THREE.MathUtils.degToRad(t.rotation[0]),
			THREE.MathUtils.degToRad(t.rotation[1]),
			THREE.MathUtils.degToRad(t.rotation[2]),
		);
		inst.root.scale.set(t.scale[0], t.scale[1], t.scale[2]);
		inst.root.visible = o.visible;
	}

	/**
	 * Port of open-media's AnimatedObject useFrame: an object with a
	 * multi-keyframe track is driven by sampling that track at the playhead —
	 * the stored `transform`/`posture` fields are stale once a track exists
	 * (they only mirror the last live edit). Objects with 0-1 keyframes, or
	 * whose liveEditTime still matches the playhead (a gizmo drag / pose edit
	 * staged on top of the track), read the plain fields instead. Static
	 * objects degrade to the plain path with no mode branch.
	 */
	function applyTrackSampling(o: SceneObject) {
		const inst = instances.get(o.id);
		if (!inst) return;
		const elapsed = shotStore.playback.elapsed;
		const isLive = o.liveEditTime !== null && Math.abs(o.liveEditTime - elapsed) < 1e-6;
		const effectiveLive = isLive || o.keyframes.length <= 1;
		if (effectiveLive) return; // applyTransform/applyPosture already wrote the plain fields
		const sample = sampleTrack(o.keyframes, elapsed);
		const t = sample.transform;
		inst.root.position.set(t.position[0], t.position[1], t.position[2]);
		inst.root.rotation.set(
			THREE.MathUtils.degToRad(t.rotation[0]),
			THREE.MathUtils.degToRad(t.rotation[1]),
			THREE.MathUtils.degToRad(t.rotation[2]),
		);
		inst.root.scale.set(t.scale[0], t.scale[1], t.scale[2]);
		inst.root.visible = o.visible;
		if (isCharacterType(o.type) && sample.posture && inst.figure) {
			writePosture(inst.figure as THREE.Object3D & { posture?: unknown }, sample.posture);
		}
	}

	function applyPosture(o: SceneObject) {
		const inst = instances.get(o.id);
		if (!inst?.figure) return;
		const figure = inst.figure as THREE.Object3D & { posture?: unknown };
		const effective = o.posture ?? o.defaultPosture;
		if (effective) {
			writePosture(figure, effective);
			groundFigure(figure);
		}
		// No stored posture: leave the mannequin at its constructor stance.
		// Writing a fabricated all-empty posture here would NaN mannequin-js's
		// fixed-arity joint setters and the figure would vanish from the render.
	}

	// ── pose tool: joint handles ────────────────────────────────────

	/** Small sphere per editable joint of the selected figure, in root space. */
	const poseHandleGroup = new THREE.Group();
	let poseHandleTargetId: string | null = null;
	let poseDragJoint: string | null = null;
	let poseDragStart: { x: number; y: number; dofs: number[] } | null = null;

	const POSE_HANDLE_GEO = new THREE.SphereGeometry(0.035, 10, 8);
	const POSE_HANDLE_MAT = new THREE.MeshBasicMaterial({ color: 0xffc14d, depthTest: false, transparent: true });
	const POSE_HANDLE_ACTIVE_MAT = new THREE.MeshBasicMaterial({ color: 0xff5f5f, depthTest: false, transparent: true });

	/** Rebuild (idempotently) the joint handles for the selected mannequin. */
	function syncPoseHandles() {
		const sel = shotStore.selectedObject;
		const active =
			sel && shotStore.activeTool === 'pose' && isCharacterType(sel.type) && instances.get(sel.id)?.ready;
		if (!active) {
			if (poseHandleGroup.parent) scene.remove(poseHandleGroup);
			poseHandleTargetId = null;
			return;
		}
		if (poseHandleTargetId !== sel!.id) {
			poseHandleGroup.clear();
			poseHandleTargetId = sel!.id;
		}
		const inst = instances.get(sel!.id)!;
		if (!inst.figure) return;
		poseHandleGroup.name = `posegizmo:${sel!.id}`;
		// one handle per JOINT_CONFIGS entry (26 joints; fingers collapse to one)
		for (let i = 0; i < JOINT_CONFIGS.length; i++) {
			const cfg = JOINT_CONFIGS[i];
			let handle = poseHandleGroup.getObjectByName(cfg.mannequinKey) as THREE.Mesh | undefined;
			if (!handle) {
				handle = new THREE.Mesh(POSE_HANDLE_GEO, POSE_HANDLE_MAT);
				handle.name = cfg.mannequinKey;
				poseHandleGroup.add(handle);
			}
			const joint = getJoint(inst.figure, cfg.mannequinKey);
			if (joint?.getWorldPosition) {
				inst.root.updateMatrixWorld(true);
				const world = joint.getWorldPosition(new THREE.Vector3());
				inst.root.worldToLocal(world); // handles live inside the object root
				handle.position.copy(world);
			}
			handle.visible = !sel!.locked;
			const mat = handle.name === poseDragJoint ? POSE_HANDLE_ACTIVE_MAT : POSE_HANDLE_MAT;
			if (handle.material !== mat) handle.material = mat;
		}
		if (!poseHandleGroup.parent) scene.add(poseHandleGroup);
	}

	function pickJointHandle(e: PointerEvent, raycaster: THREE.Raycaster, pointer: THREE.Vector2): string | null {
		if (!poseHandleGroup.parent) return null;
		const rect = renderer.domElement.getBoundingClientRect();
		pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
		pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
		raycaster.setFromCamera(pointer, camera);
		const hits = raycaster.intersectObjects([...poseHandleGroup.children], false);
		return hits.length > 0 ? hits[0].object.name : null;
	}

	/**
	 * Maps screen delta onto the joint's first two DOFs (screen x → dof 0,
	 * screen y → dof 1). Angle-space drag, not a projection — good enough for
	 * blockout posing (open-media's JointGizmo does the same two-axis mapping).
	 */
	function dragPoseJoint(jointKey: string, dx: number, dy: number) {
		const sel = shotStore.selectedObject;
		if (!sel) return;
		const inst = instances.get(sel.id);
		if (!inst?.figure) return;
		const cfg = JOINT_CONFIGS.find((c) => c.mannequinKey === jointKey);
		if (!cfg) return;
		if (poseDragJoint !== jointKey) {
			poseDragJoint = jointKey;
			poseDragStart = { x: dx, y: dy, dofs: cfg.dofs.map((d) => getDOF(getJoint(inst.figure, cfg.mannequinKey), d)) };
		}
		const joint = getJoint(inst.figure, jointKey);
		for (let i = 0; i < Math.min(2, cfg.dofs.length); i++) {
			const dof = cfg.dofs[i];
			const delta = i === 0 ? dx : dy;
			const next = THREE.MathUtils.clamp(
				(poseDragStart?.dofs[i] ?? 0) + delta * 0.5,
				dof.min,
				dof.max,
			);
			setDOF(joint, dof, next);
		}
	}

	/** Persist the dragged pose back into the store (which autosaves + SSEs). */
	function commitPoseDrag() {
		const sel = shotStore.selectedObject;
		const jointKey = poseDragJoint;
		poseDragJoint = null;
		poseDragStart = null;
		if (!sel || !jointKey) return;
		const inst = instances.get(sel.id);
		if (!inst?.figure) return;
		try {
			const posture = readPosture(inst.figure as unknown as { posture?: unknown } & Record<string, unknown>);
			shotStore.updateObjectPosture(sel.id, posture);
		} catch {
			// posture read failed — leave the stored pose as it was
		}
	}

	function syncScene() {
		const storeObjects = shotStore.objects;
		// remove stale
		const ids = new Set(storeObjects.map((o) => o.id));
		for (const [id, inst] of instances) {
			if (!ids.has(id)) {
				scene.remove(inst.root);
				instances.delete(id);
			}
		}
		// add/update
		let anyFigurePending = false;
		for (const o of storeObjects) {
			applyTransform(o);
			if (isCharacterType(o.type)) {
				applyPosture(o);
				if (!instances.get(o.id)?.ready) anyFigurePending = true;
			}
			applyTrackSampling(o); // wins over the plain fields when a track exists
		}
		if (anyFigurePending) figureRevision++; // re-arm the change-driven shot solve
		// selection gizmo (suppressed in pose mode — joint handles own the figure)
		const selected = shotStore.selectedObject;
		const gizmoActive = selected && shotStore.activeTool !== 'select' && shotStore.activeTool !== 'pose';
		if (gizmoActive && instances.has(selected.id)) {
			const inst = instances.get(selected.id)!;
			if (gizmo.object !== inst.root) {
				gizmo.attach(inst.root);
			}
			gizmo.setMode(
				shotStore.activeTool === 'move' ? 'translate' : shotStore.activeTool === 'rotate' ? 'rotate' : 'scale',
			);
			gizmo.enabled = !selected.locked;
			gizmoHelper.visible = true;
		} else {
			gizmo.detach();
			gizmoHelper.visible = false;
		}
		syncPoseHandles();
	}

	function solveShotFromSelection() {
		// The store holds params; the frontend owns the solve math (same split
		// as open-media's set_shot → ComposerShell effect).
		const params = shotStore.shotParams;
		if (!params?.shotSize) return;
		const characterObj =
			shotStore.objects.find((o) => o.id === shotStore.selectedObjectId && isCharacterType(o.type)) ??
			shotStore.objects.find((o) => isCharacterType(o.type));
		if (!characterObj) return;
		const inst = instances.get(characterObj.id);
		if (!inst?.figure) return;
		const anchors = getCharacterAnchors(inst.figure);
		const second = shotStore.objects.find((o) => o.id !== characterObj.id && isCharacterType(o.type));
		const secondFigure = second ? instances.get(second.id)?.figure : undefined;
		const secondAnchors = second && secondFigure ? getCharacterAnchors(secondFigure) : undefined;
		const preset = getCompositionPreset(params.composition ?? 'center');
		const shot = solveShot({
			shotSize: params.shotSize,
			angle: params.angle ?? 'front',
			elevation: params.elevation ?? 'eye',
			composition: preset,
			anchors,
			targetAnchors: secondAnchors,
			fovDeg: 40,
			aspect: camera.aspect,
		});
		// A NaN here (bad anchor geometry, degenerate bounding box) would stick
		// in the camera forever and render an empty frame — refuse to apply it.
		if (
			!Number.isFinite(shot.position.x) ||
			!Number.isFinite(shot.position.y) ||
			!Number.isFinite(shot.position.z) ||
			!Number.isFinite(shot.target.x) ||
			!Number.isFinite(shot.target.y) ||
			!Number.isFinite(shot.target.z)
		) {
			return;
		}
		shotStore.beginHistoryGroup();
		orbit.target.copy(shot.target);
		camera.position.copy(shot.position);
		camera.updateProjectionMatrix();
		orbit.update();
		shotStore.endHistoryGroup();
	}

	// ── capture ─────────────────────────────────────────────────────

	function captureShot(): string {
		renderFrame();
		return renderer.domElement.toDataURL('image/png');
	}

	// ── camera track ────────────────────────────────────────────────

	/** Samples the camera track at `time` with smooth lerp/slerp between keyframes. */
	function sampleCameraTrack(time: number): { position: [number, number, number]; target: [number, number, number]; fov: number } | null {
		const track = shotStore.cameraTrack;
		if (track.length === 0) return null;
		const sorted = track.length > 1 ? [...track].sort((a, b) => a.time - b.time) : track;
		if (sorted.length === 1 || time <= sorted[0].time) {
			const k = sorted[0];
			return { position: [...k.position], target: [...k.target], fov: k.fov };
		}
		const last = sorted[sorted.length - 1];
		if (time >= last.time) {
			const k = last;
			return { position: [...k.position], target: [...k.target], fov: k.fov };
		}
		let a = sorted[0];
		let b = last;
		for (let i = 0; i < sorted.length - 1; i++) {
			if (time >= sorted[i].time && time <= sorted[i + 1].time) {
				a = sorted[i];
				b = sorted[i + 1];
				break;
			}
		}
		const span = b.time - a.time;
		const k = span > 0 ? 0.5 - 0.5 * Math.cos(Math.PI * ((time - a.time) / span)) : 0;
		const pos: [number, number, number] = [
			a.position[0] + (b.position[0] - a.position[0]) * k,
			a.position[1] + (b.position[1] - a.position[1]) * k,
			a.position[2] + (b.position[2] - a.position[2]) * k,
		];
		const tgt: [number, number, number] = [
			a.target[0] + (b.target[0] - a.target[0]) * k,
			a.target[1] + (b.target[1] - a.target[1]) * k,
			a.target[2] + (b.target[2] - a.target[2]) * k,
		];
		return { position: pos, target: tgt, fov: a.fov + (b.fov - a.fov) * k };
	}

let playbackClockPrev = 0;
let playbackWasPlaying = false;
// true while a video export pass is rendering — tick() idles so the fixed-step
// export clock owns the playhead/renders and never fights the recorder.
let exporting = false;

	function applyCameraAt(time: number) {
		const sample = sampleCameraTrack(time);
		if (!sample) return;
		if (
			!sample.position.every(Number.isFinite) ||
			!sample.target.every(Number.isFinite) ||
			!Number.isFinite(sample.fov)
		) {
			return; // poisoned keyframe data — keep the current camera
		}
		camera.position.set(sample.position[0], sample.position[1], sample.position[2]);
		orbit.target.set(sample.target[0], sample.target[1], sample.target[2]);
		if (Math.abs(camera.fov - sample.fov) > 0.01) {
			camera.fov = sample.fov;
			camera.updateProjectionMatrix();
		}
	}

	/** Advance the store clock while playing; driven from tick(). */
	function advancePlayback(now: number) {
		const playing = shotStore.playback.playing;
		if (playing && !playbackWasPlaying) playbackClockPrev = now;
		if (playing) {
			const dt = now - playbackClockPrev;
			let next = shotStore.playback.elapsed + dt;
			if (next >= shotStore.playback.duration) {
				next = 0; // loop for preview; export runs its own pass
			}
			shotStore.setElapsed(next);
		}
		playbackWasPlaying = playing;
		playbackClockPrev = now;
	}

	function renderFrame() {
		renderer.render(scene, camera);
	}

	function flushCaptureRequest() {
		// If the backend has a pending capture_request (agent asked), render +
		// upload it. The store never sees this; the page wires oncapture.
		fetch(`/api/shots/${shotStore.compositionId}`)
			.then((r) => (r.ok ? r.json() : null))
			.then(async (comp) => {
				if (!comp?.capture_request_json) return;
				if (comp.capture_requested_at && comp.capture_requested_at <= captureRequestedAt) return;
				captureRequestedAt = Date.now();
				if (!oncapture) return;
				const dataUrl = captureShot();
				oncapture(dataUrl);
			})
			.catch(() => {});
	}

	// ── loop ────────────────────────────────────────────────────────

	function tick() {
		raf = requestAnimationFrame(tick);
		if (exporting) return; // export drives the scene on its own fixed clock
		const now = performance.now() / 1000;
		advancePlayback(now);
		// While playing (or scrubbed onto the track) the camera track owns the
		// view; orbit is disabled so it cannot fight the sampled pose. During a
		// gizmo drag TransformControls already disabled orbit — leave it alone,
		// because a per-frame `orbit.enabled = true` here would fight the gizmo.
		const playing = shotStore.playback.playing;
		if (!gizmoDragging) orbit.enabled = !playing;
		if (playing) {
			applyCameraAt(shotStore.playback.elapsed);
		}
		orbit.update();
		syncScene();
		renderFrame();
	}

	onMount(() => {
		renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
		renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
		renderer.shadowMap.enabled = true;
		container.appendChild(renderer.domElement);

		scene = new THREE.Scene();
		scene.background = new THREE.Color(0x14161c);

		camera = new THREE.PerspectiveCamera(40, 1, 0.01, 200);
		camera.position.set(4, 3, 6);

		const hemi = new THREE.HemisphereLight(0xffffff, 0x223, 0.9);
		scene.add(hemi);
		const dir = new THREE.DirectionalLight(0xffffff, 1.4);
		dir.position.set(5, 10, 4);
		dir.castShadow = true;
		scene.add(dir);

		grid = new THREE.GridHelper(40, 40, 0x3a4152, 0x242a36);
		scene.add(grid);
		const ground = new THREE.Mesh(
			new THREE.PlaneGeometry(80, 80),
			new THREE.ShadowMaterial({ opacity: 0.25 }),
		);
		ground.rotation.x = -Math.PI / 2;
		ground.receiveShadow = true;
		scene.add(ground);

		orbit = new OrbitControls(camera, renderer.domElement);
		orbit.enableDamping = true;

		gizmo = new TransformControls(camera, renderer.domElement);
		gizmo.setSize(0.8);
		gizmoHelper = gizmo.getHelper();
		scene.add(gizmoHelper);
		gizmo.addEventListener('dragging-changed', (e) => {
			const dragging = (e as unknown as { value: boolean }).value;
			gizmoDragging = dragging;
			orbit.enabled = !dragging;
			shotStore.dragInProgress = dragging;
			if (!dragging) {
				// one commit per gesture
				const obj = shotStore.selectedObject;
				if (obj) {
					const root = instances.get(obj.id)?.root;
					if (root) {
						shotStore.updateObjectTransform(obj.id, {
							position: [root.position.x, root.position.y, root.position.z],
							rotation: [
								THREE.MathUtils.radToDeg(root.rotation.x),
								THREE.MathUtils.radToDeg(root.rotation.y),
								THREE.MathUtils.radToDeg(root.rotation.z),
							],
							scale: [root.scale.x, root.scale.y, root.scale.z],
						});
					}
				}
			}
		});
		gizmo.addEventListener('objectChange', () => {
			const obj = shotStore.selectedObject;
			const root = obj ? instances.get(obj.id)?.root : null;
			if (obj && root) {
				shotStore.beginHistoryGroup();
				shotStore.updateObjectTransform(obj.id, {
					position: [root.position.x, root.position.y, root.position.z],
					rotation: [
						THREE.MathUtils.radToDeg(root.rotation.x),
						THREE.MathUtils.radToDeg(root.rotation.y),
						THREE.MathUtils.radToDeg(root.rotation.z),
					],
					scale: [root.scale.x, root.scale.y, root.scale.z],
				});
				shotStore.endHistoryGroup();
			}
		});

		// selection + pose-mode picking
		const raycaster = new THREE.Raycaster();
		const pointer = new THREE.Vector2();
		let downAt = 0;
		let downPoseJoint: string | null = null;
		let downX = 0;
		let downY = 0;
		const onPointerUp = (e: PointerEvent) => {
			if (downPoseJoint) {
				downPoseJoint = null;
				if (!shotStore.playback.playing && !gizmoDragging) orbit.enabled = true;
				commitPoseDrag();
				return;
			}
			if (Date.now() - downAt > 250) return; // drag, not a click
			const rect = renderer.domElement.getBoundingClientRect();
			pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
			pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
			raycaster.setFromCamera(pointer, camera);
			// pose mode: clicking a handle selects nothing new
			if (shotStore.activeTool === 'pose' && shotStore.selectedObject) return;
			const hits = raycaster.intersectObjects([...instances.values()].map((i) => i.root), true);
			for (const hit of hits) {
				let node: THREE.Object3D | null = hit.object;
				while (node && !instances.has(node.name)) node = node.parent;
				if (node) {
					shotStore.selectObject(node.name);
					onselectionchange?.();
					return;
				}
			}
			shotStore.clearSelection();
		};
		renderer.domElement.addEventListener('pointerdown', (e) => {
			downAt = Date.now();
			downX = e.clientX;
			downY = e.clientY;
			// In pose mode a press on a joint handle starts a pose drag instead
			// of an orbit; press elsewhere still orbits.
			downPoseJoint = null;
			if (shotStore.activeTool === 'pose' && shotStore.selectedObject) {
				const hit = pickJointHandle(e, raycaster, pointer);
				if (hit) {
					downPoseJoint = hit;
					orbit.enabled = false;
				}
			}
		});
		renderer.domElement.addEventListener('pointermove', (e) => {
			if (!downPoseJoint) return;
			dragPoseJoint(downPoseJoint, e.clientX - downX, e.clientY - downY);
		});
		window.addEventListener('pointerup', onPointerUp);
		cleanupPointerUp = () => window.removeEventListener('pointerup', onPointerUp);

		resizeObserver = new ResizeObserver(() => {
			const w = container.clientWidth;
			const h = container.clientHeight;
			if (w === 0 || h === 0) return;
			renderer.setSize(w, h);
			camera.aspect = w / h;
			camera.updateProjectionMatrix();
		});
		resizeObserver.observe(container);

		raf = requestAnimationFrame(tick);
	});

	onDestroy(() => {
		cancelAnimationFrame(raf);
		resizeObserver?.disconnect();
		cleanupPointerUp?.();
		gizmo?.dispose();
		orbit?.dispose();
		renderer?.dispose();
	});

	// ── video export ────────────────────────────────────────────────

	/**
	 * Renders one pass of the camera track to a video clip via
	 * canvas.captureStream + MediaRecorder (MP4 where Chromium supports it,
	 * WebM fallback). Resolves with a data URL for POST /api/shots/{id}/captures.
	 */
	function pickVideoMime(): string | null {
		const candidates = [
			'video/mp4;codecs=avc1.42E01E',
			'video/mp4',
			'video/webm;codecs=vp9',
			'video/webm',
		];
		if (typeof MediaRecorder === 'undefined') return null;
		for (const mime of candidates) {
			if (MediaRecorder.isTypeSupported(mime)) return mime;
		}
		return null;
	}

	function blobToDataUrl(blob: Blob): Promise<string> {
		return new Promise((resolve, reject) => {
			const reader = new FileReader();
			reader.onload = () => resolve(String(reader.result));
			reader.onerror = () => reject(new Error(t('shot.errBlob')));
			reader.readAsDataURL(blob);
		});
	}

	async function exportVideo(): Promise<{ dataUrl: string; ext: string }> {
		const mime = pickVideoMime();
		if (!mime) throw new Error(t('shot.errRecorder'));
		// A camera track (≥2 keys) drives camera motion, but a scene of
		// keyframed objects still animates with a static camera — the 3D
		// editor content is what we export, so only demand SOME renderable
		// scene, not a camera track specifically.
		const hasObjects = shotStore.objects.some((o) => o.type !== 'camera');
		if (shotStore.cameraTrack.length < 2 && !hasObjects) {
			throw new Error(t('shot.errNoData'));
		}
		const stream = renderer.domElement.captureStream(EXPORT_FPS);
		const chunks: Blob[] = [];
		const recorder = new MediaRecorder(stream, { mimeType: mime });
		recorder.ondataavailable = (e) => {
			if (e.data.size > 0) chunks.push(e.data);
		};
		const done = new Promise<void>((resolve) => {
			recorder.onstop = () => resolve();
		});

		const duration = shotStore.playback.duration;
		// Record in REAL TIME: captureStream(N) stamps frames on the wall clock,
		// so the playhead must advance one N fps tick per real-time frame. The
		// old requestAnimationFrame loop ran at the display refresh (60 Hz+),
		// scrubbing 0→duration twice as fast and halving the exported clip.
		// A fixed timestep with drift compensation keeps the output exactly
		// `duration` seconds regardless of monitor refresh or frame jitter.
		const fps = EXPORT_FPS;
		const frameMs = 1000 / fps;
		const totalFrames = Math.max(1, Math.round(duration * fps));

		shotStore.setPlaying(false);
		exporting = true;
		recorder.start();
		try {
			const start = performance.now();
			for (let i = 0; i <= totalFrames; i++) {
				const t = Math.min(i / fps, duration);
				shotStore.setElapsed(t);
				applyCameraAt(t);
				syncScene();
				renderFrame();
				const target = start + (i + 1) * frameMs;
				const wait = target - performance.now();
				if (wait > 0) await new Promise((r) => setTimeout(r, wait));
			}
			// One frame of tail so the recorder flushes the last presented frame.
			await new Promise((r) => setTimeout(r, frameMs));
		} finally {
			exporting = false;
			shotStore.setElapsed(0);
		}
		recorder.stop();
		await done;
		stream.getTracks().forEach((tr) => tr.stop());

		const ext = mime.includes('mp4') ? 'mp4' : 'webm';
		const blob = new Blob(chunks, { type: mime });
		if (blob.size === 0) throw new Error(t('shot.errNoData'));
		return { dataUrl: await blobToDataUrl(blob), ext };
	}

	// expose capture + request polling to the page
	export function captureNow(): string {
		return captureShot();
	}
	export function pollCaptureRequest() {
		flushCaptureRequest();
	}
	export function exportVideoClip(): Promise<{ dataUrl: string; ext: string }> {
		return exportVideo();
	}

	// Camera keyframe requests from the timeline: the store asks (nonce), the
	// viewport answers with the live pose. $effect re-runs per request.
	let lastKeyframeNonce = 0;
	$effect(() => {
		const req = shotStore.cameraKeyframeRequest;
		if (!req || req.nonce === lastKeyframeNonce) return;
		lastKeyframeNonce = req.nonce;
		shotStore.setCameraKeyframe(req.time, {
			position: [camera.position.x, camera.position.y, camera.position.z],
			target: [orbit.target.x, orbit.target.y, orbit.target.z],
			fov: camera.fov,
		});
	});

	/**
	 * Change-driven shot solve (mirrors open-media's ComposerShell effect on
	 * [selectedId, shotParams]): the preset applies ONCE when the framing or
	 * selection changes — never per-frame, so a manual orbit afterwards is not
	 * yanked back by a re-applied preset. untrack keeps internal object reads
	 * (anchors, instances) from widening the trigger set.
	 */
	$effect(() => {
		void shotStore.shotParams;
		void shotStore.selectedObjectId;
		void figureRevision;
		if (!camera || shotStore.playback.playing) return;
		untrack(() => solveShotFromSelection());
	});

	$effect(() => {
		if (grid) grid.visible = showGrid;
	});

	// Scrub: when not playing, sample the track at the (user-moved) playhead.
	let lastScrubbedElapsed = -1;
	$effect(() => {
		const t = shotStore.playback.elapsed;
		if (shotStore.playback.playing) return;
		if (Math.abs(t - lastScrubbedElapsed) < 1e-6) return;
		lastScrubbedElapsed = t;
		if (shotStore.cameraTrack.length > 0) {
			applyCameraAt(t);
		}
	});

	// Dev/debug probe: lets tests and console inspection read the live three
	// state (camera, target, solved shot) without touching the store contract.
	const debugProbe = {
		get camera() {
			return camera ? { position: camera.position.toArray(), fov: camera.fov } : null;
		},
		get target() {
			return orbit ? orbit.target.toArray() : null;
		},
		get instances() {
			return [...instances.entries()].map(([id, inst]) => ({
				id,
				type: inst.type,
				ready: inst.ready,
				hasFigure: !!inst.figure,
			}));
		},
		get poseHandles() {
			return poseHandleGroup.parent
				? { count: poseHandleGroup.children.length, target: poseHandleTargetId }
				: null;
		},
	};

	onMount(() => {
		(window as unknown as { __shotDebug?: typeof debugProbe }).__shotDebug = debugProbe;
	});
</script>

<div class="viewport" bind:this={container}>
	<div class="viewport-toolbar">
		<ViewportToolbar oncapture={() => oncapture?.(captureShot())} bind:showGrid bind:showComposition />
	</div>
	{#if showComposition}
		<div class="composition-guide" aria-hidden="true">
			<span class="thirds-v"></span><span class="thirds-v"></span><span class="thirds-h"></span><span class="thirds-h"></span>
			<span class="cross-h"></span><span class="cross-v"></span>
		</div>
	{/if}
</div>

<style>
	.viewport {
		width: 100%;
		height: 100%;
		min-height: 320px;
		overflow: hidden;
		background: #14161c;
		position: relative;
	}
	.viewport :global(canvas) {
		display: block;
	}
	.viewport-toolbar {
		position: absolute;
		top: 10px;
		left: 50%;
		transform: translateX(-50%);
		z-index: 10;
		max-width: calc(100% - 20px);
	}
	.composition-guide {
		position: absolute;
		inset: 0;
		z-index: 5;
		pointer-events: none;
	}
	.composition-guide .thirds-v {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 1px;
		background: rgba(255, 255, 255, 0.14);
	}
	.composition-guide .thirds-v:nth-of-type(1) {
		left: 33.33%;
	}
	.composition-guide .thirds-v:nth-of-type(2) {
		left: 66.66%;
	}
	.composition-guide .thirds-h {
		position: absolute;
		left: 0;
		right: 0;
		height: 1px;
		background: rgba(255, 255, 255, 0.14);
	}
	.composition-guide .thirds-h:nth-of-type(3) {
		top: 33.33%;
	}
	.composition-guide .thirds-h:nth-of-type(4) {
		top: 66.66%;
	}
	.composition-guide .cross-h {
		position: absolute;
		top: 50%;
		left: 50%;
		width: 14px;
		height: 1px;
		transform: translate(-50%, -50%);
		background: rgba(255, 255, 255, 0.35);
	}
	.composition-guide .cross-v {
		position: absolute;
		top: 50%;
		left: 50%;
		width: 1px;
		height: 14px;
		transform: translate(-50%, -50%);
		background: rgba(255, 255, 255, 0.35);
	}
</style>
