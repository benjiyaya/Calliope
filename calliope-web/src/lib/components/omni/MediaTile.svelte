<script lang="ts">
	/**
	 * MediaTile — square thumbnail for image/ref/audio inputs.
	 * Clicking opens a tabbed asset picker modal (characters / environments /
	 * items / uploads) instead of a clipped dropdown.
	 */
	import { assetUrl } from '$lib/api';
	import type { AssetOption } from '$lib/assetPicker';
	import type { ComfyDynamicInput } from '$lib/comfy/types';
	import AssetPickerModal from './AssetPickerModal.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import { acceptForKind } from '$lib/comfy/useUpload.svelte';

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
	let pickerOpen = $state(false);

	function openPicker() {
		if (uploadingName) return;
		if (matchingAssets.length || allowUpload) {
			pickerOpen = true;
			return;
		}
		fileInput?.click();
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
	const matchingAssets = $derived.by(() => {
		// The same path can arrive twice (e.g. a scene clip that was also
		// uploaded to the playground). Duplicate keys crash the keyed each
		// in the picker grid, so dedupe by path first.
		const seen = new Set<string>();
		return assetOptions.filter((o) => {
			const match =
				!o.kind || o.kind === input.kind || (isImageKind && o.kind === 'image');
			if (!match || seen.has(o.path)) return false;
			seen.add(o.path);
			return true;
		});
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

<div class="tile-wrap">
	<input
		bind:this={fileInput}
		type="file"
		class="sr-only"
		accept={accept || acceptForKind(input.kind)}
		onchange={onFileChosen}
	/>

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
		onkeydown={(e) => {
			if (e.key === 'Enter' || e.key === ' ') {
				e.preventDefault();
				openPicker();
			}
		}}
		role="button"
		tabindex="0"
		title={displayLabel}
		aria-haspopup="dialog"
		aria-expanded={pickerOpen}
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

<AssetPickerModal
	bind:open={pickerOpen}
	title="Choose {displayLabel}"
	assets={matchingAssets}
	{value}
	kind={input.kind}
	{allowUpload}
	onselect={onselectAsset}
	onupload={() => fileInput?.click()}
	onclear={value ? onclear : undefined}
/>

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
