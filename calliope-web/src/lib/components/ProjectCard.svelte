<script lang="ts">
	import { goto } from '$app/navigation';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { assetUrl, projects, type Project } from '$lib/api';
	import { toast } from '$lib/toast';
	import Button from '$lib/components/ui/Button.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import StatusChip from '$lib/components/ui/StatusChip.svelte';

	interface Props {
		project: Project;
	}

	const { project }: Props = $props();

	const client = useQueryClient();

	const STATUS_LABEL: Record<string, string> = {
		draft: 'Draft',
		in_progress: 'In Progress',
		completed: 'Ready',
	};

	const stats = $derived(
		project.stats ?? {
			scene_count: 0,
			character_count: 0,
			asset_ready_count: 0,
			asset_total_count: 0,
		},
	);

	// Explicit project cover wins; otherwise fall back to the first asset image.
	// The project prop is stable for the lifetime of a card, so the eager
	// queryKey capture below is intentional.
	// svelte-ignore state_referenced_locally
	const assetsQuery = createQuery({
		queryKey: ['assets', project.id],
		queryFn: () => projects.getAssets(project.id),
		staleTime: 60_000,
	});

	const thumbUrl = $derived.by(() => {
		if (project.cover_path) return assetUrl(project.cover_path);
		const data = $assetsQuery.data;
		if (!data) return null;
		const char = data.characters.find((c) => c.portrait_path || c.sheet_path);
		const path =
			char?.portrait_path ??
			char?.sheet_path ??
			data.locations.find((l) => l.reference_image_path)?.reference_image_path ??
			null;
		return assetUrl(path);
	});

	// A broken cover (deleted file, 404) falls back to the gradient tile.
	let thumbFailedFor = $state<string | null>(null);

	function relativeTime(iso: string): string {
		const then = new Date(iso).getTime();
		if (Number.isNaN(then)) return '';
		const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hours = Math.round(mins / 60);
		if (hours < 24) return `${hours}h ago`;
		const days = Math.round(hours / 24);
		if (days < 30) return `${days}d ago`;
		const months = Math.round(days / 30);
		if (months < 12) return `${months}mo ago`;
		return `${Math.round(months / 12)}y ago`;
	}

	const updatedLabel = $derived(relativeTime(project.updated_at));
	const updatedTitle = $derived(new Date(project.updated_at).toLocaleString());

	// Overflow menu (⋯) — closes on outside click, focus leaving, or Esc.
	let menuOpen = $state(false);
	let menuEl = $state<HTMLDivElement | null>(null);

	$effect(() => {
		if (!menuOpen) return;
		const onDocClick = (e: MouseEvent) => {
			if (menuEl && !menuEl.contains(e.target as Node)) menuOpen = false;
		};
		const onKey = (e: KeyboardEvent) => {
			if (e.key === 'Escape') menuOpen = false;
		};
		const onFocusIn = (e: FocusEvent) => {
			if (menuEl && !menuEl.contains(e.target as Node)) menuOpen = false;
		};
		window.addEventListener('click', onDocClick);
		window.addEventListener('keydown', onKey);
		document.addEventListener('focusin', onFocusIn);
		return () => {
			window.removeEventListener('click', onDocClick);
			window.removeEventListener('keydown', onKey);
			document.removeEventListener('focusin', onFocusIn);
		};
	});

	let renameOpen = $state(false);
	let renameValue = $state('');

	const renameMutation = createMutation({
		mutationFn: (title: string) => projects.update(project.id, { title }),
		onSuccess: (updated) => {
			client.invalidateQueries({ queryKey: ['projects'] });
			toast.success(`Renamed to “${updated.title}”`);
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : 'Could not rename project');
		},
	});

	function openRename() {
		menuOpen = false;
		renameValue = project.title;
		renameOpen = true;
	}

	function submitRename() {
		const title = renameValue.trim();
		renameOpen = false;
		if (!title || title === project.title) return;
		$renameMutation.mutate(title);
	}

	let deleteOpen = $state(false);

	const deleteMutation = createMutation({
		mutationFn: () => projects.delete(project.id),
		onSuccess: () => {
			client.invalidateQueries({ queryKey: ['projects'] });
			toast.success(`Deleted “${project.title}”`);
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : 'Could not delete project');
		},
	});
</script>

<article class="project-card">
	<button
		type="button"
		class="card-main"
		aria-label="Open project {project.title}"
		onclick={() => goto(`/project/${project.id}`)}
	>
		<div class="card-thumb">
			{#if thumbUrl && thumbFailedFor !== thumbUrl}
				<img
					class="thumb-img"
					src={thumbUrl}
					alt=""
					loading="lazy"
					onerror={() => (thumbFailedFor = thumbUrl)}
				/>
			{:else}
				<div class="card-thumb-icon"><Icon name="video" size={26} /></div>
			{/if}
			<div class="card-status">
				<StatusChip status={project.status} label={STATUS_LABEL[project.status]} />
			</div>
		</div>
		<div class="card-body">
			<div class="card-title">
				{project.title}
				<span class="open-arrow">→</span>
			</div>
			<p class="card-desc">{project.idea || 'No story idea yet.'}</p>
			<div class="card-meta">
				<div class="card-stats">
					<span class="card-stat">
						<Icon name="script" size={13} />
						{stats.scene_count} scenes
					</span>
					<span class="card-stat">
						<Icon name="assets" size={13} />
						{stats.character_count} characters
					</span>
					<span class="card-stat">
						<Icon name="image" size={13} />
						{stats.asset_ready_count}/{stats.asset_total_count} assets
					</span>
				</div>
				<span title={updatedTitle}>{updatedLabel}</span>
			</div>
		</div>
	</button>

	<div class="menu-wrap" bind:this={menuEl}>
		<button
			type="button"
			class="menu-btn"
			aria-label="Actions for {project.title}"
			aria-haspopup="menu"
			aria-expanded={menuOpen}
			onclick={() => (menuOpen = !menuOpen)}
		>
			⋯
		</button>
		{#if menuOpen}
			<div class="menu" role="menu" aria-label="Project actions">
				<button type="button" role="menuitem" class="menu-item" onclick={openRename}>
					<Icon name="edit" size={14} />
					Rename
				</button>
				<button
					type="button"
					role="menuitem"
					class="menu-item"
					onclick={() => {
						menuOpen = false;
						goto(`/project/${project.id}?stage=assets`);
					}}
				>
					<Icon name="image" size={14} />
					Change cover…
				</button>
				<button
					type="button"
					role="menuitem"
					class="menu-item danger"
					onclick={() => {
						menuOpen = false;
						deleteOpen = true;
					}}
				>
					<Icon name="trash" size={14} />
					Delete
				</button>
			</div>
		{/if}
	</div>
</article>

<Modal bind:open={renameOpen} title="Rename project">
	<form
		onsubmit={(e) => {
			e.preventDefault();
			submitRename();
		}}
	>
		<label class="field">
			<span class="field-label">Title</span>
			<input class="field-input" bind:value={renameValue} required />
		</label>
	</form>
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (renameOpen = false)}>Cancel</Button>
		<Button
			variant="primary"
			disabled={!renameValue.trim() || $renameMutation.isPending}
			onclick={submitRename}
		>
			Rename
		</Button>
	{/snippet}
</Modal>

<ConfirmDialog
	bind:open={deleteOpen}
	title="Delete project?"
	message="This deletes “{project.title}” and its story data. Generated asset files stay on disk. This cannot be undone."
	confirmLabel="Delete"
	danger
	onconfirm={() => $deleteMutation.mutate()}
/>

<style>
	.project-card {
		position: relative;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		overflow: hidden;
		transition: all 0.2s;
	}
	.project-card:hover {
		border-color: var(--accent);
		transform: translateY(-2px);
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
	}
	.project-card:hover .open-arrow {
		opacity: 1;
		transform: translateX(0);
	}
	.card-main {
		display: block;
		width: 100%;
		padding: 0;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.card-main:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
	}
	.card-thumb {
		height: 170px;
		background: linear-gradient(135deg, var(--bg-elevated) 0%, #1a1625 100%);
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
	}
	.card-thumb::before {
		content: '';
		position: absolute;
		inset: 0;
		background:
			radial-gradient(circle at 20% 30%, rgba(139, 92, 246, 0.12) 0%, transparent 40%),
			radial-gradient(circle at 80% 70%, rgba(59, 130, 246, 0.08) 0%, transparent 40%);
	}
	.thumb-img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.card-thumb-icon {
		width: 64px;
		height: 64px;
		border-radius: var(--radius-md);
		background: rgba(139, 92, 246, 0.12);
		border: 1px solid rgba(139, 92, 246, 0.2);
		color: var(--accent);
		display: flex;
		align-items: center;
		justify-content: center;
		position: relative;
		z-index: 1;
	}
	.card-status {
		position: absolute;
		top: 8px;
		right: 8px;
		z-index: 2;
		filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.6));
	}
	.card-body {
		padding: var(--space-md);
	}
	.card-title {
		font-size: 16px;
		font-weight: 600;
		margin: 0 0 var(--space-xs);
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: var(--space-sm);
	}
	.open-arrow {
		color: var(--accent);
		opacity: 0;
		transform: translateX(-8px);
		transition: all 0.2s;
		font-size: 14px;
	}
	.card-desc {
		color: var(--text-secondary);
		font-size: 13px;
		line-height: 1.5;
		margin: 0 0 var(--space-md);
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.card-meta {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: var(--space-sm);
		font-size: 12px;
		color: var(--text-muted);
	}
	.card-stats {
		display: flex;
		gap: var(--space-md);
		flex-wrap: wrap;
	}
	.card-stat {
		display: flex;
		align-items: center;
		gap: 4px;
	}
	.menu-wrap {
		position: absolute;
		top: 8px;
		left: 8px;
		z-index: 3;
	}
	.menu-btn {
		width: 30px;
		height: 30px;
		border: 1px solid transparent;
		border-radius: var(--radius-sm);
		background: rgba(10, 10, 12, 0.55);
		color: var(--text-secondary);
		font-size: 16px;
		line-height: 1;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		opacity: 0;
		transition: all 0.15s;
	}
	.project-card:hover .menu-btn,
	.menu-btn:focus-visible,
	.menu-btn[aria-expanded='true'] {
		opacity: 1;
	}
	.menu-btn:hover {
		background: var(--bg-elevated);
		color: var(--text-primary);
		border-color: var(--border);
	}
	.menu-btn:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.menu {
		position: absolute;
		top: 34px;
		left: 0;
		min-width: 140px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
		padding: 4px;
		display: flex;
		flex-direction: column;
	}
	.menu-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 8px 10px;
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-primary);
		font-size: 13px;
		font-family: inherit;
		cursor: pointer;
		text-align: left;
	}
	.menu-item:hover {
		background: rgba(255, 255, 255, 0.06);
	}
	.menu-item.danger {
		color: var(--error);
	}
	.menu-item.danger:hover {
		background: rgba(239, 68, 68, 0.12);
	}
	.menu-item:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
	}
	@media (prefers-reduced-motion: reduce) {
		.project-card:hover {
			transform: none;
		}
	}
</style>
