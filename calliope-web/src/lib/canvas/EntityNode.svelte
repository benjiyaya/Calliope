<script lang="ts">
	import { Handle, Position, type Node, type NodeProps } from '@xyflow/svelte';
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import NodeComposer, { type ComposerReference } from '$lib/canvas/NodeComposer.svelte';
	import { assetUrl } from '$lib/api';
	import type { IconName } from '$lib/components/ui/icons';

	interface EntityNodeData extends Record<string, unknown> {
		canvasNodeId: number;
		entityType: 'character' | 'location' | 'item' | 'scene' | 'clip';
		title: string;
		/** `#3.2` — the mention token for a clip card. Absent on other entities. */
		mention?: string | null;
		/** Poster image (env image / reference sheet) — shown while not playing. */
		imagePath: string | null;
		/** Clip path — when set the card becomes a playable video card. */
		videoPath: string | null;
		/** Seed for a clip card's composer. */
		prompt?: string;
		workflowId?: number | null;
		references?: ComposerReference[];
		/** Story cards open the lightbox. Clip cards select and show the composer. */
		onOpenMedia?: (kind: 'image' | 'video') => void;
		onPreview?: () => void;
		onRegenerate?: (prompt: string) => void | Promise<void>;
		onRequestReview?: (prompt: string) => void;
		onSendToAgent?: (prompt: string) => void;
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
			case 'clip':
				return 'play';
		}
	}

	let { data, selected }: NodeProps<EntityNode> = $props();

	const posterSrc = $derived(assetUrl(data.imagePath));
	// '#t=0.1' media fragment: with preload="metadata" browsers paint nothing
	// until playback, so the thumbnail sat black. The fragment seeks+paints
	// the first frame without playing (same trick as ClipMonitor).
	const videoSrc = $derived(data.videoPath ? assetUrl(data.videoPath) + '#t=0.1' : null);

	const clipCard = $derived(data.entityType === 'clip');
	const downloadUrl = $derived(assetUrl(data.videoPath) ?? posterSrc);

	function activate() {
		// Story cards open the lightbox. A clip card selects and shows the
		// composer; Preview on that bar opens the player. Nothing plays in-card.
		if (clipCard) return;
		if (videoSrc) {
			data.onOpenMedia?.('video');
		} else if (posterSrc) {
			data.onOpenMedia?.('image');
		}
	}
</script>

{#snippet card()}
	{#if data.entityType === 'character' || data.entityType === 'location'}
		<Handle type="source" position={Position.Right} id="asset" isConnectable={false} />
	{/if}
	{#if data.entityType === 'clip'}
		<Handle type="target" position={Position.Left} id="clip" isConnectable={false} />
	{/if}
	<header>
		<Icon name={iconFor(data.entityType)} size={14} />
		{#if data.mention}
			<span class="mention">{data.mention}</span>
		{/if}
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
	{#if selected && clipCard}
		{#key data.prompt ?? ''}
		<NodeComposer
			prompt={data.prompt ?? ''}
			mediaUrl={downloadUrl}
			canRegenerate={data.workflowId != null}
			references={data.references ?? []}
			onPreview={() => data.onPreview?.()}
			onRegenerate={(prompt) => data.onRegenerate?.(prompt)}
			onRequestReview={(prompt) => data.onRequestReview?.(prompt)}
			onSendToAgent={(prompt) => data.onSendToAgent?.(prompt)}
		/>
		{/key}
	{/if}
	{#if videoSrc}
		<span class="out-kind">video</span>
	{:else if posterSrc}
		<span class="out-kind">image</span>
	{/if}
{/snippet}

{#if clipCard}
	<div class={['entity-node', { wide: selected }]} role="group">
		{@render card()}
	</div>
{:else}
	<div
		class="entity-node"
		role="button"
		tabindex="0"
		onclick={activate}
		onkeydown={(e) => {
			if (e.key === 'Enter') activate();
		}}
	>
		{@render card()}
	</div>
{/if}

<style>
.entity-node {
	position: relative;
	width: 240px;
	background: var(--bg-surface);
	border: 1px solid var(--border);
	border-radius: 14px;
	overflow: visible;
	cursor: pointer;
	transition: border-color 0.15s;
}
	.entity-node.wide {
		width: 320px;
	}
	.entity-node.wide .out-kind {
		display: none;
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
	.mention {
		flex-shrink: 0;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.02em;
		color: var(--text-primary);
		background: color-mix(in srgb, var(--accent) 28%, transparent);
		border-radius: 999px;
		padding: 1px 6px;
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
	.entity-node :global(.svelte-flow__handle) {
		width: 8px;
		height: 8px;
		background: #8b87c9;
		border: none;
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
