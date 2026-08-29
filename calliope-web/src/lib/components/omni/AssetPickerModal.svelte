<script lang="ts">
	/**
	 * Tabbed asset picker for Omni media tiles (characters / environments /
	 * items / uploads / clips). Replaces the old single-column dropdown.
	 */
	import { assetUrl } from '$lib/api';
	import {
		assetDisplayName,
		assetGroup,
		tabsForMediaKind,
		type AssetGroup,
		type AssetOption,
	} from '$lib/assetPicker';
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';

	interface Props {
		open?: boolean;
		title?: string;
		assets?: AssetOption[];
		value?: string;
		kind?: string;
		allowUpload?: boolean;
		onselect: (path: string) => void;
		onupload?: () => void;
		onclear?: () => void;
	}

	let {
		open = $bindable(false),
		title = 'Choose reference',
		assets = [],
		value = '',
		kind = 'image',
		allowUpload = true,
		onselect,
		onupload,
		onclear,
	}: Props = $props();

	let tab = $state<AssetGroup>('character');
	let query = $state('');
	let wasOpen = $state(false);

	const tabs = $derived(tabsForMediaKind(kind));

	function hideBroken(e: Event) {
		const el = e.currentTarget as HTMLElement | null;
		if (el) el.style.display = 'none';
	}

	$effect(() => {
		const now = open;
		if (now && !wasOpen) {
			query = '';
			const match = assets.find((a) => a.path === value);
			const preferred = match ? assetGroup(match) : null;
			const firstWithItems =
				tabs.find((t) => assets.some((a) => assetGroup(a) === t.id))?.id ?? tabs[0]?.id;
			tab =
				(preferred && tabs.some((t) => t.id === preferred) ? preferred : firstWithItems) ??
				'upload';
		}
		wasOpen = now;
	});

	// The modal instance can be reused across tiles whose kind differs (e.g. a
	// workflow swap turns an image node into video). If the stored tab is not in
	// the visible list, the body would render one tab while the header shows
	// another — clamp it instead.
	$effect(() => {
		if (tabs.length > 0 && !tabs.some((t) => t.id === tab)) {
			tab = tabs[0].id;
		}
	});

	const q = $derived(query.trim().toLowerCase());

	const visible = $derived.by(() => {
		const inTab = assets.filter((a) => assetGroup(a) === tab);
		if (!q) return inTab;
		return inTab.filter(
			(a) => a.label.toLowerCase().includes(q) || assetDisplayName(a).toLowerCase().includes(q),
		);
	});

	function countFor(id: AssetGroup): number {
		return assets.filter((a) => assetGroup(a) === id).length;
	}

	const emptyHint = $derived.by(() => {
		if (q) return `No matches for “${query.trim()}”.`;
		if (tab === 'character') return 'No character sheets yet. Generate them on Assets.';
		if (tab === 'location') return 'No environment images yet. Generate them on Assets.';
		if (tab === 'item') return 'No misc. item images yet. Generate them on Assets.';
		if (tab === 'clip') return 'No scene clips in this film yet.';
		return allowUpload ? 'No uploads yet. Use Upload new… below.' : 'No uploads yet.';
	});

	function pick(path: string) {
		onselect(path);
		open = false;
	}

	function upload() {
		onupload?.();
		open = false;
	}

	function clear() {
		onclear?.();
		open = false;
	}
</script>

<Modal bind:open {title} size="lg">
	<div class="picker">
		{#if tabs.length > 1}
			<div class="tabs" role="tablist" aria-label="Asset type">
				{#each tabs as t (t.id)}
					<button
						type="button"
						role="tab"
						id="asset-tab-{t.id}"
						aria-selected={tab === t.id}
						class:active={tab === t.id}
						onclick={() => (tab = t.id)}
					>
						{t.label}
						<span class="count">{countFor(t.id)}</span>
					</button>
				{/each}
			</div>
		{/if}

		<label class="search">
			<Icon name="search" size={14} />
			<input
				type="search"
				placeholder="Search in {tabs.find((t) => t.id === tab)?.label ?? 'this list'}…"
				bind:value={query}
				aria-label="Search assets"
			/>
		</label>

		<div class="grid-wrap" role="tabpanel" aria-labelledby="asset-tab-{tab}">
			{#if visible.length === 0}
				<p class="empty">{emptyHint}</p>
			{:else}
				<div class="grid">
					{#each visible as opt (opt.path)}
						{@const selected = opt.path === value}
						{@const src = assetUrl(opt.path)}
						{@const isVideo = (opt.kind ?? kind) === 'video'}
						<button
							type="button"
							class="card"
							class:selected
							onclick={() => pick(opt.path)}
							title={opt.label}
						>
							<div class="thumb">
								{#if isVideo}
									<!-- svelte-ignore a11y_media_has_caption -->
									<video src={src} muted playsinline preload="metadata" onerror={hideBroken}></video>
								{:else if (opt.kind ?? kind) === 'audio'}
									<Icon name="music" size={28} />
								{:else}
									<img src={src} alt="" onerror={hideBroken} />
								{/if}
								{#if selected}
									<span class="check"><Icon name="check" size={12} /></span>
								{/if}
							</div>
							<span class="name">{assetDisplayName(opt)}</span>
						</button>
					{/each}
				</div>
			{/if}
		</div>
	</div>

	{#snippet footer()}
		{#if value && onclear}
			<Button variant="ghost" onclick={clear}>Clear slot</Button>
		{/if}
		{#if allowUpload}
			<Button variant="secondary" onclick={upload}>
				<Icon name="upload" size={14} />
				Upload new…
			</Button>
		{/if}
		<Button variant="ghost" onclick={() => (open = false)}>Cancel</Button>
	{/snippet}
</Modal>

<style>
	.picker {
		display: flex;
		flex-direction: column;
		gap: 12px;
		min-height: 320px;
	}

	.tabs {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}

	.tabs button {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		color: var(--text-secondary);
		border-radius: var(--radius-sm);
		padding: 7px 12px;
		cursor: pointer;
		min-height: 34px;
		font: inherit;
		font-size: 13px;
	}

	.tabs button.active {
		background: var(--accent);
		border-color: var(--accent);
		color: #fff;
	}

	.tabs button:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.count {
		font-size: 11px;
		opacity: 0.8;
		font-variant-numeric: tabular-nums;
	}

	.search {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 10px;
		min-height: 36px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
		color: var(--text-muted);
	}

	.search input {
		flex: 1;
		min-width: 0;
		border: none;
		background: transparent;
		color: var(--text-primary);
		font: inherit;
		font-size: 13px;
		outline: none;
	}

	.search:focus-within {
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}

	.grid-wrap {
		overflow-y: auto;
		overscroll-behavior: contain;
		max-height: min(52vh, 480px);
		min-height: 200px;
	}

	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(128px, 1fr));
		gap: 10px;
	}

	.card {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 0;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}

	.thumb {
		position: relative;
		aspect-ratio: 1;
		border-radius: var(--radius-md);
		border: 1.5px solid var(--border);
		background: var(--bg-elevated);
		overflow: hidden;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--text-muted);
	}

	.card:hover .thumb {
		border-color: var(--text-muted);
	}

	.card.selected .thumb {
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}

	.card:focus-visible .thumb {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.thumb img,
	.thumb video {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.check {
		position: absolute;
		top: 6px;
		right: 6px;
		width: 20px;
		height: 20px;
		border-radius: 9999px;
		background: var(--accent);
		color: #fff;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.name {
		font-size: 12px;
		color: var(--text-secondary);
		line-height: 1.3;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		padding: 0 2px;
	}

	.card.selected .name {
		color: var(--text-primary);
		font-weight: 600;
	}

	.empty {
		margin: 48px 8px;
		text-align: center;
		color: var(--text-muted);
		font-size: 13px;
		line-height: 1.45;
	}
</style>
