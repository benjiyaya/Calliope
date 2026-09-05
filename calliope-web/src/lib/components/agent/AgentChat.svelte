<script lang="ts">
	import type { AgentMessage, Job } from '$lib/api';
	import { assetUrl } from '$lib/api';
	import type { WorkflowMention } from '$lib/agentComposer';
	import AgentToolCard from './AgentToolCard.svelte';
	import AgentArtifactCard, { type ArtifactJob } from './AgentArtifactCard.svelte';
	import ReasoningPanel from './ReasoningPanel.svelte';
	import { agentColor, agentDisplayName } from './agentPalette';
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		messages: AgentMessage[];
		/** In-flight tool calls for the current turn (from agent.tool SSE). */
		liveTools: { tool: string; args?: Record<string, unknown> | null; result?: unknown; phase: 'running' | 'done' | 'error' }[];
		/** Streaming assistant text for the current turn (from agent.token SSE). */
		streaming: string;
		/** Streaming reasoning text for the current turn (from agent.thinking SSE). */
		streamingReasoning: string;
		/** Live jobs for the linked project (to resolve enqueued artifacts). */
		jobs?: Job[];
		running: boolean;
		/** Welcome suggestions shown only on an empty (new-chat) timeline. */
		suggestions?: { label: string; prompt: string }[];
		/** True while the selected session's history is still fetching. */
		loading?: boolean;
		/** Called with a suggestion's prompt to pre-fill the composer. */
		onSuggestion?: (prompt: string) => void;
		/** Called with an option's label + scope when a question-card option is clicked. */
		onAnswer?: (option: string, scope: string, questionSeq: number) => void;
	}

	let {
		messages,
		liveTools,
		streaming,
		streamingReasoning,
		jobs = [],
		running,
		suggestions = [],
		loading = false,
		onSuggestion,
		onAnswer,
	}: Props = $props();

	let listEl = $state<HTMLDivElement | null>(null);
	let pinned = $state(true);

	// Derived tool rows carry no status — infer failure from the result shape,
	// same as the live SSE path does.
	function toolPhase(m: AgentMessage): 'done' | 'error' {
		if (m.status === 'error') return 'error';
		const r = m.tool_result as { ok?: boolean } | null | undefined;
		return r && typeof r === 'object' && r.ok === false ? 'error' : 'done';
	}

	// Tool results that enqueue render jobs expose their job ids — these render
	// as inline artifact cards (Playground-style) that track live job status.
	const ENQUEUE_TOOLS = new Set(['enqueue_asset_jobs', 'enqueue_video_jobs', 'run_workflow']);
	const jobsById = $derived(new Map((jobs ?? []).map((j) => [j.id, j])));

	/** Later tool results (wait_for_jobs) often have the finished status + paths. */
	const trailJobsById = $derived.by(() => {
		const map = new Map<number, ArtifactJob>();
		for (const row of messages) {
			if (row.role !== 'tool' || !row.tool_result || typeof row.tool_result !== 'object') continue;
			const listed = (row.tool_result as { jobs?: unknown }).jobs;
			if (!Array.isArray(listed)) continue;
			for (const raw of listed) {
				if (!raw || typeof raw !== 'object') continue;
				const j = raw as ArtifactJob;
				if (typeof j.id !== 'number') continue;
				const prev = map.get(j.id);
				map.set(j.id, prev ? { ...prev, ...j } : j);
			}
		}
		return map;
	});

	function artifactJobs(m: AgentMessage): ArtifactJob[] {
		const result = m.tool_result;
		if (!result || typeof result !== 'object') return [];
		const stubs = (result as { jobs?: unknown }).jobs;
		if (!Array.isArray(stubs)) return [];
		return stubs
			.filter((j) => j && typeof j === 'object' && typeof (j as { id?: unknown }).id === 'number')
			.map((j) => {
				const stub = j as ArtifactJob;
				return jobsById.get(stub.id) ?? trailJobsById.get(stub.id) ?? {
					id: stub.id,
					kind: stub.kind ?? 'image',
					status: stub.status ?? 'pending',
					output_paths: stub.output_paths,
					error: stub.error,
				};
			});
	}

	// Keep pinned to bottom on new content unless the user scrolled up.
	$effect(() => {
		void messages.length;
		void streaming;
		void streamingReasoning;
		void liveTools.length;
		void jobs.length;
		if (listEl && pinned) {
			requestAnimationFrame(() => {
				listEl?.scrollTo({ top: listEl.scrollHeight });
			});
		}
	});

	function onScroll() {
		const el = listEl;
		if (!el) return;
		pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
	}

	function escapeRegExp(s: string): string {
		return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	}

	// ── ask_user question cards ──────────────────────────────────────────
	// The card is DERIVED from the message stream: the persisted ask_user
	// tool row carries {question_seq, options, scope}; it stays actionable
	// until a later user message (the card answer or any prose reply).

	interface OpenQuestion {
		questionSeq: number;
		options: string[];
		scope: string;
	}

	const lastQuestion = $derived.by<OpenQuestion | null>(() => {
		for (let i = messages.length - 1; i >= 0; i--) {
			const m = messages[i];
			if (m.role === 'user') return null;
			if (m.role === 'tool' && m.tool_name === 'ask_user' && m.tool_result) {
				const r = m.tool_result as {
					ok?: boolean;
					question_seq?: number;
					options?: unknown;
					scope?: string;
				};
				if (r.ok !== false && Array.isArray(r.options)) {
					return {
						questionSeq: r.question_seq ?? m.id,
						options: r.options as string[],
						scope: r.scope ?? 'info',
					};
				}
				return null;
			}
		}
		return null;
	});

	function answerLabel(scope: string): string {
		if (scope === 'render') return 'Your click records approval to generate.';
		if (scope === 'destructive_replace') return 'Your click records approval to replace content.';
		return '';
	}

	function bubbleParts(
		content: string,
		mentions: WorkflowMention[],
	): Array<{ type: 'text' | 'chip'; text: string }> {
		if (!mentions.length) return [{ type: 'text', text: content }];
		const names = [...new Set(mentions.map((m) => m.name).filter(Boolean))].sort(
			(a, b) => b.length - a.length,
		);
		if (names.length === 0) return [{ type: 'text', text: content }];
		const re = new RegExp(`(@(?:${names.map(escapeRegExp).join('|')}))`, 'g');
		const parts: Array<{ type: 'text' | 'chip'; text: string }> = [];
		let last = 0;
		for (const match of content.matchAll(re)) {
			const idx = match.index ?? 0;
			if (idx > last) parts.push({ type: 'text', text: content.slice(last, idx) });
			parts.push({ type: 'chip', text: match[1] ?? '' });
			last = idx + (match[0]?.length ?? 0);
		}
		if (last < content.length) parts.push({ type: 'text', text: content.slice(last) });
		return parts.length ? parts : [{ type: 'text', text: content }];
	}
</script>

<div class="chat" bind:this={listEl} onscroll={onScroll}>
	{#if loading}
		<div class="empty">
			<p class="empty-hint">Loading chat…</p>
		</div>
	{:else if messages.length === 0 && !running}
		<div class="empty">
			<div class="empty-mark"><Icon name="sparkle" size={22} /></div>
			<p class="empty-eyebrow">Calliope</p>
			<p class="empty-title">What are we making?</p>
			<p class="empty-hint">
				Tell your production agent the idea and it takes the story to script to video — or ask it to
				work on one of your projects.
			</p>
			{#if suggestions.length > 0}
				<div class="suggestions">
					{#each suggestions as s (s.label)}
						<button type="button" class="suggestion" onclick={() => onSuggestion?.(s.prompt)}>
							{s.label}
						</button>
					{/each}
				</div>
			{/if}
		</div>
	{/if}

	{#each messages as m (m.id)}
		{#if m.role === 'user'}
			<div class="row user">
				<div class="bubble user-bubble">
					{#if (m.attachments ?? []).length > 0}
						<div class="user-thumbs">
							{#each (m.attachments ?? []) as a (a.path)}
								{@const href = assetUrl(a.path)}
								{#if a.kind === 'image' && href}
									<img class="user-thumb" src={href} alt={a.name} />
								{:else}
									<span class="user-thumb file" title={a.name}>
										<Icon name={a.kind === 'audio' ? 'music' : 'video'} size={14} />
									</span>
								{/if}
							{/each}
						</div>
					{/if}
					<span class="user-text">
						{#each bubbleParts(m.content, (m.mentions ?? []) as WorkflowMention[]) as part, i (`${i}-${part.type}`)}
							{#if part.type === 'chip'}
								<span class="wf-chip">{part.text}</span>
							{:else}{part.text}{/if}
						{/each}
					</span>
				</div>
			</div>
		{:else if m.role === 'tool'}
			<div class="row tool-row">
				<AgentToolCard
					name={m.tool_name ?? 'tool'}
					args={m.tool_args}
					result={m.tool_result}
					phase={toolPhase(m)}
				/>
				{#if m.tool_name === 'ask_user' && lastQuestion && m.id === messages.findLast((x) => x.role === 'tool' && x.tool_name === 'ask_user')?.id}
					<div class="question-card">
						{#each lastQuestion.options as opt (opt)}
							<button
								type="button"
								class="question-option"
								class:affirmative={opt.toLowerCase().startsWith('yes')}
								disabled={running}
								onclick={() => onAnswer?.(opt, lastQuestion.scope, lastQuestion.questionSeq)}
							>
								{opt}
							</button>
						{/each}
						{#if answerLabel(lastQuestion.scope)}
							<p class="question-scope">{answerLabel(lastQuestion.scope)}</p>
						{/if}
					</div>
				{/if}
				{#if m.tool_name && ENQUEUE_TOOLS.has(m.tool_name)}
					{@const arts = artifactJobs(m)}
					{#if arts.length > 0}
						<div class="artifacts">
							{#each arts as job (job.id)}
								<AgentArtifactCard {job} />
							{/each}
						</div>
					{/if}
				{/if}
			</div>
		{:else if m.role === 'assistant'}
			{@const color = agentColor(m.agent_name)}
			<div class="row assistant" style:--agent={color}>
				<div class="agent-tag">
					<span class="agent-dot" style:background={color}></span>
					{agentDisplayName(m.agent_name)}
				</div>
				{#if m.reasoning}
					<div class="reasoning-wrap">
						<ReasoningPanel reasoning={m.reasoning} />
					</div>
				{/if}
				<div
					class="bubble assistant-bubble"
					class:err={m.status === 'error' || m.status === 'cancelled'}
				>
					{m.content}
				</div>
			</div>
		{/if}
	{/each}

	{#if running}
		<div class="row assistant" style:--agent={agentColor(null)}>
			<div class="agent-tag">
				<span class="agent-dot" style:background={agentColor(null)}></span>
				{agentDisplayName(null)}
			</div>
			<div class="bubble assistant-bubble live">
				{#if liveTools.length > 0}
					<div class="tools">
						{#each liveTools as t, i (i)}
							<AgentToolCard name={t.tool} args={t.args} result={t.result} phase={t.phase} />
						{/each}
					</div>
				{/if}
				{#if streaming}
					<div class="stream-text">{streaming}<span class="caret"></span></div>
				{:else}
					<div class="thinking"><span class="pulse"></span> working…</div>
				{/if}
				{#if streamingReasoning}
					<ReasoningPanel streaming={streamingReasoning} />
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.chat {
		flex: 1;
		min-height: 0;
		min-width: 0;
		width: 100%;
		overflow-y: auto;
		overflow-x: hidden;
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 4px 4px 12px;
	}
	.empty {
		margin: auto;
		max-width: 540px;
		text-align: center;
		color: var(--text-muted);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 8px;
		padding: 40px 16px;
	}
	.empty-mark {
		width: 48px;
		height: 48px;
		border-radius: var(--radius-md);
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--accent);
		background:
			radial-gradient(circle at 30% 25%, rgba(139, 92, 246, 0.35) 0%, transparent 60%),
			var(--bg-elevated);
		border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
		box-shadow: 0 0 32px var(--accent-glow);
		margin-bottom: 6px;
	}
	.empty-eyebrow {
		margin: 0;
		font-family: var(--font-display);
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.28em;
		text-transform: uppercase;
		color: var(--accent);
	}
	.empty-title {
		margin: 0;
		font-family: var(--font-display);
		font-size: 26px;
		font-weight: 700;
		letter-spacing: -0.02em;
		color: var(--text-primary);
	}
	.empty-hint {
		margin: 0;
		font-size: 13.5px;
		line-height: 1.6;
		max-width: 420px;
	}
	.suggestions {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 8px;
		margin-top: 16px;
	}
	.suggestion {
		border: 1px solid var(--border);
		background: var(--bg-surface);
		color: var(--text-secondary);
		border-radius: 999px;
		padding: 8px 16px;
		font-size: 13px;
		font-weight: 500;
		font-family: inherit;
		cursor: pointer;
		transition: all 0.15s;
	}
	.suggestion:hover {
		color: var(--text-primary);
		border-color: var(--accent);
		background: color-mix(in srgb, var(--accent) 12%, var(--bg-surface));
	}
	.suggestion:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.row {
		display: flex;
		flex-direction: column;
	}
	.row.user {
		align-items: flex-end;
	}
	.row.assistant {
		align-items: flex-start;
	}
	.row.tool-row {
		align-items: flex-start;
		padding-left: 6px;
		width: 100%;
	}
	.agent-tag {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--agent, var(--accent));
		margin-bottom: 4px;
		padding-left: 2px;
	}
	.agent-dot {
		width: 8px;
		height: 8px;
		border-radius: 999px;
		flex-shrink: 0;
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--agent, var(--accent)) 25%, transparent);
	}
	.reasoning-wrap {
		margin-bottom: 4px;
		max-width: 86%;
	}
	.bubble {
		max-width: 86%;
		border-radius: var(--radius-md);
		padding: 10px 14px;
		font-size: 13.5px;
		line-height: 1.6;
		white-space: pre-wrap;
		word-break: break-word;
	}
	.user-bubble {
		background: color-mix(in srgb, var(--accent) 16%, var(--bg-surface));
		border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
		color: var(--text-primary);
	}
	.user-thumbs {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-bottom: 8px;
	}
	.user-thumb {
		display: block;
		width: 48px;
		height: 48px;
		object-fit: cover;
		border-radius: var(--radius-sm);
		border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
		background: var(--bg-elevated);
	}
	.user-thumb.file {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		color: var(--text-muted);
	}
	.user-text {
		white-space: pre-wrap;
	}
	.wf-chip {
		display: inline;
		padding: 1px 7px;
		margin: 0 1px;
		border-radius: 999px;
		background: color-mix(in srgb, var(--accent) 22%, var(--bg-elevated));
		border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
		color: var(--accent);
		font-weight: 600;
		font-size: 13px;
		white-space: nowrap;
	}
	.assistant-bubble {
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-left: 3px solid var(--agent, var(--accent));
		color: var(--text-primary);
	}
	.question-card {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
		padding: 4px 2px 2px;
	}
	.question-option {
		border: 1px solid color-mix(in srgb, var(--agent, var(--accent)) 45%, transparent);
		background: var(--bg-elevated);
		color: var(--text-primary);
		border-radius: 999px;
		padding: 6px 14px;
		font-size: 13px;
		font-weight: 600;
		cursor: pointer;
		transition:
			background 120ms ease,
			border-color 120ms ease,
			transform 120ms ease;
	}
	.question-option:hover:enabled {
		background: color-mix(in srgb, var(--agent, var(--accent)) 18%, var(--bg-elevated));
		border-color: var(--agent, var(--accent));
		transform: translateY(-1px);
	}
	.question-option:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.question-option.affirmative {
		border-color: var(--accent);
		color: var(--accent);
	}
	.question-scope {
		width: 100%;
		margin: 0;
		font-size: 11px;
		color: var(--text-muted);
	}
	.assistant-bubble.err {
		border-color: rgba(239, 68, 68, 0.4);
		border-left-color: rgba(239, 68, 68, 0.6);
		color: var(--error);
	}
	.bubble.live {
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-width: 92%;
		width: 100%;
	}
	.tools {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.artifacts {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		margin-top: 8px;
		padding-left: 4px;
	}
	.stream-text {
		white-space: pre-wrap;
	}
	.caret {
		display: inline-block;
		width: 7px;
		height: 14px;
		margin-left: 2px;
		background: var(--accent);
		animation: blink 1s step-end infinite;
		vertical-align: text-bottom;
	}
	@keyframes blink {
		50% {
			opacity: 0;
		}
	}
	.thinking {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--text-secondary);
		font-size: 13px;
	}
	.pulse {
		width: 8px;
		height: 8px;
		border-radius: 999px;
		background: var(--accent);
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
</style>
