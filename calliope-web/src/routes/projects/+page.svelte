<script lang="ts">
	import { goto } from '$app/navigation';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { projects, type Project } from '$lib/api';
	import ProjectCard from '$lib/components/ProjectCard.svelte';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
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
			toast.success(`Project “${data.title}” created`);
			goto(`/project/${data.id}`);
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : 'Could not create project');
		},
	});

	const DURATIONS = ['30 seconds', '1 minute', '2 minutes', '5 minutes', '10 minutes'];

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

	const FILTERS: { id: string; label: string }[] = [
		{ id: 'all', label: 'All' },
		{ id: 'in_progress', label: 'In Progress' },
		{ id: 'completed', label: 'Completed' },
		{ id: 'draft', label: 'Drafts' },
	];

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
		New Project
	</Button>
</AppHeader>

<main class="container">
	<div class="hero">
		<div>
			<h1>Projects</h1>
			<p>Your local story-to-video workspace. Pick up where you left off.</p>
		</div>
	</div>

	{#if showForm}
		<form class="new-project-card" onsubmit={onCreate}>
			<div class="form-head">
				<div>
					<p class="eyebrow">New reel</p>
					<h3>New Project</h3>
				</div>
				<Button variant="ghost" onclick={() => (showForm = false)}>Close</Button>
			</div>

			<label class="field">
				<span class="field-label">Title</span>
				<input class="field-input" bind:value={title} placeholder="Moonlit Harbor" required />
			</label>

			<label class="field">
				<span class="field-label">Story idea</span>
				<textarea
					class="field-textarea"
					bind:value={idea}
					placeholder="A lighthouse keeper finds a glowing bottle that shows memories of sailors lost at sea…"
					rows={4}
				></textarea>
				<p class="field-hint">Optional — you can refine this later on the Story stage.</p>
			</label>

			<div class="form-grid">
				<label class="field">
					<span class="field-label">Genre</span>
					<select class="field-select" bind:value={genre}>
						<option>Adventure / Mystery</option>
						<option>Drama</option>
						<option>Sci-Fi</option>
						<option>Fantasy</option>
						<option>Horror</option>
						<option>Romance</option>
						<option>Thriller</option>
					</select>
				</label>
				<label class="field">
					<span class="field-label">Tone</span>
					<select class="field-select" bind:value={tone}>
						<option>Cinematic, atmospheric</option>
						<option>Dark, tense</option>
						<option>Whimsical, warm</option>
						<option>Gritty, realistic</option>
						<option>Epic, sweeping</option>
					</select>
				</label>
				<label class="field">
					<span class="field-label">Target duration</span>
					<select class="field-select" bind:value={duration}>
						{#each DURATIONS as d (d)}
							<option value={d}>{d}</option>
						{/each}
					</select>
					<p class="field-hint">Guides beat and scene counts on the Story stage.</p>
				</label>
			</div>

			<div class="form-actions">
				<Button variant="secondary" onclick={() => (showForm = false)}>Cancel</Button>
				<Button variant="primary" type="submit" loading={$createProjectMutation.isPending}>
					Create project
				</Button>
			</div>
		</form>
	{/if}

	<div class="toolbar">
		<input
			class="search field-input"
			bind:value={search}
			placeholder="Search projects…"
			aria-label="Search projects"
		/>
		<div class="filter-group" role="group" aria-label="Filter by status">
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
		<div class="grid" aria-busy="true" aria-label="Loading projects">
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
			title="Couldn't load projects"
			body={$projectsQuery.error instanceof Error
				? $projectsQuery.error.message
				: 'The backend did not respond.'}
		>
			{#snippet icon()}
				<Icon name="alert" size={28} />
			{/snippet}
			{#snippet action()}
				<Button variant="primary" onclick={() => $projectsQuery.refetch()}>
					<Icon name="retry" size={15} />
					Retry
				</Button>
			{/snippet}
		</EmptyState>
	{:else if all.length === 0}
		<EmptyState
			title="No projects yet"
			body="Calliope turns a story idea into characters, shots and finished video — all on your machine."
		>
			{#snippet icon()}
				<Icon name="video" size={28} />
			{/snippet}
			{#snippet action()}
				<Button variant="primary" onclick={openForm}>
					<Icon name="plus" size={15} />
					Create your first project
				</Button>
			{/snippet}
		</EmptyState>
	{:else if filtered.length === 0}
		<EmptyState
			title={search.trim()
				? `No projects match “${search.trim()}”`
				: 'No projects match the current filter'}
			body="Try a different search or clear the filters."
		>
			{#snippet icon()}
				<Icon name="search" size={28} />
			{/snippet}
			{#snippet action()}
				<Button variant="secondary" onclick={clearFilters}>Clear filters</Button>
			{/snippet}
		</EmptyState>
	{:else}
		<div class="grid">
			{#each filtered as project (project.id)}
				<ProjectCard {project} />
			{/each}
			<button type="button" class="new-card" onclick={openForm}>
				<div class="new-card-icon"><Icon name="plus" size={24} /></div>
				<div class="new-card-title">Create New Project</div>
				<div class="new-card-sub">Start with a story idea</div>
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
