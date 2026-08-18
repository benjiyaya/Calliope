<script lang="ts">
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import StatusChip from '$lib/components/ui/StatusChip.svelte';

	interface Props {
		src: string | null;
		alt: string;
		placeholder: string;
		wide?: boolean;
		/** Taller 4:3 media-first crop (asset card grids). */
		tall?: boolean;
		/** Latest generation-job state for this asset, if the parent tracks jobs. */
		jobState?: 'generating' | 'failed' | null;
		onPreview?: (src: string) => void;
	}

	let {
		src,
		alt,
		placeholder,
		wide = false,
		tall = false,
		jobState = null,
		onPreview,
	}: Props = $props();

	let available = $state(false);

	$effect(() => {
		void src;
		available = false;
	});

	// Job state (live from the queue) wins over the media-derived state.
	const chip = $derived.by((): { status: string; label: string } => {
		if (jobState === 'generating') return { status: 'generating', label: 'Generating' };
		if (jobState === 'failed') return { status: 'failed', label: 'Failed' };
		if (available) return { status: 'ready', label: 'Ready' };
		if (src) return { status: 'missing', label: 'Missing' };
		return { status: 'pending', label: 'Pending' };
	});

	function open() {
		if (available && src && onPreview) onPreview(src);
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			open();
		}
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex a11y_no_noninteractive_element_interactions -->
<div
	class="thumb"
	class:wide
	class:tall
	class:clickable={available && !!onPreview}
	role={available && onPreview ? 'button' : undefined}
	tabindex={available && onPreview ? 0 : undefined}
	onclick={open}
	onkeydown={available && onPreview ? onKey : undefined}
>
	<SafeMedia
		class="fill"
		{src}
		{alt}
		label={placeholder}
		onAvailable={() => (available = true)}
		onUnavailable={() => (available = false)}
	/>
	<span class="thumb-chip">
		<StatusChip status={chip.status} label={chip.label} />
	</span>
</div>

<style>
	.thumb {
		aspect-ratio: 1;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		display: flex;
		align-items: center;
		justify-content: center;
		position: relative;
		overflow: hidden;
		padding: 0;
		margin: 0;
		width: 100%;
		color: inherit;
		font: inherit;
		box-sizing: border-box;
	}
	.thumb.wide {
		aspect-ratio: 16 / 10;
	}
	.thumb.tall {
		aspect-ratio: 4 / 3;
	}
	.thumb.clickable {
		cursor: zoom-in;
	}
	.thumb.clickable:hover {
		border-color: var(--accent, #a78bfa);
	}
	.thumb.clickable:focus-visible {
		outline: 2px solid var(--accent, #a78bfa);
		outline-offset: 2px;
	}
	.thumb :global(.fill) {
		width: 100%;
		height: 100%;
		object-fit: cover;
		border: none;
		border-radius: 0;
		min-height: 0;
	}
	.thumb :global(.safe-media-ph.fill) {
		position: absolute;
		inset: 0;
	}
	.thumb-chip {
		position: absolute;
		top: 8px;
		left: 8px;
		z-index: 1;
		filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.6));
	}
</style>
