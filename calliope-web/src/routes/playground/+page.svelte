<script lang="ts">
	import { onMount } from 'svelte';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import OmniComposer from '$lib/components/OmniComposer.svelte';
	import AttachToProject from '$lib/components/AttachToProject.svelte';
	import ImageLightbox from '$lib/components/ImageLightbox.svelte';
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import { assetUrl, jobsApi, playgroundApi, workflows, type Job, type Workflow } from '$lib/api';
	import { connectEvents } from '$lib/events';
	import { handleJobEvent, progressFor } from '$lib/jobProgress';
	import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import { toast } from '$lib/toast';

	const client = useQueryClient();

	let workflowId = $state<number | ''>('');
	let formValues = $state<Record<string, string | number>>({});
	let formError = $state('');
	let lastWorkflowId = $state<number | '' | null>(null);
	let previewSrc = $state<string | null>(null);
	let previewAlt = $state('');
	let previewKind = $state<'image' | 'video'>('image');
	let deletingId = $state<number | null>(null);
	let attempted = $state(false);
	let missingLabels = $state<string[]>([]);

	const workflowsQuery = createQuery({
		queryKey: ['workflows'],
		queryFn: workflows.list,
	});

	const jobsQuery = createQuery({
		queryKey: ['playground-jobs'],
		queryFn: playgroundApi.jobs,
		refetchInterval: (q) => {
			const list = (q.state.data as Job[] | undefined) ?? [];
			const busy = list.some((j) => j.status === 'pending' || j.status === 'running');
			return busy ? 5000 : false;
		},
		placeholderData: (prev) => prev,
	});

	const enabledWorkflows = $derived(
		($workflowsQuery.data ?? []).filter((w: Workflow) => w.is_enabled),
	);

	const selected = $derived(enabledWorkflows.find((w) => w.id === workflowId) ?? null);

	// Mode: filter by workflow kind — Video Generation / Image Generation
	let mode = $state<'video' | 'image'>('video');

	const modeWorkflows = $derived(enabledWorkflows.filter((w) => w.kind === mode));

	// When mode changes, keep workflowId valid within the mode's list
	$effect(() => {
		const list = modeWorkflows;
		if (list.length === 0) return;
		if (workflowId === '' || !list.some((w) => w.id === workflowId)) {
			workflowId = list[0].id;
		}
	});

	// If default mode 'video' has no workflows but image does, flip
	$effect(() => {
		if (workflowId === '' && enabledWorkflows.length > 0) {
			const hasVideo = enabledWorkflows.some((w) => w.kind === 'video');
			if (!hasVideo) mode = 'image';
		}
	});

	$effect(() => {
		if (workflowId === lastWorkflowId) return;
		lastWorkflowId = workflowId;
		formValues = {};
		formError = '';
		attempted = false;
	});

	const generateMutation = createMutation({
		mutationFn: () => {
			if (workflowId === '') throw new Error('Select a workflow');
			// Seed defaults for required-but-unset fields from schema
			const withDefaults: Record<string, string | number> = { ...formValues };
			for (const inp of selected?.input_schema ?? []) {
				if (
					withDefaults[inp.nodeId] === undefined &&
					inp.defaultValue !== undefined &&
					inp.defaultValue !== ''
				) {
					withDefaults[inp.nodeId] = inp.defaultValue;
				}
			}
			return playgroundApi.generate({
				workflow_id: Number(workflowId),
				input_values: withDefaults,
			});
		},
		onSuccess: () => {
			formError = '';
			client.invalidateQueries({ queryKey: ['playground-jobs'] });
			toast.success('Generation queued');
		},
		onError: (err) => {
			formError = err instanceof Error ? err.message : 'Generate failed';
			toast.error(formError);
		},
	});

	function tryGenerate() {
		if ($generateMutation.isPending) return;
		if (workflowId === '') {
			toast.error('Select a workflow first');
			return;
		}
		if (missingLabels.length > 0) {
			attempted = true;
			toast.error(`Missing required inputs: ${missingLabels.join(', ')}`);
			return;
		}
		$generateMutation.mutate();
	}

	async function deleteJob(job: Job) {
		const n = job.output_paths?.length ?? 0;
		const msg =
			n > 0
				? `Delete artifact #${job.id}? This removes the DB record and ${n} file(s) on disk.`
				: `Delete artifact #${job.id}? This removes the DB record.`;
		if (!confirm(msg)) return;
		deletingId = job.id;
		try {
			const r = await playgroundApi.deleteJob(job.id);
			await client.invalidateQueries({ queryKey: ['playground-jobs'] });
			const gone = r.deleted_files?.length ?? 0;
			const miss = r.missing_files?.length ?? 0;
			if (gone && miss) {
				toast.success(`Deleted #${job.id} (${gone} file(s); ${miss} already missing)`);
			} else if (gone) {
				toast.success(`Deleted #${job.id} and ${gone} file(s)`);
			} else {
				toast.success(`Deleted #${job.id}`);
			}
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Delete failed');
		} finally {
			deletingId = null;
		}
	}

	onMount(() => {
		let refreshTimer: ReturnType<typeof setTimeout> | null = null;
		const scheduleRefresh = () => {
			if (refreshTimer) return;
			refreshTimer = setTimeout(() => {
				refreshTimer = null;
				client.invalidateQueries({ queryKey: ['playground-jobs'] });
			}, 400);
		};

		const stop = connectEvents((ev) => {
			// Feed the shared progress store (ProgressBars read it live)
			handleJobEvent(ev);
			if (ev.type === 'job.progress') return;
			if (
				ev.type === 'job.created' ||
				ev.type === 'job.started' ||
				ev.type === 'job.completed' ||
				ev.type === 'job.failed' ||
				ev.type === 'job.deleted' ||
				ev.type === 'asset.ready'
			) {
				scheduleRefresh();
			}
		});

		return () => {
			if (refreshTimer) clearTimeout(refreshTimer);
			stop();
		};
	});

	const jobs = $derived(($jobsQuery.data ?? []) as Job[]);
	const jobsLoading = $derived($jobsQuery.isLoading && jobs.length === 0);
	const doneCount = $derived(jobs.filter((j) => j.status === 'done').length);

	function isVideoPath(path: string, kind: string) {
		const lower = path.toLowerCase();
		return kind === 'video' || lower.endsWith('.mp4') || lower.endsWith('.webm');
	}

	function statusWord(status: string) {
		if (status === 'done') return 'Ready';
		if (status === 'running') return 'Running';
		if (status === 'pending') return 'Queued';
		if (status === 'failed') return 'Failed';
		return status;
	}

	function switchMode(next: 'video' | 'image') {
		mode = next;
	}

	function resetAll() {
		formValues = {};
		formError = '';
		attempted = false;
		missingLabels = [];
	}
</script>

<div class="shell">
	<AppHeader active="playground" />

	<main class="omni-layout">
		<!-- Mode tabs (Kling-style) -->
		<div class="mode-tabs" role="tablist" aria-label="Generation mode">
			<button
				type="button"
				role="tab"
				aria-selected={mode === 'video'}
				class="mode-tab"
				class:active={mode === 'video'}
				onclick={() => switchMode('video')}
			>
				<Icon name="video" size={15} />
				Video Generation
			</button>
			<button
				type="button"
				role="tab"
				aria-selected={mode === 'image'}
				class="mode-tab"
				class:active={mode === 'image'}
				onclick={() => switchMode('image')}
			>
				<Icon name="image" size={15} />
				Image Generation
			</button>
			<div class="spacer"></div>
			<button type="button" class="mode-tab reset" onclick={resetAll} title="Clear all inputs">
				<Icon name="retry" size={14} />
				Reset
			</button>
		</div>

		<!-- Results scroll above the docked composer -->
		<section class="results" aria-labelledby="pg-artifacts">
			<header class="artifacts-head">
				<h2 id="pg-artifacts">Artifacts</h2>
				<p class="count-block">
					<span class="count mono">{jobs.length}</span>
					<span class="count-label">{doneCount} ready</span>
				</p>
			</header>

			{#if jobsLoading}
				<div class="rail">
					<div class="skeleton" aria-hidden="true"></div>
					<div class="skeleton" aria-hidden="true"></div>
				</div>
			{:else if jobs.length === 0}
				<div class="empty-rail">
					<p class="empty-title">Nothing queued yet</p>
					<p class="muted">Generations land here — pending first, then the media.</p>
				</div>
			{:else}
				<ul class="rail">
					{#each jobs as job (job.id)}
						{@const primaryPath = job.output_paths?.[0] ?? null}
						{@const primaryMedia = primaryPath ? assetUrl(primaryPath) : null}
						<li
							class="card"
							class:done={job.status === 'done'}
							class:failed={job.status === 'failed'}
						>
							<div class="card-meta">
								<span class="id mono">#{job.id}</span>
								<span class="badge {job.status}">{statusWord(job.status)}</span>
								<span class="kind muted">{job.kind}</span>
							</div>

							{#if job.status === 'pending' || job.status === 'running'}
								{@const prog = progressFor(job.id)}
								<div class="waiting" aria-busy="true">
									<span class="pulse"></span>
									<div class="waiting-main">
										<span class="waiting-text">
											{job.status === 'running' ? (prog?.message ?? 'Generating…') : 'Queued'}
										</span>
										{#if job.status === 'running'}
											<ProgressBar
												size="sm"
												value={prog?.progress ?? 0}
												indeterminate={prog == null}
											/>
										{/if}
									</div>
								</div>
							{/if}

							{#if job.error}
								<p class="err">{job.error}</p>
							{/if}

							{#if primaryMedia && primaryPath}
								{#if isVideoPath(primaryPath, job.kind)}
									<button
										type="button"
										class="media-hit"
										aria-label="Play video, artifact {job.id}"
										title="Play in viewer"
										onclick={() => {
											previewSrc = primaryMedia;
											previewAlt = `Artifact #${job.id}`;
											previewKind = 'video';
										}}
									>
										<div class="media-frame">
											<SafeMedia
												class="artifact"
												src={primaryMedia}
												kind="video"
												label="Video unavailable"
												controls={false}
											/>
											<span class="play-badge" aria-hidden="true">
												<Icon name="play" size={22} />
											</span>
										</div>
									</button>
								{:else}
									<button
										type="button"
										class="media-hit"
										aria-label="View image, artifact {job.id}"
										onclick={() => {
											previewSrc = primaryMedia;
											previewAlt = `Artifact #${job.id}`;
											previewKind = 'image';
										}}
									>
										<div class="media-frame">
											<SafeMedia
												class="artifact"
												src={primaryMedia}
												alt="Artifact {job.id}"
												label="Image unavailable"
											/>
										</div>
									</button>
								{/if}
							{:else if job.status === 'done'}
								<div class="missing">
									<p>File missing on disk</p>
									{#if primaryPath}
										<p class="mono path-hint">{primaryPath.split(/[/\\]/).pop()}</p>
									{/if}
								</div>
							{/if}

							<div class="card-actions">
								{#if primaryPath && primaryMedia}
									<AttachToProject path={primaryPath} kind={job.kind} />
								{/if}
								{#if job.status === 'failed'}
									<button
										class="btn btn-secondary"
										type="button"
										onclick={() =>
											jobsApi.retry(job.id).then(() =>
												client.invalidateQueries({ queryKey: ['playground-jobs'] }),
											)}
									>
										Retry
									</button>
								{/if}
								<button
									class="btn btn-ghost danger"
									type="button"
									disabled={deletingId === job.id}
									onclick={() => deleteJob(job)}
								>
									{deletingId === job.id ? 'Deleting…' : 'Delete'}
								</button>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<!-- Composer docked at bottom — always visible (Kling Omni style) -->
		<div class="composer-dock">
			{#if modeWorkflows.length === 0}
				<div class="empty-mode">
					<p class="empty-title">No enabled {mode === 'video' ? 'video' : 'image'} workflows</p>
					<p class="muted">
						Enable a workflow in <a href="/settings?tab=workflows">Settings → Workflows</a>.
					</p>
				</div>
			{:else if selected}
				<OmniComposer
					inputs={selected.input_schema}
					bind:values={formValues}
					workflow={selected}
					workflows={modeWorkflows}
					onWorkflowChange={(id) => (workflowId = id)}
					allowUpload
					showErrors={attempted}
					onValidityChange={(m) => (missingLabels = m)}
					onSubmit={tryGenerate}
					submitting={$generateMutation.isPending}
				/>
				{#if formError}
					<p class="err" role="alert">{formError}</p>
				{/if}
			{/if}
		</div>
	</main>
</div>

<ImageLightbox
	src={previewSrc}
	alt={previewAlt}
	kind={previewKind}
	caption={previewKind === 'video' ? `Video ${previewAlt}` : undefined}
	onClose={() => {
		previewSrc = null;
		previewAlt = '';
	}}
/>

<style>
	.shell {
		height: 100vh;
		display: flex;
		flex-direction: column;
		background: var(--bg-primary);
		overflow: hidden;
	}

	/* Fixed viewport column: tabs + scrollable results + docked composer */
	.omni-layout {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 12px var(--space-xl) 16px;
		min-height: 0;
		overflow: hidden;
		background: var(--bg-primary);
		max-width: 980px;
		width: 100%;
		margin: 0 auto;
		box-sizing: border-box;
	}

	/* ── Mode tabs ───────────────────────────────────────────── */
	.mode-tabs {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}

	.spacer {
		flex: 1;
	}

	.mode-tab {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		height: 36px;
		padding: 0 14px;
		border-radius: 9999px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		color: var(--text-secondary);
		font-size: 13px;
		font-weight: 500;
		font-family: var(--font-body);
		cursor: pointer;
		transition:
			border-color 0.15s,
			color 0.15s,
			background 0.15s;
	}

	.mode-tab:hover {
		color: var(--text-primary);
		border-color: var(--text-muted);
	}

	.mode-tab.active {
		background: var(--bg-surface);
		color: var(--text-primary);
		border-color: var(--accent);
	}

	.mode-tab.reset {
		color: var(--text-muted);
	}

	.mode-tab:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	/* ── Scrollable results ──────────────────────────────────── */
	.results {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
		padding-right: 2px;
	}

	.artifacts-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--space-md);
		padding-bottom: var(--space-sm);
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
		position: sticky;
		top: 0;
		background: var(--bg-primary);
		z-index: 1;
	}

	h2 {
		margin: 0;
		font-family: var(--font-display);
		font-size: 18px;
		font-weight: 700;
		letter-spacing: -0.03em;
		color: var(--text-primary);
	}

	.count-block {
		margin: 0;
		display: flex;
		align-items: baseline;
		gap: 6px;
	}

	.count {
		font-size: 15px;
		color: var(--text-primary);
		font-weight: 600;
	}

	.count-label {
		font-size: 12px;
		color: var(--text-muted);
	}

	.rail {
		list-style: none;
		margin: 0;
		padding: 0 0 8px;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
		gap: var(--space-md);
		align-items: start;
	}

	.card {
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		padding: 10px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-width: 0;
		overflow: hidden;
	}

	.card-meta {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.id {
		font-size: 12px;
		color: var(--text-muted);
	}

	.kind {
		font-size: 11px;
		margin-left: auto;
	}

	.badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		border-radius: 9999px;
		font-size: 11px;
		font-weight: 600;
	}

	.badge.done {
		background: color-mix(in srgb, var(--success) 15%, transparent);
		color: var(--success);
	}

	.badge.failed {
		background: color-mix(in srgb, var(--error) 15%, transparent);
		color: var(--error);
	}

	.badge.pending,
	.badge.running {
		background: color-mix(in srgb, var(--warning) 15%, transparent);
		color: var(--warning);
	}

	.err {
		color: var(--error);
		font-size: 13px;
		margin: 0;
	}

	.waiting {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.waiting-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.waiting-text {
		font-size: 13px;
		color: var(--text-secondary);
	}

	.pulse {
		width: 8px;
		height: 8px;
		flex-shrink: 0;
		border-radius: 9999px;
		background: var(--warning);
		animation: pulse 1.2s ease-in-out infinite;
	}

	@keyframes pulse {
		0%,
		100% {
			opacity: 0.4;
		}
		50% {
			opacity: 1;
		}
	}

	.media-hit {
		padding: 0;
		border: none;
		background: transparent;
		cursor: zoom-in;
		display: block;
		width: 100%;
		min-width: 0;
	}

	/* Video thumb: native controls off, custom play affordance instead */
	.media-hit:has(:global(video)) {
		cursor: pointer;
	}

	.media-hit:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
		border-radius: var(--radius-sm);
	}

	.play-badge {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		pointer-events: none;
		transition: background 0.15s;
	}

	.play-badge::before {
		content: '';
		width: 52px;
		height: 52px;
		border-radius: 9999px;
		background: rgba(0, 0, 0, 0.55);
		backdrop-filter: blur(2px);
		box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.18);
		transition:
			transform 0.15s ease,
			background 0.15s;
	}

	.play-badge :global(svg) {
		position: absolute;
		color: #fff;
		fill: currentColor;
		filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.6));
		/* optical centering for a solid play triangle */
		margin-left: 3px;
	}

	.media-hit:hover .play-badge::before,
	.media-hit:focus-visible .play-badge::before {
		background: rgba(0, 0, 0, 0.72);
		transform: scale(1.06);
	}

	.media-frame {
		position: relative;
		width: 100%;
		aspect-ratio: 4 / 3;
		border-radius: var(--radius-sm);
		overflow: hidden;
		background: var(--bg-elevated);
	}

	.media-frame :global(.artifact),
	.media-frame :global(img),
	.media-frame :global(video) {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
		border-radius: 0;
	}

	.missing {
		padding: 12px;
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
		color: var(--text-muted);
		font-size: 13px;
	}

	.path-hint {
		font-size: 11px;
		margin: 4px 0 0;
		overflow-wrap: anywhere;
	}

	.card-actions {
		display: flex;
		gap: 8px;
		align-items: center;
		justify-content: flex-end;
		flex-wrap: wrap;
		margin-top: 2px;
	}

	.skeleton {
		height: 180px;
		border-radius: var(--radius-md);
		background: linear-gradient(
			90deg,
			var(--bg-surface) 25%,
			var(--bg-elevated) 50%,
			var(--bg-surface) 75%
		);
		background-size: 200% 100%;
		animation: shimmer 1.5s infinite;
	}

	@keyframes shimmer {
		0% {
			background-position: 200% 0;
		}
		100% {
			background-position: -200% 0;
		}
	}

	.btn {
		border-radius: 9999px;
		font-size: 12px;
		font-weight: 600;
		padding: 6px 14px;
		cursor: pointer;
		transition: all 0.15s;
	}

	.btn-secondary {
		background: transparent;
		border: 1px solid var(--border);
		color: var(--text-secondary);
	}

	.btn-secondary:hover {
		border-color: var(--text-muted);
		color: var(--text-primary);
	}

	.btn-ghost.danger {
		background: transparent;
		border: 1px solid transparent;
		color: var(--text-muted);
	}

	.btn-ghost.danger:hover {
		color: var(--error);
		border-color: rgba(239, 68, 68, 0.4);
	}

	.empty-rail {
		padding: 40px 24px;
		text-align: center;
		border: 1px dashed var(--border);
		border-radius: var(--radius-lg);
		margin: auto 0;
	}

	.empty-title {
		margin: 0 0 4px;
		font-size: 15px;
		font-weight: 600;
		color: var(--text-primary);
	}

	.muted {
		color: var(--text-secondary);
		font-size: 13px;
		line-height: 1.5;
		margin: 0;
	}

	.muted a {
		color: var(--accent);
	}

	/* ── Composer dock (never shrinks away) ──────────────────── */
	.composer-dock {
		flex-shrink: 0;
		min-height: 0;
	}

	.composer-dock :global(.omni-shell) {
		/* overflow:hidden + flex shrink was collapsing this to 2px */
		flex-shrink: 0;
	}

	.empty-mode {
		padding: 28px 20px;
		text-align: center;
		border: 1px dashed var(--border);
		border-radius: var(--radius-lg);
		background: var(--bg-surface);
	}
</style>
