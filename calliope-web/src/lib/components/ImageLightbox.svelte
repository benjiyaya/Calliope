<script lang="ts">
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		src: string | null;
		alt?: string;
		onClose: () => void;
		/** Optional filmstrip — enables prev/next buttons and arrow keys. */
		images?: string[];
		index?: number;
		onnavigate?: (i: number) => void;
		caption?: string;
		/** Render a video player instead of a zoomable image. */
		kind?: 'image' | 'video';
	}

	let {
		src,
		alt = '',
		onClose,
		images,
		index = 0,
		onnavigate,
		caption,
		kind = 'image',
	}: Props = $props();

	let zoomed = $state(false);

	const isVideo = $derived(kind === 'video');

	$effect(() => {
		void src;
		zoomed = false;
	});

	const hasNav = $derived(Boolean(images && images.length > 1 && onnavigate));

	function step(delta: number) {
		if (!images || images.length === 0 || !onnavigate) return;
		onnavigate((index + delta + images.length) % images.length);
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') onClose();
		else if (hasNav && e.key === 'ArrowLeft') step(-1);
		else if (hasNav && e.key === 'ArrowRight') step(1);
	}

	const downloadName = $derived.by(() => {
		if (!src) return 'media';
		try {
			const query = src.split('?')[1] ?? '';
			const path = new URLSearchParams(query).get('path');
			const name = path ? decodeURIComponent(path).split(/[\\/]/).pop() : null;
			if (name) return name;
		} catch {
			// fall through to the generic name
		}
		return alt ? `${alt.trim().replace(/\s+/g, '-').toLowerCase()}.png` : 'media';
	});
</script>

<svelte:window onkeydown={src ? onKeydown : undefined} />

{#if src}
	<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
	<div
		class="backdrop"
		role="dialog"
		aria-modal="true"
		aria-label={isVideo ? 'Video preview' : 'Image preview'}
		tabindex="-1"
		onclick={onClose}
	>
		<div class="toolbar">
			{#if hasNav}
				<span class="counter">{index + 1} / {images?.length ?? 0}</span>
			{/if}
			{#if !isVideo}
				<button
					class="tool"
					type="button"
					aria-label={zoomed ? 'Fit to screen' : 'Zoom to 100%'}
					title={zoomed ? 'Fit to screen' : 'Zoom to 100%'}
					onclick={() => (zoomed = !zoomed)}
				>
					<Icon name="zoom-in" size={17} />
				</button>
			{/if}
			<a
				class="tool"
				href={src}
				download={downloadName}
				aria-label="Download media"
				title="Download"
				onclick={(e) => e.stopPropagation()}
			>
				<Icon name="download" size={17} />
			</a>
			<button
				class="tool"
				type="button"
				aria-label="Close preview"
				title="Close (Esc)"
				onclick={onClose}
			>
				<Icon name="close" size={17} />
			</button>
		</div>

		{#if hasNav}
			<button
				class="nav prev"
				type="button"
				aria-label="Previous image"
				onclick={(e) => {
					e.stopPropagation();
					step(-1);
				}}
			>
				<Icon name="chevron-left" size={22} />
			</button>
			<button
				class="nav next"
				type="button"
				aria-label="Next image"
				onclick={(e) => {
					e.stopPropagation();
					step(1);
				}}
			>
				<Icon name="chevron-right" size={22} />
			</button>
		{/if}

		{#if isVideo}
			<!-- Clicks on the player (play/pause/seek) must not close the modal -->
			<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
			<div class="player-frame" onclick={(e) => e.stopPropagation()}>
				<SafeMedia
					class="player"
					{src}
					{alt}
					kind="video"
					label="Video unavailable"
					autoplay
					controls
					preload="auto"
				/>
			</div>
		{:else}
			<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
			<div
				class="frame"
				class:zoomed
				title={zoomed ? 'Click to fit' : 'Click to zoom'}
				onclick={(e) => {
					e.stopPropagation();
					zoomed = !zoomed;
				}}
			>
				<SafeMedia class="preview" {src} {alt} label="Image unavailable" />
			</div>
		{/if}
		{#if caption}
			<p class="caption">{caption}</p>
		{/if}
		<p class="hint">
			{#if isVideo}
				Click outside or press Esc to close ·
			{:else}
				Click the image to zoom · Click outside or press Esc to close ·
			{/if}
			<a href={src} target="_blank" rel="noopener" onclick={(e) => e.stopPropagation()}
				>Open original</a
			>
		</p>
	</div>
{/if}

<style>
	.backdrop {
		position: fixed;
		inset: 0;
		z-index: 10000;
		background: rgba(0, 0, 0, 0.88);
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		padding: 48px 24px 32px;
		gap: 12px;
	}
	.toolbar {
		position: absolute;
		top: 12px;
		right: 16px;
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.counter {
		font-size: 12px;
		color: rgba(255, 255, 255, 0.65);
		margin-right: 4px;
		font-variant-numeric: tabular-nums;
	}
	.tool {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		border: none;
		border-radius: 999px;
		background: rgba(255, 255, 255, 0.12);
		color: #fff;
		cursor: pointer;
		transition: background 0.15s;
	}
	.tool:hover {
		background: rgba(255, 255, 255, 0.22);
	}
	.tool:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.nav {
		position: absolute;
		top: 50%;
		transform: translateY(-50%);
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		border: none;
		border-radius: 999px;
		background: rgba(255, 255, 255, 0.1);
		color: #fff;
		cursor: pointer;
		transition: background 0.15s;
	}
	.nav:hover {
		background: rgba(255, 255, 255, 0.22);
	}
	.nav:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.nav.prev {
		left: 16px;
	}
	.nav.next {
		right: 16px;
	}
	.frame {
		max-width: min(96vw, 1600px);
		max-height: calc(100vh - 140px);
		display: flex;
		overflow: auto;
		cursor: zoom-in;
	}
	.frame.zoomed {
		cursor: zoom-out;
	}
	.frame :global(.preview) {
		max-width: min(96vw, 1600px);
		max-height: calc(100vh - 140px);
		width: auto;
		height: auto;
		object-fit: contain;
		border-radius: 4px;
		box-shadow: 0 8px 40px rgba(0, 0, 0, 0.5);
		min-height: 120px;
		min-width: 200px;
		margin: auto;
	}
	.frame.zoomed :global(.preview) {
		max-width: none;
		max-height: none;
	}
	/* ── Video player mode ───────────────────────────────────── */
	.player-frame {
		max-width: min(96vw, 1600px);
		width: 100%;
		display: flex;
		justify-content: center;
	}
	.player-frame :global(.player) {
		max-width: min(96vw, 1600px);
		max-height: calc(100vh - 160px);
		width: 100%;
		border-radius: 6px;
		background: #000;
		box-shadow: 0 8px 40px rgba(0, 0, 0, 0.5);
	}
	.caption {
		margin: 0;
		font-size: 13px;
		color: rgba(255, 255, 255, 0.85);
		max-width: min(96vw, 720px);
		text-align: center;
	}
	.hint {
		margin: 0;
		font-size: 12px;
		color: rgba(255, 255, 255, 0.55);
	}
	.hint a {
		color: rgba(255, 255, 255, 0.85);
	}
</style>
