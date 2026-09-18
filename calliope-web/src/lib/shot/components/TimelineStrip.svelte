<script lang="ts">
	/**
	 * Timeline: a camera row (keyframes the viewport camera; plays the track
	 * that drives video export) and an object row (keyframes the selected
	 * object's transform/posture; playback samples every tracked object).
	 * Both on the same 0..duration playhead (max 60s).
	 */
	import { shotStore, MAX_TIMELINE_DURATION } from '../shotStore.svelte';
	import type { CameraKeyframe } from '../shotStore.svelte';
	import { t } from '$lib/i18n.svelte';

	let { onExportVideo }: { onExportVideo?: () => void } = $props();

	let trackEl: HTMLDivElement | null = null;
	let exporting = $state(false);
	let draggingKeyframeId: string | null = null;

	const duration = $derived(shotStore.playback.duration);
	const playing = $derived(shotStore.playback.playing);
	const elapsed = $derived(shotStore.playback.elapsed);
	const track = $derived(shotStore.cameraTrack);
	const objects = $derived(shotStore.objects);
	const selectedObject = $derived(shotStore.selectedObject);
	const selectedKeyframeId = $derived(shotStore.selectedKeyframeId);

	/** Export needs a camera track (≥2 keys) or any scene object to frame. */
	const canExport = $derived(track.length >= 2 || objects.some((o) => o.type !== 'camera'));
	const exportDisabledReason = $derived(
		canExport ? '' : track.length < 2 ? t('shot.exportHintNeedBeats') : t('shot.exportHint'),
	);

	const pct = $derived(duration > 0 ? (elapsed / duration) * 100 : 0);

	function timeAt(clientX: number): number {
		if (!trackEl) return 0;
		const rect = trackEl.getBoundingClientRect();
		const k = Math.min(Math.max((clientX - rect.left) / rect.width, 0), 1);
		return k * duration;
	}

	function onTrackScrub(e: PointerEvent) {
		if (!trackEl || playing) return;
		shotStore.setElapsed(timeAt(e.clientX));
	}

	function onDurationInput(e: Event) {
		const value = Number((e.target as HTMLInputElement).value);
		if (Number.isFinite(value)) shotStore.setDuration(value);
	}

	function keyframeStyle(k: CameraKeyframe) {
		return `left:${duration > 0 ? (k.time / duration) * 100 : 0}%`;
	}

	function togglePlay() {
		shotStore.setPlaying(!playing);
	}

	// ── object keyframe row ─────────────────────────────────────────

	function addKeyframeAtPlayhead() {
		if (!selectedObject) return;
		shotStore.addKeyframe(selectedObject.id, elapsed);
	}

	function commitStagedKeyframe() {
		if (!selectedObject) return;
		shotStore.commitLiveKeyframe(selectedObject.id);
	}

	function deleteSelectedKeyframe() {
		if (!selectedObject || !selectedKeyframeId) return;
		shotStore.deleteKeyframe(selectedObject.id, selectedKeyframeId);
	}

	function onMarkerPointerDown(e: PointerEvent, keyframeId: string) {
		if (!selectedObject) return;
		e.stopPropagation();
		shotStore.selectKeyframe(keyframeId);
		draggingKeyframeId = keyframeId;
		const onMove = (ev: PointerEvent) => {
			if (!draggingKeyframeId) return;
			shotStore.moveKeyframeTime(selectedObject.id, draggingKeyframeId, timeAt(ev.clientX));
		};
		const onUp = () => {
			draggingKeyframeId = null;
			window.removeEventListener('pointermove', onMove);
			window.removeEventListener('pointerup', onUp);
		};
		window.addEventListener('pointermove', onMove);
		window.addEventListener('pointerup', onUp);
	}
</script>

<div class="timeline">
	<div class="controls">
		<button class="icon-btn" onclick={togglePlay} title={playing ? t('shot.timelinePause') : t('shot.timelinePlay')}>
			{playing ? '❚❚' : '▶'}
		</button>
		<label class="dur">
			<input
				type="number"
				min="1"
				max={MAX_TIMELINE_DURATION}
				step="1"
				value={Math.round(duration)}
				onchange={onDurationInput}
			/>
			<span>s</span>
		</label>
		<span class="time">{elapsed.toFixed(1)}s</span>
	</div>

	<div
		class="track"
		bind:this={trackEl}
		onpointerdown={onTrackScrub}
		role="slider"
		aria-label={t('shot.timelinePlayhead')}
		aria-valuemin={0}
		aria-valuemax={duration}
		aria-valuenow={elapsed}
		tabindex="0"
	>
		<div class="rail"></div>
		<div class="playhead" style={`left:${pct}%`}></div>
		{#each track as k (k.id)}
			<button
				class="keyframe cam"
				style={keyframeStyle(k)}
				title={t('shot.camKeyframe', { time: k.time.toFixed(1) })}
				onclick={(e) => {
					e.stopPropagation();
					shotStore.setElapsed(k.time);
				}}
				ondblclick={(e) => {
					e.stopPropagation();
					shotStore.removeCameraKeyframe(k.id);
				}}
			></button>
		{/each}
		{#if selectedObject}
			{#each selectedObject.keyframes as k (k.id)}
				<button
					class="keyframe obj"
					class:selected={k.id === selectedKeyframeId}
					style={`left:${duration > 0 ? (k.time / duration) * 100 : 0}%`}
					title={t('shot.objKeyframe', { name: selectedObject.name, time: k.time.toFixed(1) })}
					onpointerdown={(e) => onMarkerPointerDown(e, k.id)}
					ondblclick={(e) => {
						e.stopPropagation();
						shotStore.deleteKeyframe(selectedObject.id, k.id);
					}}
				></button>
			{/each}
		{/if}
	</div>

	<div class="actions">
		<button class="icon-btn" onclick={() => shotStore.requestCameraKeyframeAt(elapsed)} title={t('shot.keyframeCamAdd')}>
			◉
		</button>		<button class="icon-btn" onclick={() => shotStore.removeCameraKeyframeAt(elapsed)} title={t('shot.keyframeCamRemove')}>
			◌
		</button>
		{#if selectedObject}
			<span class="obj-actions">
				<button class="icon-btn obj-add" onclick={addKeyframeAtPlayhead} title={t('shot.keyframeObjAdd', { name: selectedObject.name })}>
					◆+
				</button>
				<button
					class="icon-btn"
					onclick={deleteSelectedKeyframe}
					disabled={!selectedKeyframeId || selectedObject.keyframes.length <= 1}
					title={selectedKeyframeId ? t('shot.keyframeObjDelete') : t('shot.keyframeObjSelectFirst')}
				>
					◆−
				</button>
			</span>
		{/if}
		<button
			class="export"
			onclick={onExportVideo}
			disabled={exporting || !canExport}
			title={canExport ? t('shot.exportTitle') : exportDisabledReason}
		>
			{t('shot.exportVideo')}
		</button>
	</div>
</div>

<style>
	.timeline {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 6px 12px;
		border-top: 1px solid var(--border, #232733);
	}
	.controls {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.icon-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		min-width: 26px;
		height: 26px;
		border: 1px solid var(--border, #2a2f3a);
		background: transparent;
		color: inherit;
		border-radius: 6px;
		cursor: pointer;
		font-size: 11px;
		padding: 0 6px;
	}
	.icon-btn:hover:not(:disabled) {
		background: rgba(255, 255, 255, 0.06);
	}
	.icon-btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.obj-actions {
		display: flex;
		gap: 4px;
	}
	.obj-add {
		color: #7dd88a;
	}
	.export {
		border: 1px solid var(--border, #2a2f3a);
		background: transparent;
		color: inherit;
		border-radius: 6px;
		padding: 5px 10px;
		font-size: 12px;
		cursor: pointer;
		white-space: nowrap;
	}
	.export:hover:not(:disabled) {
		border-color: var(--accent, #6366f1);
	}
	.export:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.dur {
		display: flex;
		align-items: center;
		gap: 3px;
		font-size: 11px;
		opacity: 0.7;
	}
	.dur input {
		width: 46px;
		background: rgba(0, 0, 0, 0.3);
		border: 1px solid var(--border, #2a2f3a);
		color: inherit;
		border-radius: 4px;
		padding: 3px 5px;
		font-size: 12px;
	}
	.time {
		font-size: 11px;
		opacity: 0.55;
		min-width: 38px;
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.track {
		position: relative;
		flex: 1;
		height: 26px;
		cursor: pointer;
		touch-action: none;
	}
	.rail {
		position: absolute;
		inset: 11px 0;
		border-radius: 3px;
		background: rgba(255, 255, 255, 0.08);
	}
	.playhead {
		position: absolute;
		top: 2px;
		bottom: 2px;
		width: 2px;
		background: var(--accent, #6366f1);
		transform: translateX(-1px);
		pointer-events: none;
	}
	.keyframe {
		position: absolute;
		top: 50%;
		width: 10px;
		height: 10px;
		border-radius: 2px;
		transform: translate(-5px, -50%) rotate(45deg);
		background: #e0b34d;
		border: 1px solid rgba(0, 0, 0, 0.5);
		cursor: grab;
		padding: 0;
		z-index: 2;
	}
	.keyframe.cam {
		background: #e0b34d;
	}
	.keyframe.obj {
		background: #4ade80;
	}
	.keyframe.obj.selected {
		background: #f87171;
	}
	.keyframe:hover {
		filter: brightness(1.15);
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 6px;
	}
</style>
