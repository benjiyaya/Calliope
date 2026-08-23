<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { toStore } from 'svelte/store';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import AgentChat from '$lib/components/agent/AgentChat.svelte';
	import AgentComposer from '$lib/components/agent/AgentComposer.svelte';
	import AgentSessionSidebar from '$lib/components/agent/AgentSessionSidebar.svelte';
	import AgentPlanPanel from '$lib/components/agent/AgentPlanPanel.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import {
		agentApi,
		jobsApi,
		playgroundApi,
		workflows,
		type AgentMessage,
		type AgentPlan,
		type AgentSession,
		type AgentTask,
		type Job,
	} from '$lib/api';
	import type { AgentComposerPayload, WorkflowOption } from '$lib/agentComposer';
	import { AGENT_TASK_PROMPTS, AGENT_TASK_TITLES, isAgentTaskKind } from '$lib/agentTasks';
	import { connectEvents } from '$lib/events';
	import { toast } from '$lib/toast';

	const client = useQueryClient();

	let activeId = $state<number | null>(null);
	let streaming = $state('');
	let streamingReasoning = $state('');
	let liveTools = $state<
		{ tool: string; args?: Record<string, unknown> | null; result?: unknown; phase: 'running' | 'done' | 'error' }[]
	>([]);
	let showLinkPicker = $state(false);
	let livePlan = $state<AgentPlan | null>(null);
	// Pre-filled composer text (deep-link handoff + welcome suggestions). The
	// nonce forces re-application even when the same text is requested twice.
	let composerDraft = $state('');
	let composerNonce = $state(0);
	let deepLinkHandled = $state(false);

	const SUGGESTIONS: { label: string; prompt: string }[] = [
		{
			label: 'Create a new film',
			prompt: 'Create a new film project and guide me through drafting the story.',
		},
		{
			label: 'Draft a storyline',
			prompt: 'Draft a storyline — beats, characters, environments, and misc. items.',
		},
		{
			label: 'Write a script',
			prompt: 'Write the full script — turn the storyline into ordered scenes.',
		},
	];

	const SESSION_KEY = 'calliope.agents.activeSession';

	function readStoredSession(): number | null {
		const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(SESSION_KEY) : null;
		const n = raw == null ? Number.NaN : Number(raw);
		return Number.isFinite(n) ? n : null;
	}

	const sessionsQuery = createQuery({
		queryKey: ['agent-sessions'],
		queryFn: () => agentApi.listSessions(),
		refetchInterval: 5000, // survives SSE drop: running state + new sessions keep flowing
	});

	const sessions = $derived($sessionsQuery.data ?? []);
	const activeSession = $derived(
		activeId != null ? (sessions.find((s) => s.id === activeId) ?? null) : null,
	);
	const running = $derived(Boolean(activeSession?.running || activeSession?.status === 'running'));

	const sessionQuery = createQuery(
		toStore(() => ({
			queryKey: ['agent-session', activeId],
			queryFn: () => agentApi.getSession(activeId!),
			enabled: activeId != null,
			// While the agent runs, poll the persisted trail so navigating away
			// and back still shows every intermediate message.
			refetchInterval: () => (running ? 1500 : false),
		})),
	);
	const messages = $derived($sessionQuery.data?.messages ?? []);

	const sessionPlan = $derived($sessionQuery.data?.plan ?? null);
	const plan = $derived(livePlan ?? sessionPlan);

	const projectId = $derived(activeSession?.project?.id ?? null);

	// Live jobs for the linked project — resolves enqueued artifacts to their
	// current status + output paths, refreshing while anything is in flight.
	const jobsQuery = createQuery(
		toStore(() => ({
			queryKey: ['agent-project-jobs', projectId ?? 'sandbox'],
			queryFn: () => (projectId != null ? jobsApi.list(projectId) : playgroundApi.jobs()),
			enabled: activeId != null,
			refetchInterval: (q: { state: { data?: Job[] } }) => {
				const list = q.state.data ?? [];
				return list.some((j) => j.status === 'pending' || j.status === 'running')
					? 2500
					: false;
			},
		})),
	);
	const jobs = $derived($jobsQuery.data ?? []);

	const linkableQuery = createQuery(
		toStore(() => ({
			queryKey: ['agent-linkable-projects'],
			queryFn: agentApi.listLinkableProjects,
			enabled: showLinkPicker,
		})),
	);

	const workflowsQuery = createQuery({
		queryKey: ['workflows'],
		queryFn: workflows.list,
	});
	const enabledWorkflows = $derived<WorkflowOption[]>(
		($workflowsQuery.data ?? [])
			.filter((w) => w.is_enabled)
			.map((w) => ({
				id: w.id,
				name: w.name,
				kind: w.kind,
				description: w.description,
				is_enabled: w.is_enabled,
			})),
	);

	// Restore last-open session (or pick the newest) — survives page switches.
	// Skipped while a deep-link handoff is in flight so it doesn't clobber the
	// freshly created, project-linked session.
	$effect(() => {
		if (activeId != null || sessions.length === 0 || deepLinkHandled) return;
		const stored = readStoredSession();
		activeId = sessions.some((s) => s.id === stored) ? stored! : sessions[0].id;
	});
	$effect(() => {
		if (activeId != null) localStorage.setItem(SESSION_KEY, String(activeId));
	});

	// Deep-link contract from the Project stages: /agents?project=<id>&task=<story|script>
	$effect(() => {
		if (deepLinkHandled) return;
		const projectParam = page.url.searchParams.get('project');
		const taskParam = page.url.searchParams.get('task');
		if (projectParam == null && taskParam == null) return;
		deepLinkHandled = true;
		void openDeepLink(projectParam, taskParam);
	});

	function setComposerDraft(text: string) {
		composerDraft = text;
		composerNonce++;
	}

	function resetComposer() {
		composerDraft = '';
		composerNonce = 0;
	}

	function seedEmptySession(s: AgentSession) {
		client.setQueryData(['agent-session', s.id], { ...s, messages: [], plan: null });
	}

	async function newSandbox() {
		resetComposer();
		try {
			const s = await agentApi.createSession({});
			seedEmptySession(s);
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			activeId = s.id;
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not create session');
		}
	}

	async function newProjectChat(projectId: number) {
		resetComposer();
		try {
			const s = await agentApi.createSession({ project_id: projectId });
			seedEmptySession(s);
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			activeId = s.id;
			toast.success('Linked chat created');
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not create session');
		}
	}

	async function openDeepLink(projectParam: string | null, taskParam: string | null) {
		const projectId =
			projectParam != null && projectParam !== '' ? Number(projectParam) : null;
		const task = isAgentTaskKind(taskParam) ? taskParam : null;
		const prompt = task ? AGENT_TASK_PROMPTS[task] : '';
		const title = task ? AGENT_TASK_TITLES[task] : undefined;
		try {
			const s = await agentApi.createSession({
				...(projectId != null && Number.isFinite(projectId) ? { project_id: projectId } : {}),
				...(title ? { title } : {}),
			});
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			activeId = s.id;
			if (prompt) setComposerDraft(prompt);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not open linked chat');
		} finally {
			goto('/agents', { replaceState: true, keepFocus: true, noScroll: true });
		}
	}

	async function removeSession(id: number) {
		if (!confirm('Delete this chat and its message history?')) return;
		try {
			await agentApi.deleteSession(id);
			if (activeId === id) activeId = null;
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			toast.success('Session deleted');
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not delete session');
		}
	}

	async function linkSession(projectId: number) {
		if (activeId == null) return;
		try {
			await agentApi.patchSession(activeId, { project_id: projectId });
			showLinkPicker = false;
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			toast.success('Session linked to project');
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Link failed');
		}
	}

	async function unlinkSession() {
		if (activeId == null) return;
		if (!confirm('Unlink this chat from its project? The chat stays, but the agent loses project access.')) return;
		try {
			await agentApi.patchSession(activeId, { unlink: true });
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			toast.info('Session unlinked — back to sandbox');
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Unlink failed');
		}
	}

	const sendMutation = createMutation({
		mutationFn: (payload: AgentComposerPayload) => agentApi.postMessage(activeId!, payload),
		onMutate: () => {
			streaming = '';
			streamingReasoning = '';
			liveTools = [];
			livePlan = null;
			// Drop the cached plan too so a prior turn's checklist doesn't
			// flash while the planner computes the new one.
			if (activeId != null) {
				client.setQueryData<AgentSession & { messages: AgentMessage[]; plan?: AgentPlan | null }>(
					['agent-session', activeId],
					(old) => (old ? { ...old, plan: null } : old),
				);
			}
		},
		onSuccess: async () => {
			await client.invalidateQueries({ queryKey: ['agent-session', activeId] });
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : 'Failed to send');
		},
	});

	async function cancelRun() {
		if (activeId == null) return;
		try {
			await agentApi.cancel(activeId);
			toast.info('Run cancelled');
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Cancel failed');
		}
	}

	/** Send from the landing (no session yet): lazily create a sandbox, then post. */
	async function handleSend(payload: AgentComposerPayload) {
		if (activeId != null) {
			$sendMutation.mutate(payload);
			return;
		}
		try {
			const s = await agentApi.createSession({});
			await client.invalidateQueries({ queryKey: ['agent-sessions'] });
			activeId = s.id;
			$sendMutation.mutate(payload);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : 'Could not create session');
		}
	}

	function upsertSession(s: AgentSession) {
		client.setQueryData<AgentSession[]>(['agent-sessions'], (old) => {
			const list = Array.isArray(old) ? old : [];
			return list.some((x) => x.id === s.id)
				? list.map((x) => (x.id === s.id ? { ...x, ...s } : x))
				: [...list, s];
		});
	}

	function appendMessage(sid: number, msg: AgentMessage) {
		if (sid !== activeId) return;
		client.setQueryData<AgentSession & { messages: AgentMessage[] }>(
			['agent-session', sid],
			(old) => {
				if (!old || !Array.isArray(old.messages)) return old; // wait for refetch
				if (old.messages.some((m) => m.id === msg.id)) return old;
				return { ...old, messages: [...old.messages, msg] };
			},
		);
	}

	onMount(() => {
		const stop = connectEvents((ev) => {
			if (ev.type === 'agent.session.updated') {
				const s = ev.data?.session as AgentSession | undefined;
				if (s) upsertSession(s);
			} else if (ev.type === 'agent.message') {
				const msg = ev.data?.message as AgentMessage | undefined;
				if (msg) {
					appendMessage(msg.session_id, msg);
					// Persisted trail supersedes the live views (no duplicates).
					if (msg.session_id === activeId) {
						if (msg.role === 'tool') liveTools = [];
						if (msg.role === 'assistant' && !msg.agent_name) {
							streaming = '';
							streamingReasoning = '';
						}
					}
				}
				client.invalidateQueries({ queryKey: ['agent-sessions'] });
			} else if (ev.type === 'agent.token') {
				if (ev.data?.session_id === activeId && !ev.data?.agent_name) {
					streaming += String(ev.data?.content ?? '');
				}
			} else if (ev.type === 'agent.thinking') {
				if (ev.data?.session_id === activeId && !ev.data?.agent_name) {
					streamingReasoning += String(ev.data?.content ?? '');
				}
			} else if (ev.type === 'agent.tool') {
				if (ev.data?.session_id !== activeId) return;
				const name = String(ev.data?.tool ?? '');
				const phase = String(ev.data?.phase ?? '');
				if (phase === 'start') {
					liveTools.push({
						tool: name,
						args: (ev.data?.args as Record<string, unknown>) ?? null,
						phase: 'running',
					});
				} else if (phase === 'finish') {
					const idx = [...liveTools]
						.reverse()
						.findIndex((t) => t.tool === name && t.phase === 'running');
					if (idx >= 0) {
						const real = liveTools.length - 1 - idx;
						liveTools[real] = {
							...liveTools[real],
							result: ev.data?.result,
							phase:
								(ev.data?.result as { ok?: boolean } | undefined)?.ok === false
									? 'error'
									: 'done',
						};
					}
				}
			} else if (ev.type === 'agent.plan') {
				if (ev.data?.session_id !== activeId) return;
				const tasks = (ev.data?.tasks as AgentTask[] | undefined) ?? [];
				livePlan = {
					tasks: tasks.map((t) => ({ ...t, status: t.status ?? 'pending' })),
					note: (ev.data?.note as string | null) ?? null,
				};
			} else if (ev.type === 'agent.task') {
				if (ev.data?.session_id !== activeId) return;
				const index = ev.data?.index;
				const status = ev.data?.status as AgentTask['status'] | undefined;
				if (typeof index === 'number' && status) {
					livePlan = livePlan
						? {
								...livePlan,
								tasks: livePlan.tasks.map((t, i) =>
									i === index ? { ...t, status } : t,
								),
							}
						: livePlan;
				}
			} else if (
				ev.type === 'story.ready' ||
				ev.type === 'job.created' ||
				ev.type === 'job.started' ||
				ev.type === 'job.completed' ||
				ev.type === 'job.failed'
			) {
				client.invalidateQueries({ queryKey: ['agent-sessions'] });
				client.invalidateQueries({ queryKey: ['agent-project-jobs'] });
				client.invalidateQueries({ queryKey: ['playground-jobs'] });
			}
		});
		return stop;
	});
</script>

<div class="shell">
	<AppHeader active="agents" />

	<div class="workspace">
		<AgentSessionSidebar
			{sessions}
			activeId={activeId}
			onSelect={(id) => {
				activeId = id;
				streaming = '';
				streamingReasoning = '';
				liveTools = [];
				livePlan = null;
				showLinkPicker = false;
				resetComposer();
			}}
			onNewSandbox={newSandbox}
			onNewProjectChat={newProjectChat}
			onDelete={removeSession}
		/>

		<main class="panel">
			<header class="ws-head">
				<div class="ws-title-block">
					{#if activeSession}
						<h2>{activeSession.title}</h2>
						{#if activeSession.project}
							<a class="ws-project" href="/project/{activeSession.project.id}">
								<Icon name="folder" size={13} />
								{activeSession.project.title}
								<Icon name="external-link" size={12} />
							</a>
							{#if !running}
								<button
									type="button"
									class="link-btn"
									onclick={unlinkSession}
									title="Unlink from project (back to sandbox)"
								>
									<Icon name="link-off" size={12} /> Unlink
								</button>
							{/if}
						{:else}
							<span class="ws-sandbox">
								<Icon name="sparkle" size={13} />
								Sandbox — no project yet
							</span>
							{#if !running}
								<button
									type="button"
									class="link-btn"
									onclick={() => (showLinkPicker = !showLinkPicker)}
									title="Link this chat to an existing project"
								>
									<Icon name="link" size={12} /> Link a project
								</button>
							{/if}
						{/if}
					{:else}
						<h2>New chat</h2>
						<span class="ws-sandbox">
							<Icon name="sparkle" size={13} /> Calliope — your production agent
						</span>
					{/if}
				</div>
				{#if running}
					<div class="ws-status">
						<span class="run-dot"></span> Agent working…
					</div>
				{/if}
			</header>

			{#if activeSession && showLinkPicker && !activeSession.project && !running}
				<div class="link-picker">
					<div class="lp-head">Link this chat to a project</div>
					{#if $linkableQuery.isLoading}
						<div class="lp-empty">Loading projects…</div>
					{:else if ($linkableQuery.data ?? []).length === 0}
						<div class="lp-empty">No projects yet — ask the agent to create one instead.</div>
					{:else}
						<div class="lp-list">
							{#each $linkableQuery.data ?? [] as p (p.id)}
								<button type="button" class="lp-item" onclick={() => linkSession(p.id)}>
									<Icon name="folder" size={13} />
									<span class="lp-title">{p.title}</span>
									<span class="lp-status">{p.status}</span>
								</button>
							{/each}
						</div>
					{/if}
				</div>
			{/if}

			{#if $sessionQuery.isError}
				<div class="session-error" role="alert">
					Could not load this chat
					{#if $sessionQuery.error instanceof Error}
						— {$sessionQuery.error.message}
					{/if}
				</div>
			{/if}
			<AgentChat
				{messages}
				{liveTools}
				{streaming}
				{streamingReasoning}
				{jobs}
				{running}
				loading={activeId != null && $sessionQuery.isLoading}
				suggestions={SUGGESTIONS}
				onSuggestion={setComposerDraft}
			/>

			{#if running && plan && plan.tasks.length > 0}
				<AgentPlanPanel {plan} />
			{/if}

			{#key activeId}
				<AgentComposer
					{running}
					draft={composerDraft}
					draftNonce={composerNonce}
					workflows={enabledWorkflows}
					onSend={handleSend}
					onCancel={cancelRun}
				/>
			{/key}
		</main>
	</div>
</div>

<style>
	.shell {
		display: flex;
		flex-direction: column;
		height: 100vh;
		overflow: hidden;
	}
	.workspace {
		flex: 1;
		display: flex;
		min-height: 0;
	}
	.panel {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		padding: 16px 24px 18px;
		gap: 10px;
		overflow-x: hidden;
	}
	.ws-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-md);
		flex-shrink: 0;
		padding-bottom: 8px;
		border-bottom: 1px solid var(--border);
	}
	.ws-title-block {
		display: flex;
		align-items: center;
		gap: 12px;
		min-width: 0;
		flex-wrap: wrap;
	}
	.ws-head h2 {
		margin: 0;
		font-size: 16px;
		font-weight: 700;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 40vw;
	}
	.ws-project {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 12.5px;
		color: var(--accent);
		text-decoration: none;
		border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
		background: color-mix(in srgb, var(--accent) 10%, transparent);
		padding: 3px 10px;
		border-radius: 999px;
	}
	.ws-project:hover {
		background: color-mix(in srgb, var(--accent) 18%, transparent);
	}
	.ws-sandbox {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--text-muted);
		border: 1px dashed var(--border);
		padding: 3px 10px;
		border-radius: 999px;
	}
	.link-btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		font-size: 11.5px;
		color: var(--text-secondary);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 3px 10px;
		cursor: pointer;
	}
	.link-btn:hover {
		color: var(--text-primary);
		border-color: var(--accent);
		color: var(--accent);
	}
	.link-btn:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.link-picker {
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		background: var(--bg-surface);
		padding: 10px;
		flex-shrink: 0;
		max-height: 200px;
		overflow-y: auto;
	}
	.lp-head {
		font-size: 11px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
		margin-bottom: 8px;
	}
	.lp-empty {
		font-size: 12.5px;
		color: var(--text-muted);
		padding: 4px 0;
	}
	.lp-list {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.lp-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 10px;
		border-radius: var(--radius-sm);
		border: 1px solid transparent;
		background: transparent;
		color: var(--text-primary);
		font-size: 13px;
		cursor: pointer;
		text-align: left;
	}
	.lp-item:hover {
		background: var(--bg-elevated);
		border-color: var(--accent);
	}
	.lp-title {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.lp-status {
		font-size: 11px;
		color: var(--text-muted);
		flex-shrink: 0;
	}
	.ws-status {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 12.5px;
		color: var(--warning);
		flex-shrink: 0;
	}
	.run-dot {
		width: 8px;
		height: 8px;
		border-radius: 999px;
		background: var(--warning);
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
	.session-error {
		flex-shrink: 0;
		font-size: 12.5px;
		color: var(--error);
		padding: 8px 10px;
		border: 1px solid color-mix(in srgb, var(--error) 35%, transparent);
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--error) 8%, transparent);
	}
</style>
