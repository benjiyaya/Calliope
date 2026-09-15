<script lang="ts">
	/**
	 * Bottom strip of saved captures for this composition — images (viewport
	 * PNGs) and videos (camera-track exports). Thumbnails open the shared
	 * ImageLightbox (video captures get the lightbox's video player); each
	 * thumbnail offers a copy-path action so the file can be pasted into
	 * generation inputs.
	 */
	import ImageLightbox from '$lib/components/ImageLightbox.svelte';
	import { shotApi } from '$lib/shot/api';
	import { t } from '$lib/i18n.svelte';

	interface Capture {
		id: number;
		kind?: 'image' | 'video';
		file_path: string | null;
		label: string | null;
	}

	let { captures, compositionId }: { captures: Capture[]; compositionId: number | null } = $props();

	let lightboxIndex = $state<number | null>(null);
	let copiedId = $state<number | null>(null);

	const current = $derived(lightboxIndex !== null ? captures[lightboxIndex] : null);
	const currentIsVideo = $derived(current?.kind === 'video');

	async function remove(capture: Capture) {
		if (compositionId === null) return;
		await shotApi.deleteCapture(compositionId, capture.id);
	}

	function srcOf(c: Capture): string {
		return `/api/file?path=${encodeURIComponent(c.file_path ?? '')}`;
	}
</script>

<div class="strip">
	<span class="label">{t('shot.captures', { count: captures.length })}</span>
	<div class="thumbs">
		{#each captures as c, i (c.id)}
			{#if c.file_path}
				<figure class="thumb" title={c.label ?? ''}>
					<button class="open" onclick={() => (lightboxIndex = i)}>
						{#if c.kind === 'video'}
							<span class="video-badge">▶ {t('shot.videoBadge')}</span>
						{/if}
						<img src={srcOf(c)} alt={c.label ?? t('shot.captureAlt')} />
					</button>
					<figcaption>
						<button
							class="mini"
							onclick={() => {
								void navigator.clipboard.writeText(c.file_path ?? '');
								copiedId = c.id;
								setTimeout(() => (copiedId = null), 1200);
							}}
						>{copiedId === c.id ? '✓' : '⧉'}</button>
						<button class="mini danger" onclick={() => remove(c)}>✕</button>
					</figcaption>
				</figure>
			{/if}
		{:else}
			<span class="empty">{t('shot.noCaptures')}</span>
		{/each}
	</div>
</div>

{#if current && current.file_path}
	<ImageLightbox
		src={srcOf(current)}
		alt={current.label ?? t('shot.captureAlt')}
		caption={current.label ?? undefined}
		kind={currentIsVideo ? 'video' : 'image'}
		images={captures.filter((c) => c.file_path).map((c) => srcOf(c))}
		index={lightboxIndex ?? 0}
		onnavigate={(i) => (lightboxIndex = i)}
		onClose={() => (lightboxIndex = null)}
	/>
{/if}

<style>
	.strip {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 8px 12px;
		border-top: 1px solid var(--border, #232733);
		min-height: 74px;
	}
	.label {
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		opacity: 0.6;
		white-space: nowrap;
	}
	.thumbs {
		display: flex;
		gap: 8px;
		overflow-x: auto;
		align-items: center;
	}
	.thumb {
		margin: 0;
		position: relative;
	}
	.open {
		display: block;
		position: relative;
		border: 1px solid var(--border, #2a2f3a);
		border-radius: 6px;
		overflow: hidden;
		padding: 0;
		background: none;
		cursor: zoom-in;
	}
	.open img {
		display: block;
		height: 56px;
		width: auto;
	}
	.video-badge {
		position: absolute;
		bottom: 4px;
		left: 4px;
		z-index: 1;
		font-size: 9px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		background: rgba(0, 0, 0, 0.65);
		color: #fff;
		border-radius: 4px;
		padding: 1px 5px;
		pointer-events: none;
	}
	figcaption {
		display: flex;
		gap: 4px;
		justify-content: center;
		margin-top: 2px;
	}
	.mini {
		background: none;
		border: 0;
		color: inherit;
		opacity: 0.6;
		font-size: 11px;
		cursor: pointer;
		padding: 0 2px;
	}
	.mini:hover {
		opacity: 1;
	}
	.danger {
		color: #e5697a;
	}
	.empty {
		font-size: 12px;
	 opacity: 0.45;
	}
</style>
