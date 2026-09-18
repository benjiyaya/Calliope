<script lang="ts">
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { libraryApi, type LibraryMediaItem } from '$lib/api';
	import { assetUrl } from '$lib/api';
	import { t } from '$lib/i18n.svelte';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import AttachToProject from '$lib/components/AttachToProject.svelte';
	import ImageLightbox from '$lib/components/ImageLightbox.svelte';
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Skeleton from '$lib/components/ui/Skeleton.svelte';
	import { toast } from '$lib/toast';

	const client = useQueryClient();

	const mediaQuery = createQuery({
		queryKey: ['library-media'],
		queryFn: libraryApi.list,
	});

	const items = $derived($mediaQuery.data ?? []);

	// ---- filter + lazy paging ------------------------------------------------
	// The listing endpoint returns the full (small) file index; filtering and
	// paging are client-side. Media heavy <video> cards make a 50-per-page cap
	// necessary even so.

	const PAGE_SIZE = 50;
	let filter = $state<'all' | 'image' | 'video'>('all');
	let visibleCount = $state(PAGE_SIZE);
	let loadingMore = $state(false);
	let sentinel = $state<HTMLElement | null>(null);

	const filtered = $derived(
		filter === 'all' ? items : items.filter((i) => i.kind === filter),
	);
	const visible = $derived(filtered.slice(0, visibleCount));
	const hasMore = $derived(filtered.length > visible.length);
	const remaining = $derived(filtered.length - visible.length);

	function loadMore() {
		if (!hasMore || loadingMore) return;
		loadingMore = true;
		// Yield a frame so the loading row paints before the (sync) append.
		setTimeout(() => {
			visibleCount += PAGE_SIZE;
			loadingMore = false;
		}, 120);
	}

	$effect(() => {
		// Auto-append the next page when the sentinel scrolls near the viewport.
		if (!sentinel || !hasMore) return;
		const io = new IntersectionObserver(
			(entries) => {
				if (entries.some((e) => e.isIntersecting)) loadMore();
			},
			{ rootMargin: '400px' },
		);
		io.observe(sentinel);
		return () => io.disconnect();
	});

	$effect(() => {
		// New filter → back to the first page.
		void filter;
		visibleCount = PAGE_SIZE;
	});

	let selected = $state<Set<string>>(new Set());
	let preview = $state<{ src: string; kind: 'image' | 'video'; name: string } | null>(null);
	let attachItem = $state<LibraryMediaItem | null>(null);
	let confirmingDelete = $state(false);

	const selectedCount = $derived(selected.size);
	const allSelected = $derived(
		filtered.length > 0 && filtered.every((i) => selected.has(i.path)),
	);

	function toggle(path: string) {
		const next = new Set(selected);
		if (next.has(path)) next.delete(path);
		else next.add(path);
		selected = next;
	}

	function toggleAll() {
		selected = allSelected ? new Set() : new Set(filtered.map((i) => i.path));
	}

	function fmtSize(bytes: number): string {
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
	}

	const deleteMutation = createMutation({
		mutationFn: (paths: string[]) => libraryApi.deleteMany(paths),
		onSuccess: (res) => {
			selected = new Set();
			confirmingDelete = false;
			void client.invalidateQueries({ queryKey: ['library-media'] });
			const kept = res.results.filter((r) => r.status === 'kept');
			if (kept.length > 0) {
				toast.error(t('library.deletedPartial', { kept: kept.length }));
			} else {
				toast.success(t('library.deleted', { count: res.deleted.length }));
			}
		},
		onError: (err) => {
			confirmingDelete = false;
			toast.error(err instanceof Error ? err.message : t('library.deleteFailed'));
		},
	});

	function deleteSelected() {
		if (selected.size === 0) return;
		$deleteMutation.mutate([...selected]);
	}

	function openPreview(item: LibraryMediaItem) {
		const src = assetUrl(item.path);
		if (!src) return;
		preview = { src, kind: item.kind, name: item.name };
	}
</script>

<AppHeader active="library">
	{#if selectedCount > 0}
		<Button variant="danger" onclick={() => (confirmingDelete = true)}>
			<Icon name="close" size={14} />
			{t('library.deleteSelected', { count: selectedCount })}
		</Button>
	{/if}
</AppHeader>

<main class="container">
	<div class="toolbar">
		<div class="filters" role="tablist" aria-label={t('library.filterLabel')}>
			<button
				type="button"
				class="filter-chip"
				class:active={filter === 'all'}
				aria-pressed={filter === 'all'}
				onclick={() => (filter = 'all')}
			>
				{t('library.filterAll')} <span class="chip-count">{items.length}</span>
			</button>
			<button
				type="button"
				class="filter-chip"
				class:active={filter === 'image'}
				aria-pressed={filter === 'image'}
				onclick={() => (filter = 'image')}
			>
				{t('library.filterImage')}
				<span class="chip-count">
					{items.filter((i) => i.kind === 'image').length}
				</span>
			</button>
			<button
				type="button"
				class="filter-chip"
				class:active={filter === 'video'}
				aria-pressed={filter === 'video'}
				onclick={() => (filter = 'video')}
			>
				{t('library.filterVideo')}
				<span class="chip-count">
					{items.filter((i) => i.kind === 'video').length}
				</span>
			</button>
		</div>
		<label class="select-all">
			<input type="checkbox" checked={allSelected} onchange={toggleAll} />
			<span>{allSelected ? t('library.deselectAll') : t('library.selectAll')}</span>
		</label>
		<span class="count" data-testid="library-count">
			{t('library.itemCount', { count: filtered.length })}
		</span>
	</div>

	{#if $mediaQuery.isLoading}
		<div class="grid">
			{#each Array(8) as _, i (i)}
				<div class="card skeleton-card"><Skeleton height="220px" /></div>
			{/each}
		</div>
	{:else if filtered.length === 0}
		<EmptyState
			title={filter === 'all' ? t('library.emptyTitle') : t('library.emptyFilterTitle', { kind: filter === 'image' ? t('library.filterImage') : t('library.filterVideo') })}
			body={filter === 'all' ? t('library.emptyBody') : t('library.emptyFilterBody')}
		/>
	{:else}
		<div class="grid">
			{#each visible as item (item.path)}
				<div class="card" class:selected={selected.has(item.path)}>
					<label class="check">
						<input
							type="checkbox"
							checked={selected.has(item.path)}
							onchange={() => toggle(item.path)}
						/>
					</label>
					<button
						type="button"
						class="media"
						onclick={() => openPreview(item)}
						title={item.name}
					>
						<SafeMedia src={assetUrl(item.path)} kind={item.kind} alt={item.name} controls={false} />
					</button>
					<div class="meta">
						<span class="name" title={item.name}>{item.name}</span>
						<span class="sub">{item.kind} · {fmtSize(item.size)}</span>
					</div>
					<div class="card-actions">
						<Button variant="secondary" onclick={() => (attachItem = item)}>
							{t('library.addToProject')}
						</Button>
					</div>
				</div>
			{/each}
		</div>
		{#if hasMore}
			<div class="load-more" bind:this={sentinel} aria-hidden="true"></div>
			<div class="more-row">
				{#if loadingMore}
					<span class="loading-note">{t('library.loadingMore')}</span>
				{:else}
					<Button variant="secondary" onclick={loadMore}>
						{t('library.loadMore', { count: remaining })}
					</Button>
				{/if}
			</div>
		{/if}
	{/if}
</main>

{#if preview}
	<ImageLightbox
		src={preview.src}
		alt={preview.name}
		kind={preview.kind}
		caption={preview.name}
		onClose={() => (preview = null)}
	/>
{/if}

{#if attachItem}
	<div class="modal-backdrop">
		<div
			class="modal"
			role="dialog"
			aria-modal="true"
			aria-label={t('library.addToProject')}
		>
			<div class="modal-head">
				<strong>{t('library.addToProject')}</strong>
				<button type="button" class="modal-close" onclick={() => (attachItem = null)}>
					<Icon name="close" size={14} />
				</button>
			</div>
			<p class="modal-file">{attachItem.name}</p>
			<AttachToProject path={attachItem.path} kind={attachItem.kind} />
		</div>
	</div>
{/if}

{#if confirmingDelete}
	<div class="modal-backdrop" role="presentation">
		<div class="modal confirm" role="alertdialog" aria-modal="true">
			<p>{t('library.confirmDelete', { count: selectedCount })}</p>
			<div class="modal-actions">
				<Button variant="danger" loading={$deleteMutation.isPending} onclick={deleteSelected}>
					{t('common.delete')}
				</Button>
				<Button variant="ghost" onclick={() => (confirmingDelete = false)}>
					{t('common.cancel')}
				</Button>
			</div>
		</div>
	</div>
{/if}

<style>
	.toolbar {
		display: flex;
		align-items: center;
		gap: 16px;
		margin-bottom: 14px;
		flex-wrap: wrap;
	}
	.filters {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.filter-chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: transparent;
		color: var(--text-secondary);
		font-size: 13px;
		font-weight: 500;
		cursor: pointer;
		transition: color 0.15s, background 0.15s, border-color 0.15s;
	}
	.filter-chip:hover {
		color: var(--text-primary);
		background: var(--bg-elevated);
	}
	.filter-chip.active {
		color: var(--text-primary);
		background: var(--bg-elevated);
		border-color: var(--accent);
	}
	.filter-chip:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.chip-count {
		font-size: 11px;
		color: var(--text-muted);
		background: rgba(255, 255, 255, 0.06);
		border-radius: 999px;
		padding: 1px 7px;
	}
	.select-all {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 13px;
		color: var(--text-secondary);
		cursor: pointer;
	}
	.count {
		font-size: 13px;
		color: var(--text-muted);
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: 14px;
	}
	.card {
		position: relative;
		display: flex;
		flex-direction: column;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		overflow: hidden;
		transition: border-color 0.15s ease;
	}
	.card.selected {
		border-color: var(--accent);
	}
	.card :global(.media-el) {
		object-fit: cover;
	}
	.skeleton-card {
		height: 240px;
	}
	.check {
		position: absolute;
		top: 8px;
		left: 8px;
		z-index: 2;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		background: rgba(0, 0, 0, 0.55);
		border-radius: var(--radius-sm);
		cursor: pointer;
	}
	.check input {
		width: 15px;
		height: 15px;
		accent-color: var(--accent);
		cursor: pointer;
	}
	.media {
		display: block;
		width: 100%;
		height: 150px;
		padding: 0;
		border: none;
		background: rgba(0, 0, 0, 0.3);
		cursor: zoom-in;
		overflow: hidden;
	}
	.media :global(img),
	.media :global(video) {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.meta {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 10px 12px 6px;
		min-width: 0;
	}
	.name {
		font-size: 13px;
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.sub {
		font-size: 11px;
		color: var(--text-muted);
	}
	.card-actions {
		padding: 8px 12px 12px;
	}
	.load-more {
		height: 1px;
	}
	.more-row {
		display: flex;
		justify-content: center;
		padding: 18px 0 6px;
	}
	.loading-note {
		font-size: 13px;
		color: var(--text-muted);
	}
	.modal-backdrop {
		position: fixed;
		inset: 0;
		z-index: 60;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.6);
		padding: 20px;
	}
	.modal {
		width: min(420px, 100%);
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-lg);
		padding: 16px;
	}
	.modal.confirm {
		width: min(360px, 100%);
	}
	.modal-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 8px;
	}
	.modal-close {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 24px;
		height: 24px;
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
	}
	.modal-close:hover {
		color: var(--text-primary);
		background: rgba(255, 255, 255, 0.06);
	}
	.modal-file {
		margin: 0 0 10px;
		font-size: 12px;
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.modal-actions {
		display: flex;
		gap: 8px;
		margin-top: 12px;
	}
</style>
