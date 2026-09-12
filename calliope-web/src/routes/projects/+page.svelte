<script lang="ts">
	import { goto } from '$app/navigation';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { projects, type Project } from '$lib/api';
	import { t } from '$lib/i18n.svelte';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import ProjectCard from '$lib/components/ProjectCard.svelte';
	import Skeleton from '$lib/components/ui/Skeleton.svelte';
	import { toast } from '$lib/toast';

	const client = useQueryClient();

	const projectsQuery = createQuery({
		queryKey: ['projects'],
		queryFn: projects.list,
	});

	const createProjectMutation = createMutation({
		mutationFn: projects.create,
		onSuccess: (data: Project) => {
			client.invalidateQueries({ queryKey: ['projects'] });
			toast.success(t('projects.created', { title: data.title }));
			goto(`/project/${data.id}`);
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('projects.createFailed'));
		},
	});

	// English values are sent to the backend; display labels come from the dictionary.
	const GENRES: Record<string, string> = {
		'Adventure / Mystery': 'genres.adventure',
		Drama: 'genres.drama',
		'Sci-Fi': 'genres.scifi',
		Fantasy: 'genres.fantasy',
		Horror: 'genres.horror',
		Romance: 'genres.romance',
		Thriller: 'genres.thriller',
	};
	const TONES: Record<string, string> = {
		'Cinematic, atmospheric': 'tones.cinematic',
		'Dark, tense': 'tones.dark',
		'Whimsical, warm': 'tones.whimsical',
		'Gritty, realistic': 'tones.gritty',
		'Epic, sweeping': 'tones.epic',
	};
	const DURATIONS: Record<string, string> = {
		'30 seconds': 'durations.30',
		'1 minute': 'durations.1m',
		'2 minutes': 'durations.2m',
		'5 minutes': 'durations.5m',
		'10 minutes': 'durations.10m',
	};

	let title = $state('');
	let idea = $state('');
	let genre = $state('Adventure / Mystery');
	let tone = $state('Cinematic, atmospheric');
	let duration = $state('2 minutes');
	let showForm = $state(false);
	let search = $state('');
	let filter = $state('all');

	function onCreate(e: Event) {
		e.preventDefault();
		if (!title.trim()) return;
		$createProjectMutation.mutate({
			title: title.trim(),
			idea: idea.trim() || undefined,
			genre: genre || undefined,
			tone: tone || undefined,
			target_duration: duration || undefined,
		});
	}

	function openForm() {
		showForm = true;
		title = '';
		idea = '';
		genre = 'Adventure / Mystery';
		tone = 'Cinematic, atmospheric';
		duration = '2 minutes';
	}

	const FILTERS = $derived(
		[
			{ id: 'all', key: 'projects.filterAll' },
			{ id: 'in_progress', key: 'projects.filterProgress' },
			{ id: 'completed', key: 'projects.filterCompleted' },
			{ id: 'draft', key: 'projects.filterDraft' },
		].map((f) => ({ id: f.id, label: t(f.key) })),
	);

	const all = $derived($projectsQuery.data ?? []);
	const filtered = $derived.by(() => {
		let list = all;
		if (filter !== 'all') list = list.filter((p) => p.status === filter);
		const q = search.trim().toLowerCase();
		if (q) {
			list = list.filter(
				(p) => p.title.toLowerCase().includes(q) || (p.idea ?? '').toLowerCase().includes(q),
			);
		}
		return list;
	});

	function clearFilters() {
		search = '';
		filter = 'all';
	}
</script>

<AppHeader active="projects">
	<Button variant="primary" onclick={openForm}>
		<Icon name="plus" size={15} />
		{t('projects.new')}
	</Button>
</AppHeader>

<main class="container">
	<div class="hero">
		<div>
			<h1>{t('projects.title')}</h1>
			<p>{t('projects.subtitle')}</p>
		</div>
	</div>

	{#if showForm}
		<form class="new-project-card" onsubmit={onCreate}>
			<div class="form-head">
				<div>
					<p class="eyebrow">{t('projects.newReel')}</p>
					<h3>{t('projects.new')}</h3>
				</div>
				<Button variant="ghost" onclick={() => (showForm = false)}>{t('projects.close')}</Button>
			</div>

			<label class="field">
				<span class="field-label">{t('projects.titleField')}</span>
				<input class="field-input" bind:value={title} placeholder={t('projects.titlePlaceholder')} required />
			</label>

			<label class="field">
				<span class="field-label">{t('projects.ideaField')}</span>
				<textarea
					class="field-textarea"
					bind:value={idea}
					placeholder={t('projects.ideaPlaceholder')}
					rows={4}
				></textarea>
				<p class="field-hint">{t('projects.ideaHint')}</p>
			</label>

			<div class="form-grid">
				<label class="field">
					<span class="field-label">{t('projects.genre')}</span>
					<select class="field-select" bind:value={genre}>
						{#each Object.keys(GENRES) as v (v)}
							<option value={v}>{t(GENRES[v])}</option>
						{/each}
					</select>
				</label>
				<label class="field">
					<span class="field-label">{t('projects.tone')}</span>
					<select class="field-select" bind:value={tone}>
						{#each Object.keys(TONES) as v (v)}
							<option value={v}>{t(TONES[v])}</option>
						{/each}
					</select>
				</label>
				<label class="field">
					<span class="field-label">{t('projects.duration')}</span>
					<select class="field-select" bind:value={duration}>
						{#each Object.keys(DURATIONS) as v (v)}
							<option value={v}>{t(DURATIONS[v])}</option>
						{/each}
					</select>
					<p class="field-hint">{t('projects.durationHint')}</p>
				</label>
			</div>

			<div class="form-actions">
				<Button variant="secondary" onclick={() => (showForm = false)}>{t('projects.cancel')}</Button>
				<Button variant="primary" type="submit" loading={$createProjectMutation.isPending}>
					{t('projects.create')}
				</Button>
			</div>
		</form>
	{/if}

	<div class="toolbar">
		<input
			class="search field-input"
			bind:value={search}
			placeholder={t('projects.searchPlaceholder')}
			aria-label={t('projects.searchLabel')}
		/>
		<div class="filter-group" role="group" aria-label={t('projects.filterBy')}>
			{#each FILTERS as f (f.id)}
				<button
					type="button"
					class="pill"
					class:active={filter === f.id}
					aria-pressed={filter === f.id}
					onclick={() => (filter = f.id)}
				>
					{f.label}
				</button>
			{/each}
		</div>
	</div>

	{#if $projectsQuery.isLoading}
		<div class="grid" aria-busy="true" aria-label={t('projects.loading')}>
			{#each [1, 2, 3, 4, 5, 6] as n (n)}
				<div class="skel-card">
					<Skeleton height="170px" />
					<div class="skel-body">
						<Skeleton width="60%" height="16px" />
						<Skeleton />
						<Skeleton width="80%" />
						<Skeleton width="40%" height="12px" />
					</div>
				</div>
			{/each}
		</div>
	{:else if $projectsQuery.isError}
		<EmptyState
			title={t('projects.loadError')}
			body={$projectsQuery.error instanceof Error
				? $projectsQuery.error.message
				: t('projects.loadErrorBody')}
		>
			{#snippet icon()}
				<Icon name="alert" size={28} />
			{/snippet}
			{#snippet action()}
				<Button variant="primary" onclick={() => $projectsQuery.refetch()}>
					<Icon name="retry" size={15} />
					{t('common.retry')}
				</Button>
			{/snippet}
		</EmptyState>
	{:else if all.length === 0}
		<EmptyState title={t('projects.emptyTitle')} body={t('projects.emptyBody')}>
			{#snippet icon()}
				<Icon name="video" size={28} />
			{/snippet}
			{#snippet action()}
				<Button variant="primary" onclick={openForm}>
					<Icon name="plus" size={15} />
					{t('projects.firstProject')}
				</Button>
			{/snippet}
		</EmptyState>
	{:else if filtered.length === 0}
		<EmptyState
			title={search.trim()
				? t('projects.noMatchSearch', { q: search.trim() })
				: t('projects.noMatchFilter')}
			body={t('projects.noMatchBody')}
		>
			{#snippet icon()}
				<Icon name="search" size={28} />
			{/snippet}
			{#snippet action()}
				<Button variant="secondary" onclick={clearFilters}>{t('projects.clearFilters')}</Button>
			{/snippet}
		</EmptyState>
	{:else}
		<div class="grid">
			{#each filtered as project (project.id)}
				<ProjectCard {project} />
			{/each}
			<button type="button" class="new-card" onclick={openForm}>
				<div class="new-card-icon"><Icon name="plus" size={24} /></div>
				<div class="new-card-title">{t('projects.newCard')}</div>
				<div class="new-card-sub">{t('projects.newCardSub')}</div>
			</button>
		</div>
	{/if}
</main>

<style>
	.container {
		max-width: 1280px;
		margin: 0 auto;
		padding: var(--space-xl);
	}
	.hero {
		display: flex;
		justify-content: space-between;
		align-items: flex-end;
		margin-bottom: var(--space-xl);
	}
	.hero h1 {
		margin: 0 0 var(--space-sm);
		font-size: 32px;
	}
	.hero p {
		margin: 0;
		color: var(--text-secondary);
		font-size: 15px;
	}
	.new-project-card {
		background: rgba(255, 255, 255, 0.03);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: var(--radius-lg);
		padding: var(--space-lg);
		margin-bottom: var(--space-xl);
		box-shadow: 0 0 0 1px rgba(139, 92, 246, 0.08);
	}
	.form-head {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		margin-bottom: var(--space-md);
	}
	.eyebrow {
		margin: 0 0 4px;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--accent);
		font-weight: 600;
	}
	.new-project-card h3 {
		margin: 0;
		font-size: 20px;
	}
	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-md);
	}
	@media (max-width: 700px) {
		.form-grid {
			grid-template-columns: 1fr;
		}
	}
	.form-actions {
		display: flex;
		justify-content: flex-end;
		gap: var(--space-sm);
		margin-top: var(--space-sm);
	}
	.toolbar {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: var(--space-lg);
		gap: var(--space-md);
		flex-wrap: wrap;
	}
	.search {
		flex: 1;
		max-width: 360px;
	}
	.filter-group {
		display: flex;
		gap: var(--space-xs);
	}
	.pill {
		border: 1px solid transparent;
		border-radius: 999px;
		background: transparent;
		color: var(--text-secondary);
		padding: 8px 14px;
		cursor: pointer;
		font-size: 13px;
		font-weight: 500;
		font-family: inherit;
		transition:
			background 0.15s,
			color 0.15s,
			border-color 0.15s;
	}
	.pill:hover {
		background: var(--bg-elevated);
		color: var(--text-primary);
	}
	.pill.active {
		background: var(--bg-elevated);
		border-color: var(--border);
		color: var(--text-primary);
	}
	.pill:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
		gap: var(--space-lg);
	}
	.skel-card {
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		overflow: hidden;
	}
	.skel-card :global(.skeleton) {
		border-radius: 0;
	}
	.skel-body {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: var(--space-md);
	}
	.skel-body :global(.skeleton) {
		border-radius: var(--radius-sm);
	}
	.new-card {
		border: 2px dashed var(--border);
		background: transparent;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		min-height: 320px;
		color: var(--text-muted);
		gap: var(--space-md);
		border-radius: var(--radius-lg);
		cursor: pointer;
		font-family: inherit;
		transition: all 0.15s;
	}
	.new-card:hover {
		border-color: var(--accent);
		color: var(--text-primary);
		background: rgba(139, 92, 246, 0.04);
	}
	.new-card:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.new-card-icon {
		width: 56px;
		height: 56px;
		border-radius: var(--radius-md);
		background: var(--bg-elevated);
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.new-card-title {
		font-weight: 600;
		font-size: 15px;
	}
	.new-card-sub {
		font-size: 13px;
		color: var(--text-muted);
	}
</style>