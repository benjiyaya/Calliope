<script lang="ts">
	import { onMount } from 'svelte';
	import { toStore } from 'svelte/store';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import AgentChat from '$lib/components/agent/AgentChat.svelte';
	import AgentComposer from '$lib/components/agent/AgentComposer.svelte';
	import { agentApi, playgroundApi, workflows, type AgentMessage, type AgentPlan, type AgentSession, type AgentTask } from '$lib/api';
	import type { AgentComposerPayload, SkillOption, WorkflowOption } from '$lib/agentComposer';
	import { connectEvents } from '$lib/events';
	import { toast } from '$lib/toast';
	import { t } from '$lib/i18n.svelte';
	import { shotStore } from '$lib/shot/shotStore.svelte';
	import { shotApi, type Capture } from '$lib/shot/api';
	import ShotViewport from '$lib/shot/components/ShotViewport.svelte';
	import ScenePanel from '$lib/shot/components/ScenePanel.svelte';
	import ShotInspector from '$lib/shot/components/ShotInspector.svelte';
	import CaptureStrip from '$lib/shot/components/CaptureStrip.svelte';
	import ShotSessionRail from '$lib/shot/components/ShotSessionRail.svelte';
	import TimelineStrip from '$lib/shot/components/TimelineStrip.svelte';

	const client = useQueryClient();

	// ---- multi-session: one composition per session, rail-switchable -------
	// The rail lists sandbox sessions that own a composition ("Scene · *").
	// Selection persists in localStorage; deleting a session removes its
	// composition first (the FK alone would orphan it).

const ACTIVE_SESSION_KEY = 'calliope.shot.activeSession';
const RAIL_KEY = 'calliope.shot.railCollapsed';
const CHAT_KEY = 'calliope.shot.chatCollapsed';

let sessionId = $state<number | null>(null);
let captures = $state<Capture[]>([]);
let compositionReady = $state(false);
let switching = $state(false);
// SSR-safe: this component initializes during server render too, where
// localStorage does not exist (a bare access 500s a direct page load).
let railCollapsed = $state(false);
let chatCollapsed = $state(false);

$effect(() => {
	railCollapsed = localStorage.getItem(RAIL_KEY) === '1';
	chatCollapsed = localStorage.getItem(CHAT_KEY) === '1';
});

function toggleRail() {
	railCollapsed = !railCollapsed;
	localStorage.setItem(RAIL_KEY, railCollapsed ? '1' : '0');
}

function toggleChat() {
	chatCollapsed = !chatCollapsed;
	localStorage.setItem(CHAT_KEY, chatCollapsed ? '1' : '0');
}

	function isSceneSession(s: AgentSession): boolean {
		return s.project_id == null && s.origin === 'scene';
	}

	async function refreshCaptures() {
		if (shotStore.compositionId == null) return;
		captures = await shotApi.listCaptures(shotStore.compositionId);
	}

	async function activateSession(id: number, { persist = true } = {}) {
		if (switching) return;
		switching = true;
		try {
			if (shotStore.compositionId != null) await shotStore.flushSave();
			sessionId = id;
			compositionReady = false;
			if (persist) localStorage.setItem(ACTIVE_SESSION_KEY, String(id));
			const comp = await shotStore.loadComposition(id);
			if (comp && comp.title === 'Untitled composition') {
				const title = `Scene · ${id}`;
				await agentApi.patchSession(id, { title });
				await fetch(`/api/shots/${comp.id}`, {
					method: 'PATCH',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ title }),
				});
			}
			await refreshCaptures();
			compositionReady = true;
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('buildScene.openCompositionFailed'));
			compositionReady = true;
		} finally {
			switching = false;
		}
	}

	$effect(() => {
		if (compositionReady) return;
		void (async () => {
			try {
				// origin=scene: this page's own rail entries. The default list
				// (AI Canvas) excludes them, so the two surfaces never leak
				// into each other.
				const sessions = await agentApi.listSceneSessions();
				const stored = Number(localStorage.getItem(ACTIVE_SESSION_KEY) ?? '');
				const sceneSessions = sessions.filter(isSceneSession);
				const initial =
					sceneSessions.find((s) => s.id === stored) ??
					sceneSessions[0] ??
					null;
				if (initial) {
					await activateSession(initial.id, { persist: false });
				} else {
					const session = await agentApi.createSession({ origin: 'scene' });
					await activateSession(session.id);
				}
				compositionReady = true;
			} catch (err) {
				toast.error(err instanceof Error ? err.message : t('buildScene.openCompositionFailed'));
				compositionReady = true;
			}
		})();
	});

	async function handleNewScene() {
		if (switching) return;
		try {
			const session = await agentApi.createSession({ origin: 'scene' });
			await activateSession(session.id);
			await client.invalidateQueries({ queryKey: ['agent-sessions', 'scene'] });
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('buildScene.createSceneFailed'));
		}
	}

	async function handleDeleteScene(id: number) {
		if (switching) return;
		try {
			// Delete the composition FIRST — agent_sessions FK would otherwise
			// orphan it (ON DELETE SET NULL) and its captures would linger.
			const comps = await shotApi.listCompositions();
			const mine = comps.find((c) => c.agent_session_id === id);
			if (mine) await shotApi.deleteComposition(mine.id);
			await agentApi.deleteSession(id);
			if (Number(localStorage.getItem(ACTIVE_SESSION_KEY)) === id) {
				localStorage.removeItem(ACTIVE_SESSION_KEY);
			}
			const sessions = await agentApi.listSceneSessions();
			const next = sessions.find(isSceneSession);
			await client.invalidateQueries({ queryKey: ['agent-sessions', 'scene'] });
			if (next && next.id !== sessionId) {
				await activateSession(next.id);
			} else if (!next) {
				await handleNewScene();
			}
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('buildScene.deleteSceneFailed'));
		}
	}

	// ---- capture flow (UI button AND agent request_capture) ----------------

	let viewportRef: ReturnType<typeof ShotViewport> | null = $state(null);

	async function handleCaptureDataUrl(dataUrl: string) {
		if (shotStore.compositionId == null) return;
		const saved = await shotApi.uploadCapture(shotStore.compositionId, dataUrl, 'viewport capture');
		if (saved) {
			await refreshCaptures();
			toast.success(t('buildScene.captureSaved'));
		}
	}

	let exportStatus = $state<string | null>(null);

	async function captureFromUi() {
		// The Three.js viewport renders the still directly.
		if (!viewportRef) return;
		const dataUrl = (viewportRef as unknown as { captureNow: () => string }).captureNow();
		await handleCaptureDataUrl(dataUrl);
	}

	let exportingVideo = $state(false);

	async function handleExportVideo() {
		const comp = shotStore.compositionId;
		if (comp == null || exportingVideo) return;
		exportingVideo = true;
		exportStatus = t('buildScene.videoExported');
		try {
			if (!viewportRef) throw new Error(t('buildScene.legacyNeedsViewport'));
			const { dataUrl } = await (viewportRef as unknown as {
				exportVideoClip: () => Promise<{ dataUrl: string; ext: string }>;
			}).exportVideoClip();
			const saved = await shotApi.uploadCapture(comp, dataUrl, 'viewport video export');
			if (saved) {
				await refreshCaptures();
				toast.success(t('buildScene.videoExported'));
			} else {
				toast.error(t('buildScene.uploadSizeFail'));
			}
		} catch (err) {
			exportStatus = null;
			toast.error(err instanceof Error ? err.message : t('buildScene.videoExportFailed'));
		} finally {
			exportingVideo = false;
		}
	}

	// ---- chat (mirrors canvas page's SSE wiring, minus session rail) -------

	let streaming = $state('');
	let streamingReasoning = $state('');
	// Tracks which agent produced the last reasoning delta so the name prefix
	// is written once per transition, not per token.
	let thinkingAgent: string | null = null;
	let liveTools = $state<
		{ tool: string; args?: Record<string, unknown> | null; result?: unknown; phase: 'running' | 'done' | 'error' }[]
	>([]);
	let livePlan = $state<AgentPlan | null>(null);
	let composerDraft = $state('');
	let composerNonce = $state(0);

	const sessionQuery = createQuery(
		toStore(() => ({
			queryKey: ['agent-session', sessionId],
			queryFn: () => agentApi.getSession(sessionId!),
			enabled: sessionId != null,
		})),
	);
	const messages = $derived($sessionQuery.data?.messages ?? []);
	const sessionsQuery = createQuery({
		queryKey: ['agent-sessions', 'scene'],
		queryFn: agentApi.listSceneSessions,
	});
	const sessions = $derived($sessionsQuery.data ?? []);
	const activeSession = $derived(sessionId != null ? (sessions.find((s) => s.id === sessionId) ?? null) : null);
	const running = $derived(Boolean(activeSession?.running || activeSession?.status === 'running'));

	const skillsQuery = createQuery({
		queryKey: ['agent-skills'],
		queryFn: agentApi.listSkills,
	});
	const skillOptions = $derived<SkillOption[]>($skillsQuery.data ?? []);

	// @workflow picker works here too: a tagged run_workflow from the builder
	// chat follows the same render-approval rules as canvas.
	const workflowsQuery = createQuery({
		queryKey: ['workflows'],
		queryFn: workflows.list,
	});
	const composerWorkflows = $derived<WorkflowOption[]>(
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

	const SHOT_SUGGESTIONS = [
		{ labelKey: 'buildScene.suggestionTwoPerson', prompt: 'Add a male and a female character facing each other, then frame a medium two-shot from the left.' },
		{ labelKey: 'buildScene.suggestionHeroPose', prompt: 'Add a character in a hero pose — arms wide — with a low-angle close-up.' },
		{ labelKey: 'buildScene.suggestionBlockout', prompt: 'Blockout a simple room: floor plane, two walls, and put a character inside.' },
		{ labelKey: 'buildScene.suggestionCapture', prompt: 'Frame this as a medium close-up and capture a reference image.' },
	];
	const suggestionOptions = $derived(
		SHOT_SUGGESTIONS.map((s) => ({ label: t(s.labelKey), prompt: s.prompt })),
	);

	const sendMutation = createMutation({
		mutationFn: (payload: AgentComposerPayload) => agentApi.postMessage(sessionId!, payload),
		onMutate: () => {
			const s = sessions.find((x) => x.id === sessionId);
			const steering = Boolean(s?.running || s?.status === 'running');
			if (!steering) {
				streaming = '';
				streamingReasoning = '';
				thinkingAgent = null;
				liveTools = [];
				livePlan = null;
			}
		},
		onSuccess: async () => {
			await client.invalidateQueries({ queryKey: ['agent-session', sessionId] });
			await client.invalidateQueries({ queryKey: ['agent-sessions', 'scene'] });
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('buildScene.sendFailed'));
		},
	});

	async function handleSend(payload: AgentComposerPayload) {
		if (sessionId == null) {
			toast.error(t('buildScene.stillLoading'));
			return;
		}
		$sendMutation.mutate(payload);
	}

	function handleQuestionAnswer(option: string, _scope: string, questionSeq: number) {
		if (running || sessionId == null) return;
		handleSend({ content: option, mentions: [], attachments: [], answer_to: questionSeq });
	}

	async function cancelRun() {
		if (sessionId == null) return;
		try {
			await agentApi.cancel(sessionId);
			await client.invalidateQueries({ queryKey: ['agent-sessions', 'scene'] });
			await client.invalidateQueries({ queryKey: ['agent-session', sessionId] });
			toast.info(t('buildScene.runCancelled'));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('buildScene.cancelFailed'));
		}
	}

	function upsertSession(s: AgentSession) {
		client.setQueryData<AgentSession[]>(['agent-sessions', 'scene'], (old) => {
			const list = Array.isArray(old) ? old : [];
			return list.some((x) => x.id === s.id)
				? list.map((x) => (x.id === s.id ? { ...x, ...s } : x))
				: [...list, s];
		});
	}

	function appendMessage(sid: number, msg: AgentMessage) {
		if (sid !== sessionId) return;
		client.setQueryData<AgentSession & { messages: AgentMessage[] }>(['agent-session', sid], (old) => {
			if (!old || !Array.isArray(old.messages)) return old;
			if (old.messages.some((m) => m.id === msg.id)) return old;
			return { ...old, messages: [...old.messages, msg] };
		});
	}

	// ---- agent tool live view: pose tools mutate server-side; SSE updates --
	// The agent's shot tools append events + publish shot.updated; the store
	// applies remote versions unless a drag is in progress (defer-during-drag,
	// same rule as the canvas graph sync).

	const POSE_TOOLS = new Set([
		'get_scene',
		'add_object',
		'delete_object',
		'rename_object',
		'set_transform',
		'reset_transform',
		'select_object',
		'clear_scene',
		'set_joint',
		'set_posture',
		'list_poses',
		'apply_pose',
		'reset_pose',
		'set_shot',
		'list_shot_presets',
	]);

	onMount(() => {
		const stop = connectEvents((ev) => {
			if (ev.type === 'events.resync') {
				client.invalidateQueries({ queryKey: ['agent-sessions', 'scene'] });
				if (sessionId != null) client.invalidateQueries({ queryKey: ['agent-session', sessionId] });
				void refreshCaptures();
				return;
			}
			switch (ev.type) {
				case 'shot.updated':
					shotStore.handleShotUpdated(ev.data as { shot_id?: number; reason?: string });
					// A pending agent capture request rides the same event: the
					// viewport checks capture_request_json, renders the PNG, and
					// posts it (the router clears the request, which is the
					// request_capture tool's completion signal).
					if (shotStore.dragInProgress == false) {
						(viewportRef as unknown as { pollCaptureRequest?: () => void } | null)?.pollCaptureRequest?.();
					}
					break;
				case 'shot.capture.saved':
					void refreshCaptures();
					break;
				case 'agent.session.updated': {
					const s = ev.data?.session as AgentSession | undefined;
					if (s) upsertSession(s);
					break;
				}
				case 'agent.message': {
					const msg = ev.data?.message as AgentMessage | undefined;
					if (msg) {
						appendMessage(msg.session_id, msg);
						if (msg.session_id === sessionId) {
						if (msg.role === 'tool') liveTools = [];
						if (msg.role === 'assistant' && !msg.agent_name) {
							streaming = '';
							streamingReasoning = '';
							thinkingAgent = null;
						}
						}
					}
					client.invalidateQueries({ queryKey: ['agent-sessions', 'scene'] });
					break;
				}
				case 'agent.token':
					if (ev.data?.session_id === sessionId && !ev.data?.agent_name) {
						streaming += String(ev.data?.content ?? '');
					}
					break;
				case 'agent.thinking':
					// Reasoning is auxiliary: stream it from ANY agent (incl.
					// sub-agents) — the old `!agent_name` filter left
					// delegated turns as a bare "working..." card. Prefix only
					// on agent change; deltas arrive per token.
					if (ev.data?.session_id === sessionId) {
						const aname = (ev.data?.agent_name as string | undefined) ?? null;
						if (aname !== thinkingAgent) {
							streamingReasoning += aname ? `${aname}: ` : '';
							thinkingAgent = aname;
						}
						streamingReasoning += String(ev.data?.content ?? '');
					}
					break;
				case 'agent.tool': {
					if (ev.data?.session_id !== sessionId) return;
					const name = String(ev.data?.tool ?? '');
					const phase = String(ev.data?.phase ?? '');
					if (phase === 'start') {
						liveTools.push({
							tool: name,
							args: (ev.data?.args as Record<string, unknown>) ?? null,
							phase: 'running',
						});
					} else if (phase === 'finish') {
						const idx = [...liveTools].reverse().findIndex((t) => t.tool === name && t.phase === 'running');
						if (idx >= 0) {
							const real = liveTools.length - 1 - idx;
							liveTools[real] = {
								...liveTools[real],
								result: ev.data?.result,
								phase: (ev.data?.result as { ok?: boolean } | undefined)?.ok === false ? 'error' : 'done',
							};
						}
					}
					// A shot tool finished server-side: refetch promptly so the
					// viewport follows even before shot.updated SSE lands.
					if (POSE_TOOLS.has(name) && phase === 'finish') {
						shotStore.handleShotUpdated({ shot_id: shotStore.compositionId ?? undefined, reason: 'tool' });
					}
					break;
				}
				case 'agent.plan': {
					if (ev.data?.session_id !== sessionId) return;
					const tasks = (ev.data?.tasks as AgentTask[] | undefined) ?? [];
					livePlan = {
						tasks: tasks.map((t) => ({ ...t, status: t.status ?? 'pending' })),
						note: (ev.data?.note as string | null) ?? null,
					};
					break;
				}
				case 'agent.task': {
					if (ev.data?.session_id !== sessionId) return;
					const index = ev.data?.index;
					const status = ev.data?.status as AgentTask['status'] | undefined;
					if (typeof index === 'number' && status) {
						livePlan = livePlan
							? {
									...livePlan,
									tasks: livePlan.tasks.map((t, i) => (i === index ? { ...t, status } : t)),
								}
							: livePlan;
					}
					break;
				}
			}
		});
		return stop;
	});
</script>

<div class="shell">
	<AppHeader active="build-scene" />

	<div class="workspace">
		<ShotSessionRail
			{sessions}
			activeId={sessionId}
			onSelect={(id) => activateSession(id)}
			onNew={handleNewScene}
			onDelete={handleDeleteScene}
			collapsed={railCollapsed}
			onToggleCollapse={toggleRail}
		/>
		<aside class="left-panel">
			<ScenePanel />
			<div class="inspector-slot">
				<ShotInspector oncapture={captureFromUi} />
			</div>
		</aside>

		<main class="center-panel">
			{#if compositionReady}
				<div class="viewport-wrap">
					<ShotViewport bind:this={viewportRef} oncapture={handleCaptureDataUrl} />
				</div>
				<TimelineStrip onExportVideo={handleExportVideo} />
				{#if exportStatus}
					<p class="export-status" data-export-status>
						{exportStatus}
					</p>
				{/if}
				<CaptureStrip {captures} compositionId={shotStore.compositionId} />
			{:else}
				<div class="loading">{t('buildScene.preparing')}</div>
			{/if}
		</main>

		<aside class="chat-panel" class:collapsed={chatCollapsed}>
			<header class="chat-head">
				{#if chatCollapsed}
					<span class="chat-title-vert">{t('buildScene.title')}</span>
				{:else}
					<span class="chat-title">{activeSession?.title ?? t('buildScene.title')}</span>
					<span class="badge">{t('buildScene.builderBadge')}</span>
				{/if}
				<button
					class="chat-toggle"
					onclick={toggleChat}
					title={chatCollapsed ? t('buildScene.expandChat') : t('buildScene.collapseChat')}
					aria-expanded={!chatCollapsed}
				>
					{chatCollapsed ? '‹' : '›'}
				</button>
			</header>
			{#if !chatCollapsed}
				<AgentChat
					{messages}
					{liveTools}
					{streaming}
					{streamingReasoning}
					jobs={[]}
					{running}
					loading={sessionId != null && $sessionQuery.isLoading}
					suggestions={suggestionOptions}
					onSuggestion={(text) => {
						composerDraft = text;
						composerNonce++;
					}}
					onAnswer={handleQuestionAnswer}
				/>
				<AgentComposer
					{running}
					draft={composerDraft}
					draftNonce={composerNonce}
					workflows={composerWorkflows}
					skills={skillOptions}
					onSend={handleSend}
					onCancel={cancelRun}
				/>
			{/if}
		</aside>
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
	.left-panel {
		width: 280px;
		flex-shrink: 0;
		border-right: 1px solid var(--border, #232733);
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.inspector-slot {
		flex: 1;
		min-height: 0;
		border-top: 1px solid var(--border, #232733);
		overflow: hidden;
	}
	.center-panel {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	/* Owns ONLY the viewport; the timeline and capture strip are separate
	   flex rows below so neither can overflow onto the other. */
	.viewport-wrap {
		flex: 1;
		min-height: 0;
		position: relative;
		display: flex;
		overflow: hidden;
		background: #0b1220;
	}
	.loading {
		flex: 1;
		display: grid;
		place-items: center;
		opacity: 0.5;
		font-size: 13px;
	}
	.chat-panel {
		width: 360px;
		flex-shrink: 0;
		border-left: 1px solid var(--border, #232733);
		display: flex;
		flex-direction: column;
		min-height: 0;
		transition: width 0.18s ease;
	}
	.chat-panel.collapsed {
		width: 40px;
	}
	.chat-panel.collapsed .chat-head {
		flex-direction: column;
		align-items: center;
		padding: 10px 4px;
		gap: 8px;
		border-bottom: none;
	}
	.chat-title-vert {
		writing-mode: vertical-rl;
		font-size: 12px;
		font-weight: 600;
		opacity: 0.75;
		letter-spacing: 0.04em;
	}
	.chat-toggle {
		border: 1px solid var(--border, #2a2f3a);
		background: transparent;
		color: inherit;
		border-radius: 6px;
		width: 22px;
		height: 22px;
		display: flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
		font-size: 13px;
		flex-shrink: 0;
	}
	.chat-toggle:hover {
		background: rgba(255, 255, 255, 0.08);
	}
	.chat-head {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 14px;
		border-bottom: 1px solid var(--border, #232733);
	}
	.chat-head .chat-toggle {
		margin-left: auto;
	}
	.chat-panel.collapsed .chat-head .chat-toggle {
		margin-left: 0;
	}
	.chat-title {
		font-size: 13px;
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.badge {
		margin-left: auto;
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		border: 1px solid var(--border, #2a2f3a);
		border-radius: 999px;
		padding: 2px 8px;
		opacity: 0.7;
	}
	.export-status {
		margin: 0;
		padding: 4px 12px;
		font-size: 12px;
		color: var(--muted, #9aa3b5);
	}
</style>
