<script lang="ts">
	interface Props {
		src: string | null | undefined;
		alt?: string;
		kind?: 'image' | 'video';
		label?: string;
		class?: string;
		loading?: 'lazy' | 'eager';
		controls?: boolean;
		preload?: 'none' | 'metadata' | 'auto';
		autoplay?: boolean;
		onUnavailable?: () => void;
		onAvailable?: () => void;
	}

	let {
		src = null,
		alt = '',
		kind = 'image',
		label = 'No media',
		class: klass = '',
		loading = 'lazy',
		controls = true,
		preload = 'metadata',
		autoplay = false,
		onUnavailable,
		onAvailable,
	}: Props = $props();

	let failed = $state(false);

	$effect(() => {
		void src;
		failed = false;
		if (!src) onUnavailable?.();
	});

	const usable = $derived(Boolean(src) && !failed);

	function markAvailable() {
		onAvailable?.();
	}

	function markFailed() {
		failed = true;
		onUnavailable?.();
	}
</script>

{#if usable && src}
	{#if kind === 'video'}
		<!-- svelte-ignore a11y_media_has_caption -->
		<video
			class={klass}
			{src}
			{controls}
			{preload}
			{autoplay}
			onloadeddata={markAvailable}
			onerror={markFailed}
		></video>
	{:else}
		<img
			class={klass}
			{src}
			{alt}
			{loading}
			onload={markAvailable}
			onerror={markFailed}
		/>
	{/if}
{:else}
	<div class="safe-media-ph {klass}" role="img" aria-label={label}>
		<span>{label}</span>
	</div>
{/if}

<style>
	.safe-media-ph {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		min-height: 72px;
		box-sizing: border-box;
		background:
			repeating-linear-gradient(
				-45deg,
				transparent,
				transparent 6px,
				rgba(255, 255, 255, 0.03) 6px,
				rgba(255, 255, 255, 0.03) 12px
			),
			var(--bg-elevated, #16161d);
		border: 1px dashed var(--border, #2a2a35);
		border-radius: inherit;
		color: var(--text-muted, #8b8b9a);
		font-size: 12px;
		font-weight: 500;
		letter-spacing: 0.02em;
		text-align: center;
		padding: 12px;
	}

	.safe-media-ph span {
		opacity: 0.9;
	}
</style>
