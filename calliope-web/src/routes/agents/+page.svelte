<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { agentApi, canvasApi } from '$lib/api';
	import { AGENT_TASK_PROMPTS, AGENT_TASK_TITLES, isAgentTaskKind } from '$lib/agentTasks';

	let failed = $state('');

	onMount(async () => {
		try {
			const projectParam = page.url.searchParams.get('project');
			const taskParam = page.url.searchParams.get('task');
			let sessionId: number;
			let prefill = '';

			if (projectParam != null || taskParam != null) {
				// Deep-link contract from the Project stages: /agents?project=<id>&task=<story|script>
				const pid = projectParam != null && projectParam !== '' ? Number(projectParam) : null;
				const task = isAgentTaskKind(taskParam) ? taskParam : null;
				const s = await agentApi.createSession({
					...(pid != null && Number.isFinite(pid) ? { project_id: pid } : {}),
					...(task ? { title: AGENT_TASK_TITLES[task] } : {}),
				});
				sessionId = s.id;
				prefill = task ? AGENT_TASK_PROMPTS[task] : '';
			} else {
				const sessions = await agentApi.listSessions();
				const stored = Number(localStorage.getItem('calliope.agents.activeSession'));
				const found = sessions.find((s) => s.id === stored) ?? sessions[0];
				sessionId = found?.id ?? (await agentApi.createSession({})).id;
			}

			const session = await agentApi.getSession(sessionId);
			const graph = session.project_id
				? await canvasApi.ensureForProject(session.project_id)
				: await canvasApi.ensureForSession(session.id);
			const url = `/canvas/${graph.canvas.id}?session=${session.id}`;
			if (prefill) sessionStorage.setItem('calliope.canvas.composerPrefill', prefill);
			goto(url, { replaceState: true });
		} catch (err) {
			failed = err instanceof Error ? err.message : 'Could not open canvas';
		}
	});
</script>

{#if failed}
	<div class="redirect-error" role="alert">{failed}</div>
{:else}
	<div class="redirecting">Opening canvas…</div>
{/if}

<style>
	.redirecting,
	.redirect-error {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 100vh;
		color: var(--text-secondary);
		font-size: 14px;
	}
	.redirect-error {
		color: #f87171;
	}
</style>
