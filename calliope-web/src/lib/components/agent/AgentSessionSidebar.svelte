<script lang="ts">
	import type { AgentSession } from '$lib/api';
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		sessions: AgentSession[];
		activeId: number | null;
		onSelect: (id: number) => void;
		onNewSandbox: () => void;
		onNewProjectChat: (projectId: number) => void;
		onDelete: (id: number) => void;
	}

	let { sessions, activeId, onSelect, onNewSandbox, onNewProjectChat, onDelete }: Props =
		$props();

	const sandbox = $derived(sessions.filter((s) => s.project_id == null));
	const grouped = $derived.by(() => {
		const map = new Map<number, { project: AgentSession['project']; items: AgentSession[] }>();
		for (const s of sessions) {
			if (s.project_id == null) continue;
			const key = s.project_id;
			if (!map.has(key)) map.set(key, { project: s.project ?? null, items: [] });
			map.get(key)!.items.push(s);
		}
		return [...map.values()];
	});
</script>

<aside class="sidebar">
	<button type="button" class="new-chat" onclick={onNewSandbox}>
		<Icon name="plus" size={14} />
		New sandbox chat
	</button>

	{#if sessions.length === 0}
		<p class="muted">No sessions yet.</p>
	{/if}

	{#if sandbox.length > 0}
		<div class="group">
			<div class="group-head">
				<span class="group-title">Sandbox</span>
				<span class="count">{sandbox.length}</span>
			</div>
			{#each sandbox as s (s.id)}
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
						aria-label="Delete session"
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
		</div>
	{/if}

	{#each grouped as g (g.project?.id ?? 0)}
		<div class="group">
			<div class="group-head">
				<span class="group-title" title={g.project?.title}>
					{g.project?.title ?? `Project #${g.project?.id ?? '?'}`}
				</span>
				<span class="count">{g.items.length}</span>
				<span
					class="add-chat"
					role="button"
					tabindex="0"
					aria-label="New chat for this project"
					title="New chat for this project"
					onclick={(e) => {
						e.stopPropagation();
						if (g.project) onNewProjectChat(g.project.id);
					}}
					onkeydown={(e) => {
						if (e.key === 'Enter') {
							e.stopPropagation();
							if (g.project) onNewProjectChat(g.project.id);
						}
					}}
				>
					<Icon name="plus" size={12} />
				</span>
			</div>
			{#each g.items as s (s.id)}
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
						aria-label="Delete session"
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
		</div>
	{/each}
</aside>

<style>
	.sidebar {
		width: 240px;
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
	.new-chat {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 7px;
		width: 100%;
		height: 36px;
		border-radius: var(--radius-md);
		border: 1px dashed var(--border);
		background: transparent;
		color: var(--text-secondary);
		font-size: 13px;
		font-weight: 500;
		cursor: pointer;
		margin-bottom: 8px;
	}
	.new-chat:hover {
		color: var(--text-primary);
		border-color: var(--accent);
	}
	.new-chat:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.muted {
		color: var(--text-muted);
		font-size: 12px;
		padding: 8px;
		margin: 0;
	}
	.group {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.group-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 8px 8px 4px;
	}
	.group-title {
		font-size: 10.5px;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.count {
		font-size: 10px;
		color: var(--text-muted);
		flex-shrink: 0;
	}
	.add-chat {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		border-radius: var(--radius-sm);
		color: var(--text-muted);
		cursor: pointer;
		flex-shrink: 0;
	}
	.add-chat:hover {
		color: var(--accent);
		background: color-mix(in srgb, var(--accent) 14%, transparent);
	}
	.item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 8px;
		border-radius: var(--radius-sm);
		border: none;
		background: transparent;
		color: var(--text-secondary);
		font-size: 12.5px;
		cursor: pointer;
		text-align: left;
	}
	.item:hover {
		background: var(--bg-elevated);
		color: var(--text-primary);
	}
	.item.active {
		background: var(--bg-elevated);
		color: var(--text-primary);
		box-shadow: inset 0 -2px 0 var(--accent);
	}
	.dot {
		width: 7px;
		height: 7px;
		border-radius: 999px;
		background: var(--text-muted);
		opacity: 0.5;
		flex-shrink: 0;
	}
	.dot.run {
		background: var(--warning);
		opacity: 1;
		animation: pulse 1.1s ease-in-out infinite;
	}
	@keyframes pulse {
		0%,
		100% {
			opacity: 0.35;
		}
		50% {
			opacity: 1;
		}
	}
	.title {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.del {
		display: none;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 22px;
		border-radius: var(--radius-sm);
		color: var(--text-muted);
		flex-shrink: 0;
	}
	.item:hover .del {
		display: inline-flex;
	}
	.del:hover {
		color: var(--error);
		background: rgba(239, 68, 68, 0.12);
	}
</style>
