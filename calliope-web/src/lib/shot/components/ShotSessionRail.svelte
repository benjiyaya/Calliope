<script lang="ts">
	/**
	 * Build Scene session rail — one 3D composition per agent session. Lists
	 * scene-origin sandbox sessions, switches between them, creates new ones,
	 * deletes with the session. Mirrors AgentSessionSidebar's collapsed-rail
	 * and list styling.
	 */
	import type { AgentSession } from '$lib/api';
	import Icon from '$lib/components/ui/Icon.svelte';
	import { t } from '$lib/i18n.svelte';

	interface Props {
		sessions: AgentSession[];
		activeId: number | null;
		onSelect: (id: number) => void;
		onNew: () => void;
		onDelete: (id: number) => void;
		collapsed?: boolean;
		onToggleCollapse?: () => void;
	}

	let {
		sessions,
		activeId,
		onSelect,
		onNew,
		onDelete,
		collapsed = false,
		onToggleCollapse,
	}: Props = $props();

	const scenes = $derived(
		sessions.filter((s) => s.project_id == null && s.origin === 'scene'),
	);
</script>

{#if collapsed}
	<aside class="sidebar collapsed">
		<button
			type="button"
			class="rail-btn"
			onclick={() => onToggleCollapse?.()}
			title={t('shot.railExpand')}
			aria-label={t('shot.railExpand')}
		>
			<Icon name="drag" size={14} />
		</button>
		<button
			type="button"
			class="rail-btn"
			onclick={onNew}
			title={t('shot.newScene')}
			aria-label={t('shot.newScene')}
		>
			<Icon name="plus" size={14} />
		</button>
		<div class="rail-dots">
			{#each scenes as s (s.id)}
				<button
					type="button"
					class="rail-dot"
					class:active={s.id === activeId}
					class:run={s.running || s.status === 'running'}
					onclick={() => onSelect(s.id)}
					title={s.title}
					aria-label={t('shot.railOpen', { title: s.title })}
				></button>
			{/each}
		</div>
	</aside>
{:else}
	<aside class="sidebar">
		{#if onToggleCollapse}
			<button
				type="button"
				class="collapse-toggle"
				onclick={() => onToggleCollapse?.()}
				title={t('shot.railCollapse')}
				aria-label={t('shot.railCollapse')}
			>
				<Icon name="drag" size={14} />
			</button>
		{/if}
		<button type="button" class="new-chat" onclick={onNew}>
			<Icon name="plus" size={14} />
			{t('shot.newScene')}
		</button>

		{#if scenes.length === 0}
			<p class="muted">{t('shot.railNoScenes')}</p>
		{/if}

		{#each scenes as s (s.id)}
			<button
				type="button"
				class="item"
				class:active={s.id === activeId}
				onclick={() => onSelect(s.id)}
			>
				<span class="dot" class:run={s.running || s.status === 'running'}></span>
				<span class="title">{s.title}</span>
				<span
					class="del"
					role="button"
					tabindex="-1"
					aria-label={t('shot.railDeleteScene')}
					onclick={(e) => {
						e.stopPropagation();
						onDelete(s.id);
					}}
					onkeydown={(e) => {
						if (e.key === 'Enter') {
							e.stopPropagation();
							onDelete(s.id);
						}
					}}
				>
					<Icon name="trash" size={12} />
				</span>
			</button>
		{/each}
	</aside>
{/if}

<style>
	.sidebar {
		width: 190px;
		flex-shrink: 0;
		border-right: 1px solid var(--border);
		background: var(--bg-surface);
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 12px 10px;
		overflow-y: auto;
		min-height: 0;
	}
	.sidebar.collapsed {
		width: 44px;
		padding: 10px 7px;
		align-items: center;
		gap: 8px;
	}
	.collapse-toggle {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		height: 26px;
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		margin-bottom: 4px;
	}
	.collapse-toggle:hover {
		color: var(--text-primary);
		background: var(--bg-elevated);
	}
	.rail-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 30px;
		height: 30px;
		border: 1px dashed var(--border);
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-secondary);
		cursor: pointer;
	}
	.rail-btn:hover {
		color: var(--text-primary);
		border-color: var(--accent);
	}
	.rail-dots {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 10px;
		padding-top: 6px;
		overflow-y: auto;
		min-height: 0;
	}
	.rail-dot {
		width: 10px;
		height: 10px;
		border-radius: 50%;
		border: 1px solid var(--border);
		background: var(--bg-elevated);
		cursor: pointer;
		padding: 0;
	}
	.rail-dot:hover {
		border-color: var(--accent);
	}
	.rail-dot.active {
		background: var(--accent);
		border-color: var(--accent);
	}
	.rail-dot.run {
		animation: rail-pulse 1.4s ease-in-out infinite;
	}
	@keyframes rail-pulse {
		0%,
		100% {
			box-shadow: 0 0 0 0 var(--accent-glow);
		}
		50% {
			box-shadow: 0 0 0 5px transparent;
		}
	}
	.new-chat {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 7px;
		width: 100%;
		height: 32px;
		border-radius: var(--radius-md);
		border: 1px dashed var(--border);
		background: transparent;
		color: var(--text-secondary);
		font-size: 12px;
		font-weight: 500;
		cursor: pointer;
		margin-bottom: 8px;
		flex-shrink: 0;
	}
	.new-chat:hover {
		color: var(--text-primary);
		border-color: var(--accent);
	}
	.muted {
		font-size: 12px;
		opacity: 0.5;
		margin: 4px 2px;
	}
	.item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		border: 0;
		border-radius: var(--radius-sm);
		background: transparent;
		color: inherit;
		padding: 6px 8px;
		cursor: pointer;
		text-align: left;
		font-size: 12.5px;
	}
	.item:hover {
		background: var(--bg-elevated);
	}
	.item.active {
		background: rgba(99, 102, 241, 0.22);
	}
	.dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--border);
		flex-shrink: 0;
	}
	.dot.run {
		background: var(--accent, #6366f1);
		animation: dot-pulse 1.4s ease-in-out infinite;
	}
	@keyframes dot-pulse {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.35;
		}
	}
	.title {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.del {
		display: none;
		color: var(--text-muted);
		cursor: pointer;
		padding: 2px;
		border-radius: 4px;
		flex-shrink: 0;
	}
	.item:hover .del {
		display: flex;
	}
	.del:hover {
		color: #e5697a;
	}
</style>
