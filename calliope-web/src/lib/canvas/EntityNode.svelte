<script lang="ts">
	import { type Node, type NodeProps } from '@xyflow/svelte';
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import { assetUrl } from '$lib/api';
	import type { IconName } from '$lib/components/ui/icons';

	interface EntityNodeData extends Record<string, unknown> {
		canvasNodeId: number;
		entityType: 'character' | 'location' | 'item' | 'scene';
		title: string;
		/** Poster image (env image / reference sheet) — shown while not playing. */
		imagePath: string | null;
		/** Clip path — when set the card becomes a playable video card. */
		videoPath: string | null;
		/** Called on card click: opens the large media preview (no navigation). */
		onOpenMedia?: (kind: 'image' | 'video') => void;
	}

	type EntityNode = Node<EntityNodeData, 'entity'>;

	function iconFor(entityType: EntityNodeData['entityType']): IconName {
		switch (entityType) {
			case 'character':
				return 'sparkle';
			case 'location':
				return 'image';
			case 'item':
				return 'folder';
			case 'scene':
				return 'film';
		}
	}

	let { data }: NodeProps<EntityNode> = $props();

	const posterSrc = $derived(assetUrl(data.imagePath));
	// '#t=0.1' media fragment: with preload="metadata" browsers paint nothing
	// until playback, so the thumbnail sat black. The fragment seeks+paints
	// the first frame without playing (same trick as ClipMonitor).
	const videoSrc = $derived(data.videoPath ? assetUrl(data.videoPath) + '#t=0.1' : null);

	function activate() {
		// One rule for every card (artifact and entity alike): click opens the
		// modal player/lightbox — video cards NEVER play in-card.
		if (videoSrc) {
			data.onOpenMedia?.('video');
		} else if (posterSrc) {
			data.onOpenMedia?.('image');
		}
	}
</script>

<div
	class="entity-node"
	role="button"
	tabindex="0"
	onclick={activate}
	onkeydown={(e) => {
		if (e.key === 'Enter') activate();
	}}
>
	<header>
		<Icon name={iconFor(data.entityType)} size={14} />
		<span class="title" title={data.title}>{data.title}</span>
		{#if videoSrc}
			<span class="play-badge" aria-hidden="true">
				<Icon name="play" size={10} />
			</span>
		{/if}
	</header>
	<div class="media">
		{#if videoSrc && posterSrc}
			<!-- Idle video card: show the poster (cheap static file, never a
				mounted <video>). Click opens the modal player. -->
			<SafeMedia class="media-el" src={posterSrc} alt={data.title} kind="image" controls={false} />
		{:else if videoSrc}
			<!-- No poster available: #t=0.1 first-frame thumbnail. -->
			<!-- svelte-ignore a11y_media_has_caption -->
			<video
				class="media-el"
				src={videoSrc}
				muted
				playsinline
				preload="metadata"
			></video>
		{:else if posterSrc}
			<SafeMedia class="media-el" src={posterSrc} alt={data.title} kind="image" controls={false} />
		{:else}
			<span class="placeholder">{data.entityType}</span>
		{/if}
	</div>
	{#if videoSrc}
		<span class="out-kind">video</span>
	{:else if posterSrc}
		<span class="out-kind">image</span>
	{/if}
</div>

<style>
.entity-node {
	width: 240px;
	background: var(--bg-surface);
	border: 1px solid var(--border);
	border-radius: 14px;
	overflow: visible;
	cursor: pointer;
	transition: border-color 0.15s;
}
	.entity-node:hover {
		border-color: #3a3a44;
	}
	/* Svelte Flow marks the selected node wrapper with .selected */
	:global(.svelte-flow__node.selected) .entity-node {
		border-color: var(--accent);
		box-shadow:
			0 0 0 1px var(--accent),
			0 0 40px var(--accent-glow);
	}
	header {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 9px 12px;
		color: var(--text-secondary);
	}
	.title {
		flex: 1;
		min-width: 0;
		font-size: 12.5px;
		font-weight: 500;
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.play-badge {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		border-radius: 50%;
		background: color-mix(in srgb, var(--accent) 22%, transparent);
		color: var(--text-primary);
		flex-shrink: 0;
	}
	.media {
		height: 132px;
		background: var(--bg-elevated);
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
		border-radius: 0 0 14px 14px;
	}
	.media :global(.media-el),
	.media-el {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.placeholder {
		font-size: 10px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
	}

	.out-kind {
		position: absolute;
		right: 16px;
		bottom: 10px;
		font-size: 8.5px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: lowercase;
		color: var(--text-muted);
		pointer-events: none;
		background: rgba(0, 0, 0, 0.55);
		padding: 1px 5px;
		border-radius: 999px;
	}
</style>
