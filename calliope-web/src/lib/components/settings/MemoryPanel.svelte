<script lang="ts">
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { agentApi, projects } from '$lib/api';
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import { toast } from '$lib/toast';

	const client = useQueryClient();
	const memoriesQuery = createQuery({
		queryKey: ['agent-memories'],
		queryFn: agentApi.listMemories,
	});
	const projectsQuery = createQuery({
		queryKey: ['projects'],
		queryFn: projects.list,
	});

	let newContent = $state('');
	let newScope = $state<'global' | 'project'>('global');
	let newProjectId = $state<number | null>(null);
	let newKind = $state('preference');

	const addMutation = createMutation({
		mutationFn: () =>
			agentApi.addMemory({
				content: newContent.trim(),
				scope: newScope,
				project_id: newScope === 'project' ? newProjectId : null,
				kind: newKind,
			}),
		onSuccess: () => {
			newContent = '';
			client.invalidateQueries({ queryKey: ['agent-memories'] });
			toast.success('Memory saved');
		},
		onError: (err) => toast.error(err instanceof Error ? err.message : 'Could not save'),
	});

	const deleteMutation = createMutation({
		mutationFn: (id: number) => agentApi.deleteMemory(id),
		onSuccess: () => client.invalidateQueries({ queryKey: ['agent-memories'] }),
		onError: (err) => toast.error(err instanceof Error ? err.message : 'Could not delete'),
	});

	const canAdd = $derived(
		newContent.trim().length > 0 && (newScope === 'global' || newProjectId != null),
	);

	const globalMemories = $derived(
		($memoriesQuery.data ?? []).filter((m) => m.scope === 'global'),
	);
	const projectMemories = $derived(
		($memoriesQuery.data ?? []).filter((m) => m.scope === 'project'),
	);
</script>

<section class="panel">
	<h1>Memory</h1>
	<p class="lead">
		Durable preferences and conventions the agent carries across chats. The
		agent saves these itself when you state a preference ("I always want…",
		"never do…"); you can add or delete them here. Saved memories are
		injected into every agent turn.
	</p>

	<div class="add-row">
		<input
			class="field-input add-input"
			placeholder="e.g. User prefers terse scene descriptions"
			maxlength={500}
			bind:value={newContent}
			onkeydown={(e) => {
				if (e.key === 'Enter' && canAdd) $addMutation.mutate();
			}}
		/>
		<select class="field-input scope-select" bind:value={newScope}>
			<option value="global">Global</option>
			<option value="project">Project</option>
		</select>
		{#if newScope === 'project'}
			<select class="field-input scope-select" bind:value={newProjectId}>
				<option value={null}>Choose project…</option>
				{#each ($projectsQuery.data ?? []) as p (p.id)}
					<option value={p.id}>{p.title}</option>
				{/each}
			</select>
		{/if}
		<select class="field-input scope-select" bind:value={newKind}>
			<option value="preference">preference</option>
			<option value="convention">convention</option>
			<option value="correction">correction</option>
		</select>
		<Button
			variant="primary"
			size="sm"
			disabled={!canAdd || $addMutation.isPending}
			onclick={() => $addMutation.mutate()}
		>
			Add
		</Button>
	</div>

	{#if $memoriesQuery.isLoading}
		<p class="muted">Loading memories…</p>
	{:else if globalMemories.length === 0 && projectMemories.length === 0}
		<p class="muted">
			No memories yet. Tell the agent a preference ("always give me
			16:9", "never show the villain's face") and it will save one — or
			add it above.
		</p>
	{:else}
		{#if globalMemories.length > 0}
			<h2 class="group-head">Global</h2>
			<div class="mem-list">
				{#each globalMemories as m (m.id)}
					<div class="mem-row">
						<span class="mem-kind">{m.kind}</span>
						<span class="mem-content">{m.content}</span>
						<span class="mem-meta">
							{m.source === 'user' ? 'you' : 'agent'} · used {m.use_count}×
						</span>
						<button
							type="button"
							class="mem-del"
							title="Forget"
							onclick={() => $deleteMutation.mutate(m.id)}
						>
							<Icon name="trash" size={13} />
						</button>
					</div>
				{/each}
			</div>
		{/if}
		{#if projectMemories.length > 0}
			<h2 class="group-head">Per project</h2>
			<div class="mem-list">
				{#each projectMemories as m (m.id)}
					<div class="mem-row">
						<span class="mem-kind">{m.kind}</span>
						<span class="mem-project">{m.project_title ?? `#${m.project_id}`}</span>
						<span class="mem-content">{m.content}</span>
						<span class="mem-meta">
							{m.source === 'user' ? 'you' : 'agent'} · used {m.use_count}×
						</span>
						<button
							type="button"
							class="mem-del"
							title="Forget"
							onclick={() => $deleteMutation.mutate(m.id)}
						>
							<Icon name="trash" size={13} />
						</button>
					</div>
				{/each}
			</div>
		{/if}
	{/if}
</section>

<style>
	.panel {
		background: rgba(255, 255, 255, 0.03);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: var(--radius-lg);
		padding: var(--space-lg);
		margin-top: var(--space-lg);
	}
	.panel h1 {
		margin: 0 0 6px;
		font-size: 22px;
	}
	.lead {
		margin: 0 0 var(--space-lg);
		color: var(--text-secondary);
		font-size: 14px;
	}
	.add-row {
		display: flex;
		gap: 8px;
		align-items: center;
		margin-bottom: var(--space-md);
		flex-wrap: wrap;
	}
	.add-input {
		flex: 1;
		min-width: 220px;
	}
	.field-input {
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--text-primary);
		padding: 8px 10px;
		font-size: 13px;
		font-family: inherit;
	}
	.field-input:focus {
		outline: none;
		border-color: var(--accent);
	}
	.scope-select {
		width: auto;
		max-width: 160px;
	}
	.group-head {
		margin: var(--space-md) 0 6px;
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.mem-list {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.mem-row {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 12px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: rgba(0, 0, 0, 0.18);
		font-size: 13px;
	}
	.mem-kind {
		flex-shrink: 0;
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--accent);
		border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
		border-radius: 999px;
		padding: 1px 8px;
	}
	.mem-project {
		flex-shrink: 0;
		font-weight: 600;
		color: var(--text-secondary);
		max-width: 160px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mem-content {
		flex: 1;
		min-width: 0;
		color: var(--text-primary);
	}
	.mem-meta {
		flex-shrink: 0;
		font-size: 11px;
		color: var(--text-muted);
	}
	.mem-del {
		flex-shrink: 0;
		border: none;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		padding: 4px;
		border-radius: var(--radius-sm);
	}
	.mem-del:hover {
		color: var(--error);
		background: rgba(239, 68, 68, 0.12);
	}
	.muted {
		color: var(--text-muted);
		font-size: 13px;
	}
</style>
