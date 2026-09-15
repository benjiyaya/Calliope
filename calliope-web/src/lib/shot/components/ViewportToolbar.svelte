<script lang="ts">
	/**
	 * Viewport toolbar pill — Svelte port of open-media's WorkspaceToolbar
	 * (ToolButtons) + ComposerShell topbar actions: gizmo tools (Select /
	 * Move / Rotate / Scale / Pose), undo/redo, grid + composition-guide
	 * toggles, and the capture button. Tool state lives on the shot store;
	 * the two view toggles are local (the viewport owns the grid mesh and
	 * the CSS overlay) and passed up through bindable props.
	 */
	import { shotStore, type ComposerTool } from '../shotStore.svelte';
	import { t } from '$lib/i18n.svelte';

	let {
		oncapture,
		showGrid = $bindable(true),
		showComposition = $bindable(false),
	}: {
		oncapture?: () => void;
		showGrid?: boolean;
		showComposition?: boolean;
	} = $props();

	const activeTool = $derived(shotStore.activeTool);
	const selected = $derived(shotStore.selectedObject);
	// Pose applies to mannequins only (mirrors the store's canPoseSelection).
	const canPose = $derived(
		!!selected && selected.type !== 'camera' && !['cube', 'plane', 'cylinder', 'sphere', 'capsule', 'cone', 'torus'].includes(selected.type),
	);

	function setTool(tool: ComposerTool) {
		shotStore.setActiveTool(tool);
	}

	const TOOLS = $derived<{ id: ComposerTool; label: string; title: string; disabled?: boolean }[]>([
		{ id: 'select', label: t('shot.toolSelect'), title: t('shot.toolSelectTitle') },
		{ id: 'move', label: t('shot.toolMove'), title: t('shot.toolMoveTitle') },
		{ id: 'rotate', label: t('shot.toolRotate'), title: t('shot.toolRotateTitle') },
		{ id: 'scale', label: t('shot.toolScale'), title: t('shot.toolScaleTitle') },
		{ id: 'pose', label: t('shot.toolPose'), title: canPose ? t('shot.toolPoseTitle') : t('shot.toolPoseDisabled'), disabled: !canPose },
	]);
</script>

<div class="toolbar" role="toolbar" aria-label={t('shot.viewportTools')}>
	<div class="group">
		{#each TOOLS as tool (tool.id)}
			<button
				class="btn"
				class:on={activeTool === tool.id}
				disabled={tool.disabled}
				onclick={() => setTool(tool.id)}
				title={tool.title}
			>
				{tool.label}
			</button>
		{/each}
	</div>

	<span class="divider"></span>

	<div class="group">
		<button class="btn" onclick={() => shotStore.undo()} disabled={!shotStore.canUndo} title={t('shot.undoTitle')}>
			↶
		</button>
		<button class="btn" onclick={() => shotStore.redo()} disabled={!shotStore.canRedo} title={t('shot.redoTitle')}>
			↷
		</button>
	</div>

	<span class="divider"></span>

	<div class="group">
		<button class="btn" class:on={showGrid} onclick={() => (showGrid = !showGrid)} title={t('shot.gridTitle')}>
			{t('shot.grid')}
		</button>
		<button class="btn" class:on={showComposition} onclick={() => (showComposition = !showComposition)} title={t('shot.compositionTitle')}>
			{t('shot.composition')}
		</button>
	</div>

	<span class="divider"></span>

	<button class="btn capture" onclick={() => oncapture?.()} title={t('shot.captureTitle')}>
		◎ {t('shot.capture')}
	</button>
</div>

<style>
	.toolbar {
		display: flex;
		align-items: center;
		gap: 6px;
		border: 1px solid var(--border, #2a2f3a);
		background: rgba(16, 18, 24, 0.82);
		border-radius: 999px;
		padding: 4px 8px;
		backdrop-filter: blur(4px);
	}
	.group {
		display: flex;
		align-items: center;
		gap: 2px;
	}
	.divider {
		width: 1px;
		height: 16px;
		background: var(--border, #2a2f3a);
		margin: 0 2px;
	}
	.btn {
		border: 1px solid transparent;
		background: transparent;
		color: inherit;
		border-radius: 999px;
		padding: 3px 9px;
		font-size: 12px;
		font-weight: 500;
		cursor: pointer;
		white-space: nowrap;
		opacity: 0.85;
	}
	.btn:hover:not(:disabled) {
		background: rgba(255, 255, 255, 0.08);
		opacity: 1;
	}
	.btn.on {
		background: rgba(99, 102, 241, 0.25);
		border-color: var(--accent, #6366f1);
		opacity: 1;
	}
	.btn:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}
	.capture {
		border-color: var(--border, #2a2f3a);
	}
	.capture:hover {
		border-color: var(--accent, #6366f1);
	}
</style>
