<script lang="ts">
	import type { Node, NodeProps } from '@xyflow/svelte';
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import { assetUrl } from '$lib/api';
	import type { IconName } from '$lib/components/ui/icons';

	interface EntityNodeData extends Record<string, unknown> {
		canvasNodeId: number;
		entityType: 'character' | 'location' | 'item' | 'scene';
		title: string;
		imagePath: string | null;
		onOpen?: () => void;
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

	const mediaSrc = $derived(assetUrl(data.imagePath));
</script>

<div
	class="entity-node"
	role="button"
	tabindex="0"
	onclick={() => data.onOpen?.()}
	onkeydown={(e) => e.key === 'Enter' && data.onOpen?.()}
>
	<header>
		<Icon name={iconFor(data.entityType)} size={14} />
		<span class="title" title={data.title}>{data.title}</span>
	</header>
	<div class="media">
		{#if mediaSrc}
			<SafeMedia src={mediaSrc} alt={data.title} kind="image" controls={false} />
		{:else}
			<span class="placeholder">{data.entityType}</span>
		{/if}
	</div>
</div>

<style>
	.entity-node {
		width: 240px;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: 14px;
		overflow: hidden;
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
	.media {
		height: 132px;
		background: var(--bg-elevated);
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.media :global(img),
	.media :global(video) {
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
</style>
