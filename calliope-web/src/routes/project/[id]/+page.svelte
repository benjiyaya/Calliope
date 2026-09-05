<script lang="ts">
	import { onMount } from 'svelte';
	import { toStore } from 'svelte/store';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { createQuery, useQueryClient } from '@tanstack/svelte-query';
	import NavRail from '$lib/components/NavRail.svelte';
	import StoryStage from '$lib/components/StoryStage.svelte';
	import AssetsStage from '$lib/components/AssetsStage.svelte';
	import ScriptStage from '$lib/components/ScriptStage.svelte';
	import QueueStage from '$lib/components/QueueStage.svelte';
	import ActivityPanel from '$lib/components/ActivityPanel.svelte';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Skeleton from '$lib/components/ui/Skeleton.svelte';
	import StatusChip from '$lib/components/ui/StatusChip.svelte';
	import { jobsApi, projects, settings } from '$lib/api';
	import { connectEvents, type CalliopeEvent, type EventConnectionState } from '$lib/events';
	import { handleJobEvent } from '$lib/jobProgress';

	interface Props {
		data: { id: string };
	}

	let { data }: Props = $props();
	const client = useQueryClient();
	const projectId = $derived(Number(data.id));
	const enabled = $derived(!isNaN(projectId));

	// Stage <-> URL contract: the active stage lives in ?stage=story|assets|script|video.
	// Switching stages = goto('?stage=<id>'); deep links and browser Back/Forward work.
	const STAGES = ['story', 'assets', 'script', 'video'] as const;
	type Stage = (typeof STAGES)[number];

	function stageFromUrl(url: URL): Stage {
		const s = url.searchParams.get('stage');
		return (STAGES as readonly string[]).includes(s ?? '') ? (s as Stage) : 'story';
	}

	const STAGE_LABELS: Record<Stage, string> = {
		story: '1 · Story',
		assets: '2 · Assets',
		script: '3 · Script',
		video: '4 · Video',
	};

	let activeTab = $state<Stage>(stageFromUrl(page.url));
	let activityCollapsed = $state(false);
	let logs = $state<CalliopeEvent[]>([]);
	let connState = $state<EventConnectionState>('connecting');

	// URL → state: deep links, browser Back/Forward
	$effect(() => {
		const s = stageFromUrl(page.url);
		if (s !== activeTab) activeTab = s;
	});

	// State → URL: every stage switch goes through goto so history works
	function selectStage(id: string) {
		const stage: Stage = (STAGES as readonly string[]).includes(id) ? (id as Stage) : 'story';
		if (stage === activeTab) return;
		activeTab = stage;
		goto(`?stage=${stage}`, { keepFocus: true, noScroll: true });
	}

	const storyQuery = createQuery(
		toStore(() => ({
			queryKey: ['story', projectId],
			queryFn: () => projects.getStory(projectId),
			enabled,
		})),
	);

	const settingsQuery = createQuery({
		queryKey: ['settings'],
		queryFn: settings.get,
	});

	// Same query keys as QueueStage/ActivityPanel — cache is shared.
	// Refreshes are SSE-driven (job.* events + events.resync below), no polling.
	const jobsQuery = createQuery(
		toStore(() => ({
			queryKey: ['jobs', projectId],
			queryFn: () => jobsApi.list(projectId),
			enabled,
		})),
	);

	const queueStatusQuery = createQuery(
		toStore(() => ({
			queryKey: ['queue-status'],
			queryFn: jobsApi.queueStatus,
			enabled,
		})),
	);

	const isConfigured = $derived(
		Boolean($settingsQuery.data?.llm_base_url && $settingsQuery.data?.llm_model),
	);
	const projectTitle = $derived($storyQuery.data?.project?.title ?? 'Project');

	const runningCount = $derived(
		($jobsQuery.data ?? []).filter((j) => j.status === 'running').length,
	);
	const queuedCount = $derived(
		($jobsQuery.data ?? []).filter((j) => j.status === 'pending').length,
	);
	const queuePaused = $derived(Boolean($queueStatusQuery.data?.paused));

	onMount(() => {
		return connectEvents(
			(ev) => {
				if (ev.type === 'events.resync') {
					// SSE reconnected: refill anything missed while the stream was down.
					client.invalidateQueries({ queryKey: ['jobs'] });
					client.invalidateQueries({ queryKey: ['queue-status'] });
					client.invalidateQueries({ queryKey: ['assets'] });
					client.invalidateQueries({ queryKey: ['story'] });
					client.invalidateQueries({ queryKey: ['scenes'] });
					return;
				}
				// Live per-job progress for ProgressBars (Activity log stays clean)
				handleJobEvent(ev);
				// Don't keep Comfy poll ticks in the Activity log
				if (ev.type !== 'job.progress') {
					logs = [...logs, ev].slice(-120);
				}
				if (
					ev.type.startsWith('job.') ||
					ev.type === 'asset.ready' ||
					ev.type === 'agent.thinking' ||
					ev.type.startsWith('story.')
				) {
					client.invalidateQueries({ queryKey: ['jobs'] });
					client.invalidateQueries({ queryKey: ['queue-status'] });
					client.invalidateQueries({ queryKey: ['assets'] });
					client.invalidateQueries({ queryKey: ['story'] });
					client.invalidateQueries({ queryKey: ['scenes'] });
				}
			},
			(state) => (connState = state),
		);
	});
</script>

<div class="shell">
	<AppHeader crumb={`· ${projectTitle}`} />

	{#if !enabled}
		<div class="invalid-wrap">
			<EmptyState
				title="Project not found"
				body={`“${data.id}” isn’t a valid project id — the link may be broken or the project was deleted.`}
			>
				{#snippet icon()}
					<Icon name="folder" size={28} />
				{/snippet}
				{#snippet action()}
					<Button variant="primary" onclick={() => goto('/projects')}>Back to projects</Button>
				{/snippet}
			</EmptyState>
		</div>
	{:else}
		<div class="workspace-bar">
			<span class="stage-label">{STAGE_LABELS[activeTab]}</span>
			<Button
				variant="secondary"
				size="sm"
				title="Open the Video stage"
				onclick={() => selectStage('video')}
			>
				<Icon name="queue" size={13} />
				{runningCount + queuedCount > 0
					? `${runningCount} running · ${queuedCount} queued`
					: 'Queue idle'}
				{#if queuePaused}
					<StatusChip status="paused" />
				{/if}
			</Button>
		</div>

		<div class="workspace">
			<NavRail active={activeTab} onSelect={selectStage} />

			<main class="stage" class:stage-fill={activeTab === 'video'}>
				{#if activeTab === 'story'}
					{#if $storyQuery.isLoading}
						<div class="card skel" aria-busy="true">
							<Skeleton width="36%" height="22px" />
							<Skeleton width="88%" />
							<Skeleton width="72%" />
							<Skeleton height="140px" />
						</div>
					{:else if $storyQuery.isError}
						<div class="card" style="color: var(--error)">
							Failed to load story: {$storyQuery.error?.message ?? 'unknown error'}
						</div>
					{:else if $storyQuery.data}
						<StoryStage
							{projectId}
							story={$storyQuery.data}
							configured={isConfigured}
							onGoAssets={() => selectStage('assets')}
						/>
					{/if}
				{:else if activeTab === 'assets'}
					<AssetsStage {projectId} />
				{:else if activeTab === 'script'}
					<ScriptStage {projectId} />
				{:else if activeTab === 'video'}
					<QueueStage {projectId} {projectTitle} />
				{/if}
			</main>

			<ActivityPanel
				{logs}
				{projectId}
				{connState}
				collapsed={activityCollapsed}
				onToggle={() => (activityCollapsed = !activityCollapsed)}
			/>
		</div>
	{/if}
</div>

<style>
	.shell {
		display: flex;
		flex-direction: column;
		height: 100vh;
		overflow: hidden;
	}
	.invalid-wrap {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-lg);
		overflow-y: auto;
	}
	.workspace-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-md);
		padding: 8px var(--space-lg);
		border-bottom: 1px solid var(--border);
		background: var(--bg-surface);
		flex-shrink: 0;
	}
	.stage-label {
		font-family: var(--font-display);
		font-size: 13px;
		font-weight: 600;
		letter-spacing: 0.02em;
		color: var(--text-secondary);
	}
	.workspace {
		display: flex;
		flex: 1;
		overflow: hidden;
	}
	.stage {
		flex: 1;
		padding: var(--space-lg);
		overflow-y: auto;
		background: var(--bg-primary);
		min-width: 0;
	}
	.stage.stage-fill {
		overflow: hidden;
		display: flex;
		flex-direction: column;
		min-height: 0;
		/* Bleed to column edges — Nav/Activity already frame the stage.
		   Old max-width+margin:auto looked off-center because this column
		   sits between unequal side rails. */
		padding: 12px 0 12px;
	}
	.stage.stage-fill > :global(.queue-root) {
		flex: 1;
		min-height: 0;
		padding-inline: 16px;
	}
	/* Player hero breaks out to full stage width; strip/composer keep inset */
	.stage.stage-fill :global(.hero) {
		margin-inline: -16px;
		width: auto;
	}
	.stage.stage-fill :global(.hero .frame) {
		border-radius: 0;
		border-left: none;
		border-right: none;
	}
	.card {
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		padding: var(--space-lg);
	}
	.skel {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
</style>
