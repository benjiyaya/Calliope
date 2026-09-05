<script lang="ts">
	import { createQuery } from '@tanstack/svelte-query';
	import { agentApi } from '$lib/api';
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import Skeleton from '$lib/components/ui/Skeleton.svelte';
	import { toast } from '$lib/toast';

	interface SkillSummary {
		name: string;
		description: string;
		version: string;
		tags: string[];
		dir: string;
	}

	const skillsQuery = createQuery({
		queryKey: ['agent-skills'],
		queryFn: agentApi.listSkills,
	});
	const pathQuery = createQuery({
		queryKey: ['agent-skills-path'],
		queryFn: agentApi.skillsPath,
	});

	// Expanded skill (file list open) + the file whose body is being previewed.
	let expanded = $state<string | null>(null);
	let filesBySkill = $state<Record<string, string[]>>({});
	let preview = $state<{
		skill: string;
		path: string;
		content: string;
		truncated: boolean;
	} | null>(null);
	let previewLoading = $state(false);

	async function toggleFiles(name: string) {
		if (expanded === name) {
			expanded = null;
			return;
		}
		expanded = name;
		preview = null;
		if (!filesBySkill[name]) {
			try {
				const res = await agentApi.skillFiles(name);
				filesBySkill = { ...filesBySkill, [name]: res.files };
			} catch {
				filesBySkill = { ...filesBySkill, [name]: [] };
			}
		}
	}

	async function openFile(skill: string, file: string) {
		previewLoading = true;
		preview = null;
		try {
			const res = await agentApi.readSkillFile(skill, file);
			preview = { skill, path: file, content: res.content, truncated: res.truncated };
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not read file');
		} finally {
			previewLoading = false;
		}
	}

	async function copyText(text: string, quiet = false) {
		try {
			await navigator.clipboard.writeText(text);
			if (!quiet) toast.success('Copied');
		} catch {
			/* clipboard unavailable — text stays selectable */
		}
	}
</script>

<section class="panel">
	<div class="panel-head">
		<div>
			<h1>Skills</h1>
			<p class="lead">
				Reusable expertise the agent loads on demand — prompt patterns, workflow recipes,
				styling guides. Skills are plain folders you edit outside the app.
			</p>
		</div>
		<Button
			variant="secondary"
			size="sm"
			onclick={() => {
				$skillsQuery.refetch();
				toast.info('Skills refreshed');
			}}
		>
			<Icon name="retry" size={14} />
			Refresh
		</Button>
	</div>

	{#if $pathQuery.data?.path}
		<div class="path-banner">
			<Icon name="folder" size={14} />
			<span class="mono path-text">{$pathQuery.data.path}</span>
			<button
				type="button"
				class="mini-btn"
				onclick={() => copyText($pathQuery.data?.path ?? '')}
				title="Copy folder path"
			>
				copy path
			</button>
		</div>
	{/if}

	<div class="callout info">
		<Icon name="edit" size={16} />
		<div class="callout-body">
			<p class="howto-title"><strong>How to add a skill</strong></p>
			<ol class="howto">
				<li>
					<strong>Create a folder</strong> inside the skills path above, named after your
					skill (lowercase, dashes — e.g. <code class="mono">night-city-prompts</code>).
				</li>
				<li>
					<strong>Add a <code class="mono">SKILL.md</code></strong> inside it, starting with
					YAML frontmatter so Calliope can list it:
					<pre class="mono snippet">{`---
name: night-city-prompts
description: "Use when the user wants neon/cyberpunk night-scene prompts."
version: 1.0.0
---

# Night City Prompts
Write the guidance the agent should follow here…`}</pre>
					<p class="snippet-note">
						<code class="mono">name</code> is what the agent types after
						<code class="mono">/</code>; <code class="mono">description</code> tells the agent
						<em>when</em> to use the skill — write it like a trigger condition.
					</p>
				</li>
				<li>
					<strong>Extra files are welcome</strong> (e.g.
					<code class="mono">references/style-guide.md</code>). Mention them in the SKILL.md
					body ("Load references/style-guide.md for the full spec") — the agent reads them
					with the same containment guards.
				</li>
				<li>
					<strong>Save, then Refresh.</strong> The skill appears below, in the chat composer's
					<code class="mono">/</code> picker, and the agent lists it automatically. Editing
					an existing SKILL.md takes effect on the next agent turn — no restart.
				</li>
			</ol>
			<p class="note">
				Built-in skills are seeded into this folder on first run and are yours to edit —
				Calliope never overwrites your changes.
			</p>
		</div>
	</div>

	{#if $skillsQuery.isLoading}
		<div class="stack">
			<Skeleton height="72px" />
			<Skeleton height="72px" />
		</div>
	{:else if ($skillsQuery.data ?? []).length === 0}
		<p class="empty">No skills found — add one using the steps above.</p>
	{:else}
		<div class="stack">
			{#each $skillsQuery.data ?? [] as skill (skill.dir)}
				<article class="skill-card">
					<header class="card-head">
						<button
							type="button"
							class="expand"
							aria-expanded={expanded === skill.dir}
							onclick={() => toggleFiles(skill.dir)}
							title={expanded === skill.dir ? 'Hide files' : 'Show files'}
						>
							<Icon
								name={expanded === skill.dir ? 'chevron-down' : 'chevron-right'}
								size={14}
							/>
						</button>
						<div class="card-text">
							<div class="name-row">
								<span class="name">/{skill.name}</span>
								{#if skill.version}
									<span class="version">v{skill.version}</span>
								{/if}
							</div>
							{#if skill.description}
								<p class="desc">{skill.description}</p>
							{/if}
							{#if Array.isArray(skill.tags) && skill.tags.length > 0}
								<div class="tags">
									{#each skill.tags as tag (tag)}
										<span class="tag">{tag}</span>
									{/each}
								</div>
							{/if}
						</div>
					</header>
					{#if expanded === skill.dir}
						<div class="files">
							<p class="files-label">Files in <span class="mono">{skill.dir}/</span></p>
							{#each filesBySkill[skill.dir] ?? [] as file (file)}
								<button
									type="button"
									class="file-row"
									class:active={preview?.skill === skill.dir && preview?.path === file}
									onclick={() => openFile(skill.dir, file)}
								>
									<Icon name="edit" size={12} />
									<span class="mono">{file}</span>
								</button>
							{:else}
								<span class="muted">No files found</span>
							{/each}
						</div>
					{/if}
				</article>
			{/each}
		</div>
	{/if}

	{#if previewLoading}
		<p class="muted">Loading file…</p>
	{:else if preview}
		<div class="preview-panel">
			<header class="preview-head">
				<span class="mono preview-path">{preview.skill}/{preview.path}</span>
				{#if preview.truncated}
					<span class="truncated-chip">truncated to 8k chars</span>
				{/if}
				<button
					type="button"
					class="mini-btn"
					onclick={() => copyText(preview?.content ?? '')}
				>
					copy
				</button>
				<button type="button" class="mini-btn" onclick={() => (preview = null)}>close</button>
			</header>
			<pre class="mono preview-body">{preview.content}</pre>
		</div>
	{/if}
</section>

<style>
	.panel {
		background: rgba(255, 255, 255, 0.03);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: var(--radius-lg);
		padding: var(--space-lg);
	}
	.panel-head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-md);
		margin-bottom: var(--space-md);
	}
	.panel-head h1 {
		margin: 0 0 6px;
		font-size: 22px;
	}
	.panel-head .lead {
		margin: 0;
	}
	.lead {
		color: var(--text-secondary);
		font-size: 14px;
	}
	.path-banner {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 14px;
		margin-bottom: var(--space-md);
		border-radius: var(--radius-md);
		border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
		background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
	}
	.path-banner :global(.icon) {
		color: var(--accent);
		flex-shrink: 0;
	}
	.path-text {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--text-primary);
		font-size: 12.5px;
	}
	.mini-btn {
		flex-shrink: 0;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--text-secondary);
		border-radius: 999px;
		padding: 2px 10px;
		font-size: 11px;
		cursor: pointer;
	}
	.mini-btn:hover {
		color: var(--text-primary);
		border-color: var(--text-secondary);
	}
	.callout {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 14px 16px;
		margin-bottom: var(--space-lg);
		border-radius: var(--radius-md);
		border: 1px solid rgba(255, 255, 255, 0.08);
		background: rgba(0, 0, 0, 0.18);
	}
	.callout.info :global(.icon) {
		color: var(--accent);
		flex-shrink: 0;
		margin-top: 2px;
	}
	.callout-body {
		min-width: 0;
	}
	.callout-body p {
		margin: 0;
		font-size: 13px;
		line-height: 1.55;
		color: var(--text-secondary);
	}
	.howto-title {
		margin-bottom: 6px;
	}
	.howto {
		margin: 6px 0 0;
		padding-left: 20px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.howto li {
		font-size: 13px;
		line-height: 1.55;
		color: var(--text-secondary);
	}
	.snippet {
		margin: 8px 0 4px;
		padding: 10px 12px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-size: 11.5px;
		line-height: 1.5;
		overflow-x: auto;
		color: var(--text-primary);
		white-space: pre;
	}
	.snippet-note {
		font-size: 12px;
		color: var(--text-muted);
	}
	.note {
		margin-top: 10px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.stack {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.empty {
		color: var(--text-muted);
		font-size: 14px;
	}
	.skill-card {
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: var(--radius-md);
		background: rgba(0, 0, 0, 0.18);
		padding: 12px 14px;
	}
	.card-head {
		display: flex;
		align-items: flex-start;
		gap: 8px;
	}
	.expand {
		border: none;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		padding: 2px;
		margin-top: 2px;
		flex-shrink: 0;
	}
	.expand:hover {
		color: var(--text-primary);
	}
	.card-text {
		min-width: 0;
	}
	.name-row {
		display: flex;
		align-items: baseline;
		gap: 8px;
		flex-wrap: wrap;
	}
	.name {
		font-weight: 600;
		font-size: 14px;
		color: #7dd3fc;
	}
	.version {
		font-size: 11px;
		color: var(--text-muted);
	}
	.desc {
		margin: 4px 0 0;
		font-size: 13px;
		line-height: 1.5;
		color: var(--text-secondary);
	}
	.tags {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 8px;
	}
	.tag {
		font-size: 10.5px;
		padding: 2px 8px;
		border-radius: 999px;
		border: 1px solid var(--border);
		color: var(--text-muted);
	}
	.files {
		margin-top: 10px;
		padding: 10px 12px;
		border-top: 1px solid var(--border);
	}
	.files-label {
		margin: 0 0 6px;
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}
	.file-row {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 6px 8px;
		border: none;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-secondary);
		font-size: 12.5px;
		cursor: pointer;
		text-align: left;
	}
	.file-row:hover,
	.file-row.active {
		background: var(--bg-elevated);
		color: var(--text-primary);
	}
	.file-row.active {
		color: var(--accent);
	}
	.preview-panel {
		margin-top: var(--space-md);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		background: var(--bg-elevated);
		overflow: hidden;
	}
	.preview-head {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 12px;
		border-bottom: 1px solid var(--border);
	}
	.preview-path {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 12px;
		color: var(--text-secondary);
	}
	.truncated-chip {
		font-size: 10.5px;
		color: var(--warning);
		border: 1px solid rgba(245, 158, 11, 0.4);
		border-radius: 999px;
		padding: 1px 8px;
	}
	.preview-body {
		margin: 0;
		padding: 12px 14px;
		max-height: 420px;
		overflow: auto;
		font-size: 12px;
		line-height: 1.55;
		color: var(--text-primary);
		white-space: pre-wrap;
		word-break: break-word;
	}
	.muted {
		color: var(--text-muted);
		font-size: 13px;
	}
</style>
