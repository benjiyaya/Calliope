<script lang="ts">
	import { tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { toStore } from 'svelte/store';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { assetUrl, jobsApi, projects, type Job, type Scene, type Clip } from '$lib/api';
	import { estimateTargetSeconds } from '$lib/durationBudget';
	import { agentDeepLink } from '$lib/agentTasks';
	import { toast } from '$lib/toast';
	import { t } from '$lib/i18n.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import StatusChip from '$lib/components/ui/StatusChip.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		projectId: number;
	}

	let { projectId }: Props = $props();
	const client = useQueryClient();
	let editing = $state<Scene | null>(null);
	let editOpen = $state(false);
	let highlightId = $state<number | null>(null);
	let adding = $state(false);
	let deletingId = $state<number | null>(null);
	let sceneToDelete = $state<Scene | null>(null);
	let deleteOpen = $state(false);

	const scenesQuery = createQuery(
		toStore(() => ({
			queryKey: ['scenes', projectId],
			queryFn: () => projects.getScenes(projectId),
		})),
	);

	// Shared cache key with QueueStage — clip job state shows up on scene cards
	// too. Refreshed via SSE invalidation from the project page, not polling.
	const jobsQuery = createQuery(
		toStore(() => ({
			queryKey: ['jobs', projectId],
			queryFn: () => jobsApi.list(projectId),
		})),
	);

	// Cache-shared with the parent's story query — only used for the target-length hint.
	const storyQuery = createQuery(
		toStore(() => ({
			queryKey: ['story', projectId],
			queryFn: () => projects.getStory(projectId),
		})),
	);

	// Cache-shared with AssetsStage — resolves scene location chips.
	const assetsQuery = createQuery(
		toStore(() => ({
			queryKey: ['assets', projectId],
			queryFn: () => projects.getAssets(projectId),
		})),
	);

	const scenes = $derived($scenesQuery.data?.scenes ?? []);
	const sceneCount = $derived(scenes.length);
	const totalClips = $derived(scenes.reduce((n, s) => n + (s.clips?.length ?? 0), 0));
	const totalSec = $derived($scenesQuery.data?.estimated_duration_sec ?? 0);
	const targetSec = $derived.by(() => {
		const target = $storyQuery.data?.project?.target_duration;
		return target ? estimateTargetSeconds(target) : 0;
	});

	const busy = $derived(adding || deletingId != null);

	// ── Break into shots (coverage expansion) ────────────────────────────
	let expandingIds = $state<number[]>([]);
	let expandingAll = $state(false);
	let expandedIds = $state<number[]>([]);

	function isExpanded(scene: Scene): boolean {
		return (scene.clips?.length ?? 0) > 1 || expandedIds.includes(scene.id);
	}

	async function breakIntoShots(scene: Scene) {
		if (expandingIds.length > 0 || expandingAll) return;
		expandingIds = [...expandingIds, scene.id];
		try {
			const res = await projects.expandClips(projectId, { scene_ids: [scene.id] });
			expandedIds = [...new Set([...expandedIds, scene.id])];
			await client.invalidateQueries({ queryKey: ['scenes'] });
			toast.success(
				t('script.toast.breakIntoShots', {
					num: scene.order_index,
					clips: res.scenes?.[0]?.clip_count ?? '?',
				}),
			);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('script.toast.breakFail'));
		} finally {
			expandingIds = expandingIds.filter((id) => id !== scene.id);
		}
	}

	async function breakAllIntoShots() {
		if (expandingIds.length > 0 || expandingAll) return;
		if (
			!window.confirm(t('script.toast.breakAllConfirm', { count: sceneCount }))
		)
			return;
		expandingAll = true;
		try {
			const res = await projects.expandClips(projectId, { all: true });
			expandedIds = scenes.map((s) => s.id);
			await client.invalidateQueries({ queryKey: ['scenes'] });
			toast.success(t('script.toast.boardExpanded', { count: res.total_clips }));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('script.toast.breakAllFail'));
		} finally {
			expandingAll = false;
		}
	}

	async function deleteClip(clip: Clip, scene: Scene) {
		try {
			await projects.deleteClip(projectId, clip.id);
			await client.invalidateQueries({ queryKey: ['scenes'] });
			toast.success(t('script.toast.deletedShot', { num: `${scene.order_index}.${clip.order_index}` }));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('script.toast.deleteClipFail'));
		}
	}

	const saveMutation = createMutation({
		mutationFn: () =>
			projects.updateScene(projectId, editing!.id, {
				heading: editing!.heading,
				action: editing!.action,
				dialog: editing!.dialog,
				duration_sec: editing!.duration_sec,
			}),
		onSuccess: () => {
			editing = null;
			editOpen = false;
			client.invalidateQueries({ queryKey: ['scenes'] });
			toast.success(t('script.toast.sceneSaved'));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('script.toast.saveSceneFail'));
		},
	});

	// Chain-from-previous toggle — persists on the scene; consumed at render time.
	let chainPendingId = $state<number | null>(null);
	async function toggleChain(scene: Scene) {
		if (chainPendingId != null) return;
		chainPendingId = scene.id;
		try {
			await projects.updateScene(projectId, scene.id, {
				chain_from_prev: !scene.chain_from_prev,
			});
			await client.invalidateQueries({ queryKey: ['scenes'] });
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('script.toast.updateSceneFail'));
		} finally {
			chainPendingId = null;
		}
	}

	function jobForScene(sceneId: number): Job | undefined {
		const jobs = ($jobsQuery.data ?? []).filter(
			(j) => j.scene_id === sceneId && j.kind === 'video',
		);
		if (jobs.length === 0) return undefined;
		return [...jobs].sort((a, b) => b.id - a.id)[0];
	}

	function jobForClip(clipId: number): Job | undefined {
		const jobs = ($jobsQuery.data ?? []).filter(
			(j) => (j as Job & { clip_id?: number | null }).clip_id === clipId && j.kind === 'video',
		);
		if (jobs.length === 0) return undefined;
		return [...jobs].sort((a, b) => b.id - a.id)[0];
	}

	/** Per-scene production status — only uses fields the API actually exposes. */
	function sceneStatus(scene: Scene): { status: string; label: string } {
		const job = jobForScene(scene.id);
		if (job && (job.status === 'pending' || job.status === 'running')) {
			return { status: 'generating', label: t('script.status.rendering') };
		}
		if (job?.status === 'failed') return { status: 'failed', label: t('script.status.renderFailed') };
		if (scene.video_path || job?.status === 'done') {
			return { status: 'ready', label: t('script.status.clipReady') };
		}
		return { status: 'idle', label: t('script.status.noClip') };
	}

	function locationName(scene: Scene): string | null {
		if (scene.location_id == null) return null;
		const locs = $assetsQuery.data?.locations ?? [];
		return locs.find((l) => l.id === scene.location_id)?.name ?? null;
	}

	function avatarFor(char: { portrait_path: string | null; sheet_path: string | null }) {
		return assetUrl(char.portrait_path ?? char.sheet_path);
	}

	function initialsOf(name: string): string {
		const parts = name.trim().split(/\s+/).filter(Boolean);
		if (parts.length === 0) return '?';
		return parts
			.slice(0, 2)
			.map((p) => p[0].toUpperCase())
			.join('');
	}

	/** Hide a broken avatar image (portrait moved/deleted on disk), revealing initials. */
	function hideBrokenAvatar(e: Event) {
		const img = e.currentTarget as HTMLImageElement | null;
		if (img) img.style.display = 'none';
	}

	function formatClock(sec: number): string {
		const s = Math.max(0, Math.round(sec));
		const m = Math.floor(s / 60);
		const r = s % 60;
		return `${m}:${r.toString().padStart(2, '0')}`;
	}

	async function move(sceneId: number, dir: -1 | 1) {
		if (busy) return;
		const list = $scenesQuery.data?.scenes ?? [];
		const idx = list.findIndex((s) => s.id === sceneId);
		const swap = idx + dir;
		if (idx < 0 || swap < 0 || swap >= list.length) return;
		const ids = list.map((s) => s.id);
		[ids[idx], ids[swap]] = [ids[swap], ids[idx]];
		await projects.reorderScenes(projectId, ids);
		client.invalidateQueries({ queryKey: ['scenes'] });
	}

	/** Hand off script regeneration to a fresh, project-linked agent chat. */
	function requestRegenerate() {
		if (busy) return;
		goto(agentDeepLink(projectId, 'script'));
	}

	function openEdit(scene: Scene) {
		editing = { ...scene };
		editOpen = true;
	}

	async function addScene() {
		if (busy) return;
		adding = true;
		try {
			const existing = $scenesQuery.data?.scenes ?? [];
			const nextIndex = existing.length + 1;
			const created = await projects.createScene(projectId, {
				order_index: nextIndex,
				heading: `NEW SCENE ${nextIndex}`,
				action: '',
				dialog: '',
				duration_sec: 5,
			});
			// Keep order_index contiguous 1..n so Regenerate sees the real board size
			const ids = [...existing.map((s) => s.id), created.id];
			await projects.reorderScenes(projectId, ids);
			await client.invalidateQueries({ queryKey: ['scenes'] });
			await client.refetchQueries({ queryKey: ['scenes', projectId] });
			editing = { ...created, order_index: nextIndex, heading: `NEW SCENE ${nextIndex}` };
			editOpen = true;
			highlightId = created.id;
			await tick();
			document
				.getElementById(`scene-${created.id}`)
				?.scrollIntoView({ behavior: 'smooth', block: 'center' });
			toast.success(t('script.toast.sceneAdded', { num: nextIndex }));
			window.setTimeout(() => {
				if (highlightId === created.id) highlightId = null;
			}, 2800);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('script.toast.addSceneFail'));
		} finally {
			adding = false;
		}
	}

	function requestDelete(scene: Scene) {
		if (busy) return;
		sceneToDelete = scene;
		deleteOpen = true;
	}

	async function confirmDeleteScene() {
		const scene = sceneToDelete;
		sceneToDelete = null;
		if (!scene || busy) return;
		const label = scene.heading?.trim() || `Scene #${scene.order_index}`;
		deletingId = scene.id;
		try {
			await projects.deleteScene(projectId, scene.id);
			if (editing?.id === scene.id) {
				editing = null;
				editOpen = false;
			}
			const remaining = ($scenesQuery.data?.scenes ?? [])
				.filter((s) => s.id !== scene.id)
				.map((s) => s.id);
			if (remaining.length > 0) {
				await projects.reorderScenes(projectId, remaining);
			}
			await client.invalidateQueries({ queryKey: ['scenes'] });
			toast.success(t('script.toast.deleted', { name: label }));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('script.toast.deleteFailed'));
		} finally {
			deletingId = null;
		}
	}
</script>

<header class="stage-header">
	<div>
		<h2>3. Script</h2>
		<p class="muted">
			Estimated duration: {formatClock(totalSec)}
			{#if targetSec > 0}
				/ target ~{formatClock(targetSec)}
			{/if}
			{#if sceneCount > 0}
				· {sceneCount} scenes
				{#if totalClips > 0}
					/ {totalClips} shot clips
				{/if}
			{/if}
		</p>
	</div>
	<div class="stage-actions">
		<Button variant="secondary" disabled={busy} loading={adding} onclick={addScene}>
			<Icon name="plus" size={14} /> {t('script.addScene')}
		</Button>
		<Button
			variant="secondary"
			disabled={busy || expandingAll || expandingIds.length > 0 || sceneCount === 0}
			loading={expandingAll}
			onclick={breakAllIntoShots}
			title={t('script.breakAllTitle')}
		>
			<Icon name="film" size={14} /> {t('script.breakAllIntoShots')}
		</Button>
		<Button variant="primary" disabled={busy} onclick={requestRegenerate}>
			<Icon name="sparkle" size={15} /> {t('script.regenerate')}
		</Button>
	</div>
</header>

<div class="script-panel">
	{#if $scenesQuery.isLoading}
		<div class="card">{t('script.loadingScenes')}</div>
	{:else if sceneCount === 0}
		<EmptyState
			title={t('script.noScenesTitle')}
			body={t('script.noScenesBody')}
		>
			{#snippet icon()}
				<Icon name="script" size={28} />
			{/snippet}
			{#snippet action()}
				<Button variant="secondary" disabled={busy} loading={adding} onclick={addScene}>
					<Icon name="plus" size={14} /> {t('script.addScene')}
				</Button>
			{/snippet}
		</EmptyState>
	{:else}
		{#each scenes as scene, i (scene.id)}
			{@const st = sceneStatus(scene)}
			{@const locName = locationName(scene)}
			<article
				id="scene-{scene.id}"
				class="card scene"
				class:fresh={highlightId === scene.id}
				class:deleting={deletingId === scene.id}
			>
				<div class="scene-head">
					<div class="scene-title">
						<span class="grip" aria-hidden="true"><Icon name="drag" size={14} /></span>
						<span class="num">#{scene.order_index}</span>
						<strong>{scene.heading || t('script.untitledScene')}</strong>
						{#if highlightId === scene.id}
							<span class="fresh-tag">{t('script.justAdded')}</span>
						{/if}
						<StatusChip status={st.status} label={st.label} />
					</div>
					<div class="actions">
						<button
							type="button"
							class="icon-btn"
							aria-label={t('script.moveSceneUp')}
							title={t('script.moveSceneUp')}
							disabled={busy || i === 0}
							onclick={() => move(scene.id, -1)}
						>
							<Icon name="chevron-up" size={15} />
						</button>
						<button
							type="button"
							class="icon-btn"
							aria-label={t('script.moveSceneDown')}
							title={t('script.moveSceneDown')}
							disabled={busy || i === sceneCount - 1}
							onclick={() => move(scene.id, 1)}
						>
							<Icon name="chevron-down" size={15} />
						</button>
						<Button variant="secondary" size="sm" disabled={busy} onclick={() => openEdit(scene)}>
							{t('script.edit')}
						</Button>
						<Button
							variant="danger"
							size="sm"
							disabled={busy}
							loading={deletingId === scene.id}
							onclick={() => requestDelete(scene)}
						>
							{t('script.delete')}
						</Button>
					</div>
				</div>
				{#if scene.action}
					<p class="muted">{scene.action}</p>
				{:else}
					<p class="muted empty-line">{t('script.noActionYet')}</p>
				{/if}
				{#if scene.dialog}
					<pre class="dialog">{scene.dialog}</pre>
				{/if}
				<div class="clip-block">
					<div class="clip-head">
						<span class="clip-title">
							<Icon name="film" size={12} />
							{(scene.clips ?? []).length}
							{((scene.clips ?? []).length === 1 ? t('script.shotClipSingular') : t('script.shotClipPlural', { n: (scene.clips ?? []).length }))}
							{#if (scene.clips ?? []).some((c) => c.clip_path)}
								· {t('script.renderedCount', { count: (scene.clips ?? []).filter((c) => c.clip_path).length })}
							{/if}
						</span>
						<Button
							variant="secondary"
							size="sm"
							disabled={busy || expandingIds.length > 0 || expandingAll}
							loading={expandingIds.includes(scene.id)}
							onclick={() => breakIntoShots(scene)}
							title={isExpanded(scene) ? t('script.rebreakTitle') : t('script.breakTitle')}
						>
							{isExpanded(scene) ? t('script.rebreak') : t('script.break')}
						</Button>
					</div>
					{#if isExpanded(scene) && (scene.clips ?? []).length > 1}
						<ul class="clip-list">
							{#each scene.clips ?? [] as clip (clip.id)}
								{@const clipJob = jobForClip(clip.id)}
								<li class="clip-row">
									<span class="clip-num" class:ready={Boolean(clip.clip_path)}>
										#{scene.order_index}.{clip.order_index}
									</span>
									<span class="clip-desc">{clip.description || t('script.untitledShot')}</span>
									{#if clip.shot_size}
										<span class="chip">{clip.shot_size}</span>
									{/if}
									{#if clipJob && (clipJob.status === 'pending' || clipJob.status === 'running')}
										<span class="chip chip-render">{t('script.rendering')}</span>
									{:else if clip.clip_path}
										<span class="chip chip-ok">✓ {t('script.clipDone')}</span>
									{/if}
									{#if clip.duration_sec}
										<span class="clip-dur">{clip.duration_sec}s</span>
									{/if}
									<button
										type="button"
										class="icon-btn"
										aria-label={t('script.deleteShotAria', { id: `${scene.order_index}.${clip.order_index}` })}
										title={t('script.deleteShotTitle')}
										onclick={() => deleteClip(clip, scene)}
									>
										<Icon name="trash" size={13} />
									</button>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
				<div class="chips">
					{#each scene.characters ?? [] as c (c.id)}
						{@const avatar = avatarFor(c)}
						<span class="chip">
							<span class="avatar">
								<span class="initials" aria-hidden="true">{initialsOf(c.name)}</span>
								{#if avatar}
									<img src={avatar} alt="" loading="lazy" onerror={hideBrokenAvatar} />
								{/if}
							</span>
							{c.name}
						</span>
					{/each}
					{#if locName}
						<span class="chip"><Icon name="folder" size={12} /> {locName}</span>
					{/if}
					{#if scene.duration_sec}
						<span class="chip"><Icon name="clock" size={12} /> {formatClock(scene.duration_sec)}</span>
					{/if}
					{#if i > 0}
						<button
							type="button"
							class="chip chip-toggle"
							class:chip-on={Boolean(scene.chain_from_prev)}
							disabled={chainPendingId === scene.id}
						title={t('script.chainTitle')}
						onclick={() => toggleChain(scene)}
					>
						<Icon name="film" size={12} />
						{Boolean(scene.chain_from_prev) ? t('script.chainOn') : t('script.chainOff')}
						</button>
					{/if}
				</div>
			</article>
		{/each}
	{/if}
</div>

<Modal
	bind:open={editOpen}
	title={editing ? t('script.editSceneNumbered', { n: editing.order_index }) : t('script.editScene')}
	onclose={() => (editing = null)}
>
	{#if editing}
		<label class="field">
			<span class="field-label">{t('script.headingField')}</span>
			<input class="field-input" bind:value={editing.heading} placeholder={t('script.headingPlaceholder')} />
		</label>
		<label class="field">
			<span class="field-label">{t('script.actionField')}</span>
			<textarea
				class="field-textarea"
				bind:value={editing.action}
				rows="4"
				placeholder={t('script.actionPlaceholder')}
			></textarea>
		</label>
		<label class="field">
			<span class="field-label">{t('script.dialogField')}</span>
			<textarea
				class="field-textarea"
				bind:value={editing.dialog}
				rows="4"
				placeholder={t('script.dialogPlaceholder')}
			></textarea>
		</label>
		<label class="field">
			<span class="field-label">{t('script.durationField')}</span>
			<input class="field-input" type="number" bind:value={editing.duration_sec} min="1" />
		</label>
	{/if}
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (editOpen = false)}>{t('common.cancel')}</Button>
		<Button
			variant="primary"
			loading={$saveMutation.isPending}
			onclick={() => $saveMutation.mutate()}
		>
			{t('script.save')}
		</Button>
	{/snippet}
</Modal>

<ConfirmDialog
	bind:open={deleteOpen}
	title={t('script.deleteSceneTitle')}
	message={sceneToDelete
		? t('script.deleteSceneMessage', {
				heading: sceneToDelete.heading?.trim() || t('script.sceneNumber', { n: sceneToDelete.order_index }),
			})
		: ''}
	confirmLabel={t('script.delete')}
	danger
	onconfirm={() => void confirmDeleteScene()}
	oncancel={() => (sceneToDelete = null)}
/>

<style>
	.stage-header {
		/* Pins inside the stage scroll container so Add Scene / Regenerate
		   stay reachable while scrolling long scripts. */
		position: sticky;
		top: 0;
		z-index: 5;
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		margin-bottom: var(--space-lg);
		gap: var(--space-md);
		padding: 8px 0 12px;
		background: var(--bg-primary);
		border-bottom: 1px solid var(--border);
	}
	.stage-header h2 {
		margin: 0 0 4px;
		font-size: 22px;
		font-weight: 700;
	}
	.stage-actions {
		display: flex;
		gap: 8px;
	}
	.muted {
		color: var(--text-secondary);
		margin: 0;
	}
	.script-panel {
		position: relative;
		min-height: 220px;
	}
	.card {
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		padding: var(--space-lg);
		margin-bottom: var(--space-md);
		scroll-margin-top: 84px;
		scroll-margin-bottom: 24px;
	}
	.scene {
		transition:
			border-color 0.25s ease,
			box-shadow 0.25s ease,
			opacity 0.2s ease;
	}
	.scene.fresh {
		border-color: var(--accent);
		box-shadow:
			0 0 0 1px var(--accent),
			0 0 24px var(--accent-glow);
		animation: scene-in 0.45s ease-out;
	}
	.scene.deleting {
		opacity: 0.45;
	}
	@keyframes scene-in {
		from {
			opacity: 0.35;
			transform: translateY(10px);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.scene.fresh {
			animation: none;
		}
	}
	.scene-head {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 12px;
		margin-bottom: 8px;
	}
	.scene-title {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		min-width: 0;
	}
	.grip {
		display: inline-flex;
		color: var(--text-muted);
		cursor: grab;
		opacity: 0.6;
	}
	.num {
		color: var(--accent);
		font-weight: 700;
	}
	.fresh-tag {
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--accent);
		background: var(--accent-glow);
		border-radius: 999px;
		padding: 2px 8px;
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		flex-shrink: 0;
	}
	.icon-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border: 1px solid transparent;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-secondary);
		cursor: pointer;
	}
	.icon-btn:hover:not(:disabled) {
		background: rgba(255, 255, 255, 0.05);
		color: var(--text-primary);
		border-color: var(--border);
	}
	.icon-btn:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}
	.icon-btn:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.empty-line {
		font-style: italic;
		color: var(--text-muted);
	}
	.dialog {
		background: var(--bg-elevated);
		padding: 12px;
		border-radius: var(--radius-sm);
		white-space: pre-wrap;
		font-family: var(--font-mono);
		font-size: 13px;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 10px;
	}
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 2px 10px;
		font-size: 12px;
		color: var(--text-secondary);
	}
	.chip-toggle {
		cursor: pointer;
		font-family: inherit;
	}
	.chip-toggle:hover {
		color: var(--text-primary);
		border-color: #52525b;
	}
	.chip-toggle:disabled {
		opacity: 0.6;
		cursor: wait;
	}
	.chip-on {
		color: var(--accent);
		border-color: var(--accent);
		background: color-mix(in srgb, var(--accent) 12%, var(--bg-elevated));
	}
	.clip-block {
		margin-top: 10px;
		border-top: 1px dashed var(--border);
		padding-top: 8px;
	}
	.clip-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 8px;
	}
	.clip-title {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		font-weight: 600;
		color: var(--text-secondary);
	}
	.clip-list {
		list-style: none;
		margin: 8px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.clip-row {
		display: flex;
		align-items: center;
		gap: 8px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 4px 8px;
		font-size: 12px;
	}
	.clip-num {
		font-family: var(--font-mono);
		font-weight: 700;
		color: var(--text-secondary);
		flex-shrink: 0;
	}
	.clip-num.ready {
		color: var(--accent);
	}
	.clip-desc {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--text-primary);
	}
	.clip-dur {
		color: var(--text-muted);
		flex-shrink: 0;
	}
	.chip-ok {
		color: var(--accent);
		border-color: var(--accent);
	}
	.chip-render {
		color: var(--warning, #eab308);
		border-color: var(--warning, #eab308);
	}
	.avatar {
		position: relative;
		width: 20px;
		height: 20px;
		border-radius: 50%;
		overflow: hidden;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
	}
	.avatar img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.initials {
		font-size: 9px;
		font-weight: 700;
		color: var(--accent);
		letter-spacing: 0.02em;
	}
</style>
