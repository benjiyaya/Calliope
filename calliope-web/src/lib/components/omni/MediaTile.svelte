<script lang="ts">
	/**
	 * MediaTile — square thumbnail for image/ref/audio inputs.
	 *
	 * Asset picker menu is portaled to document.body so OmniComposer
	 * overflow:hidden cannot clip it (same fix as PillSelect).
	 */
	import { assetUrl } from '$lib/api';
	import type { ComfyDynamicInput } from '$lib/comfy/types';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import { acceptForKind } from '$lib/comfy/useUpload.svelte';

	interface AssetOption {
		label: string;
		path: string;
		kind?: 'image' | 'video' | 'audio';
	}

	interface Props {
		input: ComfyDynamicInput;
		value: string;
		uploadingName: string | null;
		assetOptions?: AssetOption[];
		allowUpload?: boolean;
		invalid?: boolean;
		onselectFile: (file: File) => void;
		onselectAsset: (path: string) => void;
		onclear: () => void;
	}

	let {
		input,
		value,
		uploadingName,
		assetOptions = [],
		allowUpload = true,
		invalid = false,
		onselectFile,
		onselectAsset,
		onclear,
	}: Props = $props();

	let fileInput = $state<HTMLInputElement | null>(null);
	let dragOver = $state(false);
	let showAssetMenu = $state(false);
	let tileEl = $state<HTMLElement | null>(null);
	let menuEl = $state<HTMLElement | null>(null);
	let menuPos = $state({ top: 0, left: 0, maxHeight: 280, minWidth: 200 });

	function openPicker(e: MouseEvent) {
		e.stopPropagation();
		if (uploadingName) return;
		if (matchingAssets.length) {
			showAssetMenu = !showAssetMenu;
		} else if (allowUpload) {
			fileInput?.click();
		}
	}

	function onFileChosen(e: Event) {
		const el = e.currentTarget as HTMLInputElement;
		const file = el.files?.[0];
		el.value = '';
		if (file) onselectFile(file);
	}

	function onDrop(e: DragEvent) {
		e.preventDefault();
		dragOver = false;
		if (uploadingName) return;
		const file = e.dataTransfer?.files?.[0];
		if (file) onselectFile(file);
	}

	function hideBrokenThumb(e: Event) {
		const img = e.currentTarget as HTMLImageElement | null;
		if (img) img.style.display = 'none';
	}

	const thumbSrc = $derived(value ? assetUrl(value) : null);

	const isImageKind = $derived(input.kind === 'image' || input.kind === 'image_url');
	const isVideoKind = $derived(input.kind === 'video');
	const matchingAssets = $derived(
		assetOptions.filter((o) => !o.kind || o.kind === input.kind || (isImageKind && o.kind === 'image')),
	);
	const hasAssets = $derived(matchingAssets.length > 0);

	function placeMenu() {
		if (!tileEl || !menuEl) return;
		const rect = tileEl.getBoundingClientRect();
		const gap = 6;
		const pad = 8;
		const spaceBelow = window.innerHeight - rect.bottom - gap - pad;
		const spaceAbove = rect.top - gap - pad;
		const preferred = Math.min(320, Math.max(menuEl.scrollHeight, 80));
		const openUp = spaceBelow < Math.min(preferred, 160) && spaceAbove > spaceBelow;
		const maxHeight = Math.max(120, openUp ? spaceAbove : spaceBelow);
		const h = Math.min(menuEl.scrollHeight, maxHeight);
		const minWidth = Math.max(200, rect.width);

		let top = openUp ? rect.top - h - gap : rect.bottom + gap;
		let left = rect.left;
		left = Math.max(pad, Math.min(left, window.innerWidth - minWidth - pad));
		top = Math.max(pad, Math.min(top, window.innerHeight - Math.min(h, maxHeight) - pad));

		menuPos = { top, left, maxHeight, minWidth };
	}

	function onWindowClick(e: MouseEvent) {
		if (!showAssetMenu) return;
		const t = e.target as Node;
		if (tileEl?.contains(t) || menuEl?.contains(t)) return;
		showAssetMenu = false;
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') showAssetMenu = false;
	}

	$effect(() => {
		if (!showAssetMenu || !menuEl) return;
		document.body.appendChild(menuEl);
		return () => {
			menuEl?.remove();
		};
	});

	$effect(() => {
		if (!showAssetMenu) return;
		const t = window.setTimeout(() => {
			placeMenu();
			window.addEventListener('click', onWindowClick);
			window.addEventListener('keydown', onKey);
			window.addEventListener('resize', placeMenu);
			window.addEventListener('scroll', placeMenu, true);
		}, 0);
		return () => {
			clearTimeout(t);
			window.removeEventListener('click', onWindowClick);
			window.removeEventListener('keydown', onKey);
			window.removeEventListener('resize', placeMenu);
			window.removeEventListener('scroll', placeMenu, true);
		};
	});

	$effect(() => {
		if (showAssetMenu && menuEl && tileEl) {
			void assetOptions.length;
			requestAnimationFrame(placeMenu);
		}
	});

	const displayLabel = $derived(
		input.role === 'character'
			? 'Character'
			: input.role === 'location'
				? 'Location'
				: input.role === 'video'
					? 'Video'
					: input.role === 'audio'
						? 'Audio'
						: input.label,
	);

	const accept = $derived(acceptForKind(input.kind));
</script>

<div class="tile-wrap" bind:this={tileEl}>
	<input
		bind:this={fileInput}
		type="file"
		class="sr-only"
		accept={accept || acceptForKind(input.kind)}
		onchange={onFileChosen}
	/>

	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="tile"
		class:filled={!!value}
		class:dragover={dragOver}
		class:invalid
		ondragenter={(e) => {
			e.preventDefault();
			dragOver = true;
		}}
		ondragleave={() => (dragOver = false)}
		ondragover={(e) => e.preventDefault()}
		ondrop={onDrop}
		onclick={openPicker}
		role="button"
		tabindex="0"
		title={displayLabel}
		aria-haspopup={matchingAssets.length ? 'menu' : undefined}
		aria-expanded={matchingAssets.length ? showAssetMenu : undefined}
	>
		{#if uploadingName}
			<div class="tile-uploading">
				<Spinner size="sm" />
			</div>
		{:else if value && thumbSrc && isImageKind}
			<img class="tile-thumb" src={thumbSrc} alt={displayLabel} onerror={hideBrokenThumb} />
		{:else if value && isVideoKind && thumbSrc}
			<!-- svelte-ignore a11y_media_has_caption -->
			<video class="tile-thumb" src={thumbSrc} muted playsinline></video>
		{:else if value}
			<div class="tile-file">
				<Icon name={input.kind === 'audio' ? 'music' : isVideoKind ? 'film' : 'image'} size={20} />
			</div>
		{:else}
			<div class="tile-empty">
				<Icon name="plus" size={20} />
			</div>
		{/if}

		{#if value && !uploadingName}
			<button
				type="button"
				class="tile-remove"
				onclick={(e) => {
					e.stopPropagation();
					onclear();
				}}
				aria-label="Remove {displayLabel}"
			>
				<Icon name="close" size={12} />
			</button>
		{/if}
	</div>

	<span class="tile-label">{displayLabel}</span>

	{#if invalid && !value}
		<span class="tile-req">Required</span>
	{/if}
</div>

{#if showAssetMenu && hasAssets}
	<div
		bind:this={menuEl}
		class="asset-menu"
		style="top: {menuPos.top}px; left: {menuPos.left}px; max-height: {menuPos.maxHeight}px; min-width: {menuPos.minWidth}px"
		role="menu"
	>
		{#each matchingAssets as opt (opt.path)}
			<button
				type="button"
				class="asset-item"
				class:active={opt.path === value}
				role="menuitem"
				onclick={(e) => {
					e.stopPropagation();
					onselectAsset(opt.path);
					showAssetMenu = false;
				}}
			>
				<span class="item-label">{opt.label}</span>
				{#if opt.path === value}
					<Icon name="check" size={14} />
				{/if}
			</button>
		{/each}
		{#if allowUpload}
			<button
				type="button"
				class="asset-item upload-opt"
				role="menuitem"
				onclick={(e) => {
					e.stopPropagation();
					showAssetMenu = false;
					fileInput?.click();
				}}
			>
				<Icon name="upload" size={12} />
				Upload new…
			</button>
		{/if}
	</div>
{/if}

<style>
	.tile-wrap {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 4px;
		position: relative;
	}

	.tile {
		position: relative;
		width: 64px;
		height: 64px;
		border-radius: var(--radius-md);
		border: 1.5px dashed var(--border);
		background: var(--bg-elevated);
		display: flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
		overflow: hidden;
		transition:
			border-color 0.15s,
			background 0.15s;
	}

	.tile:hover:not(.filled) {
		border-color: var(--text-muted);
	}

	.tile:focus-visible {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 3px var(--accent-glow);
	}

	.tile.dragover {
		border-color: var(--accent);
		background: var(--accent-glow);
		border-style: solid;
	}

	.tile.filled {
		border: 1px solid var(--border);
	}

	.tile.invalid {
		border-color: var(--error);
	}

	.tile-empty {
		color: var(--text-muted);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.tile-thumb {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.tile-file {
		color: var(--text-muted);
	}

	.tile-uploading {
		color: var(--text-muted);
	}

	.tile-remove {
		position: absolute;
		top: 4px;
		right: 4px;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 20px;
		border-radius: 9999px;
		background: rgba(0, 0, 0, 0.65);
		border: none;
		color: white;
		cursor: pointer;
		opacity: 0;
		transition: opacity 0.15s;
	}

	.tile:hover .tile-remove {
		opacity: 1;
	}

	.tile-remove:hover {
		background: var(--error);
	}

	.tile-label {
		font-size: 10px;
		color: var(--text-muted);
		font-weight: 500;
		max-width: 72px;
		text-align: center;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.tile-req {
		font-size: 9px;
		color: var(--error);
		font-weight: 600;
	}

	.asset-menu {
		position: fixed;
		z-index: 10000;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		box-shadow:
			0 8px 24px rgba(0, 0, 0, 0.5),
			0 0 0 1px rgba(0, 0, 0, 0.3);
		padding: 4px;
		display: flex;
		flex-direction: column;
		gap: 2px;
		overflow-y: auto;
		overscroll-behavior: contain;
	}

	.asset-item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 8px 12px;
		border-radius: var(--radius-sm);
		background: transparent;
		border: none;
		color: var(--text-secondary);
		font-size: 12px;
		font-family: var(--font-body);
		cursor: pointer;
		text-align: left;
		width: 100%;
	}

	.item-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.asset-item:hover {
		background: var(--bg-elevated);
		color: var(--text-primary);
	}

	.asset-item.active {
		color: var(--accent);
		font-weight: 600;
	}

	.upload-opt {
		color: var(--text-muted);
		border-top: 1px solid var(--border);
		margin-top: 2px;
		padding-top: 10px;
		border-radius: 0 0 var(--radius-sm) var(--radius-sm);
	}

	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}
</style>
